"""Console rendering for plans, results, budget status, reports and audits."""

from __future__ import annotations

from .budgets import BudgetInfo, budget_health, threshold_states
from .console import Console, format_percent
from .models import ActionKind, Stage, UserPlan
from .permissions import CheckResult
from .subscriptions import SubscriptionInfo

_KIND_TAG = {
    ActionKind.INVITE: "[INVITE]",
    ActionKind.CREATE: "[CREATE]",
    ActionKind.ASSIGN: "[ASSIGN]",
    ActionKind.UPDATE: "[UPDATE]",
    ActionKind.BLOCKED: "[BLOCKED]",
}
_TAG_WIDTH = 10


# --------------------------------------------------------------------------- pre-flight


def render_preflight(console: Console, results: list[CheckResult]) -> bool:
    console.print()
    console.print(console.style("PRE-FLIGHT CHECK", "bold"), force=True)
    console.print()
    ok = True
    for result in results:
        if result.status == "ok":
            console.ok(f"{result.name}" + (f"  ({result.detail})" if result.detail else ""))
        elif result.status == "warn":
            console.print(f"{console.style(console.sym('warn'), 'yellow')} {result.name}: {result.detail}", force=True)
        else:
            ok = False
            console.fail(f"{result.name}: {result.detail}")
    console.print()
    if ok:
        console.print("Ready to proceed.")
    return ok


# --------------------------------------------------------------------------- plan


def _subscription_block(console: Console, subscription: SubscriptionInfo) -> None:
    console.print()
    console.print("Subscription:")
    console.print(f"  {subscription.display_name}")
    console.print(f"  {subscription.subscription_id}")


def render_plan(console: Console, subscription: SubscriptionInfo, plans: list[UserPlan], *, dry_run: bool) -> None:
    console.print()
    console.banner("DRY RUN" if dry_run else "EXECUTION PLAN")
    _subscription_block(console, subscription)
    for plan in plans:
        console.print()
        console.print("User:")
        if plan.user.name and plan.user.name != plan.user.email:
            console.print(f"  {plan.user.name}")
        console.print(f"  {plan.user.email or plan.user.object_id}")
        console.print()
        console.print("Actions:")
        for action in plan.actions:
            _render_action(console, action, blocked_user=plan.blocked)
        if plan.blocked and plan.changes:
            console.warn("This user has blocking problems; none of the actions above will be applied for them.")
    console.print()


def _render_action(console: Console, action, *, blocked_user: bool) -> None:
    pad = " " * (2 + _TAG_WIDTH + 1)
    if action.kind == ActionKind.NOOP:
        console.print(f"  {console.style(console.sym('ok'), 'green')} {action.title}")
        pad = "    "
    else:
        color = {"BLOCKED": "red", "UPDATE": "yellow"}.get(action.kind.value, "cyan")
        tag = _KIND_TAG[action.kind].ljust(_TAG_WIDTH)
        flag = console.style("  (requires confirmation)", "yellow") if action.high_privilege and not blocked_user else ""
        console.print(f"  {console.style(tag, color, 'bold')} {action.title}{flag}")
    for detail in action.details:
        console.print(f"{pad}{detail}")
    for warning in action.warnings:
        console.print(f"{pad}{console.style('WARNING: ' + warning, 'yellow')}")


# --------------------------------------------------------------------------- confirmation


def render_high_privilege_warning(console: Console, plan: UserPlan, action) -> None:
    scope_line = next((d for d in action.details if d.startswith("Scope:")), "")
    console.print(force=True)
    console.print(console.style("WARNING", "yellow", "bold"), force=True)
    console.print(force=True)
    console.print("You are about to assign:", force=True)
    console.print(force=True)
    console.print(f"Role: {action.title}", force=True)
    console.print(scope_line, force=True)
    console.print(force=True)
    console.print(f"User: {plan.user.email}", force=True)
    console.print(force=True)
    for warning in action.warnings:
        console.print(warning, force=True)
    console.print(force=True)


# --------------------------------------------------------------------------- results


def render_progress(console: Console, plan: UserPlan, action) -> None:
    if action.status == "done":
        console.ok(f"{action.kind.value} {action.title}: {action.result}")
    elif action.status == "failed":
        console.fail(f"{action.kind.value} {action.title}: {getattr(action.error, 'reason', action.error)}")
    elif action.status == "skipped":
        console.print(f"  - {action.kind.value} {action.title}: {action.result}")


