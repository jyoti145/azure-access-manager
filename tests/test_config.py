import json

import pytest
import yaml

from src.config import load_config, parse_config, resolve_subscription_settings
from src.errors import ConfigError
from src.models import ScopeType

from conftest import SUB


def parse(text: str):
    return parse_config(yaml.safe_load(text))


def problems(text: str) -> list[str]:
    with pytest.raises(ConfigError) as info:
        parse(text)
    return info.value.problems


FULL = f"""
subscription_id: "{SUB}"
users:
  - name: "John Doe"
    email: "john@example.com"
    resource_group:
      name: "rg-john"
      location: "Central India"
      create_if_missing: true
    access:
      role: "Contributor"
      scope: "resource_group"
    budget:
      enabled: true
      amount: 10000
      currency: "inr"
      period: "monthly"
      thresholds: [50, 75, 90, 100]
      notifications:
        emails: ["admin@example.com"]
subscription_report:
  enabled: true
"""


def test_full_example_parses():
    config = parse(FULL)
    user = config.users[0]
    assert config.subscription_id == SUB
    assert user.resource_group.name == "rg-john"
    assert user.resource_group.location == "centralindia"  # normalised
    assert user.resource_group.create_if_missing is True
    # 'scope: resource_group' is accepted as the scope type keyword
    assert user.access[0].scope_type == ScopeType.RESOURCE_GROUP
    assert user.access[0].resource_group == "rg-john"
    budget = user.budget
    assert budget.name == "budget-rg-john"
    assert budget.resource_group == "rg-john"
    assert budget.currency == "INR"
    assert budget.period == "Monthly"
    assert [a.threshold for a in budget.alerts] == [50, 75, 90, 100]
    assert all(a.emails == ("admin@example.com",) for a in budget.alerts)
    assert all(a.threshold_type == "Actual" for a in budget.alerts)
    assert config.report.enabled is True


def test_multiple_users_and_access_forms():
    config = parse(f"""
subscription_id: "{SUB}"
users:
  - name: Alice
    email: alice@example.com
    access: {{scope_type: subscription, role: Reader}}
  - name: Bob
    email: bob@example.com
    access:
      scope_type: resource_group
      resource_group: rg-development
      role: Contributor
    budget: {{enabled: true, amount: 500, currency: INR}}
""")
    alice, bob = config.users
    assert alice.access[0].scope_type == ScopeType.SUBSCRIPTION
    assert alice.budget is None
    assert bob.access[0].resource_group == "rg-development"
    assert bob.budget.resource_group == "rg-development"  # inferred from the RG-scoped access
    assert bob.budget.name == "budget-rg-development"
    # no recipients configured -> defaults to the user's own email, flagged for display
    assert bob.budget.alerts[0].emails == ("bob@example.com",)
    assert bob.budget.recipients_defaulted is True


def test_access_list_with_full_scope_ids():
    config = parse(f"""
subscription_id: "{SUB}"
users:
  - email: carol@example.com
    access:
      - scope: "/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Storage/storageAccounts/sta"
        role: Storage Blob Data Reader
      - scope_type: management_group
        management_group_id: mg-platform
        role: Reader
      - scope_type: resource_group
        scope: rg-b
        role: Reader
""")
    access = config.users[0].access
    assert access[0].scope_type == ScopeType.RESOURCE
    assert access[0].resource_group == "rg-a"
    assert access[1].scope_type == ScopeType.MANAGEMENT_GROUP
    assert access[1].management_group_id == "mg-platform"
    assert access[2].resource_group == "rg-b"


def test_scope_type_is_never_defaulted_to_subscription():
    found = problems(f"""
subscription_id: "{SUB}"
users:
  - email: a@example.com
    access: {{role: Owner}}
""")
    assert any("scope_type: required" in p for p in found)


