"""Effective-permission checks for the signed-in identity and the setup pre-flight check.

Uses ``GET {scope}/providers/Microsoft.Authorization/permissions``, which returns the
actions granted to the caller at a scope (including inherited role assignments).

Limitations (Azure-side, documented rather than hidden):
* Deny assignments and Azure Policy are not reflected in this API; an action reported as
  allowed can still be denied at execution time. Such failures are reported clearly.
* Role assignments with ABAC conditions (e.g. constrained 'Role Based Access Control
  Administrator' delegation) are reported as "conditional" because the condition is only
  evaluated when the request is made.
* Microsoft Graph permissions to *invite* users cannot be checked without sending an
  invitation, so that check happens at execution time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import OperationError, RestError, translate_error
from .rest import RestClient

API_VERSION = "2022-04-01"


@dataclass
class PermissionEntry:
    actions: list[str] = field(default_factory=list)
    not_actions: list[str] = field(default_factory=list)
    condition: str | None = None


def _pattern(action_pattern: str) -> re.Pattern:
    return re.compile("^" + re.escape(action_pattern).replace(r"\*", ".*") + "$", re.IGNORECASE)


def _matches(patterns: list[str], action: str) -> bool:
    return any(_pattern(p).match(action) for p in patterns)


def evaluate(entries: list[PermissionEntry], action: str) -> tuple[bool, bool]:
    """Return (allowed, conditional_only) for ``action`` given permission entries."""
    granting = [e for e in entries if _matches(e.actions, action) and not _matches(e.not_actions, action)]
    if not granting:
        return False, False
    if any(not e.condition for e in granting):
        return True, False
    return True, True


class PermissionService:
    def __init__(self, arm: RestClient):
        self.arm = arm
        self._cache: dict[str, list[PermissionEntry]] = {}

    def entries(self, scope: str) -> list[PermissionEntry]:
        key = scope.lower()
        if key not in self._cache:
            try:
                items = list(
                    self.arm.paged(
                        f"{scope.rstrip('/')}/providers/Microsoft.Authorization/permissions",
                        params={"api-version": API_VERSION},
                    )
                )
            except RestError as exc:
                raise translate_error(exc, "Read effective permissions", scope=scope) from None
            self._cache[key] = [
                PermissionEntry(
                    actions=item.get("actions") or [],
                    not_actions=item.get("notActions") or [],
                    condition=item.get("condition"),
                )
                for item in items
            ]
        return self._cache[key]

    def check(self, scope: str, action: str) -> tuple[bool, bool]:
        return evaluate(self.entries(scope), action)


# --------------------------------------------------------------------------- pre-flight


@dataclass
class CheckResult:
    name: str
    status: str  # ok | fail | warn
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "fail"


@dataclass
class PreflightRequirements:
    """What the planned run needs. Built from config + current state by the setup engine."""

    rg_create_needed: bool = False
    rbac_scopes: list[str] = field(default_factory=list)  # existing scopes to check roleAssignments/write at
    budget_scopes: list[str] = field(default_factory=list)  # existing scopes to check budgets/write at
    needs_directory: bool = False
    needs_invite: bool = False
    check_costs: bool = True


def _short(scope: str) -> str:
    from .scopes import describe_scope

    label, name = describe_scope(scope)
    return f"{label} '{name}'"


def run_preflight(ctx, identity, subscription, requirements: PreflightRequirements, *, tenant_id: str | None = None) -> list[CheckResult]:
    """Evaluate every requirement and return one result per check category."""
    results: list[CheckResult] = []
    perms: PermissionService = ctx.permissions
    sub_scope = ctx.subscription_scope

    # Authentication / tenant
    auth_problems = []
    if tenant_id and identity.tenant_id and tenant_id.lower() != identity.tenant_id.lower():
        auth_problems.append(f"signed in to tenant {identity.tenant_id} but config tenant_id is {tenant_id}")
    if subscription.tenant_id and identity.tenant_id and subscription.tenant_id.lower() != identity.tenant_id.lower():
        auth_problems.append(
            f"subscription belongs to tenant {subscription.tenant_id} but the token is for {identity.tenant_id}"
        )
    if auth_problems:
        results.append(CheckResult("Authentication", "fail", "Tenant mismatch: " + "; ".join(auth_problems)))
    else:
        results.append(CheckResult("Authentication", "ok", f"{identity.label} via {identity.method}"))

    # Subscription
    state = subscription.state
    if state.lower() == "enabled":
        results.append(CheckResult("Subscription access", "ok", f"{subscription.display_name} ({state})"))
    elif state.lower() in ("warned", "pastdue"):
        results.append(CheckResult("Subscription access", "warn", f"Subscription state is {state}"))
    else:
        results.append(CheckResult("Subscription access", "fail", f"Subscription state is {state}; changes are not possible"))

    def check_action(scope: str, action: str) -> tuple[str, str]:
        try:
            allowed, conditional = perms.check(scope, action)
        except OperationError as exc:
            return "fail", f"could not read permissions at {_short(scope)}: {exc.reason}"
        if not allowed:
            return "fail", f"missing {action} at {_short(scope)}"
        if conditional:
            return "warn", f"{action} at {_short(scope)} is granted only with a condition (some roles may be refused)"
        return "ok", ""

    def combine(name: str, checks: list[tuple[str, str]], ok_detail: str) -> CheckResult:
        failures = [d for s, d in checks if s == "fail"]
        warnings = [d for s, d in checks if s == "warn"]
        if failures:
            return CheckResult(name, "fail", "; ".join(failures))
        if warnings:
            return CheckResult(name, "warn", "; ".join(warnings))
        return CheckResult(name, "ok", ok_detail)

    # Resource groups
    rg_checks = [check_action(sub_scope, "Microsoft.Resources/subscriptions/resourceGroups/read")]
    if requirements.rg_create_needed:
        rg_checks.append(check_action(sub_scope, "Microsoft.Resources/subscriptions/resourceGroups/write"))
    results.append(combine("Resource Group permissions", rg_checks,
                           "read + create" if requirements.rg_create_needed else "read"))

    # RBAC
    if requirements.rbac_scopes:
        checks = [check_action(s, "Microsoft.Authorization/roleAssignments/write") for s in dict.fromkeys(requirements.rbac_scopes)]
        results.append(combine("RBAC assignment permission", checks,
                               f"roleAssignments/write on {len(set(requirements.rbac_scopes))} scope(s)"))

    # Directory
    if requirements.needs_directory:
        try:
            ctx.users.check_read_access()
            detail = "can read users"
            if requirements.needs_invite:
                detail += " (invite permission is verified when the invitation is sent)"
            results.append(CheckResult("Directory (Microsoft Graph) access", "ok", detail))
        except OperationError as exc:
            results.append(CheckResult("Directory (Microsoft Graph) access", "fail", exc.reason))

    # Cost Management
    cost_checks = [check_action(s, "Microsoft.Consumption/budgets/write") for s in dict.fromkeys(requirements.budget_scopes)]
    cost_detail = f"budgets/write on {len(set(requirements.budget_scopes))} scope(s)" if requirements.budget_scopes else "read"
    if requirements.check_costs:
        try:
            result = ctx.costs.month_to_date()
            cost_detail += f"; cost data readable (billing currency: {result.currency or 'unknown'})"
        except OperationError as exc:
            cost_checks.append(("warn", f"cost data not readable: {exc.reason}"))
    if requirements.budget_scopes or requirements.check_costs:
        results.append(combine("Cost Management permissions", cost_checks, cost_detail))
    return results
