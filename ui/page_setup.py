"""Apply changes: preview (dry run) -> pre-flight -> confirm -> apply."""

from __future__ import annotations

import dataclasses
import time

import streamlit as st

from common import (
    KIND_BADGE, PAGES, badge, clear_cache, config_fingerprint, load_config_for_ui, md, run, ss, require_ctx,
)
from src.errors import ConfigError, OperationError
from src.logging_utils import log_event
from src.models import Stage
from src.permissions import run_preflight
from src.setup_engine import SetupEngine

STAGE_LABELS = {
    Stage.USERS.value: "Users & invitations",
    Stage.RESOURCE_GROUPS.value: "Resource groups",
    Stage.RBAC.value: "Role assignments",
    Stage.BUDGETS.value: "Budgets",
}


def page() -> None:
    ctx = require_ctx()
    st.title("Apply changes")
    st.caption("Step 1: preview what would change (nothing is changed). Step 2: review and apply.")

    try:
        config = load_config_for_ui()
    except ConfigError as exc:
        st.error("**Fix the configuration first**\n\n" + "\n".join(f"- {md(p)}" for p in exc.problems))
        st.page_link(PAGES["users"], label="Go to Users & access", icon=":material/group:")
        return
    if not config.users:
        st.info("No users in the configuration yet.")
        st.page_link(PAGES["users"], label="Add a user", icon=":material/person_add:")
        return

    keys = [u.email or u.object_id for u in config.users]
    with st.container(border=True):
        c1, c2 = st.columns(2)
        selected = c1.multiselect("Users", keys, default=keys, key="setup-users")
        stages = c2.pills("Steps", list(STAGE_LABELS), default=list(STAGE_LABELS), selection_mode="multi",
                          format_func=STAGE_LABELS.get, key="setup-stages")
        if st.button("Preview changes", type="primary", icon=":material/preview:", disabled=not (selected and stages)):
            ss.pop("setup_result", None)
            chosen = dataclasses.replace(config, users=[u for u in config.users if (u.email or u.object_id) in selected])
            plan = run(lambda: _build_plan(ctx, chosen, stages), "Checking the current state and your permissions…")
            if plan:
                ss.setup_plan = plan

    plan = ss.get("setup_plan")
    if plan and plan["fingerprint"] != config_fingerprint():
        st.warning("The configuration or subscription changed since the preview. Preview again.")
        ss.pop("setup_plan", None)
        plan = None

    if plan:
        _render_preflight(plan["results"])
        _render_plan(plan["plans"])
        _render_apply(plan)

    if ss.get("setup_result"):
        _render_result(ss.setup_result)


def _build_plan(ctx, config, stages: list[str]) -> dict:
    engine = SetupEngine(ctx, config, stages={Stage(s) for s in stages})
    if Stage.BUDGETS in engine.stages and any(u.budget for u in config.users):
        try:
            engine.billing_currency = ctx.costs.month_to_date().currency
        except OperationError:
            pass  # reported by the pre-flight check
    plans = engine.plan()
    subscription = ctx.subscriptions.get(ctx.subscription_id)
    results = run_preflight(ctx, ctx.identity, subscription, engine.requirements(plans), tenant_id=config.tenant_id)
    return {
        "engine": engine,
        "plans": plans,
        "results": results,
        "fingerprint": config_fingerprint(),
        "created": time.time(),
    }


def _render_preflight(results) -> None:
    st.subheader("Pre-flight check")
    with st.container(border=True):
        for result in results:
            icon = {"ok": ":green[:material/check_circle:]", "warn": ":orange[:material/warning:]",
                    "fail": ":red[:material/cancel:]"}[result.status]
            detail = f" — {md(result.detail)}" if result.detail else ""
            st.markdown(f"{icon} **{md(result.name)}**{detail}")


def _render_plan(plans) -> None:
    st.subheader("Plan")
    for plan in plans:
        user = plan.user
        if plan.blocked:
            state = badge("Blocked", "red")
        elif plan.changes:
            state = badge(f"{len(plan.changes)} change(s)", "blue")
        else:
            state = badge("Up to date", "green")
        with st.container(border=True):
            st.markdown(f"**{md(user.name)}** · {md(user.email or user.object_id)} {state}")
            for action in plan.actions:
                label, color = KIND_BADGE[action.kind]
                extra = " " + badge("needs confirmation", "orange") if action.high_privilege and not plan.blocked else ""
                st.markdown(f"{badge(label, color)} **{md(action.title)}**{extra}")
                if action.details:
                    st.caption("  \n".join(md(d) for d in action.details))
                for warning in action.warnings:
                    st.warning(md(warning))
            if plan.blocked and plan.changes:
                st.error("This user has blocking problems, so none of their changes will be applied.")


