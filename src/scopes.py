"""Azure RBAC scope parsing, construction and hierarchy checks.

Scope hierarchy (RBAC assignments inherit downwards only):

    /                                                       tenant root
    /providers/Microsoft.Management/managementGroups/<mg>   management group
    /subscriptions/<sub>                                    subscription
    /subscriptions/<sub>/resourceGroups/<rg>                resource group
    /subscriptions/<sub>/resourceGroups/<rg>/providers/...  resource
"""

from __future__ import annotations

import re

from .models import AccessSpec, Scope, ScopeType

_SUB_RE = re.compile(r"^/subscriptions/([0-9a-fA-F-]{36})/?$", re.IGNORECASE)
_RG_RE = re.compile(r"^/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([^/]+)/?$", re.IGNORECASE)
_RES_RE = re.compile(r"^/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([^/]+)/providers/(.+)$", re.IGNORECASE)
_MG_RE = re.compile(r"^/providers/Microsoft\.Management/managementGroups/([^/]+)/?$", re.IGNORECASE)


def subscription_scope(subscription_id: str) -> Scope:
    return Scope(ScopeType.SUBSCRIPTION, f"/subscriptions/{subscription_id}", subscription_id=subscription_id)


def resource_group_scope(subscription_id: str, resource_group: str) -> Scope:
    return Scope(
        ScopeType.RESOURCE_GROUP,
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
        subscription_id=subscription_id,
        resource_group=resource_group,
    )


def management_group_scope(management_group_id: str) -> Scope:
    return Scope(
        ScopeType.MANAGEMENT_GROUP,
        f"/providers/Microsoft.Management/managementGroups/{management_group_id}",
        management_group=management_group_id,
    )


def parse_scope(scope_id: str) -> Scope:
    """Classify a full ARM scope string. Raises ValueError for anything unrecognised."""
    if not scope_id or not scope_id.startswith("/"):
        raise ValueError(f"Not an ARM scope: {scope_id!r}")
    if scope_id == "/":
        return Scope(ScopeType.TENANT, "/")
    if m := _MG_RE.match(scope_id):
        return management_group_scope(m.group(1))
    if m := _SUB_RE.match(scope_id):
        return subscription_scope(m.group(1))
    if m := _RG_RE.match(scope_id):
        return resource_group_scope(m.group(1), m.group(2))
    if m := _RES_RE.match(scope_id):
        return Scope(
            ScopeType.RESOURCE,
            scope_id.rstrip("/"),
            subscription_id=m.group(1),
            resource_group=m.group(2),
            resource_name=scope_id.rstrip("/").rsplit("/", 1)[-1],
        )
    raise ValueError(f"Unrecognised ARM scope: {scope_id!r}")


def resolve_access_scope(access: AccessSpec, subscription_id: str) -> Scope:
    """Turn an AccessSpec into a concrete scope for the configured subscription."""
    if access.scope_id:
        return parse_scope(access.scope_id)
    if access.scope_type == ScopeType.SUBSCRIPTION:
        return subscription_scope(subscription_id)
    if access.scope_type == ScopeType.RESOURCE_GROUP:
        if not access.resource_group:
            raise ValueError("resource_group is required for scope_type 'resource_group'")
        return resource_group_scope(subscription_id, access.resource_group)
    if access.scope_type == ScopeType.MANAGEMENT_GROUP:
        if not access.management_group_id:
            raise ValueError("management_group_id is required for scope_type 'management_group'")
        return management_group_scope(access.management_group_id)
    if access.scope_type == ScopeType.RESOURCE:
        if not access.resource_id:
            raise ValueError("resource_id is required for scope_type 'resource'")
        return parse_scope(access.resource_id)
    raise ValueError(f"Unsupported scope type: {access.scope_type}")


def _norm(scope_id: str) -> str:
    return scope_id.rstrip("/").lower() or "/"


def same_scope(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)


def scope_contains(parent: str, child: str) -> bool:
    """True if ``parent`` is ``child`` or an ARM-path ancestor of it.

    Management-group ancestry cannot be derived from the path alone; see :func:`relation`.
    """
    p, c = _norm(parent), _norm(child)
    if p == "/":
        return True
    return c == p or c.startswith(p + "/")


def relation(assignment_scope: str, target_scope: str) -> str | None:
    """How an assignment at ``assignment_scope`` relates to ``target_scope``.

    Returns "Direct", "Inherited", "Child" or None (unrelated).
    Assignments at tenant root or a management group are only returned by Azure for
    subscriptions beneath them, so they are treated as inherited by subscription-level targets.
    """
    if same_scope(assignment_scope, target_scope):
        return "Direct"
    if scope_contains(assignment_scope, target_scope):
        return "Inherited"
    if scope_contains(target_scope, assignment_scope):
        return "Child"
    try:
        a_type = parse_scope(assignment_scope).type
        t_type = parse_scope(target_scope).type
    except ValueError:
        return None
    if a_type in (ScopeType.TENANT, ScopeType.MANAGEMENT_GROUP) and t_type in (
        ScopeType.SUBSCRIPTION,
        ScopeType.RESOURCE_GROUP,
        ScopeType.RESOURCE,
    ):
        return "Inherited"
    return None


def describe_scope(scope_id: str) -> tuple[str, str]:
    """Return (scope type label, short name) for display."""
    try:
        scope = parse_scope(scope_id)
    except ValueError:
        return ("Unknown", scope_id)
    return (scope.type.label, scope.short_name)
