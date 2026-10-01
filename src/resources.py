"""Resource inventory.

Uses Azure Resource Graph (one query, includes VM power state) and falls back to the
Resource Management API if Resource Graph is unavailable.

Limitation: Azure has no universal "status" for resources. This tool reports, in order of
preference: VM power state (Resource Graph), the resource's own 'state'/'status' property
(e.g. App Service 'Running'), otherwise the ARM provisioning state ('Succeeded', 'Failed').
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import RestError, translate_error
from .logging_utils import get_logger
from .rest import RestClient, as_json
from .validation import kql_quote

RESOURCE_GRAPH_API = "2022-10-01"
READ_PERMISSION = "Microsoft.Resources/subscriptions/resources/read"


@dataclass
class ResourceInfo:
    name: str
    type: str
    resource_group: str
    location: str
    status: str
    id: str
    kind: str | None = None
    sku: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "resource_group": self.resource_group,
            "location": self.location,
            "status": self.status,
            "kind": self.kind,
            "sku": self.sku,
            "id": self.id,
        }


def _status(provisioning: str | None, power: str | None, state: str | None) -> str:
    if power:
        return power.split("/", 1)[-1].capitalize()  # "PowerState/running" -> "Running"
    if state:
        return str(state)
    return provisioning or "Unknown"


def _rg_from_id(resource_id: str) -> str:
    parts = resource_id.split("/")
    for index, part in enumerate(parts):
        if part.lower() == "resourcegroups" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


class ResourceService:
    def __init__(self, arm: RestClient, resource_client: Any, subscription_id: str):
        self.arm = arm
        self.client = resource_client
        self.subscription_id = subscription_id

    def list(self, resource_group: str | None = None) -> list[ResourceInfo]:
        try:
            resources = self._list_resource_graph(resource_group)
        except RestError as exc:
            get_logger().debug("Resource Graph unavailable (%s); falling back to ARM resource listing", exc)
            resources = self._list_arm(resource_group)
        return sorted(resources, key=lambda r: (r.resource_group.lower(), r.type.lower(), r.name.lower()))

    def exists(self, resource_id: str) -> bool:
        query = f"resources | where id =~ '{kql_quote(resource_id)}' | project id"
        try:
            data = self.arm.post(
                "/providers/Microsoft.ResourceGraph/resources",
                params={"api-version": RESOURCE_GRAPH_API},
                json={"subscriptions": [self.subscription_id], "query": query},
            )
        except RestError as exc:
            raise translate_error(exc, "Validate resource", scope=resource_id, required_permission=READ_PERMISSION) from None
        return bool((data or {}).get("data"))

    def _list_resource_graph(self, resource_group: str | None) -> list[ResourceInfo]:
        query = "resources"
        if resource_group:
            query += f" | where resourceGroup =~ '{kql_quote(resource_group)}'"
        query += (
            " | extend powerState = tostring(properties.extended.instanceView.powerState.code),"
            " provisioningState = tostring(properties.provisioningState),"
            " state = tostring(coalesce(properties.state, properties.status))"
            " | project id, name, type, resourceGroup, location, kind, skuName = tostring(sku.name),"
            " powerState, provisioningState, state"
        )
        results: list[ResourceInfo] = []
        skip_token = None
        while True:
            options: dict[str, Any] = {"resultFormat": "objectArray", "$top": 1000}
            if skip_token:
                options["$skipToken"] = skip_token
            data = self.arm.post(
                "/providers/Microsoft.ResourceGraph/resources",
                params={"api-version": RESOURCE_GRAPH_API},
                json={"subscriptions": [self.subscription_id], "query": query, "options": options},
            ) or {}
            for row in data.get("data") or []:
                results.append(
                    ResourceInfo(
                        name=row.get("name", ""),
                        type=row.get("type", ""),
                        resource_group=_rg_from_id(row.get("id", "")) or row.get("resourceGroup", ""),
                        location=row.get("location", ""),
                        status=_status(row.get("provisioningState"), row.get("powerState"), row.get("state")),
                        id=row.get("id", ""),
                        kind=row.get("kind") or None,
                        sku=row.get("skuName") or None,
                    )
                )
            skip_token = data.get("$skipToken")
            if not skip_token:
                return results

    def _list_arm(self, resource_group: str | None) -> list[ResourceInfo]:
        try:
            if resource_group:
                items = self.client.resources.list_by_resource_group(resource_group, expand="provisioningState")
            else:
                items = self.client.resources.list(expand="provisioningState")
            results = []
            for item in items:
                data = as_json(item)
                sku = data.get("sku") or {}
                results.append(
                    ResourceInfo(
                        name=data.get("name", ""),
                        type=data.get("type", ""),
                        resource_group=_rg_from_id(data.get("id", "")),
                        location=data.get("location", ""),
                        status=_status(data.get("provisioningState") or (data.get("properties") or {}).get("provisioningState"), None, None),
                        id=data.get("id", ""),
                        kind=data.get("kind"),
                        sku=sku.get("name") if isinstance(sku, dict) else None,
                    )
                )
            return results
        except Exception as exc:
            raise translate_error(exc, "List resources", scope=f"/subscriptions/{self.subscription_id}",
                                  required_permission=READ_PERMISSION) from None
