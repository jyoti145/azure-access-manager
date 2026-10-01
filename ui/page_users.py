"""Users & access: manage the people in config.yaml without editing YAML by hand."""

from __future__ import annotations

import copy
import re

import streamlit as st
import yaml

from common import CONFIG_PATH, PAGES, md, read_raw, run, save_raw, save_text, ss
from src.config import parse_config
from src.errors import ConfigError
from src.rbac import HIGH_PRIVILEGE_ROLES

ROLES = [
    "Reader",
    "Contributor",
    "Owner",
    "User Access Administrator",
    "Role Based Access Control Administrator",
    "Virtual Machine Contributor",
    "Website Contributor",
    "Storage Blob Data Reader",
    "Storage Blob Data Contributor",
    "Cost Management Reader",
]
ROLE_HELP = {
    "Reader": "Can view everything, cannot change anything. Safest choice.",
    "Contributor": "Can create and manage resources (this can spend money), cannot give access to others.",
    "Owner": "Full control, including giving access to others.",
}
REGIONS = [
    "centralindia", "southindia", "westindia", "eastus", "eastus2", "westus2", "westus3", "northeurope",
    "westeurope", "uksouth", "francecentral", "germanywestcentral", "swedencentral", "southeastasia",
    "eastasia", "japaneast", "koreacentral", "australiaeast", "canadacentral", "brazilsouth", "uaenorth",
]
OTHER = "Other…"


def page() -> None:
    st.title("Users & access")
    st.caption(f"People and the access they should have. Saved in `{CONFIG_PATH}`. "
               "Nothing changes in Azure until you preview and apply on the **Apply changes** page.")

    try:
        raw = read_raw()
    except ConfigError as exc:
        st.error(md("; ".join(exc.problems)))
        _yaml_editor()
        return

    _validation_banner(raw)
    users_tab, add_tab, yaml_tab = st.tabs(["Users", "Add user", "Edit YAML"])
    with users_tab:
        _users_table(raw)
    with add_tab:
        _add_user(raw)
    with yaml_tab:
        _yaml_editor()


def _overrides() -> dict:
    sub = ss.get("subscription")
    return {"subscription_override": sub.subscription_id, "tenant_override": sub.tenant_id} if sub else {}


def _validation_banner(raw: dict) -> None:
    try:
        parse_config(copy.deepcopy(raw), source=str(CONFIG_PATH), **_overrides())
    except ConfigError as exc:
        st.error("**The configuration has problems**\n\n" + "\n".join(f"- {md(p)}" for p in exc.problems))


def _describe(user: dict) -> dict:
    access = user.get("access") or []
    access = access if isinstance(access, list) else [access]
    rg = user.get("resource_group")
    rg_name = rg.get("name") if isinstance(rg, dict) else rg
    roles = []
    for item in access:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope_type") or item.get("scope") or ""
        target = item.get("resource_group") or rg_name if "resource" in str(scope) or "rg" == scope else "subscription"
        roles.append(f"{item.get('role', '?')} on {target}")
    budget = user.get("budget") or {}
    budget_text = "-"
    if isinstance(budget, dict) and budget.get("enabled", True) and budget.get("amount"):
        budget_text = f"{budget.get('amount')} {budget.get('currency', '')} {budget.get('period', 'monthly')}".strip()
    return {
        "Name": user.get("name", ""),
        "Email": user.get("email", ""),
        "Access": "; ".join(roles) or "-",
        "Resource group": rg_name or "-",
        "Budget": budget_text,
        "Invite if missing": bool(user.get("invite_if_missing", False)),
    }


def _users_table(raw: dict) -> None:
    users = raw.get("users") or []
    if not users:
        st.info("No users yet. Use the **Add user** tab.")
        return
    st.dataframe([_describe(u) for u in users if isinstance(u, dict)], hide_index=True)
    st.page_link(PAGES["setup"], label="Preview and apply these settings", icon=":material/play_circle:")

    with st.expander("Remove a user from the configuration"):
        st.caption("This only removes the entry from config.yaml. It does **not** remove anyone's access in Azure; "
                   "the tool never removes access.")
        labels = [f"{u.get('name', '')} <{u.get('email', '')}>" for u in users]
        index = st.selectbox("User", range(len(users)), format_func=lambda i: labels[i], key="remove-user")
        sure = st.checkbox("Yes, remove this entry", key="remove-sure")
        if st.button("Remove", disabled=not sure, icon=":material/delete:"):
            new_raw = copy.deepcopy(raw)
            new_raw["users"].pop(index)
            if run(lambda: save_raw(new_raw)):
                ss.pop("setup_plan", None)
                st.success(f"Removed {md(labels[index])}.")
                st.rerun()


def _slug(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9-]", "-", local).strip("-")[:40] or "user"


