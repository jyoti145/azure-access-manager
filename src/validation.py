"""Validators for user-supplied identifiers. Run before any value is sent to an Azure API."""

from __future__ import annotations

import re

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
# Resource group: 1-90 chars, alphanumerics, underscore, parentheses, hyphen, period; cannot end with period.
RESOURCE_GROUP_RE = re.compile(r"^[-\w._()]{1,90}$", re.UNICODE)
MANAGEMENT_GROUP_RE = re.compile(r"^[A-Za-z0-9_.()-]{1,90}$")
LOCATION_RE = re.compile(r"^[a-z0-9]{2,40}$")
# Consumption budgets: alphanumerics, underscore and hyphen, max 63 chars.
BUDGET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
RESOURCE_ID_RE = re.compile(
    r"^/subscriptions/(?P<sub>[0-9a-fA-F-]{36})/resourceGroups/(?P<rg>[-\w._()]{1,90})"
    r"/providers/(?P<namespace>[A-Za-z0-9.]+)/(?P<rest>[^?#]+)$",
    re.UNICODE,
)
ROLE_NAME_RE = re.compile(r"^[^\x00-\x1f'\"<>]{1,128}$")
TAG_KEY_RE = re.compile(r"^[^<>%&\\?/]{1,512}$")


def is_guid(value: str | None) -> bool:
    return bool(value) and bool(GUID_RE.match(value))


def is_email(value: str | None) -> bool:
    return bool(value) and len(value) <= 254 and bool(EMAIL_RE.match(value))


def is_resource_group_name(value: str | None) -> bool:
    return bool(value) and bool(RESOURCE_GROUP_RE.match(value)) and not value.endswith(".")


def is_management_group_id(value: str | None) -> bool:
    return bool(value) and bool(MANAGEMENT_GROUP_RE.match(value))


def is_budget_name(value: str | None) -> bool:
    return bool(value) and bool(BUDGET_NAME_RE.match(value))


def is_resource_id(value: str | None) -> bool:
    return bool(value) and bool(RESOURCE_ID_RE.match(value))


def is_role_name(value: str | None) -> bool:
    return bool(value) and bool(ROLE_NAME_RE.match(value.strip()))


def normalize_location(value: str | None) -> str | None:
    """'Central India' -> 'centralindia'."""
    if value is None:
        return None
    return re.sub(r"[\s_-]+", "", str(value)).lower()


def is_location(value: str | None) -> bool:
    return bool(value) and bool(LOCATION_RE.match(value))


def odata_quote(value: str) -> str:
    """Escape a value for use inside single quotes in an OData $filter."""
    return value.replace("'", "''")


def kql_quote(value: str) -> str:
    """Escape a value for use inside single quotes in a KQL (Resource Graph) query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")
