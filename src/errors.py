"""Exception types and translation of Azure SDK / REST errors into readable messages."""

from __future__ import annotations

import re
from typing import Any

from .logging_utils import redact


class AzureManagerError(Exception):
    """Base class for all errors raised deliberately by the tool."""

    exit_code = 1


class ConfigError(AzureManagerError):
    """Invalid or missing configuration. Carries every problem found, not just the first."""

    exit_code = 2

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class UserAborted(AzureManagerError):
    exit_code = 5


class PreflightFailed(AzureManagerError):
    exit_code = 3


class OperationError(AzureManagerError):
    """A failed Azure operation, described in terms an operator can act on."""

    def __init__(
        self,
        operation: str,
        reason: str,
        *,
        user: str | None = None,
        scope: str | None = None,
        required_permission: str | None = None,
        hint: str | None = None,
        code: str | None = None,
        status: int | None = None,
    ):
        self.operation = operation
        self.reason = redact(reason)
        self.user = user
        self.scope = scope
        self.required_permission = required_permission
        self.hint = hint
        self.code = code
        self.status = status
        super().__init__(f"{operation}: {self.reason}")


class RestError(Exception):
    """Error response from an ARM or Microsoft Graph REST call."""

    def __init__(self, status: int, code: str | None, message: str, *, request_id: str | None = None):
        self.status = status
        self.code = code
        self.message = redact(message)
        self.request_id = request_id
        super().__init__(f"HTTP {status} {code or ''}: {self.message}".strip())


# Known error codes -> (reason, hint). The raw Azure message is appended when useful.
_KNOWN_CODES: dict[str, tuple[str, str | None]] = {
    "AuthorizationFailed": (
        "The authenticated account does not have permission to perform this action at this scope.",
        "Ask an Owner / User Access Administrator to grant the required role, then retry.",
    ),
    "LinkedAuthorizationFailed": (
        "The account lacks permission on a linked resource required by this operation.",
        None,
    ),
    "Forbidden": ("Access to the requested resource is forbidden for the authenticated account.", None),
    "Authorization_RequestDenied": (
        "Insufficient privileges in Microsoft Entra ID (Microsoft Graph) to complete the operation.",
        "Reading users requires User.Read.All (or Directory.Read.All); inviting guests requires "
        "User.Invite.All or the 'Guest Inviter' directory role.",
    ),
    "ResourceNotFound": ("The resource was not found.", None),
    "ResourceGroupNotFound": (
        "The resource group does not exist.",
        "Create it first or set 'create_if_missing: true' for the resource group in config.yaml.",
    ),
    "SubscriptionNotFound": (
        "The subscription was not found, or the authenticated identity has no access to it.",
        "Check subscription_id and that you are signed in to the right tenant ('az account list').",
    ),
    "InvalidSubscriptionId": ("The subscription ID is not valid.", None),
    "InvalidAuthenticationToken": (
        "The access token is invalid.",
        "Sign in again with 'az login' (or check service principal environment variables).",
    ),
    "ExpiredAuthenticationToken": ("The access token has expired.", "Sign in again with 'az login'."),
    "InvalidAuthenticationTokenTenant": (
        "Tenant mismatch: the token was issued by a different tenant than the one that owns the subscription.",
        "Run 'az login --tenant <tenant-id>' or set tenant_id in config.yaml.",
    ),
    "InvalidAuthenticationTokenAudience": ("The access token was issued for the wrong audience.", None),
    "RoleAssignmentExists": ("An identical role assignment already exists.", None),
    "RoleDefinitionDoesNotExist": ("The role definition does not exist at this scope.", None),
    "RoleAssignmentLimitExceeded": (
        "The subscription has reached the maximum number of role assignments.",
        "Remove unused assignments or assign roles to groups instead of individual users.",
    ),
    "PrincipalNotFound": (
        "The user/principal does not exist in the directory, or has not finished replicating.",
        "If the user was just invited, wait a minute and run setup again.",
    ),
    "RequestDisallowedByPolicy": ("The request was blocked by an Azure Policy assignment.", None),
    "InvalidResourceGroupLocation": (
        "A resource group with this name already exists in a different location.",
        None,
    ),
    "ScopeLocked": ("The scope is protected by a resource lock.", None),
    "TooManyRequests": ("Azure throttled the request (HTTP 429).", "Wait a few minutes and retry."),
    "429": ("Azure throttled the request (HTTP 429).", "Wait a few minutes and retry."),
    "Conflict": ("The request conflicts with the current state of the resource.", None),
}

