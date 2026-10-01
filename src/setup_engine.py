"""Plan-then-apply engine behind ``setup`` (and the drift part of ``audit``).

Planning only performs read operations and produces, for every configured user, an ordered
list of actions (OK / INVITE / CREATE / ASSIGN / UPDATE / BLOCKED). ``--dry-run`` prints that
plan and stops, so a dry run shows exactly what a real run would do. Execution applies the
change actions in order; a failure stops the remaining actions for that user only.

Idempotency comes from planning against the live state: anything that already matches the
configuration is an OK action and is never re-applied.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from . import budgets as budget_mod
from .errors import OperationError, translate_error
from .logging_utils import log_event
from .models import Action, ActionKind, AppConfig, ScopeType, Stage, UserContext, UserPlan, UserSpec
from .permissions import PreflightRequirements
from .rbac import WRITE_PERMISSION as RBAC_WRITE
from .rbac import is_high_privilege
from .resource_groups import WRITE_PERMISSION as RG_WRITE
from .scopes import describe_scope, resolve_access_scope, resource_group_scope, subscription_scope

_HIGH_PRIVILEGE_WARNINGS = {
    "owner": "This grants full control over the {scope}, including the ability to grant access to others.",
    "user access administrator": "This allows the user to grant any role (including Owner) to anyone at this {scope}.",
    "role based access control administrator": "This allows the user to manage role assignments at this {scope}.",
}


class SetupEngine:
    def __init__(
        self,
        ctx,
        config: AppConfig,
        *,
        stages: set[Stage] | None = None,
        user_filter: str | None = None,
        today: date | None = None,
        billing_currency: str | None = None,
    ):
        self.ctx = ctx
        self.config = config
        self.stages = stages or set(Stage)
        self.user_filter = user_filter.lower() if user_filter else None
        self.today = today or date.today()
        self.billing_currency = billing_currency
        self._rg_cache: dict[str, object] = {}
        self._planned_rgs: dict[str, str] = {}
        self._planned_budgets: dict[tuple[str, str], str] = {}
        self._planned_assignments: dict[tuple[str, str, str], str] = {}

    # ------------------------------------------------------------------ planning
    def selected_users(self) -> list[UserSpec]:
        users = self.config.users
        if self.user_filter:
            users = [u for u in users if self.user_filter in (u.email.lower(), (u.object_id or "").lower(), u.name.lower())]
        return users

    def plan(self) -> list[UserPlan]:
        return [self._plan_user(user) for user in self.selected_users()]

    def _plan_user(self, user: UserSpec) -> UserPlan:
        plan = UserPlan(user=user, context=UserContext())
        if self.stages & {Stage.USERS, Stage.RBAC}:
            self._plan_principal(plan)

        rg_names: list[str] = []
        if user.resource_group:
            rg_names.append(user.resource_group.name)
        if Stage.RBAC in self.stages:
            rg_names += [a.resource_group for a in user.access if a.resource_group]
        if Stage.BUDGETS in self.stages and user.budget and user.budget.resource_group:
            rg_names.append(user.budget.resource_group)
        for name in dict.fromkeys(n.lower() for n in rg_names):
            original = next(n for n in rg_names if n.lower() == name)
            self._plan_resource_group(plan, original)

        if Stage.RBAC in self.stages:
            for access in user.access:
                self._plan_access(plan, access)
        if Stage.BUDGETS in self.stages and user.budget:
            self._plan_budget(plan)
        return plan

    def _add(self, plan: UserPlan, kind: ActionKind, stage: Stage, title: str, **kwargs) -> Action:
        action = Action(kind=kind, stage=stage, title=title, user=plan.user.email, **kwargs)
        plan.actions.append(action)
        return action

    def _blocked(self, plan: UserPlan, stage: Stage, title: str, reason: str, hint: str | None = None) -> None:
        details = [reason] + ([hint] if hint else [])
        self._add(plan, ActionKind.BLOCKED, stage, title, details=details)

    # -- principal -------------------------------------------------------
    def _plan_principal(self, plan: UserPlan) -> None:
        user = plan.user
        try:
            found = self.ctx.users.get_by_id(user.object_id) if user.object_id else self.ctx.users.find_by_email(user.email)
        except OperationError as exc:
            self._blocked(plan, Stage.USERS, f"Cannot verify user {user.email or user.object_id}", exc.reason, exc.hint)
            return

        if found:
            plan.context.principal_id = found.object_id
            plan.context.display_name = found.display_name
            plan.context.user_type = found.user_type
            warnings = []
            if user.object_id and user.email and found.email and found.email.lower() != user.email.lower() \
                    and user.email.lower() not in (found.user_principal_name or "").lower():
                warnings.append(f"object_id belongs to {found.email}, not {user.email}")
            if found.account_enabled is False:
                warnings.append("the account is disabled")
            if found.external_state == "PendingAcceptance":
                warnings.append("guest invitation not yet accepted (role assignments still apply once accepted)")
            self._add(
                plan, ActionKind.NOOP, Stage.USERS, "User exists",
                details=[f"{found.display_name or user.name} ({found.user_type or 'Member'}), object ID {found.object_id}"],
                warnings=warnings,
            )
            return

        identifier = user.email or user.object_id
        if user.object_id or not user.invite_if_missing:
            self._blocked(
                plan, Stage.USERS, f"User not found in tenant: {identifier}",
                "The user does not exist in this Microsoft Entra ID tenant; an invitation is required.",
                "Set 'invite_if_missing: true' for this user to send a B2B guest invitation, or invite them manually.",
            )
            return
        if Stage.USERS not in self.stages:
            self._blocked(plan, Stage.USERS, f"User not found in tenant: {identifier}",
                          "Invitations are part of the 'users' stage, which was excluded with --only.")
            return

        def apply() -> str:
            invited = self.ctx.users.invite(
                user.email, user.name, user.invite_redirect_url, user.send_invitation_email
            )
            plan.context.principal_id = invited.object_id
            plan.context.user_type = "Guest"
            log_event("invite_user", "invited", user=user.email, resource=invited.object_id)
            return f"Invitation created (object ID {invited.object_id}, status {invited.external_state or 'PendingAcceptance'})"

        self._add(
            plan, ActionKind.INVITE, Stage.USERS, f"Guest invitation: {user.email}",
            details=[
                "User not found in tenant - a B2B guest invitation will be created",
                f"Invitation email: {'will be sent' if user.send_invitation_email else 'not sent'}",
                f"Redirect URL: {user.invite_redirect_url}",
            ],
            apply=apply,
            required_permission="Microsoft Graph: User.Invite.All",
        )

    # -- resource groups -------------------------------------------------
    def _rg_state(self, name: str):
        key = name.lower()
        if key not in self._rg_cache:
            self._rg_cache[key] = self.ctx.resource_groups.get(name)
        return self._rg_cache[key]

    def resource_group_info(self, name: str):
        """Last known state of a resource group seen during planning/execution (None if unknown)."""
        return self._rg_cache.get(name.lower())

    def _rg_pending(self, name: str | None) -> bool:
        return bool(name) and name.lower() in self._planned_rgs

    def _rg_available(self, name: str) -> bool:
        """True if the RG exists now or will be created earlier in this run."""
        if self._rg_pending(name):
            return True
        try:
            return self._rg_state(name) is not None
        except OperationError:
            return False

    def _plan_resource_group(self, plan: UserPlan, name: str) -> None:
        key = name.lower()
        spec = plan.user.resource_group if plan.user.resource_group and plan.user.resource_group.name.lower() == key else None
        if key in self._planned_rgs:
            self._add(plan, ActionKind.NOOP, Stage.RESOURCE_GROUPS, f"Resource Group {name}",
                      details=[f"Will be created for {self._planned_rgs[key]} earlier in this run"])
            return
        try:
            info = self._rg_state(name)
        except OperationError as exc:
            self._blocked(plan, Stage.RESOURCE_GROUPS, f"Cannot read Resource Group {name}", exc.reason, exc.hint)
            return
        if info is not None:
            warnings = []
            if spec and spec.location and info.location.lower() != spec.location.lower():
                warnings.append(f"existing location is {info.location}, config says {spec.location}; location is not changed")
            self._add(plan, ActionKind.NOOP, Stage.RESOURCE_GROUPS, f"Resource Group exists: {info.name}",
                      details=[f"Location: {info.location} - skipping creation"], warnings=warnings)
            return
        if spec and spec.create_if_missing and Stage.RESOURCE_GROUPS in self.stages:
            self._planned_rgs[key] = plan.user.email

            def apply(spec=spec) -> str:
                created = self.ctx.resource_groups.create(spec.name, spec.location, spec.tags)
                self._rg_cache[key] = created
                return f"Created in {created.location}"

            details = [f"Location: {spec.location}"]
            if spec.tags:
                details.append("Tags: " + ", ".join(f"{k}={v}" for k, v in spec.tags.items()))
            self._add(plan, ActionKind.CREATE, Stage.RESOURCE_GROUPS, f"Resource Group: {spec.name}",
                      details=details, apply=apply, scope=resource_group_scope(self.config.subscription_id, spec.name).id,
                      required_permission=RG_WRITE, permission_scope=subscription_scope(self.config.subscription_id).id)
            return
        hint = ("Set 'create_if_missing: true' (and a location) under the user's resource_group to create it."
                if Stage.RESOURCE_GROUPS in self.stages else "Resource group creation was excluded with --only.")
        self._blocked(plan, Stage.RESOURCE_GROUPS, f"Resource Group not found: {name}",
                      "The resource group does not exist.", hint)

    # -- RBAC ------------------------------------------------------------
    def _plan_access(self, plan: UserPlan, access) -> None:
        user = plan.user
        try:
            scope = resolve_access_scope(access, self.config.subscription_id)
        except ValueError as exc:
            self._blocked(plan, Stage.RBAC, f"Assign {access.role}", str(exc))
            return
        scope_label = f"{scope.type.label}: {scope.short_name}"
        pending = False

        if scope.type in (ScopeType.RESOURCE_GROUP, ScopeType.RESOURCE):
            if not self._rg_available(scope.resource_group):
                return  # already reported as BLOCKED by resource group planning
            pending = self._rg_pending(scope.resource_group)
            if scope.type == ScopeType.RESOURCE:
                if pending:
                    self._blocked(plan, Stage.RBAC, f"Assign {access.role}",
                                  f"Resource {scope.id} cannot exist yet: its resource group is created in this run.")
                    return
                try:
                    if not self.ctx.resources.exists(scope.id):
                        self._blocked(plan, Stage.RBAC, f"Assign {access.role}", f"Resource not found: {scope.id}")
                        return
                except OperationError as exc:
                    self._blocked(plan, Stage.RBAC, f"Assign {access.role}", exc.reason, exc.hint)
                    return
        elif scope.type == ScopeType.MANAGEMENT_GROUP:
            try:
                if self.ctx.management_groups.get(scope.management_group) is None:
                    self._blocked(plan, Stage.RBAC, f"Assign {access.role}",
                                  f"Management group '{scope.management_group}' was not found or is not visible to you.")
                    return
            except OperationError as exc:
                self._blocked(plan, Stage.RBAC, f"Assign {access.role}", exc.reason, exc.hint)
                return

        sub_scope = subscription_scope(self.config.subscription_id).id
        lookup_scope = sub_scope if pending else scope.id
        try:
            role = self.ctx.rbac.resolve_role(access.role, lookup_scope)
        except OperationError as exc:
            self._blocked(plan, Stage.RBAC, f"Assign {access.role}", exc.reason, exc.hint)
            return

        principal_key = plan.context.principal_id or user.email.lower()
        dedupe = (principal_key, role.guid.lower(), scope.id.lower())
        if dedupe in self._planned_assignments:
            self._add(plan, ActionKind.NOOP, Stage.RBAC, f"{role.name} role",
                      details=[f"Scope: {scope_label}", "Duplicate entry - already handled earlier in this run"])
            return

        notes: list[str] = []
        if plan.context.principal_id and not pending:
            try:
                direct, inherited = self.ctx.rbac.find_assignment(plan.context.principal_id, role, scope.id)
            except OperationError as exc:
                self._blocked(plan, Stage.RBAC, f"Assign {role.name}", exc.reason, exc.hint)
                return
            if direct:
                self._planned_assignments[dedupe] = user.email
                self._add(plan, ActionKind.NOOP, Stage.RBAC, f"{role.name} role already assigned",
                          details=[f"Scope: {scope_label}", f"Assignment ID: {direct.name}"])
                return
            for assignment in inherited:
                label, name = describe_scope(assignment.scope)
                notes.append(f"Note: {role.name} is already inherited from {label} '{name}'; a direct assignment will be added")

        self._planned_assignments[dedupe] = user.email
        high = is_high_privilege(role.name) or scope.type == ScopeType.MANAGEMENT_GROUP
        warnings = []
        template = _HIGH_PRIVILEGE_WARNINGS.get(role.name.lower())
        if template:
            warnings.append(template.format(scope=scope.type.label.lower()))
        if scope.type == ScopeType.MANAGEMENT_GROUP:
            warnings.append("Management group scope: the role applies to every subscription beneath it.")

        def apply(role=role, scope=scope) -> str:
            principal_id = plan.context.principal_id
            if not principal_id:
                raise OperationError(f"Assign {role.name} role", "The user's object ID is unknown.", user=user.email,
                                     scope=scope.id)
            assignment, created = self.ctx.rbac.create_assignment(
                scope.id, role, principal_id, principal_type=plan.context.principal_type, user_label=user.email
            )
            return f"Assigned (assignment {assignment.name})" if created else "Already assigned"

        self._add(
            plan, ActionKind.ASSIGN, Stage.RBAC, role.name,
            details=[f"Scope: {scope_label}", f"Scope ID: {scope.id}"] + notes,
            warnings=warnings, high_privilege=high, apply=apply, scope=scope.id,
            required_permission=RBAC_WRITE, permission_scope=sub_scope if pending else scope.id,
        )

    # -- budgets ---------------------------------------------------------
    def _plan_budget(self, plan: UserPlan) -> None:
        spec = plan.user.budget
        if spec.scope_kind == ScopeType.RESOURCE_GROUP:
            if not self._rg_available(spec.resource_group):
                return  # reported by resource group planning
            scope = resource_group_scope(self.config.subscription_id, spec.resource_group)
        else:
            scope = subscription_scope(self.config.subscription_id)
        scope_label = f"{scope.type.label}: {scope.short_name}"
        key = (scope.id.lower(), spec.name.lower())
        if key in self._planned_budgets:
            self._add(plan, ActionKind.NOOP, Stage.BUDGETS, f"Budget {spec.name}",
                      details=[f"Scope: {scope_label}", f"Handled for {self._planned_budgets[key]} earlier in this run"])
            return
        self._planned_budgets[key] = plan.user.email

        pending = scope.type == ScopeType.RESOURCE_GROUP and self._rg_pending(spec.resource_group)
        existing = None
        if not pending:
            try:
                existing = self.ctx.budgets.get(scope.id, spec.name)
            except OperationError as exc:
                self._blocked(plan, Stage.BUDGETS, f"Budget {spec.name}", exc.reason, exc.hint)
                return

        if existing and existing.currency and not self.billing_currency:
            self.billing_currency = existing.currency  # learned from Azure; used by the summary too
        currency = self.billing_currency
        warnings = []
        if spec.currency and currency and spec.currency != currency.upper():
            warnings.append(
                f"Budget amounts are in the billing currency ({currency}); config currency {spec.currency} is NOT converted."
            )
        display_currency = currency or spec.currency
        details = [
            f"Scope: {scope_label}",
            f"Amount: {_money(spec.amount, display_currency)}",
            f"Period: {spec.period}",
        ] + _alert_lines(spec)
        permission_scope = subscription_scope(self.config.subscription_id).id if pending else scope.id

        if existing is None:
            def apply(spec=spec, scope=scope) -> str:
                body = budget_mod.build_budget_body(spec, start_date=budget_mod.default_start_date(self.today))
                self.ctx.budgets.create_or_update(scope.id, spec.name, body, user_label=plan.user.email)
                return f"Created with {len(spec.alerts)} alert(s)"

            self._add(plan, ActionKind.CREATE, Stage.BUDGETS, f"Budget: {spec.name}", details=details,
                      warnings=warnings, apply=apply, scope=scope.id,
                      required_permission=budget_mod.WRITE_PERMISSION, permission_scope=permission_scope)
            return

        changes = budget_mod.diff_budget(spec, existing)
        if not changes:
            self._add(plan, ActionKind.NOOP, Stage.BUDGETS, f"Budget already exists: {spec.name}",
                      details=details[:3], warnings=warnings)
            if spec.alerts:
                self._add(plan, ActionKind.NOOP, Stage.BUDGETS, "Budget notifications already configured",
                          details=_alert_lines(spec))
            return

        def apply_update(spec=spec, scope=scope, existing=existing) -> str:
            body = budget_mod.build_budget_body(spec, etag=existing.etag, existing_time_period=existing.time_period)
            self.ctx.budgets.create_or_update(scope.id, spec.name, body, user_label=plan.user.email)
            return "Updated: " + "; ".join(changes)

        self._add(plan, ActionKind.UPDATE, Stage.BUDGETS, f"Budget: {spec.name}",
                  details=[f"Scope: {scope_label}"] + [f"Change: {c}" for c in changes], warnings=warnings,
                  apply=apply_update, scope=scope.id,
                  required_permission=budget_mod.WRITE_PERMISSION, permission_scope=permission_scope)

    # ------------------------------------------------------------------ preflight requirements
    def requirements(self, plans: list[UserPlan]) -> PreflightRequirements:
        req = PreflightRequirements()
        for plan in plans:
            for action in plan.changes:
                if action.required_permission == RG_WRITE:
                    req.rg_create_needed = True
                elif action.required_permission == RBAC_WRITE and action.permission_scope:
                    req.rbac_scopes.append(action.permission_scope)
                elif action.required_permission == budget_mod.WRITE_PERMISSION and action.permission_scope:
                    req.budget_scopes.append(action.permission_scope)
                elif action.kind == ActionKind.INVITE:
                    req.needs_invite = True
        req.needs_directory = bool(plans) and bool(self.stages & {Stage.USERS, Stage.RBAC})
        req.check_costs = Stage.BUDGETS in self.stages
        return req

    # ------------------------------------------------------------------ execution
    def execute(self, plans: list[UserPlan], on_progress: Callable[[UserPlan, Action], None] | None = None) -> None:
        for plan in plans:
            if plan.blocked:
                for action in plan.changes:
                    action.status = "skipped"
                    action.result = "skipped: user has blocking validation errors"
                log_event("setup_user", "blocked", user=plan.user.email)
                continue
            failed = False
            for action in plan.actions:
                if not action.kind.is_change:
                    continue
                if failed:
                    action.status = "skipped"
                    action.result = "skipped due to an earlier failure"
                    continue
                try:
                    action.result = action.apply() if action.apply else "done"
                    action.status = "done"
                except Exception as exc:  # every failure is reported, never a raw traceback
                    error = translate_error(exc, f"{action.kind.value} {action.title}", user=plan.user.email,
                                            scope=action.scope, required_permission=action.required_permission)
                    action.status = "failed"
                    action.error = error
                    failed = True
                    log_event("setup_action", "failed", user=plan.user.email, scope=action.scope,
                              resource=action.title, error=error.reason)
                if on_progress:
                    on_progress(plan, action)


def _money(amount: float, currency: str | None) -> str:
    from .console import format_money

    return format_money(amount, currency)


def _alert_lines(spec) -> list[str]:
    if not spec.alerts:
        return ["Alerts: none (notifications disabled)"]
    actual = [a for a in spec.alerts if a.threshold_type == "Actual"]
    forecast = [a for a in spec.alerts if a.threshold_type == "Forecasted"]
    lines = []
    if actual:
        lines.append("Alerts (actual): " + ", ".join(f"{a.threshold:g}%" for a in actual))
    if forecast:
        lines.append("Alerts (forecasted): " + ", ".join(f"{a.threshold:g}%" for a in forecast))
    recipients = sorted({e for a in spec.alerts for e in a.emails} | {f"role:{r}" for a in spec.alerts for r in a.roles}
                        | {"action group" for a in spec.alerts if a.action_groups})
    if recipients:
        suffix = " (defaulted to the user's email)" if spec.recipients_defaulted else ""
        lines.append("Recipients: " + ", ".join(recipients) + suffix)
    return lines
