"""Account: sign in with your own Azure identity and choose a subscription."""

from __future__ import annotations

import re
import shutil

import streamlit as st

from common import (
    az_login, connect, default_subscription_id, disconnect, load_subscriptions, md, read_raw, run, save_raw, ss,
)
from src.auth import sp_env_present

METHODS = {
    "cli": "Azure CLI (az login)",
    "browser": "Browser sign-in",
    "sp": "Service principal (environment variables)",
}
_TENANT_RE = re.compile(r"^[A-Za-z0-9.-]{3,253}$")


def page() -> None:
    st.title("Account")
    st.caption(
        "Sign in with **your own** Azure account. The tool runs on this computer and uses only the "
        "permissions of the account you sign in with."
    )

    _current_connection()
    _sign_in()
    _subscription_picker()


def _current_connection() -> None:
    ctx = ss.get("ctx")
    if ctx is None:
        return
    sub = ss.subscription
    with st.container(border=True):
        st.markdown(f"**Connected** {':green-badge[Active]'}")
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Signed in as**  \n{md(ss.identity.label)}")
        c2.markdown(f"**Subscription**  \n{md(sub.name)}  \n`{sub.subscription_id}`")
        c3.markdown(f"**Tenant**  \n{md(sub.tenant_name)}  \n`{sub.tenant_id}`")
        if st.button("Sign out of this session", icon=":material/logout:"):
            disconnect()
            st.rerun()


def _sign_in() -> None:
    st.subheader("1. Sign in")
    options = ["cli", "browser"] + (["sp"] if sp_env_present() else [])
    current = ss.get("auth_mode", "sp" if sp_env_present() else "cli")
    mode = st.radio(
        "Sign-in method", options, index=options.index(current) if current in options else 0,
        format_func=METHODS.get, horizontal=True,
    )
    ss.auth_mode = mode

    if mode == "cli":
        if not shutil.which("az"):
            st.warning(
                "The Azure CLI is not installed on this computer. Install it, then restart this app, "
                "or choose **Browser sign-in** above."
            )
            st.code("winget install Microsoft.AzureCLI", language="powershell")
            st.caption("Linux / macOS: https://learn.microsoft.com/cli/azure/install-azure-cli")
        else:
            st.caption(
                "Uses the account that is signed in to the Azure CLI on this computer. "
                "Use the button to sign in or switch account. A browser window opens for you to sign in."
            )
            with st.expander("Sign in / switch account with the Azure CLI", expanded=ss.get("ctx") is None):
                tenant = st.text_input(
                    "Tenant ID or domain (optional)",
                    help="Needed when your directory requires MFA, or to sign in to one specific tenant.",
                ).strip()
                if st.button("Sign in with Azure CLI", icon=":material/login:"):
                    if tenant and not _TENANT_RE.match(tenant):
                        st.error("That does not look like a tenant ID or domain.")
                    else:
                        with st.spinner("Finish signing in in the browser window that just opened…"):
                            ok, output = az_login(tenant or None)
                        if ok:
                            disconnect()
                            st.success("Signed in. Now load your subscriptions below.")
                        else:
                            st.error("Sign-in did not complete.\n\n" + md(output or "No details."))
    elif mode == "browser":
        st.caption("A Microsoft sign-in page opens in your browser. Works without the Azure CLI. "
                   "You will need to sign in again each time the app restarts.")
    else:
        st.caption("Uses AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET from the environment.")

    if st.button("Load my subscriptions", type="primary", icon=":material/cloud_sync:"):
        ss.pop("discovery", None)
        load_subscriptions(mode)


def _subscription_picker() -> None:
    discovery = ss.get("discovery")
    if not discovery:
        return
    choices, issues = discovery
    st.subheader("2. Choose a subscription")
    st.markdown(f"Signed in as **{md(ss.identity.label)}** via {md(ss.identity.method)}")

    for issue in issues:
        hint = f"  \nTo use it, sign in to that tenant: `{issue.hint}`" if issue.hint else ""
        st.warning(f"**{md(issue.tenant_name)}** (`{issue.tenant_id}`) could not be read: {md(issue.reason)}{hint}")

    if not choices:
        st.error(
            "No subscriptions were found for this account. If you just created one, wait a minute and "
            "reload. If your tenant requires MFA, sign in to it with the tenant ID above."
        )
        return

    current = ss.get("subscription")
    preferred = (current.subscription_id if current else None) or default_subscription_id()
    index = next((i for i, c in enumerate(choices) if c.subscription_id.lower() == (preferred or "").lower()), 0)
    choice = st.selectbox("Subscription", choices, index=index, format_func=lambda c: c.label)
    if choice.state.lower() != "enabled":
        st.warning(f"This subscription is **{md(choice.state)}**; changes may not be possible.")
    remember = st.checkbox("Remember this subscription as the default (saved in config.yaml)", value=True)

    if st.button("Use this subscription", type="primary", icon=":material/check_circle:"):
        if not run(lambda: connect(choice)):
            return
        if remember:
            def save():
                raw = read_raw()
                raw["subscription_id"] = choice.subscription_id
                raw["tenant_id"] = choice.tenant_id
                save_raw(raw)
            run(save)
        st.success(f"Using **{md(choice.name)}**.")
        st.rerun()
