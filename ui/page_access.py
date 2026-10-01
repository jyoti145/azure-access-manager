"""Access: who has which role where, with Direct vs Inherited."""

from __future__ import annotations

import streamlit as st

from common import cached, md, page_header, require_ctx
from src.reports import collect_assignments
from src.scopes import resource_group_scope

WHOLE = "Whole subscription"


def page() -> None:
    ctx = require_ctx()
    page_header("Access", "Role assignments. Inherited roles come from a higher level (subscription or management group).",
                refresh_key="refresh-access")

    rgs = cached("resource_groups", lambda: ctx.resource_groups.list(), "Loading resource groups…") or []
    target = st.selectbox("Show access effective on", [WHOLE] + [rg.name for rg in rgs], key="access-target")
    scope = None if target == WHOLE else resource_group_scope(ctx.subscription_id, target).id

    def load():
        warnings: list[str] = []
        rows, _ = collect_assignments(ctx, warnings, scope=scope)
        return rows, warnings

    data = cached(("access", target), load, "Loading role assignments…")
    if data is None:
        return
    rows, warnings = data
    for warning in dict.fromkeys(warnings):
        st.warning(md(warning))

    c1, c2, c3 = st.columns([2, 2, 1])
    search = c1.text_input("Search", placeholder="name, email or object ID", key="access-search").lower()
    roles = c2.multiselect("Roles", sorted({r["role"] for r in rows}), key="access-roles")
    kind = c3.segmented_control("Type", ["All", "Direct", "Inherited"], default="All", key="access-kind")

    filtered = [
        r for r in rows
        if (not search or search in (r["principal_name"] or "").lower() or search in (r["principal_email"] or "").lower()
            or search in r["principal_id"].lower())
        and (not roles or r["role"] in roles)
        and (kind in (None, "All") or r["assignment_type"] == kind)
    ]
    st.dataframe(
        [
            {
                "Name": r["principal_name"],
                "Email / UPN": r["principal_email"] or "",
                "Type": r["principal_type"] + (" (guest)" if r.get("user_type") == "Guest" else ""),
                "Role": r["role"],
                "Scope": r["scope_name"],
                "Scope type": r["scope_type"],
                "Assignment": r["assignment_type"],
                "Assignment ID": r["assignment_id"],
            }
            for r in filtered
        ],
        hide_index=True,
    )
    st.caption(f"{len(filtered)} of {len(rows)} assignment(s). This tool never removes access; use the Audit page "
               "to find assignments that need review.")
