"""Budgets: spend against every budget, with threshold status."""

from __future__ import annotations

import streamlit as st

from common import HEALTH_COLOR, PAGES, badge, cached, md, money, page_header, require_ctx, ss
from src.budgets import budget_health, threshold_states
from src.reports import collect_budgets


def page() -> None:
    ctx = require_ctx()
    page_header("Budgets", "Spend against each budget. Budgets send email alerts; they do not stop spending.",
                refresh_key="refresh-budgets")

    def load():
        warnings: list[str] = []
        return collect_budgets(ctx, warnings), warnings

    data = cached("budgets", load, "Loading budgets…")
    if data is None:
        return
    budgets, warnings = data
    for warning in dict.fromkeys(warnings):
        st.warning(md(warning))
    if not budgets:
        st.info("No budgets in this subscription yet. Add a budget to a user on **Users & access**, then apply it.")
        st.page_link(PAGES["users"], label="Users & access", icon=":material/group:")
        return

    filter_text = st.text_input("Filter", placeholder="budget or resource group name", key="budget-filter").lower()
    for budget in budgets:
        scope_name = budget.scope.rsplit("/", 1)[-1] if budget.scope else ""
        if filter_text and filter_text not in budget.name.lower() and filter_text not in scope_name.lower():
            continue
        _card(budget, scope_name)
    st.caption("Azure evaluates budgets periodically, and cost data lags usage by 8-24 hours.")


def _card(budget, scope_name: str) -> None:
    usage = budget.usage_percent
    health = budget_health(usage)
    scope_label = "Subscription" if "/resourcegroups/" not in budget.scope.lower() else "Resource group"
    with st.container(border=True):
        st.markdown(f"**{md(budget.name)}** {badge(health, HEALTH_COLOR[health])}  \n"
                    f"{md(scope_label)}: `{scope_name}` · {md(budget.time_grain)} · since {md(budget.start_date or 'N/A')}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Budget", money(budget.amount, budget.currency))
        c2.metric("Spent", money(budget.current_spend, budget.currency))
        c3.metric("Remaining", money(budget.remaining, budget.currency))
        forecast = money(budget.forecast_spend, budget.currency) if budget.forecast_spend is not None else "N/A"
        c4.metric("Forecast", forecast)
        if usage is not None:
            st.progress(min(usage / 100, 1.0), text=f"{usage:.1f}% used")
        states = threshold_states(budget)
        if states:
            row = st.container(horizontal=True)
            for state in states:
                kind = "" if state.threshold_type == "Actual" else " forecast"
                with row:
                    st.badge(f"{state.threshold:g}%{kind}" + (" reached" if state.crossed else ""),
                             color="red" if state.crossed else "gray")
            for state in states:
                if state.crossed:
                    kind = "" if state.threshold_type == "Actual" else " forecast"
                    st.warning(f"{state.threshold:g}%{kind} budget threshold exceeded.")
        else:
            st.caption("No alerts configured.")
