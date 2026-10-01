"""Render every UI page headlessly (Streamlit AppTest) against in-memory Azure fakes."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["account", "dashboard", "users", "setup", "budgets", "access", "resources", "audit"]

SCRIPT = """
import sys
sys.path[:0] = [{ui!r}, {tests!r}, {root!r}]
import streamlit as st
import common
common.CONFIG_PATH = __import__("pathlib").Path({config!r})
pages = {{name: st.Page(lambda: None, title=name, url_path=name) for name in {pages!r}}}
common.PAGES.update(pages)
st.navigation(list(pages.values()), position="hidden")
import page_{page} as module
module.page()
"""


def fake_context():
    import conftest
    from src.auth import Identity
    from src.permissions import PermissionEntry, PermissionService
    from src.resources import ResourceInfo
    from src.subscriptions import SubscriptionInfo
    from src.users import PrincipalInfo

    class Subs:
        def get(self, sid):
            return SubscriptionInfo(sid, "Test Subscription", "22222222-2222-2222-2222-222222222222", "Enabled",
                                    "Public_2014-09-01", "AzureForStudents_2018-01-01", "On", "RoleBased", {})

    class Perms(PermissionService):
        def entries(self, scope):
            return [PermissionEntry(actions=["*"])]

    ctx = conftest.FakeContext()
    ctx.identity = Identity("Azure CLI", "User", "live.com#admin@example.com", "o", "22222222-2222-2222-2222-222222222222")
    ctx.subscriptions = Subs()
    ctx.permissions = Perms.__new__(Perms)
    ctx.costs = conftest.FakeCosts("INR", {"rg-shared": 120.0})
    ctx.resource_groups.add("rg-shared", "centralindia")
    ctx.users.add("john@example.com", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    ctx.users.resolve_principals = lambda ids: {
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": PrincipalInfo("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "John Doe",
                                                             "john@example.com", "User", "Member")}
    ctx.resources.list = lambda resource_group=None: [
        ResourceInfo("vnet1", "microsoft.network/virtualnetworks", "rg-shared", "centralindia", "Succeeded", "/x")]
    ctx.auth_client.role_assignments.add(conftest.SUB_SCOPE, "Owner", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    return ctx


CONFIG = """
subscription_id: "{sub}"
users:
  - name: John Doe
    email: john@example.com
    resource_group: {{name: rg-john, location: centralindia, create_if_missing: true}}
    access: {{scope_type: resource_group, role: Contributor}}
    budget: {{amount: 500, currency: INR, notifications: {{emails: [admin@example.com]}}}}
"""


@pytest.fixture
def app_factory(tmp_path):
    import conftest
    from src.accounts import SubscriptionChoice

    config = tmp_path / "config.yaml"
    config.write_text(CONFIG.format(sub=conftest.SUB), encoding="utf-8")
    ctx = fake_context()

    def make(page: str, connected: bool = True) -> AppTest:
        script = SCRIPT.format(ui=str(ROOT / "ui"), tests=str(ROOT / "tests"), root=str(ROOT), config=str(config),
                               pages=PAGES, page=page)
        at = AppTest.from_string(script, default_timeout=30)
        at.session_state["auto_tried"] = True
        if connected:
            at.session_state["ctx"] = ctx
            at.session_state["identity"] = ctx.identity
            at.session_state["subscription"] = SubscriptionChoice(conftest.SUB, "Test Subscription", "Enabled",
                                                                  ctx.identity.tenant_id, "Default Directory")
        return at

    make.ctx = ctx
    make.config = config
    return make


@pytest.mark.parametrize("page", [p for p in PAGES if p != "setup"])
def test_page_renders_without_errors(app_factory, page):
    at = app_factory(page).run()
    assert not at.exception, at.exception
    assert not [e.value for e in at.error], [e.value for e in at.error]


def test_pages_ask_to_sign_in_when_not_connected(app_factory):
    at = app_factory("dashboard", connected=False).run()
    assert not at.exception
    assert any("Sign in" in i.value for i in at.info)


def test_setup_preview_then_apply(app_factory):
    at = app_factory("setup").run()
    assert not at.exception
    at.button[0].click().run()  # Preview changes
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "Resource Group: rg-john" in text and "Contributor" in text
    assert app_factory.ctx.resource_groups.created == []  # preview never writes

    at.checkbox[0].check().run()  # "I have reviewed the changes"
    apply = next(b for b in at.button if b.label.startswith("Apply"))
    apply.click().run()
    assert not at.exception, at.exception
    assert app_factory.ctx.resource_groups.created == ["rg-john"]
    assert any("SUCCESS" in m.value for m in at.markdown)


def test_add_user_writes_config(app_factory):
    at = app_factory("users").run()
    at.text_input(key="add-name").input("Priya").run()
    at.text_input(key="add-email").input("priya@example.com").run()
    next(b for b in at.button if b.label == "Add user").click().run()
    assert not at.exception, at.exception
    saved = app_factory.config.read_text(encoding="utf-8")
    messages = [e.value for e in at.error] + [s.value for s in at.success]
    assert "priya@example.com" in saved and "rg-priya" in saved, messages
    assert any("Added" in s.value for s in at.success)