def _stage_status(plans: list[UserPlan], stage: Stage) -> tuple[str | None, str]:
    """(status, detail) for one stage across all users: ok | partial | fail | None (stage not used)."""
    actions = [a for p in plans for a in p.actions if a.stage == stage]
    if not actions:
        return None, ""
    bad = [a for a in actions if a.kind == ActionKind.BLOCKED or a.status in ("failed", "skipped", "pending") and a.kind.is_change]
    good = [a for a in actions if a not in bad and a.kind != ActionKind.BLOCKED]
    if not bad:
        return "ok", ""
    detail = f"{len(bad)} of {len(bad) + len(good)} item(s) not applied"
    return ("partial" if good else "fail"), detail


def render_summary(
    console: Console,
    subscription: SubscriptionInfo,
    plans: list[UserPlan],
    *,
    engine,
    mtd_cost,
    cost_error: str | None,
    executed: bool,
) -> str:
    """Final report. Returns the overall status string."""
    from .scopes import describe_scope

    console.print()
    console.banner("AZURE ACCESS MANAGER", force=True)
    _subscription_block(console, subscription)
    currency = (mtd_cost.currency if mtd_cost else None) or engine.billing_currency
    for plan in plans:
        user = plan.user
        console.print()
        console.print("User:")
        if user.name and user.name != user.email:
            console.print(f"  {user.name}")
        console.print(f"  {user.email or user.object_id}")
        if plan.context.user_type:
            console.print(f"  ({plan.context.user_type})")

        if user.resource_group:
            info = engine.resource_group_info(user.resource_group.name)
            console.print()
            console.print("Resource Group:")
            console.print(f"  {user.resource_group.name}")
            console.print(f"  {info.location if info else (user.resource_group.location or 'not created')}")

        assigns = [a for a in plan.actions if a.stage == Stage.RBAC and a.kind in (ActionKind.ASSIGN, ActionKind.NOOP)]
        if assigns:
            console.print()
            console.print("RBAC:")
            for action in assigns:
                role = action.title.replace(" role already assigned", "").replace(" role", "")
                scope = next((d.replace("Scope: ", "") for d in action.details if d.startswith("Scope: ")), "")
                mark = _mark(console, action)
                console.print(f"  {role} {mark}")
                console.print(f"  Scope: {scope}")

        budget = user.budget
        if budget:
            console.print()
            console.print("Budget:")
            per = {"Monthly": "month", "Quarterly": "quarter", "Annually": "year"}.get(budget.period, budget.period)
            console.print(f"  {console.money(budget.amount, currency or budget.currency)} / {per}  ({budget.name})")
            if budget.alerts:
                console.print()
                console.print("Alerts:")
                budget_actions = [a for a in plan.actions if a.stage == Stage.BUDGETS]
                ok = budget_actions and all(a.status != "failed" and a.kind != ActionKind.BLOCKED for a in budget_actions) \
                    and (executed or all(a.kind == ActionKind.NOOP for a in budget_actions))
                for alert in budget.alerts:
                    kind = "" if alert.threshold_type == "Actual" else " (forecast)"
                    console.print(f"  {alert.threshold:g}%{kind} {console.sym('ok') if ok else console.sym('open')}")
            spend = None
            if mtd_cost and budget.resource_group:
                spend = mtd_cost.for_resource_group(budget.resource_group)
            elif mtd_cost:
                spend = mtd_cost.total
            console.print()
            console.print("Current Spend:")
            if spend is None:
                console.print(f"  N/A{' (' + cost_error + ')' if cost_error else ''}")
            else:
                console.print(f"  {console.money(spend, currency or budget.currency)}  (month to date)")
                console.print()
                console.print("Budget Utilization:")
                console.print(f"  {format_percent(spend / budget.amount * 100 if budget.amount else None)}")
                if budget.period != "Monthly":
                    console.note("  (month-to-date spend vs. a non-monthly budget)")

    # Overall result
    console.print()
    console.banner("RESULT", force=True)
    console.print(force=True)
    console.ok("Authentication successful", force=True)
    labels = [
        (Stage.USERS, "User verified"),
        (Stage.RESOURCE_GROUPS, "Resource Group verified"),
        (Stage.RBAC, "RBAC configured"),
        (Stage.BUDGETS, "Budget configured"),
    ]
    if any(p.user.budget and p.user.budget.alerts for p in plans):
        labels.append((Stage.BUDGETS, "Alerts configured"))
    for stage, label in labels:
        status, detail = _stage_status(plans, stage)
        if status == "ok":
            console.ok(label, force=True)
        elif status == "partial":
            console.print(f"{console.style(console.sym('warn'), 'yellow')} {label} partially ({detail})", force=True)
        elif status == "fail":
            console.fail(f"{label.replace('verified', 'not verified').replace('configured', 'not configured')} ({detail})")
    if mtd_cost:
        console.ok("Cost information retrieved", force=True)
    else:
        console.print(f"{console.sym('warn')} Cost information unavailable{': ' + cost_error if cost_error else ''}", force=True)

    changes = [a for p in plans for a in p.changes]
    failed = [a for a in changes if a.status == "failed"]
    blocked = [p for p in plans if p.blocked]
    if failed or blocked:
        done = [a for a in changes if a.status == "done"]
        status = "PARTIAL" if done else "FAILED"
    elif not changes:
        status = "NO CHANGES REQUIRED"
    else:
        status = "SUCCESS"
    console.print(force=True)
    if status == "NO CHANGES REQUIRED":
        console.print("Setup is already complete.", force=True)
    color = {"SUCCESS": "green", "NO CHANGES REQUIRED": "green", "PARTIAL": "yellow", "FAILED": "red"}[status]
    console.print(f"STATUS: {console.style(status, color, 'bold')}", force=True)
    console.rule(force=True)
    for action in failed:
        error = action.error
        console.error_block(
            operation=getattr(error, "operation", action.title),
            reason=getattr(error, "reason", str(error)),
            user=getattr(error, "user", None) or action.user,
            scope=describe_scope(action.scope)[1] if action.scope else None,
            required_permission=getattr(error, "required_permission", None),
            hint=getattr(error, "hint", None),
        )
    return status