def test_all_problems_are_reported_together():
    found = problems("""
subscription_id: "SUB-ID"
users:
  - email: not-an-email
    access: {scope_type: galaxy, role: Reader}
  - name: no email
""")
    assert any("subscription_id" in p for p in found)
    assert any("invalid email" in p for p in found)
    assert any("unknown scope type" in p for p in found)
    assert any("'email' (or 'object_id') is required" in p for p in found)


def test_secrets_in_config_are_rejected():
    found = problems(f"""
subscription_id: "{SUB}"
client_secret: "abc"
users: []
""")
    assert any("credentials must not be stored" in p for p in found)


def test_budget_validation():
    found = problems(f"""
subscription_id: "{SUB}"
users:
  - email: a@example.com
    access: {{scope_type: subscription, role: Reader}}
    budget:
      amount: -5
      period: weekly
      thresholds: [10, 20, 30, 40, 50, 60]
      notifications: {{emails: [a@example.com]}}
""")
    joined = " | ".join(found)
    assert "amount" in joined
    assert "period" in joined
    assert "at most 5 notifications" in joined
    assert "no resource group to attach the budget" in joined


def test_forecast_and_per_threshold_recipients():
    config = parse(f"""
subscription_id: "{SUB}"
users:
  - email: a@example.com
    resource_group: rg-a
    access: {{role: Reader}}
    budget:
      amount: 100
      period: quarterly
      thresholds:
        - 80
        - {{threshold: 100, type: forecasted, emails: [finance@example.com]}}
      forecast_thresholds: [120]
      notifications: {{emails: [ops@example.com], roles: [Owner]}}
""")
    budget = config.users[0].budget
    assert budget.period == "Quarterly"
    assert [(a.threshold, a.threshold_type) for a in budget.alerts] == [(80, "Actual"), (100, "Forecasted"), (120, "Forecasted")]
    assert budget.alerts[0].emails == ("ops@example.com",) and budget.alerts[0].roles == ("Owner",)
    assert budget.alerts[1].emails == ("finance@example.com",)


def test_conflicting_definitions_across_users():
    found = problems(f"""
subscription_id: "{SUB}"
users:
  - email: a@example.com
    resource_group: {{name: rg-x, location: westeurope}}
    access: {{role: Reader}}
    budget: {{amount: 100, name: shared}}
  - email: b@example.com
    resource_group: {{name: rg-x, location: eastus}}
    access: {{role: Reader}}
    budget: {{amount: 200, name: shared}}
""")
    joined = " | ".join(found)
    assert "conflicting locations" in joined
    assert "defined differently" in joined


def test_resource_in_other_subscription_rejected():
    found = problems(f"""
subscription_id: "{SUB}"
users:
  - email: a@example.com
    access:
      scope_type: resource
      resource_id: /subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/rg/providers/Microsoft.Web/sites/app
      role: Reader
""")
    assert any("different subscription" in p for p in found)


def test_create_requires_location():
    found = problems(f"""
subscription_id: "{SUB}"
users:
  - email: a@example.com
    resource_group: {{name: rg-a, create_if_missing: true}}
    access: {{role: Reader}}
""")
    assert any("location: required" in p for p in found)


def test_json_config_and_subscription_env(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"users": [{"email": "a@example.com", "access": {"scope_type": "subscription", "role": "Reader"}}]}))
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", SUB)
    config = load_config(path)
    assert config.subscription_id == SUB
    assert resolve_subscription_settings(None) == (SUB, None)


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("does-not-exist.yaml")


def test_invite_flag_defaults_and_override():
    config = parse(f"""
subscription_id: "{SUB}"
defaults: {{invite_if_missing: true}}
users:
  - email: a@example.com
    access: {{scope_type: subscription, role: Reader}}
  - email: b@example.com
    invite_if_missing: false
    access: {{scope_type: subscription, role: Reader}}
""")
    assert [u.invite_if_missing for u in config.users] == [True, False]


def test_example_config_template_is_valid():
    from pathlib import Path

    template = Path(__file__).resolve().parent.parent / "config.example.yaml"
    config = load_config(template, subscription_override=SUB)
    assert config.users == []
    assert config.subscription_id == SUB
