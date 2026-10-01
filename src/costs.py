"""Actual cost from the Cost Management Query API.

Limitations (Azure-side):
* Cost data typically lags usage by 8-24 hours.
* Some offers (e.g. certain sponsorship / free / CSP-without-access subscriptions) do not
  expose Cost Management data; these return an error and are reported as 'N/A'.
* The Query API is heavily throttled; this module issues one grouped query per period
  (not one per resource group) and caches results for the lifetime of the process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .errors import error_status, translate_error
from .logging_utils import get_logger
from .rest import as_json

READ_PERMISSION = "Microsoft.CostManagement/query/read"
_COST_COLUMNS = ("PreTaxCost", "Cost", "CostUSD", "PreTaxCostUSD")
NO_RESOURCE_GROUP = "(no resource group)"


@dataclass
class CostResult:
    timeframe: str
    total: float
    currency: str | None
    by_resource_group: dict[str, float] = field(default_factory=dict)  # lower-case RG name -> cost
    truncated: bool = False

    def for_resource_group(self, name: str) -> float:
        return self.by_resource_group.get(name.lower(), 0.0)


def parse_query_result(data: dict, timeframe: str) -> CostResult:
    props = (data or {}).get("properties") or {}
    columns = [c.get("name") for c in props.get("columns") or []]
    rows = props.get("rows") or []
    cost_index = next((columns.index(c) for c in _COST_COLUMNS if c in columns), None)
    rg_index = next((columns.index(c) for c in ("ResourceGroupName", "ResourceGroup") if c in columns), None)
    currency_index = columns.index("Currency") if "Currency" in columns else None

    total = 0.0
    currency = None
    by_rg: dict[str, float] = {}
    if cost_index is not None:
        for row in rows:
            amount = float(row[cost_index] or 0)
            total += amount
            if currency_index is not None and row[currency_index]:
                currency = str(row[currency_index])
            if rg_index is not None:
                rg = str(row[rg_index] or "").lower() or NO_RESOURCE_GROUP
                by_rg[rg] = by_rg.get(rg, 0.0) + amount
    return CostResult(
        timeframe=timeframe,
        total=round(total, 2),
        currency=currency,
        by_resource_group={k: round(val, 2) for k, val in by_rg.items()},
        truncated=bool(props.get("nextLink")),
    )


_RETRY_HEADERS = (
    "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
    "Retry-After",
)


def throttle_delay(exc: BaseException, attempt: int) -> float:
    """Seconds to wait after a 429, from Cost Management's own headers (the SDK ignores them)."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    waits = []
    for name in _RETRY_HEADERS:
        try:
            waits.append(float(headers.get(name)))
        except (TypeError, ValueError):
            pass
    return min(max(waits) if waits else 10.0 * attempt, 60.0)


class CostService:
    MAX_THROTTLE_RETRIES = 3

    def __init__(self, cost_client: Any, subscription_id: str, *, sleep=time.sleep):
        self.client = cost_client
        self.subscription_id = subscription_id
        self._cache: dict[tuple, CostResult] = {}
        self._aggregation_column = "PreTaxCost"
        self._sleep = sleep

    def _usage(self, scope: str, body: dict[str, Any]) -> Any:
        """Query API call that waits out Cost Management throttling (HTTP 429)."""
        attempt = 0
        while True:
            try:
                return self.client.query.usage(scope, body)
            except Exception as exc:
                if error_status(exc) != 429 or attempt >= self.MAX_THROTTLE_RETRIES:
                    raise
                attempt += 1
                delay = throttle_delay(exc, attempt)
                get_logger().info("Cost Management throttled the request; retrying in %.0fs (attempt %s)", delay, attempt)
                self._sleep(delay)

    @property
    def subscription_scope(self) -> str:
        return f"/subscriptions/{self.subscription_id}"

    def query(self, timeframe: str, *, scope: str | None = None, group_by_resource_group: bool = True) -> CostResult:
        scope = scope or self.subscription_scope
        key = (scope.lower(), timeframe, group_by_resource_group)
        if key in self._cache:
            return self._cache[key]
        result = None
        for column in (self._aggregation_column, "Cost"):
            body: dict[str, Any] = {
                "type": "ActualCost",
                "timeframe": timeframe,
                "dataset": {"aggregation": {"totalCost": {"name": column, "function": "Sum"}}},
            }
            if group_by_resource_group:
                body["dataset"]["grouping"] = [{"type": "Dimension", "name": "ResourceGroupName"}]
            try:
                result = parse_query_result(as_json(self._usage(scope, body)), timeframe)
                self._aggregation_column = column
                break
            except Exception as exc:
                # Some billing account types only accept 'Cost' instead of 'PreTaxCost'.
                if error_status(exc) == 400 and column != "Cost":
                    continue
                raise translate_error(exc, "Query cost data", scope=scope, required_permission=READ_PERMISSION) from None
        self._cache[key] = result
        return result

    def month_to_date(self, scope: str | None = None) -> CostResult:
        return self.query("MonthToDate", scope=scope)

    def previous_month(self, scope: str | None = None) -> CostResult:
        return self.query("TheLastMonth", scope=scope)
