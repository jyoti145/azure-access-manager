"""Shared in-memory fakes for Azure services. No test touches the network."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Never write test events into the real audit log in logs/.
os.environ["AZURE_MANAGER_LOG_DIR"] = tempfile.mkdtemp(prefix="aam-test-logs-")
from azure.core.exceptions import HttpResponseError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.budgets import BudgetInfo  # noqa: E402
from src.costs import CostResult  # noqa: E402
from src.errors import OperationError  # noqa: E402
from src.rbac import RbacService  # noqa: E402
from src.resource_groups import ResourceGroupInfo  # noqa: E402
from src.scopes import relation  # noqa: E402
from src.users import DirectoryUser  # noqa: E402

SUB = "11111111-1111-1111-1111-111111111111"
SUB_SCOPE = f"/subscriptions/{SUB}"

ROLES = {
    "Owner": "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
    "Contributor": "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "Reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    "User Access Administrator": "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9",
    "Storage Blob Data Reader": "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1",
}


def http_error(status: int, code: str, message: str = "error") -> HttpResponseError:
    exc = HttpResponseError(message=f"({code}) {message}")
    exc.status_code = status
    return exc


class FakeRoleDefinitions:
    def __init__(self):
        self.list_calls = 0

    def list(self, scope, filter=None):
        self.list_calls += 1
        return [
            {
                "id": f"{scope.rstrip('/')}/providers/Microsoft.Authorization/roleDefinitions/{guid}",
                "name": guid,
                "properties": {"roleName": name, "type": "BuiltInRole"},
            }
            for name, guid in ROLES.items()
        ]

    def get_by_id(self, role_id):
        guid = role_id.rsplit("/", 1)[-1]
        name = next(n for n, g in ROLES.items() if g == guid)
        return {"id": role_id, "name": guid, "properties": {"roleName": name}}


class FakeRoleAssignments:
    def __init__(self):
        self.items: list[dict] = []
        self.create_errors: list[Exception] = []
        self.create_calls = 0

    def add(self, scope, role, principal_id, name="existing-assignment"):
        self.items.append({
            "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}",
            "name": name,
            "properties": {
                "scope": scope,
                "roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{ROLES[role]}",
                "principalId": principal_id,
                "principalType": "User",
            },
        })

    def list_for_scope(self, scope, filter=None):
        result = []
        for item in self.items:
            if filter and item["properties"]["principalId"] not in filter:
                continue
            if relation(item["properties"]["scope"], scope):
                result.append(item)
        return result

    def list_for_subscription(self, filter=None):
        return list(self.items)

    def create(self, scope, name, params):
        self.create_calls += 1
        if self.create_errors:
            raise self.create_errors.pop(0)
        item = {
            "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}",
            "name": name,
            "properties": {
                "scope": scope,
                "roleDefinitionId": params.role_definition_id,
                "principalId": params.principal_id,
                "principalType": params.principal_type,
            },
        }
        self.items.append(item)
        return item


class FakeAuthClient:
    def __init__(self):
        self.role_definitions = FakeRoleDefinitions()
        self.role_assignments = FakeRoleAssignments()


class FakeUsers:
    def __init__(self):
        self.by_email: dict[str, DirectoryUser] = {}
        self.invited: list[str] = []
        self.error: OperationError | None = None

    def add(self, email, object_id, user_type="Member"):
        self.by_email[email.lower()] = DirectoryUser(object_id, email.split("@")[0], email, email, user_type, True)

    def find_by_email(self, email):
        if self.error:
            raise self.error
        return self.by_email.get(email.lower())

    def get_by_id(self, object_id):
        return next((u for u in self.by_email.values() if u.object_id == object_id), None)

    def invite(self, email, display_name, redirect_url, send_message):
        self.invited.append(email)
        user = DirectoryUser("99999999-9999-9999-9999-999999999999", display_name, None, email, "Guest",
                             external_state="PendingAcceptance")
        self.by_email[email.lower()] = user
        return user

    def check_read_access(self):
        return None

    def resolve_principals(self, ids):
        return {}


class FakeResourceGroups:
    def __init__(self):
        self.groups: dict[str, ResourceGroupInfo] = {}
        self.created: list[str] = []

    def add(self, name, location="centralindia"):
        self.groups[name.lower()] = ResourceGroupInfo(name, location, f"{SUB_SCOPE}/resourceGroups/{name}")

    def get(self, name):
        return self.groups.get(name.lower())

    def list(self):
        return list(self.groups.values())

    def create(self, name, location, tags=None):
        self.created.append(name)
        self.add(name, location)
        return self.groups[name.lower()]


class FakeBudgets:
    def __init__(self):
        self.items: dict[tuple[str, str], BudgetInfo] = {}
        self.writes: list[tuple[str, str, dict]] = []

    def get(self, scope, name):
        return self.items.get((scope.lower(), name.lower()))

    def list(self, scope):
        return [b for (s, _), b in self.items.items() if s == scope.lower()]

    def create_or_update(self, scope, name, body, user_label=None):
        self.writes.append((scope, name, body))
        data = dict(body, name=name, id=f"{scope}/providers/Microsoft.Consumption/budgets/{name}", eTag="etag-1")
        info = BudgetInfo.from_json(data)
        self.items[(scope.lower(), name.lower())] = info
        return info


class FakeCosts:
    def __init__(self, currency="INR", by_rg=None):
        self.currency = currency
        self.by_rg = by_rg or {}

    def month_to_date(self, scope=None):
        return CostResult("MonthToDate", sum(self.by_rg.values()), self.currency, dict(self.by_rg))

    def previous_month(self, scope=None):
        return CostResult("TheLastMonth", 0.0, self.currency, {})


class FakeManagementGroups:
    def __init__(self, existing=()):
        self.existing = set(existing)

    def get(self, mg):
        return {"name": mg} if mg in self.existing else None


class FakeResources:
    def __init__(self, existing=()):
        self.existing = {r.lower() for r in existing}

    def exists(self, resource_id):
        return resource_id.lower() in self.existing

    def list(self, resource_group=None):
        return []


class FakeContext:
    def __init__(self):
        self.subscription_id = SUB
        self.subscription_scope = SUB_SCOPE
        self.identity = None
        self.auth_client = FakeAuthClient()
        self.rbac = RbacService(self.auth_client, SUB, sleep=lambda s: None)
        self.users = FakeUsers()
        self.resource_groups = FakeResourceGroups()
        self.budgets = FakeBudgets()
        self.costs = FakeCosts()
        self.management_groups = FakeManagementGroups()
        self.resources = FakeResources()

    def reset_caches(self):
        pass


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext()
