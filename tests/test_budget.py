from datetime import date

import yaml

from src.budgets import (
    BudgetInfo,
    budget_health,
    build_budget_body,
    diff_budget,
    notification_key,
    threshold_states,
)
from src.config import parse_config
from src.costs import parse_query_result
from src.models import ActionKind, AlertSpec, BudgetSpec
from src.setup_engine import SetupEngine

from conftest import SUB, SUB_SCOPE

RG_SCOPE = f"{SUB_SCOPE}/resourceGroups/rg-john"


def spec(**overrides) -> BudgetSpec:
    values = dict(
        enabled=True, name="budget-rg-john", amount=10000, period="Monthly", currency="INR",
        alerts=[AlertSpec(t, emails=("admin@example.com",)) for t in (50, 75, 90, 100)],
        resource_group="rg-john",
    )
    values.update(overrides)
    return BudgetSpec(**values)


def existing_from(body: dict, current=None, forecast=None) -> BudgetInfo:
    data = dict(body, name="budget-rg-john", id=f"{RG_SCOPE}/providers/Microsoft.Consumption/budgets/budget-rg-john", eTag="e1")
    data["properties"] = dict(data["properties"])
    if current is not None:
        data["properties"]["currentSpend"] = {"amount": current, "unit": "INR"}
    if forecast is not None:
        data["properties"]["forecastSpend"] = {"amount": forecast, "unit": "INR"}
    return BudgetInfo.from_json(data)


# --------------------------------------------------------------------------- payload


def test_budget_body():
    body = build_budget_body(spec(), start_date=date(2026, 10, 1))
    props = body["properties"]
    assert props["category"] == "Cost"
    assert props["amount"] == 10000
    assert props["timeGrain"] == "Monthly"
    assert props["timePeriod"] == {"startDate": "2026-10-01T00:00:00Z"}
    assert sorted(props["notifications"]) == sorted(
        f"Actual_GreaterThanOrEqualTo_{t}_Percent" for t in (50, 75, 90, 100)
    )
    n = props["notifications"]["Actual_GreaterThanOrEqualTo_75_Percent"]
    assert n == {"enabled": True, "operator": "GreaterThanOrEqualTo", "threshold": 75.0,
                 "thresholdType": "Actual", "contactEmails": ["admin@example.com"]}
    assert "eTag" not in body


def test_update_body_keeps_time_period_and_etag():
    body = build_budget_body(spec(), etag="e1", existing_time_period={"startDate": "2025-01-01T00:00:00Z"})
    assert body["eTag"] == "e1"
    assert body["properties"]["timePeriod"] == {"startDate": "2025-01-01T00:00:00Z"}


def test_notification_keys_are_valid_names():
    assert notification_key(AlertSpec(87.5, threshold_type="Forecasted")) == "Forecasted_GreaterThanOrEqualTo_87_5_Percent"


# --------------------------------------------------------------------------- drift detection


def test_no_diff_when_in_sync():
    existing = existing_from(build_budget_body(spec(), start_date=date(2026, 10, 1)))
    assert diff_budget(spec(), existing) == []


def test_diff_detects_amount_period_and_alert_changes():
    existing = existing_from(build_budget_body(spec(), start_date=date(2026, 10, 1)))
    desired = spec(
        amount=12000, period="Quarterly",
        alerts=[AlertSpec(50, emails=("admin@example.com",)), AlertSpec(80, emails=("admin@example.com",)),
                AlertSpec(100, emails=("finance@example.com",))],
    )
    changes = " | ".join(diff_budget(desired, existing))
    assert "amount 10000 -> 12000" in changes
    assert "period Monthly -> Quarterly" in changes
    assert "add 80% actual" in changes
    assert "remove 75% actual, 90% actual" in changes
    assert "update recipients/settings for 100% actual" in changes


def test_email_case_does_not_cause_drift():
    existing = existing_from(build_budget_body(spec(alerts=[AlertSpec(50, emails=("Admin@Example.com",))])))
    assert diff_budget(spec(alerts=[AlertSpec(50, emails=("admin@example.com",))]), existing) == []


# --------------------------------------------------------------------------- status


def test_threshold_states_and_health():
    body = build_budget_body(spec(alerts=[AlertSpec(50), AlertSpec(75), AlertSpec(100, threshold_type="Forecasted")]))
    info = existing_from(body, current=7600, forecast=10400)
    assert round(info.usage_percent, 1) == 76.0
    assert info.remaining == 2400
    states = {(s.threshold, s.threshold_type): s.crossed for s in threshold_states(info)}
    assert states == {(50, "Actual"): True, (75, "Actual"): True, (100, "Forecasted"): True}
    assert budget_health(info.usage_percent) == "WARNING"
    assert budget_health(43.2) == "HEALTHY"
    assert budget_health(95) == "CRITICAL"
    assert budget_health(100) == "EXCEEDED"
    assert budget_health(None) == "UNKNOWN"


