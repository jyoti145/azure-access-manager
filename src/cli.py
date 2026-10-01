"""Command-line interface for Azure Access Manager."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from pathlib import Path

from . import __version__
from .auth import AUTH_MODES, authenticate, build_credential
from .config import load_config, resolve_subscription_settings
from .console import Console, configure_stdio
from .context import AzureContext
from .errors import AzureManagerError, ConfigError, OperationError, UserAborted
from .logging_utils import get_logger, log_event, setup_logging
from .models import Stage
from .permissions import run_preflight
from .reports import (
    budget_row,
    build_audit,
    build_subscription_report,
    collect_assignments,
    collect_budgets,
    report_to_csv,
    rows_to_csv,
    to_json,
    write_report_files,
)
from .scopes import resource_group_scope
from .setup_engine import SetupEngine
from . import views

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_PREFLIGHT, EXIT_PARTIAL, EXIT_ABORTED = 0, 1, 2, 3, 4, 5


# --------------------------------------------------------------------------- helpers


def _config_path(args) -> Path:
    if args.config:
        return Path(args.config)
    for candidate in (Path.cwd() / "config.yaml", Path.cwd() / "config.json",
                      PROJECT_ROOT / "config.yaml", PROJECT_ROOT / "config.json"):
        if candidate.exists():
            return candidate
    return Path.cwd() / "config.yaml"


def _machine(args) -> bool:
    return getattr(args, "format", "table") in ("json", "csv")


def _connect(args, console: Console, *, config=None) -> AzureContext:
    if config is not None:
        subscription_id, tenant_id = config.subscription_id, config.tenant_id
    else:
        subscription_id, tenant_id = resolve_subscription_settings(
            _config_path(args), subscription_override=args.subscription, tenant_override=args.tenant
        )
    credential = build_credential(args.auth, tenant_id)
    identity = authenticate(credential)
    console.note(f"Signed in as {identity.label} ({identity.method}, tenant {identity.tenant_id})")
    return AzureContext(credential, subscription_id, identity)


def _load_config(args):
    return load_config(_config_path(args), subscription_override=args.subscription, tenant_override=args.tenant)


def _try_load_config(args):
    try:
        return _load_config(args)
    except ConfigError:
        return None


def _emit(args, data, rows: list[dict], render_table) -> None:
    if args.format == "json":
        print(to_json(data))
    elif args.format == "csv":
        sys.stdout.write(rows_to_csv(rows))
    else:
        render_table()


# --------------------------------------------------------------------------- commands


def cmd_validate(args, console: Console) -> int:
    config = _load_config(args)
    console.ok(f"Configuration is valid: {config.source_path}", force=True)
    console.print(f"Subscription: {config.subscription_id}")
    for user in config.users:
        console.print()
        console.print(f"{user.label}")
        if user.resource_group:
            rg = user.resource_group
            console.print(f"  Resource group: {rg.name} ({rg.location or 'location n/a'}, create_if_missing={rg.create_if_missing})")
        for access in user.access:
            target = access.resource_id or access.resource_group or access.management_group_id or config.subscription_id
            console.print(f"  Access: {access.role} @ {access.scope_type.label} {target}")
        if user.budget:
            b = user.budget
            alerts = ", ".join(f"{a.threshold:g}%{'' if a.threshold_type == 'Actual' else ' forecast'}" for a in b.alerts)
            console.print(f"  Budget: {b.name} {b.amount:g} {b.currency or ''} {b.period} - alerts: {alerts or 'none'}")
        console.print(f"  Invite if missing: {user.invite_if_missing}")
    return EXIT_OK


def cmd_setup(args, console: Console) -> int:
    config = _load_config(args)
    stages = {Stage(s) for s in args.only} if args.only else None
    console.banner("AZURE ACCESS MANAGER" + (" - DRY RUN" if args.dry_run else ""), force=True)
    ctx = _connect(args, console, config=config)
    subscription = ctx.subscriptions.get(config.subscription_id)
    log_event("setup", "started", scope=ctx.subscription_scope,
              details={"dry_run": args.dry_run, "users": len(config.users), "stages": sorted(s.value for s in (stages or set(Stage)))})

    engine = SetupEngine(ctx, config, stages=stages, user_filter=args.user)
    if not engine.selected_users():
        raise ConfigError(f"No users in {config.source_path} match --user {args.user!r}" if args.user else "No users defined in configuration")

    if Stage.BUDGETS in engine.stages and any(u.budget for u in engine.selected_users()):
        try:
            engine.billing_currency = ctx.costs.month_to_date().currency
        except OperationError:
            pass  # reported by the pre-flight check

    plans = engine.plan()
    results = run_preflight(ctx, ctx.identity, subscription, engine.requirements(plans), tenant_id=config.tenant_id)
    preflight_ok = views.render_preflight(console, results)
    views.render_plan(console, subscription, plans, dry_run=args.dry_run)

    changes = [a for p in plans if not p.blocked for a in p.changes]
    if args.dry_run:
        if not preflight_ok:
            console.fail("Pre-flight check failed: a real run would stop before making changes.")
        console.print(f"{len(changes)} change(s) would be made.", force=True)
        console.print("No changes were made.", force=True)
        log_event("setup", "dry_run", details={"changes": len(changes)})
        return EXIT_OK if preflight_ok else EXIT_PREFLIGHT

    if not preflight_ok:
        console.print()
        console.fail("Setup cannot safely continue. No changes were made.")
        log_event("setup", "preflight_failed", level=logging.WARNING)
        return EXIT_PREFLIGHT

    if changes:
        for plan in plans:
            if plan.blocked:
                continue
            for action in plan.changes:
                if action.high_privilege:
                    views.render_high_privilege_warning(console, plan, action)
                    if not args.yes and not console.confirm("Continue?"):
                        raise UserAborted("Aborted: high-privilege assignment not confirmed. No changes were made.")
                    log_event("confirm_high_privilege", "confirmed" if not args.yes else "auto_confirmed",
                              user=plan.user.email, scope=action.scope, resource=action.title)
        if not args.yes and not console.confirm(f"Apply {len(changes)} change(s) to subscription '{subscription.display_name}'?"):
            interactive = sys.stdin is not None and sys.stdin.isatty()
            raise UserAborted("Aborted. No changes were made." + ("" if interactive else
                              " (Non-interactive session: re-run with --yes for unattended execution.)"))
        console.print()
        console.print(console.style("APPLYING CHANGES", "bold"))
        engine.execute(plans, on_progress=lambda plan, action: views.render_progress(console, plan, action))

    mtd, cost_error = None, None
    try:
        mtd = ctx.costs.month_to_date()
    except OperationError as exc:
        cost_error = exc.reason
    status = views.render_summary(console, subscription, plans, engine=engine, mtd_cost=mtd,
                                  cost_error=cost_error, executed=bool(changes))
    log_event("setup", status.lower().replace(" ", "_"), scope=ctx.subscription_scope,
              details={"changes": len(changes), "failed": sum(1 for p in plans for a in p.changes if a.status == "failed")})

    if config.report.enabled and not args.no_report:
        try:
            report = build_subscription_report(
                ctx, include_resources=config.report.include_resources,
                include_costs=config.report.include_costs, include_rbac=config.report.include_rbac,
            )
            paths = write_report_files(report, args.reports_dir, "subscription-report")
            console.print(f"Subscription report: {', '.join(str(p) for p in paths)}")
        except AzureManagerError as exc:
            console.warn(f"Subscription report could not be generated: {exc}")

    return EXIT_OK if status in ("SUCCESS", "NO CHANGES REQUIRED") else EXIT_PARTIAL


def cmd_users(args, console: Console) -> int:
    config = _load_config(args)
    ctx = _connect(args, console, config=config)
    rows = []
    for user in config.users:
        row = {"name": user.name, "email": user.email, "status": "Not found", "user_type": None,
               "object_id": user.object_id, "account_enabled": None, "roles": None}
        try:
            found = ctx.users.get_by_id(user.object_id) if user.object_id else ctx.users.find_by_email(user.email)
            if found:
                assignments = ctx.rbac.assignments_for_principal(found.object_id, ctx.subscription_scope)
                row.update(status="Found", user_type=found.user_type, object_id=found.object_id,
                           account_enabled=found.account_enabled,
                           roles="; ".join(f"{ctx.rbac.role_name(a.role_definition_id)} @ {a.scope.rsplit('/', 1)[-1]}"
                                           for a in assignments) or "(none in this subscription)")
                if found.external_state == "PendingAcceptance":
                    row["status"] = "Found (invitation pending)"
            elif user.invite_if_missing:
                row["status"] = "Not found - will be invited by setup"
            else:
                row["status"] = "Not found - invitation required"
        except OperationError as exc:
            row["status"] = f"Error: {exc.reason}"
        rows.append(row)

    def table():
        console.section("Configured users")
        console.table(["Name", "Email", "Status", "Type", "Object ID", "Roles"],
                      [[r["name"], r["email"], r["status"], r["user_type"] or "-", r["object_id"] or "-", r["roles"] or "-"]
                       for r in rows], max_width=60)

    _emit(args, rows, rows, table)
    return EXIT_OK


def cmd_rbac(args, console: Console) -> int:
    ctx = _connect(args, console)
    warnings: list[str] = []
    scope = resource_group_scope(ctx.subscription_id, args.resource_group).id if args.resource_group else None
    rows, _ = collect_assignments(ctx, warnings, scope=scope)
    if args.user:
        needle = args.user.lower()
        rows = [r for r in rows if needle in (r["principal_email"] or "").lower()
                or needle in (r["principal_name"] or "").lower() or needle == r["principal_id"].lower()]
    if args.role:
        rows = [r for r in rows if r["role"].lower() == args.role.lower()]

    def table():
        console.section(f"Role assignments ({'Resource Group ' + args.resource_group if args.resource_group else 'subscription'})")
        views.render_assignment_rows(console, rows)
        views.render_warnings(console, warnings)

    _emit(args, {"role_assignments": rows, "warnings": warnings}, rows, table)
    return EXIT_OK


def cmd_budgets(args, console: Console) -> int:
    ctx = _connect(args, console)
    warnings: list[str] = []
    rgs = ctx.resource_groups.list()
    if args.resource_group:
        rgs = [rg for rg in rgs if rg.name.lower() == args.resource_group.lower()]
        if not rgs:
            raise OperationError("List budgets", f"Resource group '{args.resource_group}' not found.", code="ResourceGroupNotFound")
    budgets = collect_budgets(ctx, warnings, rgs)
    if args.resource_group:
        budgets = [b for b in budgets if b.scope.lower() != ctx.subscription_scope.lower()]
    rows = [budget_row(b) for b in budgets]

    def table():
        console.section("Budgets")
        views.render_budget_rows(console, rows)
        views.render_warnings(console, warnings)

    _emit(args, {"budgets": rows, "warnings": warnings}, rows, table)
    return EXIT_OK


def cmd_budget_status(args, console: Console) -> int:
    config = _try_load_config(args)
    ctx = _connect(args, console, config=config if config and not args.subscription else None)
    targets: list = []
    if args.resource_group:
        scope = resource_group_scope(ctx.subscription_id, args.resource_group).id
        if ctx.resource_groups.get(args.resource_group) is None:
            raise OperationError("Budget status", f"Resource group '{args.resource_group}' not found.",
                                 scope=scope, code="ResourceGroupNotFound")
        budget = ctx.budgets.get(scope, args.budget_name) if args.budget_name else None
        targets = [budget] if budget else ([] if args.budget_name else ctx.budgets.list(scope))
    elif config and config.users:
        for user in config.users:
            b = user.budget
            if not b:
                continue
            scope = (resource_group_scope(ctx.subscription_id, b.resource_group).id if b.resource_group
                     else ctx.subscription_scope)
            if args.budget_name and b.name.lower() != args.budget_name.lower():
                continue
            found = ctx.budgets.get(scope, b.name)
            if found:
                targets.append(found)
            else:
                console.warn(f"Budget '{b.name}' for {user.email} does not exist yet (run setup).")
    else:
        targets = collect_budgets(ctx, [], None)
        if args.budget_name:
            targets = [t for t in targets if t.name.lower() == args.budget_name.lower()]

    targets = list({t.id.lower(): t for t in targets}.values())
    if not targets:
        console.fail("No matching budgets found.")
        return EXIT_ERROR

    if _machine(args):
        from .budgets import threshold_states

        rows = [dict(budget_row(t), remaining=t.remaining,
                     thresholds_exceeded=", ".join(f"{s.threshold:g}% {s.threshold_type}" for s in threshold_states(t) if s.crossed))
                for t in targets]
        _emit(args, {"budgets": rows}, rows, lambda: None)
        return EXIT_OK

    console.banner("BUDGET STATUS")
    for budget in targets:
        views.render_budget_status(console, budget, label=budget.name)
        console.rule("-")
    console.note("Budget spend is evaluated by Azure periodically; cost data lags usage by 8-24 hours.")
    return EXIT_OK


def cmd_subscription_report(args, console: Console) -> int:
    ctx = _connect(args, console)
    report = build_subscription_report(
        ctx, include_resources=not args.no_resources, include_costs=not args.no_costs, include_rbac=not args.no_rbac
    )
    if args.format == "json":
        print(to_json(report))
    elif args.format == "csv":
        sys.stdout.write(report_to_csv(report))
    else:
        views.render_subscription_report(console, report)
    if args.export:
        paths = write_report_files(report, args.reports_dir, "subscription-report")
        console.print()
        console.ok("Exported: " + ", ".join(str(p) for p in paths), force=True)
    log_event("subscription_report", "generated", scope=ctx.subscription_scope)
    return EXIT_OK


def cmd_resource_groups(args, console: Console) -> int:
    ctx = _connect(args, console)
    warnings: list[str] = []
    rgs = ctx.resource_groups.list()
    counts: dict[str, int] = {}
    try:
        for resource in ctx.resources.list():
            counts[resource.resource_group.lower()] = counts.get(resource.resource_group.lower(), 0) + 1
    except OperationError as exc:
        warnings.append(f"Resource counts unavailable: {exc.reason}")
    mtd = None
    try:
        mtd = ctx.costs.month_to_date()
    except OperationError as exc:
        warnings.append(f"Cost data unavailable: {exc.reason}")
    rows = [{
        "name": rg.name, "location": rg.location, "resource_count": counts.get(rg.name.lower(), 0),
        "provisioning_state": rg.provisioning_state,
        "current_month_cost": mtd.for_resource_group(rg.name) if mtd else None,
        "currency": mtd.currency if mtd else None, "tags": rg.tags,
    } for rg in rgs]

    def table():
        console.section(f"Resource Groups ({len(rows)})")
        console.table(["Resource Group", "Location", "Resources", "State", "MTD Cost", "Tags"],
                      [[r["name"], r["location"], r["resource_count"], r["provisioning_state"],
                        console.money(r["current_month_cost"], r["currency"]),
                        ", ".join(f"{k}={v}" for k, v in r["tags"].items()) or "-"] for r in rows])
        views.render_warnings(console, warnings)

    _emit(args, {"resource_groups": rows, "warnings": warnings}, rows, table)
    return EXIT_OK


def cmd_resources(args, console: Console) -> int:
    ctx = _connect(args, console)
    rows = [r.as_dict() for r in ctx.resources.list(args.resource_group)]

    def table():
        console.section(f"Resources ({len(rows)})")
        views.render_resource_rows(console, rows)

    _emit(args, {"resources": rows}, rows, table)
    return EXIT_OK


def cmd_audit(args, console: Console) -> int:
    config = _try_load_config(args)
    if config is None:
        console.note("No valid config file found - auditing without configuration drift checks.")
    ctx = _connect(args, console, config=config if config and not args.subscription else None)
    plans = SetupEngine(ctx, config).plan() if config else []
    audit = build_audit(ctx, config, plans)
    if args.format == "json":
        print(to_json(audit))
    elif args.format == "csv":
        sys.stdout.write(report_to_csv(audit))
    else:
        views.render_audit(console, audit)
    if args.export:
        paths = write_report_files(audit, args.reports_dir, "audit")
        console.ok("Exported: " + ", ".join(str(p) for p in paths), force=True)
    log_event("audit", "completed", scope=ctx.subscription_scope, details=audit["summary"])
    return EXIT_OK


def cmd_ui(args, console: Console) -> int:
    import os
    import subprocess

    try:
        import streamlit  # noqa: F401
    except ImportError:
        raise AzureManagerError("The UI needs Streamlit. Install it with: pip install -r requirements.txt") from None
    env = dict(os.environ)
    if args.config:
        env["AZURE_MANAGER_CONFIG"] = str(Path(args.config).resolve())
    command = [
        sys.executable, "-m", "streamlit", "run", str(PROJECT_ROOT / "ui" / "app.py"),
        "--server.address", "localhost",  # never expose: the UI acts with this computer's Azure login
        "--server.port", str(args.port),
        "--browser.gatherUsageStats", "false",
        # Headless skips Streamlit's first-run "enter your email" prompt; the browser is opened below instead.
        "--server.headless", "true",
    ]
    url = f"http://localhost:{args.port}"
    console.print(f"Starting Azure Access Manager UI at {url}  (press Ctrl+C to stop)", force=True)
    log_event("ui", "started", details={"port": args.port})
    process = subprocess.Popen(command, env=env, cwd=str(PROJECT_ROOT))
    try:
        if not args.no_browser and _wait_for_ui(args.port, process):
            import webbrowser

            webbrowser.open(url)
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return EXIT_OK


def _wait_for_ui(port: int, process, timeout: float = 60) -> bool:
    """Wait until the UI server answers its health check (or the process exits)."""
    import time
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline and process.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- parser


def _stage_list(value: str) -> list[str]:
    stages = [s.strip().lower() for s in value.split(",") if s.strip()]
    valid = {s.value for s in Stage}
    bad = [s for s in stages if s not in valid]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown stage(s): {', '.join(bad)} (valid: {', '.join(sorted(valid))})")
    return stages


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("common options")
    group.add_argument("-c", "--config", help="Path to config.yaml / config.json (default: ./config.yaml)")
    group.add_argument("-s", "--subscription", help="Subscription ID (overrides config / AZURE_SUBSCRIPTION_ID)")
    group.add_argument("--tenant", help="Tenant ID to authenticate against (overrides config tenant_id)")
    group.add_argument("--auth", choices=AUTH_MODES, default="auto",
                       help="auto: service principal env vars if set, else Azure CLI (default)")
    verbosity = group.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="Show debug logging")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="Only show errors and final results")
    group.add_argument("--log-dir", default=os.environ.get("AZURE_MANAGER_LOG_DIR") or str(PROJECT_ROOT / "logs"),
                       help="Directory for log files (or set AZURE_MANAGER_LOG_DIR)")

    formats = argparse.ArgumentParser(add_help=False)
    formats.add_argument("-f", "--format", choices=("table", "json", "csv"), default="table", help="Output format")

    exporting = argparse.ArgumentParser(add_help=False)
    exporting.add_argument("--export", action="store_true", help="Also write JSON + CSV files to the reports directory")
    exporting.add_argument("--reports-dir", default=str(PROJECT_ROOT / "reports"), help="Directory for exported reports")

    parser = argparse.ArgumentParser(
        prog="azure_manager.py",
        description="Automate Azure RBAC access, resource groups, budgets and subscription reporting.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = sub.add_parser("setup", parents=[common], help="Apply config.yaml: users, resource groups, RBAC, budgets")
    p.add_argument("--dry-run", action="store_true", help="Show what would change without changing anything")
    p.add_argument("-y", "--yes", action="store_true", help="Do not prompt for confirmation (validation still runs)")
    p.add_argument("--only", type=_stage_list, metavar="STAGES",
                   help="Comma-separated subset of stages: users,resource-groups,rbac,budgets")
    p.add_argument("--user", help="Only process the configured user with this email / object ID")
    p.add_argument("--no-report", action="store_true", help="Skip writing the subscription report after setup")
    p.add_argument("--reports-dir", default=str(PROJECT_ROOT / "reports"), help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("validate", parents=[common], help="Validate the configuration file offline (no Azure calls)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("users", parents=[common, formats], help="Check configured users in the directory")
    p.set_defaults(func=cmd_users)

    p = sub.add_parser("rbac", parents=[common, formats], help="List role assignments (direct and inherited)")
    p.add_argument("-g", "--resource-group", help="Show assignments effective on this resource group")
    p.add_argument("--user", help="Filter by email, name or object ID")
    p.add_argument("--role", help="Filter by role name")
    p.set_defaults(func=cmd_rbac)

    p = sub.add_parser("budgets", parents=[common, formats], help="List budgets in the subscription")
    p.add_argument("-g", "--resource-group", help="Only budgets of this resource group")
    p.set_defaults(func=cmd_budgets)

    p = sub.add_parser("budget-status", parents=[common, formats], help="Current spend vs. budget and thresholds")
    p.add_argument("-g", "--resource-group", help="Resource group whose budget(s) to show")
    p.add_argument("-b", "--budget-name", help="Budget name")
    p.set_defaults(func=cmd_budget_status)

    p = sub.add_parser("subscription-report", parents=[common, formats, exporting], help="Detailed subscription view")
    p.add_argument("--no-resources", action="store_true", help="Skip the resource inventory")
    p.add_argument("--no-costs", action="store_true", help="Skip cost and budget data")
    p.add_argument("--no-rbac", action="store_true", help="Skip role assignments")
    p.set_defaults(func=cmd_subscription_report)

    p = sub.add_parser("resource-groups", parents=[common, formats], help="List resource groups")
    p.set_defaults(func=cmd_resource_groups)

    p = sub.add_parser("resources", parents=[common, formats], help="List resources grouped by resource group")
    p.add_argument("-g", "--resource-group", help="Only this resource group")
    p.set_defaults(func=cmd_resources)

    p = sub.add_parser("audit", parents=[common, formats, exporting], help="Drift + security + cost hygiene audit (read-only)")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("ui", parents=[common], help="Open the web UI in your browser (runs locally)")
    p.add_argument("--port", type=int, default=8501, help="Local port (default 8501)")
    p.add_argument("--no-browser", action="store_true", help="Do not open a browser window automatically")
    p.set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(quiet=args.quiet, stream=sys.stderr if _machine(args) else sys.stdout)
    log_path = setup_logging(args.log_dir, verbose=args.verbose, quiet=args.quiet)
    log_event("command", "started", message=f"azure_manager {args.command}", details={"command": args.command})
    try:
        code = args.func(args, console)
    except ConfigError as exc:
        console.fail("Configuration is invalid:")
        for problem in exc.problems:
            console.print(f"  - {problem}", force=True)
        log_event("command", "config_error", error="; ".join(exc.problems), level=logging.ERROR)
        code = EXIT_CONFIG
    except OperationError as exc:
        console.error_block(operation=exc.operation, reason=exc.reason, user=exc.user, scope=exc.scope,
                            required_permission=exc.required_permission, hint=exc.hint)
        log_event(exc.operation, "failed", user=exc.user, scope=exc.scope, error=exc.reason, level=logging.ERROR)
        code = EXIT_ERROR
    except UserAborted as exc:
        console.print(str(exc), force=True)
        log_event("command", "aborted", error=str(exc), level=logging.WARNING)
        code = EXIT_ABORTED
    except AzureManagerError as exc:
        console.fail(str(exc))
        log_event("command", "failed", error=str(exc), level=logging.ERROR)
        code = exc.exit_code
    except KeyboardInterrupt:
        console.print("\nInterrupted.", force=True)
        code = 130
    except Exception as exc:  # last resort: readable message, details only in the log file
        get_logger().error("Unexpected error", exc_info=True, extra={"operation": args.command, "result": "crashed",
                                                                     "error": exc.__class__.__name__})
        console.error_block(operation=args.command, reason=f"Unexpected error: {exc.__class__.__name__}: {exc}",
                            hint=f"Details were written to {log_path}" if log_path else None)
        if args.verbose:
            traceback.print_exc()
        code = EXIT_ERROR
    log_event("command", "finished", message=f"azure_manager {args.command} exit={code}", details={"exit_code": code})
    return code
