"""Azure RBAC: role definition resolution, idempotent role assignment and assignment listing.

* Role names are resolved to role definition IDs dynamically (nothing is hard-coded), so any
  built-in or custom role assignable at the target scope can be used.
* Assignments are always created against object IDs (never display names).
* An assignment is "already present" only if the same principal has the same role definition
  at exactly the same scope. An assignment of the same role at a parent scope is reported as
  inherited (it does grant the access, but the requested direct assignment is still created
  so that the configuration is the source of truth). Higher roles are never assumed to imply
  lower ones, and nothing is inferred upwards (Owner on a resource group != Owner on the subscription).
"""

from __future__ import annotations

import difflib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .errors import OperationError, error_code, translate_error
from .logging_utils import log_event
from .rest import as_json
from .scopes import relation
from .validation import is_guid

HIGH_PRIVILEGE_ROLES = {"owner", "user access administrator", "role based access control administrator"}
ASSIGNMENT_DESCRIPTION = "Managed by azure-access-manager"
WRITE_PERMISSION = "Microsoft.Authorization/roleAssignments/write"


def is_high_privilege(role_name: str) -> bool:
    return role_name.strip().lower() in HIGH_PRIVILEGE_ROLES


@dataclass
class RoleDefinition:
    id: str
    guid: str
    name: str
    role_type: str  # BuiltInRole | CustomRole
    description: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "RoleDefinition":
        props = data.get("properties") or {}
        return cls(
            id=data.get("id", ""),
            guid=data.get("name") or data.get("id", "").rsplit("/", 1)[-1],
            name=props.get("roleName", ""),
            role_type=props.get("type", ""),
            description=props.get("description", "") or "",
        )


@dataclass
class RoleAssignment:
    id: str
    name: str
    scope: str
    role_definition_id: str
    principal_id: str
    principal_type: str | None = None
    condition: str | None = None
    description: str | None = None
    created_on: str | None = None

    @property
    def role_guid(self) -> str:
        return self.role_definition_id.rstrip("/").rsplit("/", 1)[-1].lower()

    @classmethod
    def from_json(cls, data: dict) -> "RoleAssignment":
        props = data.get("properties") or {}
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            scope=props.get("scope", ""),
            role_definition_id=props.get("roleDefinitionId", ""),
            principal_id=props.get("principalId", ""),
            principal_type=props.get("principalType"),
            condition=props.get("condition"),
            description=props.get("description"),
            created_on=props.get("createdOn"),
        )


