"""Subscription metadata.

azure-mgmt-resource >= 24 no longer ships SubscriptionClient, so subscription details are
read from the ARM REST endpoint directly (GET /subscriptions/{id}).

Note: Azure subscriptions are global objects and have no region. The closest equivalent,
``subscriptionPolicies.locationPlacementId``, is shown as the subscription "location".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import OperationError, RestError, translate_error
from .rest import RestClient

API_VERSION = "2022-12-01"


@dataclass
class SubscriptionInfo:
    subscription_id: str
    display_name: str
    tenant_id: str | None
    state: str
    location_placement_id: str | None = None
    quota_id: str | None = None
    spending_limit: str | None = None
    authorization_source: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def offer(self) -> str | None:
        """Human-friendly offer derived from the quota ID, e.g. 'PayAsYouGo_2014-09-01' -> 'PayAsYouGo'."""
        return self.quota_id.rsplit("_", 1)[0] if self.quota_id else None

    @property
    def is_enabled(self) -> bool:
        return self.state.lower() == "enabled"

    def as_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "display_name": self.display_name,
            "tenant_id": self.tenant_id,
            "state": self.state,
            "location": self.location_placement_id,
            "offer": self.offer,
            "quota_id": self.quota_id,
            "spending_limit": self.spending_limit,
            "authorization_source": self.authorization_source,
            "tags": self.tags,
        }


class SubscriptionService:
    def __init__(self, arm: RestClient):
        self.arm = arm

    def get(self, subscription_id: str) -> SubscriptionInfo:
        try:
            data = self.arm.get(f"/subscriptions/{subscription_id}", params={"api-version": API_VERSION})
        except RestError as exc:
            if exc.status == 404 or exc.code == "SubscriptionNotFound":
                raise OperationError(
                    "Validate subscription",
                    f"Subscription {subscription_id} was not found, or the signed-in identity has no access to it.",
                    scope=f"/subscriptions/{subscription_id}",
                    hint="Check subscription_id, run 'az account list -o table', or sign in to the right tenant "
                    "with 'az login --tenant <tenant-id>'.",
                    code="SubscriptionNotFound",
                    status=404,
                ) from None
            raise translate_error(
                exc,
                "Validate subscription",
                scope=f"/subscriptions/{subscription_id}",
                required_permission="Microsoft.Resources/subscriptions/read",
            ) from None
        policies = data.get("subscriptionPolicies") or {}
        return SubscriptionInfo(
            subscription_id=data.get("subscriptionId", subscription_id),
            display_name=data.get("displayName", ""),
            tenant_id=data.get("tenantId"),
            state=data.get("state", "Unknown"),
            location_placement_id=policies.get("locationPlacementId"),
            quota_id=policies.get("quotaId"),
            spending_limit=policies.get("spendingLimit"),
            authorization_source=data.get("authorizationSource"),
            tags=data.get("tags") or {},
        )


class ManagementGroupService:
    API_VERSION = "2021-04-01"

    def __init__(self, arm: RestClient):
        self.arm = arm

    def get(self, management_group_id: str) -> dict | None:
        """Return the management group, or None if it does not exist / is not visible."""
        try:
            return self.arm.get(
                f"/providers/Microsoft.Management/managementGroups/{management_group_id}",
                params={"api-version": self.API_VERSION},
            )
        except RestError as exc:
            # Azure returns 403 rather than 404 for management groups the caller cannot see.
            if exc.status in (403, 404):
                return None
            raise translate_error(
                exc, "Read management group",
                scope=f"/providers/Microsoft.Management/managementGroups/{management_group_id}",
                required_permission="Microsoft.Management/managementGroups/read",
            ) from None
