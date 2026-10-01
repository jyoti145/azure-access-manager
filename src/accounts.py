"""Discover the tenants and subscriptions the signed-in identity can use.

Used by the UI so that anyone can run the tool with their own account: nothing about the
subscription or tenant is hard-coded, the person simply picks from what they have access to.
"""

from __future__ import annotations

from dataclasses import dataclass

from .auth import ARM_SCOPE, Identity, authenticate, build_credential, sp_env_present
from .errors import translate_error
from .rest import ARM_ENDPOINT, RestClient

API_VERSION = "2022-12-01"


@dataclass(frozen=True)
class SubscriptionChoice:
    subscription_id: str
    name: str
    state: str
    tenant_id: str
    tenant_name: str

    @property
    def label(self) -> str:
        return f"{self.name}  ·  {self.subscription_id}  ·  {self.tenant_name}"


@dataclass(frozen=True)
class TenantIssue:
    tenant_id: str
    tenant_name: str
    reason: str
    hint: str | None = None


def effective_mode(mode: str) -> str:
    if mode == "auto":
        return "sp" if sp_env_present() else "cli"
    return mode


def discover_subscriptions(mode: str = "auto") -> tuple[Identity, list[SubscriptionChoice], list[TenantIssue]]:
    """Sign in and list every subscription visible to the identity, across its tenants."""
    mode = effective_mode(mode)
    credential = build_credential(mode)
    identity = authenticate(credential)
    arm = RestClient(credential, ARM_ENDPOINT, ARM_SCOPE)
    try:
        tenants = list(arm.paged("/tenants", params={"api-version": API_VERSION}))
    except Exception as exc:
        raise translate_error(exc, "List tenants") from None

    names = {
        t["tenantId"]: t.get("displayName") or t.get("defaultDomain") or t["tenantId"]
        for t in tenants
        if t.get("tenantId")
    }
    if identity.tenant_id:
        names.setdefault(identity.tenant_id, identity.tenant_id)

    choices: list[SubscriptionChoice] = []
    issues: list[TenantIssue] = []
    for tenant_id, tenant_name in names.items():
        if tenant_id == identity.tenant_id:
            tenant_credential = credential
        elif mode == "cli":
            # The Azure CLI can get tokens for other tenants the account belongs to.
            tenant_credential = build_credential(mode, tenant_id)
        else:
            issues.append(TenantIssue(tenant_id, tenant_name,
                                      "Only the tenant you signed in to is available with this sign-in method."))
            continue
        try:
            subscriptions = list(
                RestClient(tenant_credential, ARM_ENDPOINT, ARM_SCOPE).paged(
                    "/subscriptions", params={"api-version": API_VERSION}
                )
            )
        except Exception as exc:
            error = translate_error(exc, "List subscriptions")
            hint = f"az login --tenant {tenant_id}" if mode == "cli" else error.hint
            issues.append(TenantIssue(tenant_id, tenant_name, error.reason, hint))
            continue
        for sub in subscriptions:
            choices.append(
                SubscriptionChoice(
                    subscription_id=sub.get("subscriptionId", ""),
                    name=sub.get("displayName", ""),
                    state=sub.get("state", ""),
                    tenant_id=tenant_id,
                    tenant_name=tenant_name,
                )
            )
    choices.sort(key=lambda c: (c.state.lower() != "enabled", c.name.lower()))
    return identity, choices, issues
