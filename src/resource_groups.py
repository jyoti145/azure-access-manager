"""Resource group lookup and creation (never deletion)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import error_status, translate_error
from .logging_utils import log_event
from .rest import as_json

READ_PERMISSION = "Microsoft.Resources/subscriptions/resourceGroups/read"
WRITE_PERMISSION = "Microsoft.Resources/subscriptions/resourceGroups/write"


@dataclass
class ResourceGroupInfo:
    name: str
    location: str
    id: str
    provisioning_state: str | None = None
    managed_by: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> "ResourceGroupInfo":
        return cls(
            name=data.get("name", ""),
            location=data.get("location", ""),
            id=data.get("id", ""),
            provisioning_state=(data.get("properties") or {}).get("provisioningState"),
            managed_by=data.get("managedBy"),
            tags=data.get("tags") or {},
        )


class ResourceGroupService:
    def __init__(self, resource_client: Any, subscription_id: str):
        self.client = resource_client
        self.subscription_id = subscription_id

    def get(self, name: str) -> ResourceGroupInfo | None:
        try:
            return ResourceGroupInfo.from_json(as_json(self.client.resource_groups.get(name)))
        except Exception as exc:
            if error_status(exc) == 404:
                return None
            raise translate_error(
                exc, "Read resource group", scope=self._scope(name), required_permission=READ_PERMISSION
            ) from None

    def list(self) -> list[ResourceGroupInfo]:
        try:
            return sorted(
                (ResourceGroupInfo.from_json(as_json(item)) for item in self.client.resource_groups.list()),
                key=lambda rg: rg.name.lower(),
            )
        except Exception as exc:
            raise translate_error(
                exc, "List resource groups", scope=f"/subscriptions/{self.subscription_id}",
                required_permission=READ_PERMISSION,
            ) from None

    def create(self, name: str, location: str, tags: dict[str, str] | None = None) -> ResourceGroupInfo:
        body: dict[str, Any] = {"location": location}
        if tags:
            body["tags"] = dict(tags)
        try:
            result = ResourceGroupInfo.from_json(as_json(self.client.resource_groups.create_or_update(name, body)))
        except Exception as exc:
            error = translate_error(
                exc, f"Create resource group {name}", scope=self._scope(name), required_permission=WRITE_PERMISSION
            )
            log_event("create_resource_group", "failed", resource=name, scope=self._scope(name), error=error.reason)
            raise error from None
        log_event("create_resource_group", "created", resource=result.id, scope=self._scope(name),
                  details={"location": location})
        return result

    def _scope(self, name: str) -> str:
        return f"/subscriptions/{self.subscription_id}/resourceGroups/{name}"
