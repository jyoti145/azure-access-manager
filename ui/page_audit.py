"""Audit: configuration drift, privileged access, orphaned assignments, missing budgets (read-only)."""

from __future__ import annotations

import streamlit as st

from common import SEVERITY_COLOR, badge, cached, load_config_for_ui, md, page_header, require_ctx, stamp
from src.errors import ConfigError
from src.reports import build_audit, report_to_csv, to_json
from src.setup_engine import SetupEngine


def page() -> None:
    ctx = require_ctx()
    page_header("Audit", "Read-only review of access and cost hygiene. Nothing is changed.",
                refresh_key="refresh-audit")

    try:
        config = load_config_for_ui()
    except ConfigError:
        config = None
        st.caption("The configuration has problems, so configuration drift is not checked.")

    def load():
        plans = SetupEngine(ctx, config).plan() if config and config.users else []
        return build_audit(ctx, config, plans)

    audit = cached("audit", load, "Running the audit…")
    if not audit:
        return

    summary = audit["summary"]
    cols = st.columns(4)
    for col, severity in zip(cols, ("HIGH", "MEDIUM", "LOW", "INFO")):
        col.metric(severity.title(), summary.get(severity, 0), border=True)

    if not audit["findings"]:
        st.success("No findings.")
    severities = st.pills("Show", ["HIGH", "MEDIUM", "LOW", "INFO"], default=["HIGH", "MEDIUM", "LOW", "INFO"],
                          selection_mode="multi", key="audit-severity")
    for finding in audit["findings"]:
        if finding["severity"] not in (severities or []):
            continue
        with st.container(border=True):
            st.markdown(f"{badge(finding['severity'], SEVERITY_COLOR[finding['severity']])} "
                        f"{badge(finding['category'], 'gray')} {md(finding['finding'])}")
            lines = []
            if finding.get("principal"):
                lines.append(f"Principal: {md(finding['principal'])}")
            if finding.get("recommendation"):
                lines.append(f"Recommendation: {md(finding['recommendation'])}")
            if lines:
                st.caption("  \n".join(lines))

    for warning in dict.fromkeys(audit.get("warnings", [])):
        st.warning(md(warning))

    c1, c2 = st.columns(2)
    c1.download_button("Download JSON", to_json(audit), f"audit-{stamp()}.json", mime="application/json",
                       icon=":material/download:")
    c2.download_button("Download CSV", report_to_csv(audit), f"audit-{stamp()}.csv", mime="text/csv",
                       icon=":material/download:")
