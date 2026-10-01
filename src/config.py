"""Load and validate config.yaml / config.json into typed models.

All problems are collected and reported together, so an operator can fix the
whole file in one pass instead of one error at a time.
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from . import validation as v
from .errors import ConfigError
from .models import AccessSpec, AlertSpec, AppConfig, BudgetSpec, ReportSpec, ResourceGroupSpec, ScopeType, UserSpec
from .scopes import parse_scope

DEFAULT_THRESHOLDS = [50, 75, 90, 100]
MAX_NOTIFICATIONS_PER_BUDGET = 5  # Azure Cost Management limit

_SCOPE_ALIASES = {
    "subscription": ScopeType.SUBSCRIPTION,
    "sub": ScopeType.SUBSCRIPTION,
    "resourcegroup": ScopeType.RESOURCE_GROUP,
    "rg": ScopeType.RESOURCE_GROUP,
    "managementgroup": ScopeType.MANAGEMENT_GROUP,
    "mg": ScopeType.MANAGEMENT_GROUP,
    "resource": ScopeType.RESOURCE,
}

_PERIODS = {
    "monthly": "Monthly",
    "month": "Monthly",
    "quarterly": "Quarterly",
    "quarter": "Quarterly",
    "annually": "Annually",
    "annual": "Annually",
    "yearly": "Annually",
    "year": "Annually",
}

_THRESHOLD_TYPES = {"actual": "Actual", "forecast": "Forecasted", "forecasted": "Forecasted"}
_OPERATORS = {"greaterthan": "GreaterThan", "greaterthanorequalto": "GreaterThanOrEqualTo", "equalto": "EqualTo"}

_FORBIDDEN_KEYS = {
    "password",
    "clientsecret",
    "secret",
    "accesstoken",
    "refreshtoken",
    "token",
    "azureclientsecret",
    "certificatepassword",
}


def _key(text: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(text)).lower()


def _as_bool(value: Any, where: str, problems: list[str], default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "1", "on"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "0", "off"):
        return False
    problems.append(f"{where}: expected true/false, got {value!r}")
    return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _as_date(value: Any, where: str, problems: list[str]) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        problems.append(f"{where}: invalid date {value!r} (use YYYY-MM-DD)")
        return None


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _find_secrets(node: Any, path: str = "") -> list[str]:
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if _key(key) in _FORBIDDEN_KEYS:
                found.append(here)
            found.extend(_find_secrets(value, here))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_find_secrets(item, f"{path}[{index}]"))
    return found


def _sanitize_budget_name(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]", "-", text)
    return name[:63].strip("-") or "budget"


# --------------------------------------------------------------------------- loading


def load_raw(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(
    path: str | Path,
    *,
    subscription_override: str | None = None,
    tenant_override: str | None = None,
) -> AppConfig:
    raw = load_raw(path)
    return parse_config(
        raw, source=str(path), subscription_override=subscription_override, tenant_override=tenant_override
    )


def resolve_subscription_settings(
    path: str | Path | None,
    *,
    subscription_override: str | None = None,
    tenant_override: str | None = None,
) -> tuple[str, str | None]:
    """Subscription/tenant for read-only commands. Does not require a valid users section."""
    raw: dict = {}
    if path and Path(path).exists():
        try:
            raw = load_raw(path)
        except ConfigError:
            raw = {}
    subscription_id = subscription_override or raw.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID")
    tenant_id = tenant_override or raw.get("tenant_id") or None
    if not subscription_id:
        raise ConfigError(
            "No subscription specified. Use --subscription, set subscription_id in config.yaml, "
            "or set AZURE_SUBSCRIPTION_ID."
        )
    subscription_id = str(subscription_id).strip()
    if not v.is_guid(subscription_id):
        raise ConfigError(f"subscription_id must be a GUID, got {subscription_id!r}")
    if tenant_id and not v.is_guid(str(tenant_id)):
        raise ConfigError(f"tenant_id must be a GUID, got {tenant_id!r}")
    return subscription_id, (str(tenant_id) if tenant_id else None)


# --------------------------------------------------------------------------- parsing


class _Defaults:
    def __init__(self, raw: dict, problems: list[str]):
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            problems.append("defaults: must be a mapping")
            defaults = {}
        self.location = v.normalize_location(defaults.get("location"))
        self.invite_if_missing = _as_bool(
            defaults.get("invite_if_missing", raw.get("invite_if_missing")), "defaults.invite_if_missing", problems
        )
        self.send_invitation_email = _as_bool(
            defaults.get("send_invitation_email", raw.get("send_invitation_email")),
            "defaults.send_invitation_email",
            problems,
            default=True,
        )
        self.invite_redirect_url = str(
            defaults.get("invite_redirect_url") or raw.get("invite_redirect_url") or "https://myapplications.microsoft.com"
        )
        budget = defaults.get("budget") or {}
        self.budget = budget if isinstance(budget, dict) else {}


def parse_config(
    raw: dict,
    *,
    source: str | None = None,
    subscription_override: str | None = None,
    tenant_override: str | None = None,
) -> AppConfig:
    problems: list[str] = []

    for secret_path in _find_secrets(raw):
        problems.append(
            f"{secret_path}: credentials must not be stored in the config file. "
            "Use 'az login' or AZURE_CLIENT_ID / AZURE_CLIENT_SECRET environment variables."
        )

    subscription_id = str(
        subscription_override or raw.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID") or ""
    ).strip()
    if not subscription_id:
        problems.append("subscription_id: required (or pass --subscription / set AZURE_SUBSCRIPTION_ID)")
    elif not v.is_guid(subscription_id):
        problems.append(f"subscription_id: must be a GUID like 00000000-0000-0000-0000-000000000000, got {subscription_id!r}")

    tenant_id = tenant_override or raw.get("tenant_id")
    if tenant_id is not None:
        tenant_id = str(tenant_id).strip()
        if not v.is_guid(tenant_id):
            problems.append(f"tenant_id: must be a GUID, got {tenant_id!r}")

    defaults = _Defaults(raw, problems)

    users_raw = raw.get("users") or []
    if not isinstance(users_raw, list):
        problems.append("users: must be a list")
        users_raw = []

    users: list[UserSpec] = []
    for index, user_raw in enumerate(users_raw):
        where = f"users[{index}]"
        if not isinstance(user_raw, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        user = _parse_user(user_raw, where, defaults, problems, subscription_id)
        if user:
            users.append(user)

    _cross_validate(users, problems)

    report_raw = raw.get("subscription_report") or {}
    if not isinstance(report_raw, dict):
        report_raw = {"enabled": report_raw}
    report = ReportSpec(
        enabled=_as_bool(report_raw.get("enabled"), "subscription_report.enabled", problems, default=True),
        include_resources=_as_bool(report_raw.get("include_resources"), "subscription_report.include_resources", problems, default=True),
        include_costs=_as_bool(report_raw.get("include_costs"), "subscription_report.include_costs", problems, default=True),
        include_rbac=_as_bool(report_raw.get("include_rbac"), "subscription_report.include_rbac", problems, default=True),
    )

    if problems:
        raise ConfigError(problems)
    return AppConfig(subscription_id=subscription_id, tenant_id=tenant_id, users=users, report=report, source_path=source)


def _parse_user(raw: dict, where: str, defaults: _Defaults, problems: list[str], subscription_id: str) -> UserSpec | None:
    name = str(raw.get("name") or raw.get("display_name") or "").strip()
    email = str(raw.get("email") or "").strip()
    object_id = raw.get("object_id")
    object_id = str(object_id).strip() if object_id else None

    if not email and not object_id:
        problems.append(f"{where}: 'email' (or 'object_id') is required")
        return None
    if email and not v.is_email(email):
        problems.append(f"{where}.email: invalid email address {email!r}")
    if object_id and not v.is_guid(object_id):
        problems.append(f"{where}.object_id: must be a GUID")

    redirect = str(raw.get("invite_redirect_url") or defaults.invite_redirect_url)
    if not redirect.lower().startswith("https://"):
        problems.append(f"{where}.invite_redirect_url: must be an https:// URL")

    user = UserSpec(
        name=name or email,
        email=email,
        object_id=object_id,
        invite_if_missing=_as_bool(raw.get("invite_if_missing"), f"{where}.invite_if_missing", problems, defaults.invite_if_missing),
        invite_redirect_url=redirect,
        send_invitation_email=_as_bool(
            raw.get("send_invitation_email"), f"{where}.send_invitation_email", problems, defaults.send_invitation_email
        ),
    )

    user.resource_group = _parse_resource_group(raw.get("resource_group"), f"{where}.resource_group", defaults, problems)

    access_raw = raw.get("access")
    access_items = access_raw if isinstance(access_raw, list) else ([access_raw] if access_raw else [])
    for index, item in enumerate(access_items):
        label = f"{where}.access" + (f"[{index}]" if isinstance(access_raw, list) else "")
        access = _parse_access(item, label, user.resource_group, problems, subscription_id)
        if access:
            user.access.append(access)

    budget_raw = raw.get("budget")
    if budget_raw is not None or defaults.budget.get("enabled"):
        if budget_raw is not None and not isinstance(budget_raw, dict):
            problems.append(f"{where}.budget: must be a mapping")
        else:
            merged = _deep_merge(defaults.budget, budget_raw or {})
            user.budget = _parse_budget(merged, f"{where}.budget", user, problems)
    return user


def _parse_resource_group(raw: Any, where: str, defaults: _Defaults, problems: list[str]) -> ResourceGroupSpec | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a name or a mapping")
        return None
    name = str(raw.get("name") or "").strip()
    if not v.is_resource_group_name(name):
        problems.append(f"{where}.name: invalid resource group name {name!r}")
    location = v.normalize_location(raw.get("location")) or defaults.location
    if location and not v.is_location(location):
        problems.append(f"{where}.location: invalid Azure region {raw.get('location')!r}")
    create = _as_bool(raw.get("create_if_missing"), f"{where}.create_if_missing", problems)
    if create and not location:
        problems.append(f"{where}.location: required when create_if_missing is true (or set defaults.location)")
    tags = raw.get("tags") or {}
    if not isinstance(tags, dict):
        problems.append(f"{where}.tags: must be a mapping")
        tags = {}
    for key in tags:
        if not v.TAG_KEY_RE.match(str(key)):
            problems.append(f"{where}.tags: invalid tag name {key!r}")
    return ResourceGroupSpec(
        name=name, location=location, create_if_missing=create, tags={str(k): str(val) for k, val in tags.items()}
    )


def _parse_access(
    raw: Any, where: str, rg_spec: ResourceGroupSpec | None, problems: list[str], subscription_id: str
) -> AccessSpec | None:
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a mapping")
        return None
    role = str(raw.get("role") or "").strip()
    if not role:
        problems.append(f"{where}.role: required (e.g. Reader, Contributor, Owner)")
    elif not (v.is_role_name(role) or v.is_guid(role)):
        problems.append(f"{where}.role: invalid role name {role!r}")

    scope_type_raw = raw.get("scope_type")
    scope_raw = raw.get("scope")
    scope_id = None
    scope_name = None

    if isinstance(scope_raw, str) and scope_raw.startswith("/"):
        scope_id = scope_raw.strip()
    elif scope_raw is not None:
        if scope_type_raw is None:
            scope_type_raw = scope_raw  # e.g. scope: "resource_group"
        else:
            scope_name = str(scope_raw).strip()  # e.g. scope_type: resource_group, scope: rg-dev

    scope_type: ScopeType | None = None
    if scope_id:
        try:
            parsed = parse_scope(scope_id)
            scope_type = parsed.type
            if parsed.type == ScopeType.TENANT:
                problems.append(f"{where}.scope: tenant root ('/') assignments are not supported by this tool")
            if parsed.subscription_id and subscription_id and parsed.subscription_id.lower() != subscription_id.lower():
                problems.append(f"{where}.scope: scope belongs to a different subscription than subscription_id")
        except ValueError as exc:
            problems.append(f"{where}.scope: {exc}")
            return None
    elif scope_type_raw is not None:
        scope_type = _SCOPE_ALIASES.get(_key(scope_type_raw))
        if scope_type is None:
            problems.append(
                f"{where}.scope_type: unknown scope type {scope_type_raw!r} "
                "(use management_group, subscription, resource_group or resource)"
            )
            return None
    elif raw.get("resource_group") or rg_spec:
        scope_type = ScopeType.RESOURCE_GROUP
    elif raw.get("resource_id"):
        scope_type = ScopeType.RESOURCE
    elif raw.get("management_group_id") or raw.get("management_group"):
        scope_type = ScopeType.MANAGEMENT_GROUP
    else:
        # Never default to subscription: that would silently broaden access.
        problems.append(f"{where}.scope_type: required (subscription, resource_group, management_group or resource)")
        return None

    access = AccessSpec(role=role, scope_type=scope_type, scope_id=scope_id, description=raw.get("description"))

    if scope_id:
        parsed = parse_scope(scope_id)
        access.resource_group = parsed.resource_group
        access.management_group_id = parsed.management_group
        access.resource_id = scope_id if parsed.type == ScopeType.RESOURCE else None
        return access

    if scope_type == ScopeType.RESOURCE_GROUP:
        rg = raw.get("resource_group") or scope_name or (rg_spec.name if rg_spec else None)
        if isinstance(rg, dict):
            rg = rg.get("name")
        if not v.is_resource_group_name(str(rg or "")):
            problems.append(f"{where}.resource_group: a valid resource group name is required for resource_group scope")
        access.resource_group = str(rg) if rg else None
    elif scope_type == ScopeType.MANAGEMENT_GROUP:
        mg = raw.get("management_group_id") or raw.get("management_group") or scope_name
        if not v.is_management_group_id(str(mg or "")):
            problems.append(f"{where}.management_group_id: a valid management group ID is required")
        access.management_group_id = str(mg) if mg else None
    elif scope_type == ScopeType.RESOURCE:
        rid = str(raw.get("resource_id") or scope_name or "").strip()
        if not v.is_resource_id(rid):
            problems.append(
                f"{where}.resource_id: a full resource ID is required, e.g. "
                "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<name>"
            )
        elif subscription_id and v.RESOURCE_ID_RE.match(rid).group("sub").lower() != subscription_id.lower():
            problems.append(f"{where}.resource_id: resource belongs to a different subscription than subscription_id")
        access.resource_id = rid or None
        if rid and v.is_resource_id(rid):
            access.resource_group = v.RESOURCE_ID_RE.match(rid).group("rg")
    elif scope_type == ScopeType.SUBSCRIPTION:
        other = raw.get("subscription_id")
        if other and str(other).lower() != subscription_id.lower():
            problems.append(f"{where}.subscription_id: only the configured subscription is supported")
    return access


def _parse_budget(raw: dict, where: str, user: UserSpec, problems: list[str]) -> BudgetSpec | None:
    enabled = _as_bool(raw.get("enabled"), f"{where}.enabled", problems, default=True)
    if not enabled:
        return None

    amount = raw.get("amount")
    try:
        amount = float(amount)
        if not (0 < amount < 1e12):
            raise ValueError
    except (TypeError, ValueError):
        problems.append(f"{where}.amount: must be a positive number, got {raw.get('amount')!r}")
        amount = 0.0

    period_raw = str(raw.get("period") or raw.get("time_grain") or "monthly")
    period = _PERIODS.get(_key(period_raw))
    if not period:
        problems.append(f"{where}.period: must be monthly, quarterly or annually, got {period_raw!r}")
        period = "Monthly"

    currency = raw.get("currency")
    if currency is not None:
        currency = str(currency).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            problems.append(f"{where}.currency: must be a 3-letter ISO code like INR or USD")

    scope_kind = _SCOPE_ALIASES.get(_key(raw.get("scope") or "resource_group"))
    if scope_kind not in (ScopeType.RESOURCE_GROUP, ScopeType.SUBSCRIPTION):
        problems.append(f"{where}.scope: must be resource_group or subscription")
        scope_kind = ScopeType.RESOURCE_GROUP

    resource_group = None
    if scope_kind == ScopeType.RESOURCE_GROUP:
        resource_group = raw.get("resource_group") or (user.resource_group.name if user.resource_group else None)
        if not resource_group:
            resource_group = next((a.resource_group for a in user.access if a.scope_type == ScopeType.RESOURCE_GROUP), None)
        if not resource_group:
            problems.append(
                f"{where}: no resource group to attach the budget to. Add a resource_group for the user, "
                "or set budget.scope: subscription"
            )
        elif not v.is_resource_group_name(str(resource_group)):
            problems.append(f"{where}.resource_group: invalid resource group name {resource_group!r}")

    name = raw.get("name")
    if name:
        name = str(name).strip()
        if not v.is_budget_name(name):
            problems.append(f"{where}.name: budget names may only contain letters, digits, '_' and '-' (max 63)")
    else:
        name = _sanitize_budget_name(f"budget-{resource_group}" if resource_group else "budget-subscription")

    notifications = raw.get("notifications") or {}
    if not isinstance(notifications, dict):
        problems.append(f"{where}.notifications: must be a mapping")
        notifications = {}
    notify = _as_bool(notifications.get("enabled"), f"{where}.notifications.enabled", problems, default=True)
    emails = [str(e).strip() for e in _as_list(notifications.get("emails") or notifications.get("contact_emails"))]
    roles = [str(r).strip() for r in _as_list(notifications.get("roles") or notifications.get("contact_roles"))]
    action_groups = [str(g).strip() for g in _as_list(notifications.get("action_groups"))]
    locale = notifications.get("locale")
    for email in emails:
        if not v.is_email(email):
            problems.append(f"{where}.notifications.emails: invalid email {email!r}")
    for group in action_groups:
        if not group.lower().startswith("/subscriptions/") or "/actiongroups/" not in group.lower():
            problems.append(f"{where}.notifications.action_groups: expected an action group resource ID, got {group!r}")

    defaulted = False
    if notify and not (emails or roles or action_groups) and user.email:
        emails = [user.email]
        defaulted = True

    alerts: list[AlertSpec] = []
    if notify:
        thresholds = raw.get("thresholds", DEFAULT_THRESHOLDS)
        entries = [(t, "Actual") for t in _as_list(thresholds)]
        entries += [(t, "Forecasted") for t in _as_list(raw.get("forecast_thresholds"))]
        seen: set[tuple[str, float]] = set()
        for item, default_type in entries:
            alert = _parse_alert(item, default_type, f"{where}.thresholds", emails, roles, action_groups, locale, problems)
            if alert is None:
                continue
            key = (alert.threshold_type, alert.threshold)
            if key in seen:
                continue
            seen.add(key)
            if not (alert.emails or alert.roles or alert.action_groups):
                problems.append(f"{where}: threshold {alert.threshold:g}% has no notification recipients")
            alerts.append(alert)
        if len(alerts) > MAX_NOTIFICATIONS_PER_BUDGET:
            problems.append(
                f"{where}: {len(alerts)} alerts configured but Azure allows at most "
                f"{MAX_NOTIFICATIONS_PER_BUDGET} notifications per budget"
            )

    start_date = _as_date(raw.get("start_date"), f"{where}.start_date", problems)
    end_date = _as_date(raw.get("end_date"), f"{where}.end_date", problems)
    if start_date and start_date.day != 1:
        problems.append(f"{where}.start_date: must be the first day of a month")
    if start_date and end_date and end_date <= start_date:
        problems.append(f"{where}.end_date: must be after start_date")

    return BudgetSpec(
        enabled=True,
        name=name,
        amount=amount,
        period=period,
        currency=currency,
        alerts=alerts,
        scope_kind=scope_kind,
        resource_group=str(resource_group) if resource_group else None,
        start_date=start_date,
        end_date=end_date,
        recipients_defaulted=defaulted,
    )


def _parse_alert(
    item: Any,
    default_type: str,
    where: str,
    emails: list[str],
    roles: list[str],
    action_groups: list[str],
    locale: Any,
    problems: list[str],
) -> AlertSpec | None:
    threshold_type = default_type
    operator = "GreaterThanOrEqualTo"
    alert_emails, alert_roles, alert_groups = emails, roles, action_groups
    if isinstance(item, dict):
        value = item.get("threshold", item.get("value"))
        if item.get("type") is not None:
            threshold_type = _THRESHOLD_TYPES.get(_key(item["type"]), "")
            if not threshold_type:
                problems.append(f"{where}: type must be actual or forecasted, got {item['type']!r}")
                return None
        if item.get("operator") is not None:
            operator = _OPERATORS.get(_key(item["operator"]), "")
            if not operator:
                problems.append(f"{where}: operator must be GreaterThan, GreaterThanOrEqualTo or EqualTo")
                return None
        if item.get("emails") is not None:
            alert_emails = [str(e).strip() for e in _as_list(item["emails"])]
            for email in alert_emails:
                if not v.is_email(email):
                    problems.append(f"{where}: invalid email {email!r}")
        if item.get("roles") is not None:
            alert_roles = [str(r) for r in _as_list(item["roles"])]
    else:
        value = item
    try:
        threshold = float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        problems.append(f"{where}: threshold must be a number, got {value!r}")
        return None
    if not (0 < threshold <= 1000):
        problems.append(f"{where}: threshold must be between 0 and 1000 percent, got {threshold:g}")
        return None
    return AlertSpec(
        threshold=threshold,
        threshold_type=threshold_type,
        operator=operator,
        emails=tuple(dict.fromkeys(alert_emails)),
        roles=tuple(dict.fromkeys(alert_roles)),
        action_groups=tuple(dict.fromkeys(alert_groups)),
        locale=str(locale) if locale else None,
    )


def _cross_validate(users: list[UserSpec], problems: list[str]) -> None:
    """Detect conflicts between users that would make setup non-deterministic."""
    rg_locations: dict[str, tuple[str | None, str]] = {}
    for user in users:
        rg = user.resource_group
        if rg and rg.location:
            prior = rg_locations.get(rg.name.lower())
            if prior and prior[0] and prior[0] != rg.location:
                problems.append(
                    f"resource group {rg.name!r} has conflicting locations: {prior[0]} ({prior[1]}) vs {rg.location} ({user.email})"
                )
            else:
                rg_locations[rg.name.lower()] = (rg.location, user.email)

    budgets: dict[tuple, tuple] = {}
    for user in users:
        b = user.budget
        if not b:
            continue
        key = (b.scope_kind, (b.resource_group or "").lower(), b.name.lower())
        signature = (b.amount, b.period, tuple(sorted((a.threshold_type, a.threshold, a.emails) for a in b.alerts)))
        prior = budgets.get(key)
        if prior and prior[0] != signature:
            problems.append(
                f"budget {b.name!r} on {b.resource_group or 'subscription'} is defined differently for "
                f"{prior[1]} and {user.email}; give one of them a different budget name"
            )
        else:
            budgets[key] = (signature, user.email)