class RbacService:
    def __init__(
        self,
        auth_client: Any,
        subscription_id: str,
        *,
        sleep=time.sleep,
        principal_retry_delays: tuple[int, ...] = (5, 10, 20, 30, 45),
    ):
        self.client = auth_client
        self.subscription_id = subscription_id
        self._sleep = sleep
        self._retry_delays = principal_retry_delays
        self._definitions: dict[str, list[RoleDefinition]] = {}

    # -- role definitions ----------------------------------------------
    def role_definitions(self, scope: str) -> list[RoleDefinition]:
        key = scope.lower()
        if key not in self._definitions:
            try:
                items = self.client.role_definitions.list(scope)
                self._definitions[key] = [RoleDefinition.from_json(as_json(item)) for item in items]
            except Exception as exc:
                raise translate_error(
                    exc, "List role definitions", scope=scope,
                    required_permission="Microsoft.Authorization/roleDefinitions/read",
                ) from None
        return self._definitions[key]

    def resolve_role(self, role: str, scope: str) -> RoleDefinition:
        """Resolve a role name (case-insensitive) or role definition GUID at ``scope``."""
        definitions = self.role_definitions(scope)
        wanted = role.strip().lower()
        for definition in definitions:
            if definition.name.lower() == wanted or definition.guid.lower() == wanted:
                return definition
        by_lower = {d.name.lower(): d.name for d in definitions}
        suggestions = [by_lower[c] for c in difflib.get_close_matches(wanted, list(by_lower), n=3, cutoff=0.6)]
        raise OperationError(
            "Resolve role",
            f"Role '{role}' does not exist or is not assignable at this scope.",
            scope=scope,
            hint=("Did you mean: " + ", ".join(suggestions) + "?") if suggestions else
            "List roles with: az role definition list --query \"[].roleName\" -o tsv",
            code="RoleDefinitionDoesNotExist",
        )

    def role_name(self, role_definition_id: str) -> str:
        guid = role_definition_id.rstrip("/").rsplit("/", 1)[-1].lower()
        for definitions in self._definitions.values():
            for definition in definitions:
                if definition.guid.lower() == guid:
                    return definition.name
        try:
            definitions = self.role_definitions(f"/subscriptions/{self.subscription_id}")
            for definition in definitions:
                if definition.guid.lower() == guid:
                    return definition.name
            data = as_json(self.client.role_definitions.get_by_id(role_definition_id))
            return RoleDefinition.from_json(data).name or guid
        except Exception:
            return guid

    # -- assignments ----------------------------------------------------
    def assignments_for_principal(self, principal_id: str, scope: str) -> list[RoleAssignment]:
        """All assignments of ``principal_id`` at, above or below ``scope``."""
        if not is_guid(principal_id):
            raise OperationError("List role assignments", f"Invalid principal object ID {principal_id!r}")
        try:
            items = self.client.role_assignments.list_for_scope(scope, filter=f"principalId eq '{principal_id}'")
            return [RoleAssignment.from_json(as_json(item)) for item in items]
        except Exception as exc:
            raise translate_error(
                exc, "List role assignments", scope=scope,
                required_permission="Microsoft.Authorization/roleAssignments/read",
            ) from None

    def find_assignment(
        self, principal_id: str, role: RoleDefinition, scope: str
    ) -> tuple[RoleAssignment | None, list[RoleAssignment]]:
        """Return (direct assignment at scope or None, same-role assignments inherited from parents)."""
        direct = None
        inherited: list[RoleAssignment] = []
        for assignment in self.assignments_for_principal(principal_id, scope):
            if assignment.role_guid != role.guid.lower():
                continue
            rel = relation(assignment.scope, scope)
            if rel == "Direct" and not assignment.condition:
                direct = assignment
            elif rel == "Inherited":
                inherited.append(assignment)
        return direct, inherited

    def create_assignment(
        self,
        scope: str,
        role: RoleDefinition,
        principal_id: str,
        *,
        principal_type: str = "User",
        user_label: str | None = None,
    ) -> tuple[RoleAssignment, bool]:
        """Create the assignment. Returns (assignment, created). Idempotent on RoleAssignmentExists."""
        from azure.mgmt.authorization.models import RoleAssignmentCreateParameters

        parameters = RoleAssignmentCreateParameters(
            role_definition_id=role.id,
            principal_id=principal_id,
            # Setting principalType avoids failures caused by Entra ID replication delay for new users.
            principal_type=principal_type,
            description=ASSIGNMENT_DESCRIPTION,
        )
        operation = f"Assign {role.name} role"
        delays = list(self._retry_delays)
        while True:
            try:
                result = self.client.role_assignments.create(scope, str(uuid.uuid4()), parameters)
                assignment = RoleAssignment.from_json(as_json(result))
                log_event("assign_role", "created", resource=assignment.id, user=user_label, scope=scope,
                          details={"role": role.name, "principal_id": principal_id})
                return assignment, True
            except Exception as exc:
                code = error_code(exc)
                if code == "RoleAssignmentExists":
                    direct, _ = self.find_assignment(principal_id, role, scope)
                    if direct:
                        log_event("assign_role", "already_exists", resource=direct.id, user=user_label, scope=scope)
                        return direct, False
                if code == "PrincipalNotFound" and delays:
                    delay = delays.pop(0)
                    log_event("assign_role", "retry", user=user_label, scope=scope,
                              message=f"Principal not yet replicated; retrying in {delay}s")
                    self._sleep(delay)
                    continue
                error = translate_error(exc, operation, user=user_label, scope=scope, required_permission=WRITE_PERMISSION)
                log_event("assign_role", "failed", user=user_label, scope=scope, error=error.reason)
                raise error from None

    def list_for_scope(self, scope: str) -> list[RoleAssignment]:
        """Every assignment at, above (inherited) or below ``scope``."""
        try:
            return [RoleAssignment.from_json(as_json(item)) for item in self.client.role_assignments.list_for_scope(scope)]
        except Exception as exc:
            raise translate_error(
                exc, "List role assignments", scope=scope,
                required_permission="Microsoft.Authorization/roleAssignments/read",
            ) from None

    def list_subscription_assignments(self) -> list[RoleAssignment]:
        """Every assignment that applies within the subscription, including inherited MG/root ones."""
        try:
            return [RoleAssignment.from_json(as_json(item)) for item in self.client.role_assignments.list_for_subscription()]
        except Exception as exc:
            raise translate_error(
                exc, "List role assignments", scope=f"/subscriptions/{self.subscription_id}",
                required_permission="Microsoft.Authorization/roleAssignments/read",
            ) from None