def _mark(console: Console, action) -> str:
    if action.kind == ActionKind.NOOP:
        return console.style(f"{console.sym('ok')} (already assigned)", "green")
    if action.status == "done":
        return console.style(console.sym("ok"), "green")
    if action.status == "failed":
        return console.style(console.sym("fail"), "red")
    return console.sym("open")


# --------------------------------------------------------------------------- budget status


def render_budget_status(console: Console, budget: BudgetInfo, *, label: str) -> list[str]:
    """Print one budget's status. Returns warning lines for thresholds that were crossed."""
    from .scopes import describe_scope

    scope_type, scope_name = describe_scope(budget.scope) if budget.scope else ("Scope", "?")
    usage = budget.usage_percent
    status = budget_health(usage)
    color = {"HEALTHY": "green", "WARNING": "yellow", "CRITICAL": "red", "EXCEEDED": "red"}.get(status)
    console.print()
    console.print(f"{scope_type}: {scope_name}", force=True)
    console.print()
    console.field("Budget", f"{console.money(budget.amount, budget.currency)}  ({budget.name}, {budget.time_grain})")
    console.field("Period start", budget.start_date or "N/A")
    console.field("Current Spend", console.money(budget.current_spend, budget.currency))
    console.field("Remaining", console.money(budget.remaining, budget.currency))
    console.field("Usage", format_percent(usage))
    if budget.forecast_spend is not None:
        forecast_pct = budget.forecast_spend / budget.amount * 100 if budget.amount else None
        console.field("Forecast", f"{console.money(budget.forecast_spend, budget.currency)} ({format_percent(forecast_pct)})")
    console.print()
    console.print(f"{'Status:'.ljust(18)}{console.style(status, color, 'bold') if color else status}", force=True)

    states = threshold_states(budget)
    warnings = []
    console.print()
    console.print("Thresholds:")
    if not states:
        console.print("  (no alerts configured)")
    for state in states:
        kind = "" if state.threshold_type == "Actual" else " forecast"
        if state.crossed:
            console.print(f"  {f'{state.threshold:g}%{kind}'.ljust(16)}{console.style(console.sym('hit'), 'red')}  exceeded")
            warnings.append(f"{state.threshold:g}%{kind} budget threshold exceeded ({label}).")
        else:
            console.print(f"  {f'{state.threshold:g}%{kind}'.ljust(16)}{console.sym('open')}")
    for warning in warnings:
        console.warn(warning)
    return warnings


# --------------------------------------------------------------------------- subscription report


