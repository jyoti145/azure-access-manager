"""Cost Management budgets (Microsoft.Consumption/budgets).

Notes on Azure behaviour (not limitations of this tool, but worth knowing):
* Budget amounts are always in the subscription's *billing currency*. The API has no currency
  field, so the 'currency' in config.yaml is used for display and for a mismatch warning only;
  amounts are never converted.
* A budget can have at most 5 notifications. Each notification is either 'Actual' or
  'Forecasted' and needs at least one email, contact role or action group.
* Budget evaluation runs periodically (typically every few hours), and cost data lags usage by
  8-24 hours, so 'current spend' is never real-time.
* The start date must be the first day of a month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .errors import error_status, translate_error
from .logging_utils import log_event
from .models import AlertSpec, BudgetSpec
from .rest import as_json

WRITE_PERMISSION = "Microsoft.Consumption/budgets/write"
READ_PERMISSION = "Microsoft.Consumption/budgets/read"


# --------------------------------------------------------------------------- payload building


def notification_key(alert: AlertSpec) -> str:
    threshold = f"{alert.threshold:g}".replace(".", "_")
    return f"{alert.threshold_type}_{alert.operator}_{threshold}_Percent"


def build_notification(alert: AlertSpec) -> dict[str, Any]:
    body: dict[str, Any] = {
        "enabled": alert.enabled,
        "operator": alert.operator,
        "threshold": alert.threshold,
        "thresholdType": alert.threshold_type,
        "contactEmails": list(alert.emails),
    }
    if alert.roles:
        body["contactRoles"] = list(alert.roles)
    if alert.action_groups:
        body["contactGroups"] = list(alert.action_groups)
    if alert.locale:
        body["locale"] = alert.locale
    return body


def build_notifications(alerts: list[AlertSpec]) -> dict[str, dict[str, Any]]:
    return {notification_key(alert): build_notification(alert) for alert in alerts}


def default_start_date(today: date | None = None) -> date:
    today = today or date.today()
    return date(today.year, today.month, 1)


def build_budget_body(
    spec: BudgetSpec,
    *,
    start_date: date | None = None,
    etag: str | None = None,
    existing_time_period: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """REST body for PUT .../providers/Microsoft.Consumption/budgets/{name}."""
    if existing_time_period:
        time_period = dict(existing_time_period)
        if spec.end_date:
            time_period["endDate"] = f"{spec.end_date.isoformat()}T00:00:00Z"
    else:
        start = spec.start_date or start_date or default_start_date()
        time_period = {"startDate": f"{start.isoformat()}T00:00:00Z"}
        if spec.end_date:
            time_period["endDate"] = f"{spec.end_date.isoformat()}T00:00:00Z"
    body: dict[str, Any] = {
        "properties": {
            "category": "Cost",
            "amount": spec.amount,
            "timeGrain": spec.period,
            "timePeriod": time_period,
            "notifications": build_notifications(spec.alerts),
        }
    }
    if etag:
        body["eTag"] = etag
    return body


# --------------------------------------------------------------------------- reading


@dataclass
class BudgetInfo:
    name: str
    id: str
    amount: float
    time_grain: str
    start_date: str | None = None
    end_date: str | None = None
    current_spend: float | None = None
    forecast_spend: float | None = None
    currency: str | None = None
    notifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    etag: str | None = None
    time_period: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> "BudgetInfo":
        props = data.get("properties") or {}
        current = props.get("currentSpend") or {}
        forecast = props.get("forecastSpend") or {}
        period = props.get("timePeriod") or {}
        return cls(
            name=data.get("name", ""),
            id=data.get("id", ""),
            amount=float(props.get("amount") or 0),
            time_grain=str(props.get("timeGrain") or ""),
            start_date=(period.get("startDate") or "")[:10] or None,
            end_date=(period.get("endDate") or "")[:10] or None,
            current_spend=_float(current.get("amount")),
            forecast_spend=_float(forecast.get("amount")),
            currency=current.get("unit") or forecast.get("unit"),
            notifications=props.get("notifications") or {},
            etag=data.get("eTag") or data.get("etag"),
            time_period=period,
        )

    @property
    def scope(self) -> str:
        marker = "/providers/microsoft.consumption/budgets/"
        index = self.id.lower().find(marker)
        return self.id[:index] if index >= 0 else ""

    @property
    def usage_percent(self) -> float | None:
        if self.current_spend is None or not self.amount:
            return None
        return self.current_spend / self.amount * 100

    @property
    def remaining(self) -> float | None:
        return None if self.current_spend is None else self.amount - self.current_spend


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _signature(notification: dict[str, Any]) -> tuple:
    return (
        str(notification.get("thresholdType") or "Actual").lower(),
        str(notification.get("operator") or "").lower(),
        round(float(notification.get("threshold") or 0), 4),
        tuple(sorted(e.lower() for e in notification.get("contactEmails") or [])),
        tuple(sorted(r.lower() for r in notification.get("contactRoles") or [])),
        tuple(sorted(g.lower() for g in notification.get("contactGroups") or [])),
        bool(notification.get("enabled", True)),
    )


def diff_budget(spec: BudgetSpec, existing: BudgetInfo) -> list[str]:
    """Human-readable differences between the desired budget and the one in Azure (empty = in sync)."""
    changes: list[str] = []
    if abs(existing.amount - spec.amount) > 0.005:
        changes.append(f"amount {existing.amount:g} -> {spec.amount:g}")
    if existing.time_grain.lower() != spec.period.lower():
        changes.append(f"period {existing.time_grain} -> {spec.period}")
    if spec.end_date and (existing.end_date or "") != spec.end_date.isoformat():
        changes.append(f"end date {existing.end_date or 'none'} -> {spec.end_date.isoformat()}")

    desired = {_signature(n) for n in build_notifications(spec.alerts).values()}
    current = {_signature(n) for n in existing.notifications.values()}
    if desired != current:
        def describe(sigs: set) -> str:
            return ", ".join(f"{s[2]:g}% {s[0]}" for s in sorted(sigs, key=lambda s: (s[0], s[2])))

        added, removed = desired - current, current - desired
        added_keys = {(s[0], s[2]) for s in added}
        removed_keys = {(s[0], s[2]) for s in removed}
        changed = added_keys & removed_keys
        parts = []
        if added_keys - changed:
            parts.append("add " + describe({s for s in added if (s[0], s[2]) not in changed}))
        if removed_keys - changed:
            parts.append("remove " + describe({s for s in removed if (s[0], s[2]) not in changed}))
        if changed:
            parts.append("update recipients/settings for " + ", ".join(f"{t:g}% {k}" for k, t in sorted(changed)))
        changes.append("alerts: " + "; ".join(parts))
    return changes


# --------------------------------------------------------------------------- status


@dataclass
class ThresholdState:
    threshold: float
    threshold_type: str
    crossed: bool


def threshold_states(info: BudgetInfo) -> list[ThresholdState]:
    states = []
    for notification in info.notifications.values():
        threshold = float(notification.get("threshold") or 0)
        kind = str(notification.get("thresholdType") or "Actual")
        basis = info.forecast_spend if kind.lower() == "forecasted" else info.current_spend
        crossed = False
        if basis is not None and info.amount:
            percent = basis / info.amount * 100
            crossed = percent > threshold if notification.get("operator") == "GreaterThan" else percent >= threshold
        states.append(ThresholdState(threshold, kind, crossed))
    return sorted(states, key=lambda s: (s.threshold_type != "Actual", s.threshold))


def budget_health(usage_percent: float | None) -> str:
    if usage_percent is None:
        return "UNKNOWN"
    if usage_percent >= 100:
        return "EXCEEDED"
    if usage_percent >= 90:
        return "CRITICAL"
    if usage_percent >= 75:
        return "WARNING"
    return "HEALTHY"


# --------------------------------------------------------------------------- service


class BudgetService:
    def __init__(self, consumption_client: Any):
        self.client = consumption_client

    def get(self, scope: str, name: str) -> BudgetInfo | None:
        try:
            return BudgetInfo.from_json(as_json(self.client.budgets.get(scope, name)))
        except Exception as exc:
            if error_status(exc) == 404:
                return None
            raise translate_error(exc, f"Read budget {name}", scope=scope, required_permission=READ_PERMISSION) from None

    def list(self, scope: str) -> list[BudgetInfo]:
        try:
            return [BudgetInfo.from_json(as_json(item)) for item in self.client.budgets.list(scope)]
        except Exception as exc:
            if error_status(exc) == 404:
                return []
            raise translate_error(exc, "List budgets", scope=scope, required_permission=READ_PERMISSION) from None

    def create_or_update(self, scope: str, name: str, body: dict[str, Any], *, user_label: str | None = None) -> BudgetInfo:
        updating = bool(body.get("eTag"))
        try:
            result = BudgetInfo.from_json(as_json(self.client.budgets.create_or_update(scope, name, body)))
        except Exception as exc:
            error = translate_error(
                exc, f"{'Update' if updating else 'Create'} budget {name}", user=user_label, scope=scope,
                required_permission=WRITE_PERMISSION,
            )
            log_event("budget", "failed", resource=name, user=user_label, scope=scope, error=error.reason)
            raise error from None
        log_event("budget", "updated" if updating else "created", resource=result.id or name, user=user_label, scope=scope,
                  details={"amount": body["properties"]["amount"], "time_grain": body["properties"]["timeGrain"],
                           "alerts": sorted(body["properties"]["notifications"])})
        return result
