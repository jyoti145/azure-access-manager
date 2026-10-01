"""Typed configuration and execution-plan models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Callable


class ScopeType(str, Enum):
    TENANT = "tenant"
    MANAGEMENT_GROUP = "management_group"
    SUBSCRIPTION = "subscription"
    RESOURCE_GROUP = "resource_group"
    RESOURCE = "resource"

    @property
    def label(self) -> str:
        return {
            ScopeType.TENANT: "Tenant (root)",
            ScopeType.MANAGEMENT_GROUP: "Management Group",
            ScopeType.SUBSCRIPTION: "Subscription",
            ScopeType.RESOURCE_GROUP: "Resource Group",
            ScopeType.RESOURCE: "Resource",
        }[self]


@dataclass(frozen=True)
class Scope:
    """A fully-resolved Azure RBAC scope."""

    type: ScopeType
    id: str
    subscription_id: str | None = None
    resource_group: str | None = None
    management_group: str | None = None
    resource_name: str | None = None

    @property
    def short_name(self) -> str:
        if self.type == ScopeType.RESOURCE_GROUP:
            return self.resource_group or self.id
        if self.type == ScopeType.MANAGEMENT_GROUP:
            return self.management_group or self.id
        if self.type == ScopeType.RESOURCE:
            return self.resource_name or self.id
        if self.type == ScopeType.SUBSCRIPTION:
            return self.subscription_id or self.id
        return "/"


# --------------------------------------------------------------------------- config


@dataclass
class ResourceGroupSpec:
    name: str
    location: str | None = None
    create_if_missing: bool = False
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class AccessSpec:
    role: str
    scope_type: ScopeType
    resource_group: str | None = None
    management_group_id: str | None = None
    resource_id: str | None = None
    scope_id: str | None = None  # explicit full ARM scope, if given
    description: str | None = None


@dataclass(frozen=True)
class AlertSpec:
    """One budget notification. Kept separate so new alert kinds can be added without touching budgets.py."""

    threshold: float
    threshold_type: str = "Actual"  # Actual | Forecasted
    operator: str = "GreaterThanOrEqualTo"
    emails: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    action_groups: tuple[str, ...] = ()
    enabled: bool = True
    locale: str | None = None


@dataclass
class BudgetSpec:
    enabled: bool
    name: str
    amount: float
    period: str = "Monthly"  # Monthly | Quarterly | Annually
    currency: str | None = None  # informational: budgets are always in the billing currency
    alerts: list[AlertSpec] = field(default_factory=list)
    scope_kind: ScopeType = ScopeType.RESOURCE_GROUP
    resource_group: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    recipients_defaulted: bool = False


@dataclass
class UserSpec:
    name: str
    email: str
    object_id: str | None = None
    invite_if_missing: bool = False
    invite_redirect_url: str = "https://myapplications.microsoft.com"
    send_invitation_email: bool = True
    resource_group: ResourceGroupSpec | None = None
    access: list[AccessSpec] = field(default_factory=list)
    budget: BudgetSpec | None = None

    @property
    def label(self) -> str:
        return f"{self.name} <{self.email}>" if self.name else self.email


@dataclass
class ReportSpec:
    enabled: bool = True
    include_resources: bool = True
    include_costs: bool = True
    include_rbac: bool = True


@dataclass
class AppConfig:
    subscription_id: str
    tenant_id: str | None = None
    users: list[UserSpec] = field(default_factory=list)
    report: ReportSpec = field(default_factory=ReportSpec)
    source_path: str | None = None


# --------------------------------------------------------------------------- plan


class ActionKind(str, Enum):
    NOOP = "OK"
    INVITE = "INVITE"
    CREATE = "CREATE"
    ASSIGN = "ASSIGN"
    UPDATE = "UPDATE"
    BLOCKED = "BLOCKED"

    @property
    def is_change(self) -> bool:
        return self in (ActionKind.INVITE, ActionKind.CREATE, ActionKind.ASSIGN, ActionKind.UPDATE)


class Stage(str, Enum):
    USERS = "users"
    RESOURCE_GROUPS = "resource-groups"
    RBAC = "rbac"
    BUDGETS = "budgets"


@dataclass
class Action:
    kind: ActionKind
    stage: Stage
    title: str
    details: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    user: str | None = None
    scope: str | None = None
    high_privilege: bool = False
    apply: Callable[[], str] | None = None
    # permission needed to apply this change, and the existing scope at which to check it
    required_permission: str | None = None
    permission_scope: str | None = None
    # execution results
    status: str = "pending"  # pending | done | failed | skipped
    result: str | None = None
    error: object | None = None  # OperationError


@dataclass
class UserContext:
    """Mutable per-user state shared between the actions of one user during execution."""

    principal_id: str | None = None
    principal_type: str = "User"
    display_name: str | None = None
    user_type: str | None = None


@dataclass
class UserPlan:
    user: UserSpec
    context: UserContext
    actions: list[Action] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(a.kind == ActionKind.BLOCKED for a in self.actions)

    @property
    def changes(self) -> list[Action]:
        return [a for a in self.actions if a.kind.is_change]
