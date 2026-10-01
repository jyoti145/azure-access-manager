"""Resources: resource groups and everything in them."""

from __future__ import annotations

import streamlit as st

from common import cached, page_header, require_ctx

ALL = "All resource groups"


def page() -> None:
    ctx = require_ctx()
    page_header("Resources", "Resource groups and resources in this subscription.", refresh_key="refresh-resources")

    rgs = cached("resource_groups", lambda: ctx.resource_groups.list(), "Loading resource groups…")
    resources = cached("resources", lambda: ctx.resources.list(), "Loading resources…")
    if rgs is None or resources is None:
        return

    counts: dict[str, int] = {}
    for resource in resources:
        counts[resource.resource_group.lower()] = counts.get(resource.resource_group.lower(), 0) + 1

    st.subheader(f"Resource groups ({len(rgs)})")
    st.dataframe(
        [
            {
                "Resource group": rg.name,
                "Location": rg.location,
                "Resources": counts.get(rg.name.lower(), 0),
                "State": rg.provisioning_state or "",
                "Tags": ", ".join(f"{k}={v}" for k, v in rg.tags.items()),
            }
            for rg in rgs
        ],
        hide_index=True,
    )

    st.subheader(f"Resources ({len(resources)})")
    c1, c2 = st.columns([1, 2])
    group = c1.selectbox("Resource group", [ALL] + [rg.name for rg in rgs], key="resources-group")
    search = c2.text_input("Search", placeholder="name or type", key="resources-search").lower()
    rows = [
        {
            "Name": r.name,
            "Type": r.type,
            "Resource group": r.resource_group,
            "Region": r.location,
            "Status": r.status,
        }
        for r in resources
        if (group == ALL or r.resource_group.lower() == group.lower())
        and (not search or search in r.name.lower() or search in r.type.lower())
    ]
    if rows:
        st.dataframe(rows, hide_index=True)
    else:
        st.info("No resources match.")
    st.caption("Status is the VM power state where available, otherwise the resource's own state or provisioning state.")
