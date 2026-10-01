"""Credential selection and identity discovery.

Modes:
  auto     Service principal if AZURE_CLIENT_ID + (AZURE_CLIENT_SECRET | AZURE_CLIENT_CERTIFICATE_PATH)
           + AZURE_TENANT_ID are set, otherwise the Azure CLI login (az login).
  cli      Azure CLI login only.
  sp       Service principal from environment variables only.
  browser  Interactive browser sign-in (useful where the Azure CLI is not installed).

Tokens are never printed or logged; only non-secret claims (tenant, object id, UPN, app id)
are read from the access token to show who is signed in.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from azure.core.credentials import AccessToken

from .errors import AzureManagerError, translate_error
from .logging_utils import log_event

ARM_SCOPE = "https://management.azure.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
AUTH_MODES = ("auto", "cli", "sp", "browser")


@dataclass
class Identity:
    method: str
    kind: str  # User | ServicePrincipal | ManagedIdentity
    name: str | None
    object_id: str | None
    tenant_id: str | None
    app_id: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "User":
            # Personal Microsoft accounts appear as "live.com#name@gmail.com".
            name = (self.name or "").split("#", 1)[-1]
            return name or self.object_id or "unknown user"
        return f"{self.kind} {self.app_id or self.object_id}"

    @property
    def email(self) -> str | None:
        return self.label if self.kind == "User" and "@" in self.label else None


class CachingCredential:
    """Wraps a credential and caches tokens per scope.

    AzureCliCredential runs ``az account get-access-token`` on every call, which takes
    about a second; several SDK clients share this wrapper so each scope is fetched once.
    """

    def __init__(self, inner: Any, method: str):
        self.inner = inner
        self.method = method
        self._cache: dict[tuple, AccessToken] = {}
        self._lock = threading.Lock()

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        key = (scopes, kwargs.get("tenant_id"), kwargs.get("claims"))
        with self._lock:
            token = self._cache.get(key)
            if token and token.expires_on - 300 > time.time():
                return token
        token = self.inner.get_token(*scopes, **kwargs)
        with self._lock:
            self._cache[key] = token
        return token

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if callable(close):
            close()


def sp_env_present() -> bool:
    has_secret = bool(os.environ.get("AZURE_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_CERTIFICATE_PATH"))
    return bool(os.environ.get("AZURE_CLIENT_ID") and os.environ.get("AZURE_TENANT_ID") and has_secret)


def build_credential(mode: str = "auto", tenant_id: str | None = None) -> CachingCredential:
    """Create the credential for the requested mode. Does not contact Azure yet."""
    from azure.identity import AzureCliCredential, EnvironmentCredential, InteractiveBrowserCredential

    mode = (mode or "auto").lower()
    if mode not in AUTH_MODES:
        raise AzureManagerError(f"Unknown auth mode {mode!r}; use one of {', '.join(AUTH_MODES)}")

    if mode == "sp" or (mode == "auto" and sp_env_present()):
        if not sp_env_present():
            raise AzureManagerError(
                "Service principal authentication requires AZURE_TENANT_ID, AZURE_CLIENT_ID and "
                "AZURE_CLIENT_SECRET (or AZURE_CLIENT_CERTIFICATE_PATH) environment variables."
            )
        env_tenant = os.environ.get("AZURE_TENANT_ID")
        if tenant_id and env_tenant and env_tenant.lower() != tenant_id.lower():
            raise AzureManagerError(
                f"Tenant mismatch: config tenant_id is {tenant_id} but AZURE_TENANT_ID is {env_tenant}."
            )
        return CachingCredential(EnvironmentCredential(), "Service principal (environment variables)")
    if mode == "browser":
        return CachingCredential(InteractiveBrowserCredential(tenant_id=tenant_id), "Interactive browser")
    return CachingCredential(AzureCliCredential(tenant_id=tenant_id, process_timeout=30), "Azure CLI")


def decode_claims(token: str) -> dict[str, Any]:
    """Read the (unverified) claims of a JWT. Used only to display the signed-in identity."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, ValueError):
        return {}


def identity_from_claims(claims: dict[str, Any], method: str) -> Identity:
    idtyp = claims.get("idtyp")
    upn = claims.get("upn") or claims.get("unique_name") or claims.get("preferred_username") or claims.get("email")
    if idtyp == "app" or (not upn and claims.get("appid") and claims.get("oid") and "scp" not in claims):
        kind = "ManagedIdentity" if "/managedidentities/" in str(claims.get("xms_mirid", "")).lower() else "ServicePrincipal"
    else:
        kind = "User"
    return Identity(
        method=method,
        kind=kind,
        name=upn or claims.get("name"),
        object_id=claims.get("oid"),
        tenant_id=claims.get("tid"),
        app_id=claims.get("appid") or claims.get("azp"),
    )


def authenticate(credential: CachingCredential) -> Identity:
    """Acquire an ARM token (proves the credential works) and describe the identity behind it."""
    try:
        token = credential.get_token(ARM_SCOPE)
    except Exception as exc:  # credential errors come in many shapes
        error = translate_error(exc, "Authenticate to Azure")
        log_event("authenticate", "failed", error=error.reason)
        raise error from None
    identity = identity_from_claims(decode_claims(token.token), credential.method)
    log_event(
        "authenticate",
        "success",
        user=identity.label,
        details={"method": identity.method, "tenant_id": identity.tenant_id, "kind": identity.kind},
    )
    return identity
