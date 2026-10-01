"""Dashboard: subscription overview, cost, resource groups, budgets, export."""

from __future__ import annotations

import streamlit as st

from common import PAGES, ROOT, badge, cached, HEALTH_COLOR, md, money, page_header, require_ctx, run, stamp, ss
from src.reports import build_subscription_report, report_to_csv, to_json, write_report_files


def page() -> None:
    ctx = require_ctx()
    page_header("Dashboard", f"Overview of {ss.subscription.name}", refresh_key="refresh-dashboard")

    report = cached("report", lambda: build_subscription_report(ctx),
                    "Loading subscription overview… (cost data can take up to a minute)")
    if not report:
        return
    sub, costs = report["subscription"], report["costs"]
    currency = costs.get("currency")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Resource groups", len(report["resource_groups"]), border=True)
    c2.metric("Resources", len(report["resources"]), border=True)
    c3.metric("Role assignments", len(report["role_assignments"]), border=True)
    c4.metric("Cost this month", money(costs["current_month"], currency) if costs["available"] else "N/A",
              border=True, help="Month to date. Cost data lags actual usage by 8-24 hours.")
    c5.metric("Monthly budget used",
              f"{costs['budget_utilization_pct']:.1f}%" if costs.get("budget_utilization_pct") is not None else "-",
              border=True, help=costs.get("monthly_budget_basis") or "No monthly budgets")

    with st.container(border=True):
        st.markdown("**Subscription**")
        a, b, c = st.columns(3)
        a.markdown(f"**Name**  \n{md(sub['display_name'])}  \n\n**State**  \n{badge(sub['state'], 'green' if sub['state'] == 'Enabled' else 'orange')}")
        b.markdown(f"**Subscription ID**  \n`{sub['subscription_id']}`  \n\n**Tenant ID**  \n`{sub['tenant_id']}`")
        tags = ", ".join(f"{k}={v}" for k, v in sub["tags"].items()) or "none"
        c.markdown(f"**Offer**  \n{md(sub['offer'] or 'N/A')}  \n\n**Spending limit**  \n{md(sub['spending_limit'] or 'N/A')}  \n\n**Tags**  \n{md(tags)}")
        if costs["available"] and costs.get("previous_month") is not None:
            st.caption(f"Previous month: {money(costs['previous_month'], currency)}. {costs['note']}")

    st.subheader("Resource groups")
    rows = [
        {
            "Resource group": rg["name"],
            "Location": rg["location"],
            "Resources": rg["resource_count"],
            "Cost this month": money(rg["current_month_cost"], currency) if rg["current_month_cost"] is not None else "-",
            "Budget": f"{money(rg['budget_amount'], currency)} {rg['budget_period'] or ''}".strip() if rg["budget_amount"] else "-",
            "Budget used %": rg["budget_utilization_pct"],
            "Status": rg["budget_status"] or "-",
        }
        for rg in report["resource_groups"]
    ]
    st.dataframe(
        rows, hide_index=True,
        column_config={"Budget used %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%")},
    )

    budgets = report.get("budgets") or []
    if budgets:
        st.subheader("Budgets")
        for b in budgets:
            used = f"{b['utilization_pct']:.1f}%" if b["utilization_pct"] is not None else "N/A"
            st.markdown(
                f"{badge(b['status'], HEALTH_COLOR.get(b['status'], 'gray'))} **{md(b['name'])}** "
                f"({md(b['scope_type'])}: {md(b['scope_name'])}) — {md(money(b['current_spend'], b['currency']))} "
                f"of {md(money(b['amount'], b['currency']))} {md(b['period'])} · {used}"
            )
        st.page_link(PAGES["budgets"], label="Budget details", icon=":material/savings:")

    for warning in dict.fromkeys(report.get("warnings", [])):
        st.warning(md(warning))

    st.subheader("Export")
    e1, e2, e3 = st.columns(3)
    e1.download_button("Download JSON", to_json(report), f"subscription-report-{stamp()}.json",
                       mime="application/json", icon=":material/download:")
    e2.download_button("Download CSV", report_to_csv(report), f"subscription-report-{stamp()}.csv",
                       mime="text/csv", icon=":material/download:")
    if e3.button("Save to reports folder", icon=":material/save:"):
        paths = run(lambda: write_report_files(report, ROOT / "reports", "subscription-report"))
        if paths:
            st.success("Saved: " + ", ".join(f"`{p}`" for p in paths))