_STATUS_FALLBACK = {
    400: "Azure rejected the request as invalid.",
    401: "Authentication failed.",
    403: "The authenticated account does not have permission to perform this action.",
    404: "The requested resource was not found.",
    409: "The request conflicts with the current state of the resource.",
    412: "The resource was modified concurrently (eTag mismatch).",
    429: "Azure throttled the request.",
}


def _extract(exc: BaseException) -> tuple[int | None, str | None, str]:
    """Return (status, code, message) for SDK, REST and credential exceptions."""
    if isinstance(exc, RestError):
        return exc.status, exc.code, exc.message
    status = getattr(exc, "status_code", None)
    code = None
    message = ""
    error = getattr(exc, "error", None)
    if error is not None:
        code = getattr(error, "code", None)
        message = getattr(error, "message", None) or ""
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if not message:
        message = getattr(exc, "message", None) or str(exc)
    if not code:
        match = re.search(r"\(([A-Za-z_]+)\)", message) or re.search(r"Code:\s*([A-Za-z_]+)", message)
        if match:
            code = match.group(1)
    return status, code, message.strip()


def _clean_message(message: str) -> str:
    # SDK messages are often "(Code) Message\nCode: X\nMessage: Y" - keep the first useful line.
    first = message.splitlines()[0] if message else ""
    first = re.sub(r"^\([A-Za-z_]+\)\s*", "", first)
    return first.strip()


def translate_error(
    exc: BaseException,
    operation: str,
    *,
    user: str | None = None,
    scope: str | None = None,
    required_permission: str | None = None,
) -> OperationError:
    """Convert any exception from an Azure call into an :class:`OperationError`."""
    if isinstance(exc, OperationError):
        return exc

    # Credential problems (az not logged in, missing env vars, etc.)
    from azure.core.exceptions import ClientAuthenticationError

    try:
        from azure.identity import CredentialUnavailableError
    except ImportError:  # pragma: no cover
        CredentialUnavailableError = ClientAuthenticationError  # type: ignore

    if isinstance(exc, CredentialUnavailableError):
        return OperationError(
            operation,
            f"No usable Azure credential: {_clean_message(str(exc))}",
            user=user,
            scope=scope,
            hint="Run 'az login' (Azure CLI), use '--auth browser', or set AZURE_TENANT_ID, "
            "AZURE_CLIENT_ID and AZURE_CLIENT_SECRET for a service principal.",
            code="CredentialUnavailable",
        )

    status, code, message = _extract(exc)

    if isinstance(exc, ClientAuthenticationError) and status in (None, 401):
        return OperationError(
            operation,
            f"Authentication failed: {_clean_message(message)}",
            user=user,
            scope=scope,
            hint="Sign in again with 'az login' or verify the service principal credentials.",
            code=code or "AuthenticationFailed",
            status=status,
        )

    try:
        import requests

        if isinstance(exc, requests.RequestException) and not isinstance(exc, requests.HTTPError):
            return OperationError(
                operation,
                f"Network error while contacting Azure: {exc.__class__.__name__}",
                user=user,
                scope=scope,
                hint="Check network connectivity / proxy settings and retry.",
                code="NetworkError",
            )
    except ImportError:  # pragma: no cover
        pass

    known = _KNOWN_CODES.get(code or "")
    detail = _clean_message(message)
    if known:
        reason, hint = known
        if detail and detail.lower() not in reason.lower():
            reason = f"{reason} Azure said: {detail}"
    else:
        reason = detail or _STATUS_FALLBACK.get(status or 0, "Unexpected error.")
        hint = None
        if status == 403 and not required_permission:
            reason = f"{_STATUS_FALLBACK[403]} Azure said: {detail}" if detail else _STATUS_FALLBACK[403]

    return OperationError(
        operation,
        reason,
        user=user,
        scope=scope,
        required_permission=required_permission if (status in (401, 403) or code in (
            "AuthorizationFailed", "Forbidden", "Authorization_RequestDenied", "LinkedAuthorizationFailed"
        )) else None,
        hint=hint,
        code=code,
        status=status,
    )


def error_code(exc: BaseException) -> str | None:
    """Best-effort extraction of the Azure error code from any exception."""
    if isinstance(exc, OperationError):
        return exc.code
    return _extract(exc)[1]


def error_status(exc: BaseException) -> int | None:
    if isinstance(exc, OperationError):
        return exc.status
    return _extract(exc)[0]


def describe(exc: Any) -> str:
    """Short single-line description, safe for logs."""
    status, code, message = _extract(exc)
    return redact(f"{code or exc.__class__.__name__}: {_clean_message(message)}")