def render_subscription_report(console: Console, report: dict) -> None:
    sub = report["subscription"]
    costs = report["costs"]
    currency = costs.get("currency")
    console.banner("SUBSCRIPTION REPORT")
    console.note(f"Generated {report['generated_at']}  |  signed in as {report.get('signed_in_as')}")

    console.section("Subscription Information")
    console.field("Subscription Name", sub["display_name"], 22)
    console.field("Subscription ID", sub["subscription_id"], 22)
    console.field("Tenant ID", sub["tenant_id"], 22)
    console.field("State", sub["state"], 22)
    console.field("Location", f"{sub['location'] or 'N/A'} (subscriptions are global; this is the placement ID)", 22)
    console.field("Offer", sub["offer"] or "N/A", 22)
    console.field("Spending limit", sub["spending_limit"] or "N/A", 22)
    console.field("Tags", ", ".join(f"{k}={v}" for k, v in sub["tags"].items()) or "(none)", 22)

    console.section("Cost")
    if costs["available"]:
        console.field("Current month", console.money(costs["current_month"], currency), 22)
        change = (f"  (month to date is {costs['mtd_vs_previous_pct']:.1f}% of last month)"
                  if costs.get("mtd_vs_previous_pct") is not None else "")
        console.field("Previous month", console.money(costs["previous_month"], currency) + change, 22)
        if costs["monthly_budget"]:
            console.field("Monthly budget", f"{console.money(costs['monthly_budget'], currency)}  ({costs['monthly_budget_basis']})", 22)
            console.field("Budget utilization", format_percent(costs["budget_utilization_pct"]), 22)
        else:
            console.field("Monthly budget", "(no monthly budgets)", 22)
        if costs.get("unallocated_cost"):
            console.field("Not in any RG", console.money(costs["unallocated_cost"], currency), 22)
        console.note(costs["note"])
    else:
        console.print("  Cost data unavailable (see warnings).")

    console.section("Resource Groups")
    console.table(
        ["Resource Group", "Location", "Resources", "Budget", "MTD Cost", "Budget Spend", "Utilization", "Status"],
        [
            [
                rg["name"],
                rg["location"],
                rg["resource_count"] if rg["resource_count"] is not None else "-",
                f"{console.money(rg['budget_amount'], currency)} {rg['budget_period'] or ''}".strip() if rg["budget_amount"] else "-",
                console.money(rg["current_month_cost"], currency) if rg["current_month_cost"] is not None else "-",
                console.money(rg["budget_spend"], currency) if rg["budget_spend"] is not None else "-",
                format_percent(rg["budget_utilization_pct"]) if rg["budget_utilization_pct"] is not None else "-",
                rg["budget_status"] or "-",
            ]
            for rg in report["resource_groups"]
        ],
    )

    if report["costs"]["subscription_budgets"]:
        console.section("Subscription Budgets")
        render_budget_rows(console, report["costs"]["subscription_budgets"])

    console.section("Users / RBAC")
    render_assignment_rows(console, report["role_assignments"])

    console.section("Resources")
    render_resource_rows(console, report["resources"])

    render_warnings(console, report.get("warnings", []))


def render_assignment_rows(console: Console, rows: list[dict]) -> None:
    console.table(
        ["Principal", "Email / UPN", "Type", "Role", "Scope", "Scope Type", "Assignment", "Assignment ID"],
        [
            [
                r["principal_name"], r["principal_email"] or "-",
                r["principal_type"] + (f" ({r['user_type']})" if r.get("user_type") == "Guest" else ""),
                r["role"] + (" *" if r.get("conditional") else ""),
                r["scope_name"], r["scope_type"], r["assignment_type"], r["assignment_id"],
            ]
            for r in rows
        ],
        max_width=40,
    )
    if any(r.get("conditional") for r in rows):
        console.note("* assignment has an ABAC condition")


def render_resource_rows(console: Console, rows: list[dict]) -> None:
    if not rows:
        console.print("  (none)")
        return
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["resource_group"] or "(none)", []).append(row)
    for rg, items in groups.items():
        console.print()
        console.print(console.style(f"{rg}  ({len(items)})", "bold"))
        console.table(["Resource Name", "Resource Type", "Region", "Status"],
                      [[r["name"], r["type"], r["location"], r["status"]] for r in items], indent="  ")


def render_budget_rows(console: Console, rows: list[dict]) -> None:
    console.table(
        ["Budget", "Scope", "Amount", "Period", "Spend", "Utilization", "Status", "Alerts"],
        [
            [
                b["name"], f"{b['scope_type']}: {b['scope_name']}", console.money(b["amount"], b["currency"]),
                b["period"], console.money(b["current_spend"], b["currency"]),
                format_percent(b["utilization_pct"]), b["status"], b["alerts"] or "-",
            ]
            for b in rows
        ],
    )


def render_warnings(console: Console, warnings: list[str]) -> None:
    if warnings:
        console.print()
        for warning in dict.fromkeys(warnings):
            console.warn(warning)


def render_audit(console: Console, audit: dict) -> None:
    console.banner("AUDIT")
    sub = audit["subscription"]
    console.print(f"Subscription: {sub['display_name']} ({sub['subscription_id']})")
    summary = audit["summary"]
    console.print("Findings: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
    console.print()
    if not audit["findings"]:
        console.ok("No findings.")
    colors = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "cyan", "INFO": "dim"}
    for finding in audit["findings"]:
        console.print(f"{console.style(finding['severity'].ljust(7), colors[finding['severity']], 'bold')} "
                      f"[{finding['category']}] {finding['finding']}")
        if finding.get("principal"):
            console.print(f"        Principal: {finding['principal']}")
        if finding.get("recommendation"):
            console.print(f"        {console.sym('arrow')} {finding['recommendation']}")
    render_warnings(console, audit.get("warnings", []))
