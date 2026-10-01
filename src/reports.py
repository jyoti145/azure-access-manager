"""Data collection for the subscription report, listings and audit, plus JSON/CSV export.

Every section is collected independently: if one API is not permitted (for example Cost
Management on an unsupported offer), that section is reported as unavailable with a warning
and the rest of the report is still produced.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .budgets import BudgetInfo, budget_health
from .errors import OperationError
from .models import ActionKind, AppConfig, ScopeType, UserPlan
from .rbac import HIGH_PRIVILEGE_ROLES
from .scopes import describe_scope, relation, resource_group_scope, same_scope

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
_SCOPE_DEPTH = {"Tenant (root)": 0, "Management Group": 1, "Subscription": 2, "Resource Group": 3, "Resource": 4}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- RBAC


def collect_assignments(ctx, warnings: list[str], scope: str | None = None) -> tuple[list[dict], bool]:
    """Return (assignment rows, principals_resolved). Rows carry Direct/Inherited relative to ``scope``."""
    target = scope or ctx.subscription_scope
    assignments = ctx.rbac.list_for_scope(target) if scope else ctx.rbac.list_subscription_assignments()
    principals: dict = {}
    resolved = True
    try:
        principals = ctx.users.resolve_principals([a.principal_id for a in assignments])
    except OperationError as exc:
        resolved = False
        warnings.append(f"Principal names unavailable (Microsoft Graph): {exc.reason}")

    rows = []
    for assignment in assignments:
        principal = principals.get(assignment.principal_id)
        scope_type, scope_name = describe_scope(assignment.scope)
        rel = relation(assignment.scope, target)
        if principal:
            name, email = principal.display_name, principal.email
            principal_type, user_type = principal.principal_type, principal.user_type
        else:
            name = "(deleted or unknown principal)" if resolved else assignment.principal_id
            email, principal_type, user_type = None, assignment.principal_type or "Unknown", None
        rows.append({
            "principal_name": name,
            "principal_email": email,
            "principal_type": principal_type,
            "user_type": user_type,
            "principal_id": assignment.principal_id,
            "role": ctx.rbac.role_name(assignment.role_definition_id),
            "scope_type": scope_type,
            "scope_name": scope_name,
            "scope": assignment.scope,
            "assignment_type": "Inherited" if rel == "Inherited" else "Direct",
            "assignment_id": assignment.name,
            "conditional": bool(assignment.condition),
            "principal_resolved": principal is not None,
        })
    rows.sort(key=lambda r: (_SCOPE_DEPTH.get(r["scope_type"], 9), r["scope_name"].lower(), r["role"].lower(),
                             (r["principal_name"] or "").lower()))
    return rows, resolved


# --------------------------------------------------------------------------- budgets


def budget_row(budget: BudgetInfo) -> dict:
    scope_type, scope_name = describe_scope(budget.scope) if budget.scope else ("Unknown", "")
    usage = budget.usage_percent
    return {
        "name": budget.name,
        "scope_type": scope_type,
        "scope_name": scope_name,
        "amount": budget.amount,
        "period": budget.time_grain,
        "current_spend": budget.current_spend,
        "forecast_spend": budget.forecast_spend,
        "currency": budget.currency,
        "utilization_pct": round(usage, 1) if usage is not None else None,
        "status": budget_health(usage),
        "alerts": ", ".join(
            f"{float(n.get('threshold') or 0):g}%{' (forecast)' if str(n.get('thresholdType')).lower() == 'forecasted' else ''}"
            for n in sorted(budget.notifications.values(), key=lambda n: float(n.get("threshold") or 0))
        ),
        "start_date": budget.start_date,
        "end_date": budget.end_date,
    }


def collect_budgets(ctx, warnings: list[str], resource_groups: list | None = None) -> list[BudgetInfo]:
    """Subscription-level budgets plus the budgets of every given resource group."""
    budgets: list[BudgetInfo] = []
    sub_scope = ctx.subscription_scope
    try:
        budgets += [b for b in ctx.budgets.list(sub_scope) if not b.scope or same_scope(b.scope, sub_scope)]
    except OperationError as exc:
        warnings.append(f"Budgets unavailable: {exc.reason}")
        return budgets
    if resource_groups is None:
        resource_groups = ctx.resource_groups.list()
    for rg in resource_groups:
        try:
            budgets += ctx.budgets.list(resource_group_scope(ctx.subscription_id, rg.name).id)
        except OperationError as exc:
            warnings.append(f"Resource group budgets unavailable: {exc.reason}")
            break
    return budgets


# --------------------------------------------------------------------------- subscription report


def build_subscription_report(
    ctx,
    *,
    include_resources: bool = True,
    include_costs: bool = True,
    include_rbac: bool = True,
) -> dict[str, Any]:
    warnings: list[str] = []
    subscription = ctx.subscriptions.get(ctx.subscription_id)
    resource_groups = ctx.resource_groups.list()

    resources = []
    if include_resources:
        try:
            resources = ctx.resources.list()
        except OperationError as exc:
            warnings.append(f"Resources unavailable: {exc.reason}")
    counts: dict[str, int] = {}
    for resource in resources:
        counts[resource.resource_group.lower()] = counts.get(resource.resource_group.lower(), 0) + 1

    mtd = previous = None
    budgets: list[BudgetInfo] = []
    if include_costs:
        try:
            mtd = ctx.costs.month_to_date()
            previous = ctx.costs.previous_month()
        except OperationError as exc:
            warnings.append(f"Cost data unavailable: {exc.reason}")
        budgets = collect_budgets(ctx, warnings, resource_groups)

    by_scope: dict[str, list[BudgetInfo]] = {}
    for budget in budgets:
        by_scope.setdefault(budget.scope.lower(), []).append(budget)

    currency = (mtd.currency if mtd else None) or next((b.currency for b in budgets if b.currency), None)

    rg_rows = []
    for rg in resource_groups:
        rg_budgets = by_scope.get(resource_group_scope(ctx.subscription_id, rg.name).id.lower(), [])
        primary = rg_budgets[0] if rg_budgets else None
        rg_rows.append({
            "name": rg.name,
            "location": rg.location,
            "resource_count": counts.get(rg.name.lower(), 0) if include_resources else None,
            "current_month_cost": mtd.for_resource_group(rg.name) if mtd else None,
            "previous_month_cost": previous.for_resource_group(rg.name) if previous else None,
            "budget_name": primary.name if primary else None,
            "budget_amount": primary.amount if primary else None,
            "budget_period": primary.time_grain if primary else None,
            "budget_spend": primary.current_spend if primary else None,
            "budget_utilization_pct": round(primary.usage_percent, 1) if primary and primary.usage_percent is not None else None,
            "budget_status": budget_health(primary.usage_percent) if primary else None,
            "budget_count": len(rg_budgets),
            "tags": rg.tags,
        })

    sub_budgets = by_scope.get(ctx.subscription_scope.lower(), [])
    monthly_sub = [b for b in sub_budgets if b.time_grain.lower() == "monthly"]
    if monthly_sub:
        monthly_total, basis = sum(b.amount for b in monthly_sub), "subscription monthly budget(s)"
    else:
        monthly_rg = [b for b in budgets if b.time_grain.lower() == "monthly" and not same_scope(b.scope, ctx.subscription_scope)]
        monthly_total = sum(b.amount for b in monthly_rg) if monthly_rg else None
        basis = "sum of resource group monthly budgets" if monthly_rg else None

    current_total = mtd.total if mtd else None
    costs = {
        "available": mtd is not None,
        "currency": currency,
        "current_month": current_total,
        "previous_month": previous.total if previous else None,
        "mtd_vs_previous_pct": (
            round(current_total / previous.total * 100, 1) if mtd and previous and previous.total else None
        ),
        "monthly_budget": monthly_total,
        "monthly_budget_basis": basis,
        "budget_utilization_pct": (
            round(current_total / monthly_total * 100, 1) if current_total is not None and monthly_total else None
        ),
        "unallocated_cost": mtd.by_resource_group.get("(no resource group)") if mtd else None,
        "subscription_budgets": [budget_row(b) for b in sub_budgets],
        "note": "Cost data typically lags actual usage by 8-24 hours.",
    }

    assignments: list[dict] = []
    if include_rbac:
        try:
            assignments, _ = collect_assignments(ctx, warnings)
        except OperationError as exc:
            warnings.append(f"Role assignments unavailable: {exc.reason}")

    return {
        "report": "subscription-report",
        "generated_at": _now(),
        "signed_in_as": ctx.identity.label if ctx.identity else None,
        "subscription": subscription.as_dict(),
        "resource_groups": rg_rows,
        "role_assignments": assignments,
        "resources": [r.as_dict() for r in resources],
        "costs": costs,
        "budgets": [budget_row(b) for b in budgets],
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- audit


def _finding(severity: str, category: str, finding: str, *, scope: str | None = None,
             principal: str | None = None, recommendation: str | None = None) -> dict:
    return {
        "severity": severity,
        "category": category,
        "finding": finding,
        "scope": scope,
        "principal": principal,
        "recommendation": recommendation,
    }


def build_audit(ctx, config: AppConfig | None, plans: list[UserPlan]) -> dict[str, Any]:
    """Read-only audit: configuration drift plus security / cost hygiene findings."""
    warnings: list[str] = []
    findings: list[dict] = []
    subscription = ctx.subscriptions.get(ctx.subscription_id)
    if not subscription.is_enabled:
        findings.append(_finding("HIGH", "Subscription", f"Subscription state is {subscription.state}",
                                 scope=ctx.subscription_scope))

    # Drift between config.yaml and Azure
    for plan in plans:
        for action in plan.actions:
            detail = f" ({action.details[0]})" if action.details else ""
            if action.kind == ActionKind.BLOCKED:
                findings.append(_finding("HIGH", "Configuration", f"{action.title}{detail}", scope=action.scope,
                                         principal=plan.user.email,
                                         recommendation=action.details[1] if len(action.details) > 1 else None))
            elif action.kind.is_change:
                findings.append(_finding("MEDIUM", "Drift", f"[{action.kind.value}] {action.title}{detail}",
                                         scope=action.scope, principal=plan.user.email,
                                         recommendation="Run 'python azure_manager.py setup' to apply the configuration."))

    # Role assignments
    rows: list[dict] = []
    resolved = False
    try:
        rows, resolved = collect_assignments(ctx, warnings)
    except OperationError as exc:
        warnings.append(f"Role assignments unavailable: {exc.reason}")
    for row in rows:
        who = row["principal_email"] or row["principal_name"]
        broad = row["scope_type"] in (ScopeType.SUBSCRIPTION.label, ScopeType.MANAGEMENT_GROUP.label, ScopeType.TENANT.label)
        privileged = row["role"].lower() in HIGH_PRIVILEGE_ROLES
        if privileged and row["user_type"] == "Guest":
            findings.append(_finding("HIGH", "Privileged access", f"Guest user has {row['role']} at {row['scope_type']} '{row['scope_name']}'",
                                     scope=row["scope"], principal=who,
                                     recommendation="Confirm this external user needs this access; prefer least privilege."))
        elif privileged and broad and row["principal_resolved"]:
            findings.append(_finding("MEDIUM", "Privileged access", f"{row['role']} at {row['scope_type']} '{row['scope_name']}' ({row['assignment_type']})",
                                     scope=row["scope"], principal=who,
                                     recommendation="Review regularly; consider PIM eligible assignments instead of permanent ones."))
        if resolved and not row["principal_resolved"]:
            findings.append(_finding("MEDIUM", "Orphaned assignment", f"{row['role']} assigned to a deleted or unknown principal {row['principal_id']}",
                                     scope=row["scope"],
                                     recommendation=f"Remove it after review: az role assignment delete --ids \"{row['scope']}/providers/Microsoft.Authorization/roleAssignments/{row['assignment_id']}\""))

    # Assignments on managed resource groups that are not in config.yaml
    if config:
        managed_rgs = {u.resource_group.name.lower() for u in config.users if u.resource_group}
        managed_rgs |= {a.resource_group.lower() for u in config.users for a in u.access
                        if a.scope_type == ScopeType.RESOURCE_GROUP and a.resource_group}
        configured_principals = {p.context.principal_id for p in plans if p.context.principal_id}
        for row in rows:
            if row["scope_type"] == ScopeType.RESOURCE_GROUP.label and row["scope_name"].lower() in managed_rgs \
                    and row["assignment_type"] == "Direct" and row["principal_id"] not in configured_principals:
                findings.append(_finding("LOW", "Unmanaged access",
                                         f"{row['role']} on '{row['scope_name']}' is not declared in config.yaml",
                                         scope=row["scope"], principal=row["principal_email"] or row["principal_name"],
                                         recommendation="Add it to config.yaml or remove it after review."))

    # Budgets
    try:
        resource_groups = ctx.resource_groups.list()
        budgets = collect_budgets(ctx, warnings, resource_groups)
        with_budget = {b.scope.lower() for b in budgets}
        for rg in resource_groups:
            if resource_group_scope(ctx.subscription_id, rg.name).id.lower() not in with_budget:
                findings.append(_finding("LOW", "Cost governance", f"Resource group '{rg.name}' has no budget",
                                         scope=rg.id, recommendation="Add a budget in config.yaml."))
        for budget in budgets:
            usage = budget.usage_percent
            if usage is not None and usage >= 100:
                findings.append(_finding("HIGH", "Budget", f"Budget '{budget.name}' exceeded ({usage:.1f}% used)", scope=budget.scope))
            elif usage is not None and usage >= 90:
                findings.append(_finding("MEDIUM", "Budget", f"Budget '{budget.name}' at {usage:.1f}%", scope=budget.scope))
    except OperationError as exc:
        warnings.append(f"Budget audit unavailable: {exc.reason}")

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["category"], f["finding"]))
    summary = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITY_ORDER}
    return {
        "report": "audit",
        "generated_at": _now(),
        "subscription": subscription.as_dict(),
        "summary": summary,
        "findings": findings,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- export


def _flatten(value: Any) -> Any:
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return value


def rows_to_csv(rows: list[dict]) -> str:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _flatten(v) for k, v in row.items()})
    return buffer.getvalue()


def report_to_csv(report: dict) -> str:
    """Single CSV with a leading 'record_type' column, so every section fits one file."""
    rows: list[dict] = []
    if report.get("report") == "audit":
        rows += [{"record_type": "finding", **f} for f in report["findings"]]
    else:
        rows.append({"record_type": "subscription", **report["subscription"]})
        rows.append({"record_type": "cost_summary", **{k: v for k, v in report["costs"].items() if k != "subscription_budgets"}})
        rows += [{"record_type": "resource_group", **r} for r in report["resource_groups"]]
        rows += [{"record_type": "budget", **b} for b in report.get("budgets", [])]
        rows += [{"record_type": "role_assignment", **a} for a in report["role_assignments"]]
        rows += [{"record_type": "resource", **r} for r in report["resources"]]
    rows += [{"record_type": "warning", "message": w} for w in report.get("warnings", [])]
    return rows_to_csv(rows)


def to_json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str, ensure_ascii=False)


def write_report_files(report: dict, directory: str | Path, basename: str) -> list[Path]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    json_path = directory / f"{basename}-{stamp}.json"
    csv_path = directory / f"{basename}-{stamp}.csv"
    json_path.write_text(to_json(report), encoding="utf-8")
    csv_path.write_text(report_to_csv(report), encoding="utf-8-sig", newline="")  # BOM so Excel detects UTF-8
    return [json_path, csv_path]
