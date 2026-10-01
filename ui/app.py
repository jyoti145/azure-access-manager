"""Azure Access Manager - web UI.

Start with:  python azure_manager.py ui
Runs on http://localhost only. Every person runs it on their own computer and signs in with
their own Azure account, so it can only do what that account is allowed to do.
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Azure Access Manager", page_icon=":material/admin_panel_settings:", layout="wide")

import common  # noqa: E402  (adds the project root to sys.path)
import page_access  # noqa: E402
import page_account  # noqa: E402
import page_audit  # noqa: E402
import page_budgets  # noqa: E402
import page_dashboard  # noqa: E402
import page_resources  # noqa: E402
import page_setup  # noqa: E402
import page_users  # noqa: E402
from common import ss  # noqa: E402

common.init_logging()
common.auto_connect()
connected = ss.get("ctx") is not None

pages = {
    "account": st.Page(page_account.page, title="Account", icon=":material/account_circle:", url_path="account",
                       default=not connected),
    "dashboard": st.Page(page_dashboard.page, title="Dashboard", icon=":material/dashboard:", url_path="dashboard",
                         default=connected),
    "users": st.Page(page_users.page, title="Users & access", icon=":material/group:", url_path="users"),
    "setup": st.Page(page_setup.page, title="Apply changes", icon=":material/play_circle:", url_path="apply"),
    "budgets": st.Page(page_budgets.page, title="Budgets", icon=":material/savings:", url_path="budgets"),
    "access": st.Page(page_access.page, title="Access", icon=":material/badge:", url_path="access"),
    "resources": st.Page(page_resources.page, title="Resources", icon=":material/deployed_code:", url_path="resources"),
    "audit": st.Page(page_audit.page, title="Audit", icon=":material/policy:", url_path="audit"),
}
common.PAGES.update(pages)

navigation = st.navigation({
    "Overview": [pages["dashboard"]],
    "Manage": [pages["users"], pages["setup"]],
    "Monitor": [pages["budgets"], pages["access"], pages["resources"], pages["audit"]],
    "Settings": [pages["account"]],
})

with st.sidebar:
    st.markdown("### :material/admin_panel_settings: Azure Access Manager")
    if connected:
        st.caption("Signed in as")
        st.markdown(f"**{common.md(ss.identity.label)}**")
        st.caption("Subscription")
        st.markdown(f"**{common.md(ss.subscription.name)}**  \n`{ss.subscription.subscription_id}`")
        st.page_link(pages["account"], label="Change account or subscription", icon=":material/swap_horiz:")
    else:
        st.warning("Not connected")
        st.page_link(pages["account"], label="Sign in", icon=":material/login:")
    st.divider()
    st.caption(":material/lock: Runs only on this computer, with your own Azure permissions.")

navigation.run()
