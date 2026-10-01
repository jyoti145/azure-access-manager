"""Lazily-constructed Azure clients and services shared by all commands."""

from __future__ import annotations

from functools import cached_property
from typing import Any

from .auth import ARM_SCOPE, GRAPH_SCOPE, Identity
from .rest import ARM_ENDPOINT, GRAPH_ENDPOINT, RestClient


def _resource_client_class():
    # azure-mgmt-resource >= 24 moved the client into the 'resources' sub-package.
    try:
        from azure.mgmt.resource.resources import ResourceManagementClient
    except ImportError:  # pragma: no cover - older SDK layout
        from azure.mgmt.resource import ResourceManagementClient
    return ResourceManagementClient


class AzureContext:
    def __init__(self, credential: Any, subscription_id: str, identity: Identity | None = None):
        self.credential = credential
        self.subscription_id = subscription_id
        self.identity = identity

    _SERVICES = (
        "subscriptions", "management_groups", "users", "rbac", "resource_groups",
        "budgets", "costs", "resources", "permissions",
    )

    @property
    def subscription_scope(self) -> str:
        return f"/subscriptions/{self.subscription_id}"

    def reset_caches(self) -> None:
        """Drop service-level caches (role definitions, cost results, permissions) so data is re-read."""
        for name in self._SERVICES:
            self.__dict__.pop(name, None)

    # -- raw clients ----------------------------------------------------
    @cached_property
    def arm(self) -> RestClient:
        return RestClient(self.credential, ARM_ENDPOINT, ARM_SCOPE)

    @cached_property
    def graph(self) -> RestClient:
        return RestClient(self.credential, GRAPH_ENDPOINT, GRAPH_SCOPE)

    @cached_property
    def resource_client(self):
        return _resource_client_class()(self.credential, self.subscription_id)

    @cached_property
    def auth_client(self):
        from azure.mgmt.authorization import AuthorizationManagementClient

        return AuthorizationManagementClient(self.credential, self.subscription_id)

    @cached_property
    def consumption_client(self):
        from azure.mgmt.consumption import ConsumptionManagementClient

        return ConsumptionManagementClient(self.credential, self.subscription_id)

    @cached_property
    def cost_client(self):
        from azure.mgmt.costmanagement import CostManagementClient

        return CostManagementClient(self.credential)

    # -- services -------------------------------------------------------
    @cached_property
    def subscriptions(self):
        from .subscriptions import SubscriptionService

        return SubscriptionService(self.arm)

    @cached_property
    def management_groups(self):
        from .subscriptions import ManagementGroupService

        return ManagementGroupService(self.arm)

    @cached_property
    def users(self):
        from .users import UserService

        return UserService(self.graph)

    @cached_property
    def rbac(self):
        from .rbac import RbacService

        return RbacService(self.auth_client, self.subscription_id)

    @cached_property
    def resource_groups(self):
        from .resource_groups import ResourceGroupService

        return ResourceGroupService(self.resource_client, self.subscription_id)

    @cached_property
    def budgets(self):
        from .budgets import BudgetService

        return BudgetService(self.consumption_client)

    @cached_property
    def costs(self):
        from .costs import CostService

        return CostService(self.cost_client, self.subscription_id)

    @cached_property
    def resources(self):
        from .resources import ResourceService

        return ResourceService(self.arm, self.resource_client, self.subscription_id)

    @cached_property
    def permissions(self):
        from .permissions import PermissionService

        return PermissionService(self.arm)