def test_budget_scope_from_id():
    info = existing_from(build_budget_body(spec()))
    assert info.scope == RG_SCOPE


# --------------------------------------------------------------------------- cost query parsing


def test_parse_cost_query_result():
    data = {"properties": {
        "columns": [{"name": "PreTaxCost"}, {"name": "ResourceGroupName"}, {"name": "Currency"}],
        "rows": [[1200.5, "rg-john", "INR"], [300, "RG-Other", "INR"], [10, "", "INR"]],
    }}
    result = parse_query_result(data, "MonthToDate")
    assert result.total == 1510.5
    assert result.currency == "INR"
    assert result.for_resource_group("RG-JOHN") == 1200.5
    assert result.for_resource_group("rg-other") == 300
    assert result.by_resource_group["(no resource group)"] == 10


def test_parse_cost_query_with_cost_column_and_no_rows():
    result = parse_query_result({"properties": {"columns": [{"name": "Cost"}], "rows": []}}, "TheLastMonth")
    assert result.total == 0 and result.currency is None


def test_cost_query_waits_out_throttling():
    from types import SimpleNamespace

    from conftest import http_error
    from src.costs import CostService

    throttled = http_error(429, "429", "Too many requests")
    throttled.response = SimpleNamespace(headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "7"})
    ok = {"properties": {"columns": [{"name": "PreTaxCost"}, {"name": "Currency"}], "rows": [[42, "INR"]]}}
    calls, sleeps = [], []

    def usage(scope, body):
        calls.append(body)
        if len(calls) <= 2:
            raise throttled
        return ok

    service = CostService(SimpleNamespace(query=SimpleNamespace(usage=usage)), SUB, sleep=sleeps.append)
    result = service.month_to_date()
    assert (result.total, result.currency) == (42, "INR")
    assert sleeps == [7.0, 7.0]  # waited the time Cost Management asked for


# --------------------------------------------------------------------------- engine (budget parts)


CONFIG = parse_config(yaml.safe_load(f"""
subscription_id: "{SUB}"
users:
  - email: john@example.com
    resource_group: {{name: rg-john, location: centralindia, create_if_missing: true}}
    budget:
      amount: 10000
      currency: INR
      thresholds: [50, 75, 90, 100]
      notifications: {{emails: [admin@example.com]}}
"""))


def test_budget_create_then_noop_then_update(ctx):
    ctx.users.add("john@example.com", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    engine = SetupEngine(ctx, CONFIG, today=date(2026, 10, 15))
    plans = engine.plan()
    budget_action = [a for a in plans[0].changes if a.title.startswith("Budget")][0]
    assert budget_action.kind == ActionKind.CREATE
    assert "Amount: ₹10,000" in budget_action.details
    engine.execute(plans)
    scope, name, body = ctx.budgets.writes[0]
    assert (scope, name) == (RG_SCOPE, "budget-rg-john")
    assert body["properties"]["timePeriod"]["startDate"] == "2026-10-01T00:00:00Z"

    second = SetupEngine(ctx, CONFIG).plan()[0]
    assert second.changes == []
    titles = [a.title for a in second.actions]
    assert "Budget already exists: budget-rg-john" in titles
    assert "Budget notifications already configured" in titles

    CONFIG.users[0].budget.amount = 15000
    try:
        third = SetupEngine(ctx, CONFIG)
        plans = third.plan()
        update = plans[0].changes[0]
        assert update.kind == ActionKind.UPDATE
        assert "Change: amount 10000 -> 15000" in update.details
        third.execute(plans)
        assert ctx.budgets.writes[-1][2]["eTag"] == "etag-1"
    finally:
        CONFIG.users[0].budget.amount = 10000


def test_currency_mismatch_warning(ctx):
    ctx.resource_groups.add("rg-john")
    engine = SetupEngine(ctx, CONFIG, billing_currency="USD")
    action = [a for a in engine.plan()[0].changes if a.title.startswith("Budget")][0]
    assert any("billing currency (USD)" in w and "NOT converted" in w for w in action.warnings)


def test_only_budgets_stage_skips_user_lookup(ctx):
    from src.models import Stage

    ctx.resource_groups.add("rg-john")
    plans = SetupEngine(ctx, CONFIG, stages={Stage.BUDGETS}).plan()
    assert not plans[0].blocked  # user does not exist, but users/rbac stages are excluded
    assert [a.title for a in plans[0].changes] == ["Budget: budget-rg-john"]