def _render_apply(plan: dict) -> None:
    plans, results = plan["plans"], plan["results"]
    changes = [a for p in plans if not p.blocked for a in p.changes]
    blocked = [p for p in plans if p.blocked]
    st.subheader("Apply")
    if not all(r.ok for r in results):
        st.error("The pre-flight check failed. Fix the problems above, then preview again. Nothing was changed.")
        return
    if not changes:
        if blocked:
            st.warning(f"Nothing can be applied: {len(blocked)} user(s) have blocking problems.")
        else:
            st.success("Everything is already set up. No changes required.")
        return

    created = plan["created"]
    confirmations = []
    high = [(p, a) for p in plans if not p.blocked for a in p.changes if a.high_privilege]
    for index, (p, action) in enumerate(high):
        with st.container(border=True):
            scope = next((d for d in action.details if d.startswith("Scope:")), "")
            st.markdown(f":orange[:material/warning:] **High-privilege assignment:** {md(action.title)} "
                        f"for **{md(p.user.email)}** · {md(scope)}")
            for warning in action.warnings:
                st.caption(md(warning))
            confirmations.append(st.checkbox(f"I confirm granting {action.title} to {p.user.email}",
                                             key=f"hp-{created}-{index}"))
    if blocked:
        st.info(f"{len(blocked)} user(s) have blocking problems and will be skipped.")
    reviewed = st.checkbox(f"I have reviewed the {len(changes)} change(s) above", key=f"reviewed-{created}")
    ready = reviewed and all(confirmations)
    if st.button(f"Apply {len(changes)} change(s)", type="primary", icon=":material/rocket_launch:", disabled=not ready):
        _apply(plan)


def _apply(plan: dict) -> None:
    engine, plans = plan["engine"], plan["plans"]
    log_event("setup", "started", details={"source": "ui", "users": len(plans)})
    with st.status("Applying changes…", expanded=True) as status:
        def progress(user_plan, action):
            if action.status == "done":
                status.write(f":green[:material/check:] {md(action.kind.value)} {md(action.title)}: {md(action.result)}")
            elif action.status == "failed":
                status.write(f":red[:material/close:] {md(action.kind.value)} {md(action.title)}: "
                             f"{md(getattr(action.error, 'reason', action.error))}")
            else:
                status.write(f"– {md(action.kind.value)} {md(action.title)}: {md(action.result)}")

        engine.execute(plans, on_progress=progress)
        failed = any(a.status == "failed" for p in plans for a in p.changes)
        status.update(label="Finished with errors" if failed else "All changes applied",
                      state="error" if failed else "complete")
    ss.setup_result = _summarize(plans)
    log_event("setup", ss.setup_result["status"].lower(), details={"source": "ui"})
    ss.pop("setup_plan", None)  # a plan is applied once; preview again for the next run
    clear_cache()
    st.rerun()


def _summarize(plans) -> dict:
    rows, errors = [], []
    for p in plans:
        for a in p.changes:
            rows.append({"User": p.user.email, "Action": a.kind.value, "Item": a.title,
                         "Result": a.status, "Details": a.result or ""})
            if a.status == "failed" and a.error is not None:
                errors.append(a.error)
    statuses = [r["Result"] for r in rows]
    if not rows:
        status = "NO CHANGES"
    elif all(s == "done" for s in statuses):
        status = "SUCCESS"
    elif any(s == "done" for s in statuses):
        status = "PARTIAL"
    else:
        status = "FAILED"
    return {"status": status, "rows": rows, "errors": errors}


def _render_result(result: dict) -> None:
    st.subheader("Result")
    color = {"SUCCESS": "green", "NO CHANGES": "green", "PARTIAL": "orange", "FAILED": "red"}[result["status"]]
    st.markdown(f"Status: {badge(result['status'], color)}")
    st.dataframe(result["rows"], hide_index=True)
    for error in result["errors"]:
        from common import show_error

        show_error(error)
    if result["status"] in ("SUCCESS", "PARTIAL"):
        st.caption("Run the preview again to confirm: everything should show as up to date.")