def _add_user(raw: dict) -> None:
    identity = ss.get("identity")
    c1, c2 = st.columns(2)
    name = c1.text_input("Name", key="add-name")
    email = c2.text_input("Email", key="add-email", placeholder="person@example.com").strip()
    invite = st.checkbox("Send a guest invitation if this person is not in your directory", value=True,
                         key="add-invite",
                         help="People outside your organisation (for example a Gmail address) must be invited "
                              "before they can be given access. They receive an email to accept.")

    st.markdown("**Access**")
    scope = st.radio("Where", ["resource_group", "subscription"], horizontal=True, key="add-scope",
                     format_func={"resource_group": "Their own resource group (recommended)",
                                  "subscription": "Whole subscription"}.get)
    role_choice = st.selectbox("Role", ROLES + [OTHER], key="add-role")
    role = st.text_input("Role name", key="add-role-other").strip() if role_choice == OTHER else role_choice
    if role_choice in ROLE_HELP:
        st.caption(ROLE_HELP[role_choice])
    if role.lower() in HIGH_PRIVILEGE_ROLES:
        st.warning(f"**{md(role)}** lets this person manage access for others. You will be asked to confirm "
                   "before it is applied.")

    rg_name = location = None
    create_rg = False
    if scope == "resource_group":
        r1, r2 = st.columns(2)
        suggested = f"rg-{_slug(email)}" if email else "rg-<name>"
        rg_name = r1.text_input("Resource group name", key="add-rg", placeholder=suggested,
                                help=f"Leave empty to use {suggested}").strip()
        if not rg_name and email:
            rg_name = suggested
        region = r2.selectbox("Region", REGIONS + [OTHER], key="add-region")
        location = st.text_input("Region name", key="add-region-other").strip() if region == OTHER else region
        create_rg = st.checkbox("Create the resource group if it does not exist", value=True, key="add-create")

    st.markdown("**Budget**")
    budget_on = st.toggle("Add a monthly budget with email alerts", value=scope == "resource_group",
                          key="add-budget", help="Budgets are free. They send alerts; they do not stop spending.")
    budget = None
    if budget_on:
        b1, b2, b3 = st.columns(3)
        amount = b1.number_input("Amount", min_value=1.0, value=500.0, step=100.0, key="add-amount")
        currency = b2.text_input("Currency", value="INR", key="add-currency", max_chars=3,
                                 help="Must match your subscription's billing currency. Amounts are never converted.")
        period = b3.selectbox("Period", ["monthly", "quarterly", "annually"], key="add-period")
        thresholds = st.multiselect("Alert at (% of budget)", [25, 50, 75, 80, 90, 100, 110, 125],
                                    default=[50, 75, 90, 100], key="add-thresholds")
        default_email = identity.email if identity and identity.email else ""
        emails = st.text_input("Send alerts to (comma separated)", value=default_email, key="add-alert-emails")
        budget = {
            "enabled": True,
            "amount": amount,
            "currency": currency.strip().upper(),
            "period": period,
            "thresholds": sorted(thresholds),
            "notifications": {"emails": [e.strip() for e in emails.split(",") if e.strip()]},
        }
        if scope == "subscription":
            budget["scope"] = "subscription"

    if st.button("Add user", type="primary", icon=":material/person_add:"):
        entry: dict = {"name": name or email, "email": email, "invite_if_missing": invite}
        if scope == "resource_group":
            entry["resource_group"] = {"name": rg_name, "location": location, "create_if_missing": create_rg}
            entry["access"] = {"scope_type": "resource_group", "role": role}
        else:
            entry["access"] = {"scope_type": "subscription", "role": role}
        if budget:
            entry["budget"] = budget

        new_raw = copy.deepcopy(raw)
        users = new_raw.get("users") or []
        if any(isinstance(u, dict) and str(u.get("email", "")).lower() == email.lower() for u in users):
            st.error(f"{md(email)} is already in the configuration. Remove it first or edit the YAML.")
            return
        users.append(entry)
        new_raw["users"] = users
        sub = ss.get("subscription")
        if sub and not new_raw.get("subscription_id"):
            new_raw["subscription_id"] = sub.subscription_id
        try:
            parse_config(copy.deepcopy(new_raw), **_overrides())
        except ConfigError as exc:
            mine = [p for p in exc.problems if f"users[{len(users) - 1}]" in p] or exc.problems
            st.error("**Please fix:**\n\n" + "\n".join(f"- {md(p)}" for p in mine))
            return
        if run(lambda: save_raw(new_raw)):
            ss.pop("setup_plan", None)
            st.success(f"Added **{md(email)}**. Next: preview and apply it.")
            st.page_link(PAGES["setup"], label="Go to Apply changes", icon=":material/play_circle:")


def _yaml_editor() -> None:
    st.caption("Advanced: edit the file directly. Comments are kept when you save from here. "
               "A backup is written to config.yaml.bak on every save.")
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        text, mtime = "", 0
    edited = st.text_area("config.yaml", text, height=480, key=f"yaml-{mtime}")
    c1, c2 = st.columns([1, 5])
    validate = c1.button("Validate", icon=":material/fact_check:")
    save = c2.button("Save", type="primary", icon=":material/save:")
    if validate or save:
        try:
            data = yaml.safe_load(edited) or {}
            if not isinstance(data, dict):
                raise ConfigError("Top level must be a mapping")
            parse_config(data, **_overrides())
        except yaml.YAMLError as exc:
            st.error(f"Not valid YAML: {md(exc)}")
            return
        except ConfigError as exc:
            st.error("**Problems**\n\n" + "\n".join(f"- {md(p)}" for p in exc.problems))
            return
        if save:
            if run(lambda: save_text(edited)):
                ss.pop("setup_plan", None)
                st.success("Saved.")
                st.rerun()
        else:
            st.success("The configuration is valid.")
