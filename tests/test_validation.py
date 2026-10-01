import logging

import pytest

from src import validation as v
from src.console import format_money, render_table
from src.errors import RestError, translate_error
from src.logging_utils import JsonFormatter, RedactingFilter, redact
from src.models import ScopeType
from src.permissions import PermissionEntry, evaluate
from src.scopes import describe_scope, parse_scope, relation, scope_contains

from conftest import SUB, http_error


@pytest.mark.parametrize("value,ok", [
    (SUB, True), ("11111111111111111111111111111111", False), ("SUB-ID", False), ("", False), (None, False),
])
def test_guid(value, ok):
    assert v.is_guid(value) is ok


@pytest.mark.parametrize("value,ok", [
    ("john@example.com", True), ("john.doe+tag@sub.example.co.in", True),
    ("john@", False), ("@example.com", False), ("john example@x.com", False),
])
def test_email(value, ok):
    assert v.is_email(value) is ok


@pytest.mark.parametrize("value,ok", [
    ("rg-john", True), ("rg_john.(dev)", True), ("rg.", False), ("rg/john", False), ("a" * 91, False),
])
def test_resource_group_names(value, ok):
    assert v.is_resource_group_name(value) is ok


def test_location_normalisation():
    assert v.normalize_location("Central India") == "centralindia"
    assert v.normalize_location("west-europe") == "westeurope"
    assert v.is_location("centralindia")


def test_quoting():
    assert v.odata_quote("o'brien@example.com") == "o''brien@example.com"
    assert v.kql_quote("rg'x") == "rg\\'x"


# --------------------------------------------------------------------------- scopes


def test_parse_scopes():
    assert parse_scope(f"/subscriptions/{SUB}").type == ScopeType.SUBSCRIPTION
    rg = parse_scope(f"/subscriptions/{SUB}/resourceGroups/rg-a")
    assert (rg.type, rg.resource_group) == (ScopeType.RESOURCE_GROUP, "rg-a")
    res = parse_scope(f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Storage/storageAccounts/sta")
    assert (res.type, res.resource_name, res.resource_group) == (ScopeType.RESOURCE, "sta", "rg-a")
    mg = parse_scope("/providers/Microsoft.Management/managementGroups/mg1")
    assert (mg.type, mg.management_group) == (ScopeType.MANAGEMENT_GROUP, "mg1")
    assert parse_scope("/").type == ScopeType.TENANT
    with pytest.raises(ValueError):
        parse_scope("subscriptions/x")


def test_scope_hierarchy_and_inheritance():
    sub = f"/subscriptions/{SUB}"
    rg = f"{sub}/resourceGroups/rg-a"
    assert scope_contains(sub, rg)
    assert not scope_contains(rg, sub)
    assert not scope_contains(f"{sub}/resourceGroups/rg", rg)  # prefix without path boundary
    assert relation(rg, rg.upper()) == "Direct"
    assert relation(sub, rg) == "Inherited"
    assert relation(rg, sub) == "Child"  # Owner on an RG is NOT Owner on the subscription
    assert relation("/providers/Microsoft.Management/managementGroups/mg", sub) == "Inherited"
    assert relation("/", rg) == "Inherited"
    assert describe_scope(rg) == ("Resource Group", "rg-a")


# --------------------------------------------------------------------------- permissions


def test_permission_wildcards():
    owner = [PermissionEntry(actions=["*"])]
    contributor = [PermissionEntry(actions=["*"], not_actions=["Microsoft.Authorization/*/Write", "Microsoft.Authorization/*/Delete"])]
    cost_reader = [PermissionEntry(actions=["Microsoft.Consumption/*/read", "Microsoft.CostManagement/*/read"])]
    rbac_admin = [PermissionEntry(actions=["Microsoft.Authorization/roleAssignments/write"], condition="@Request[...]")]

    assert evaluate(owner, "Microsoft.Authorization/roleAssignments/write") == (True, False)
    assert evaluate(contributor, "Microsoft.Authorization/roleAssignments/write") == (False, False)
    assert evaluate(contributor, "Microsoft.Consumption/budgets/write") == (True, False)
    assert evaluate(cost_reader, "Microsoft.CostManagement/query/read") == (True, False)
    assert evaluate(cost_reader, "Microsoft.Consumption/budgets/write") == (False, False)
    assert evaluate(rbac_admin, "microsoft.authorization/roleassignments/write") == (True, True)


# --------------------------------------------------------------------------- errors / logging


def test_translate_authorization_failed():
    error = translate_error(
        http_error(403, "AuthorizationFailed", "The client 'x' does not have authorization"),
        "Assign Contributor role", user="john@example.com", scope="rg-development",
        required_permission="Microsoft.Authorization/roleAssignments/write",
    )
    assert error.code == "AuthorizationFailed"
    assert "does not have permission" in error.reason
    assert error.required_permission == "Microsoft.Authorization/roleAssignments/write"
    assert error.user == "john@example.com"


def test_translate_tenant_mismatch_and_rest_errors():
    error = translate_error(RestError(401, "InvalidAuthenticationTokenTenant", "wrong issuer"), "Validate subscription")
    assert "Tenant mismatch" in error.reason
    assert "az login --tenant" in error.hint
    not_found = translate_error(RestError(404, None, ""), "Read")
    assert "not found" in not_found.reason.lower()
    assert not_found.required_permission is None


def test_translate_credential_unavailable():
    from azure.identity import CredentialUnavailableError

    error = translate_error(CredentialUnavailableError("Azure CLI not found on path"), "Authenticate")
    assert "az login" in error.hint


def test_redaction():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"
    text = f"Authorization: Bearer {jwt} client_secret=abc123 password: 'p@ss' token={jwt}"
    cleaned = redact(text)
    assert jwt not in cleaned and "abc123" not in cleaned and "p@ss" not in cleaned
    assert "Bearer ***" in cleaned


def test_redaction_of_env_secret(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "SuperSecretValue123")
    assert "SuperSecretValue123" not in redact("failed with SuperSecretValue123 in body")


def test_json_log_record_is_redacted():
    record = logging.LogRecord("azure_manager", logging.INFO, __file__, 1, "token=abcdef", None, None)
    record.operation, record.error = "assign_role", "Bearer xyz.abc"
    RedactingFilter().filter(record)
    line = JsonFormatter().format(record)
    assert "abcdef" not in line and "xyz.abc" not in line
    assert '"operation": "assign_role"' in line


# --------------------------------------------------------------------------- formatting


def test_money_formatting():
    assert format_money(10000, "INR") == "₹10,000"
    assert format_money(1234567.5, "INR") == "₹12,34,567.50"
    assert format_money(1234567, "USD") == "$1,234,567"
    assert format_money(5, "XYZ") == "XYZ 5"
    assert format_money(10000, "INR", ascii_only=True) == "INR 10,000"
    assert format_money(None, "INR") == "N/A"


def test_render_table():
    text = render_table(["A", "Bee"], [["1", "two"], ["three", None]])
    lines = text.splitlines()
    assert lines[0].startswith("A")
    assert lines[1].startswith("-----")
    assert "three" in lines[3]
