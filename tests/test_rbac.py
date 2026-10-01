import pytest
import yaml

from src.config import parse_config
from src.errors import OperationError
from src.models import ActionKind
from src.rbac import RoleAssignment
from src.rest import as_json
from src.setup_engine import SetupEngine

from conftest import ROLES, SUB, SUB_SCOPE, http_error

JOHN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RG_SCOPE = f"{SUB_SCOPE}/resourceGroups/rg-john"


def config(text: str):
    return parse_config(yaml.safe_load(f'subscription_id: "{SUB}"\n' + text))


JOHN = config("""
users:
  - name: John Doe
    email: john@example.com
    resource_group: {name: rg-john, location: centralindia, create_if_missing: true}
    access: {scope_type: resource_group, role: Contributor}
""")


# --------------------------------------------------------------------------- RbacService


def test_resolve_role_case_insensitive_and_by_guid(ctx):
    assert ctx.rbac.resolve_role("contributor", SUB_SCOPE).name == "Contributor"
    assert ctx.rbac.resolve_role(ROLES["Reader"], SUB_SCOPE).name == "Reader"
    ctx.rbac.resolve_role("Owner", SUB_SCOPE)
    assert ctx.auth_client.role_definitions.list_calls == 1  # cached per scope


def test_resolve_role_suggests_close_matches(ctx):
    with pytest.raises(OperationError) as info:
        ctx.rbac.resolve_role("Contributer", SUB_SCOPE)
    assert info.value.code == "RoleDefinitionDoesNotExist"
    assert "Contributor" in info.value.hint


def test_find_assignment_direct_vs_inherited(ctx):
    role = ctx.rbac.resolve_role("Contributor", SUB_SCOPE)
    ctx.auth_client.role_assignments.add(SUB_SCOPE, "Contributor", JOHN_ID, name="sub-level")
    direct, inherited = ctx.rbac.find_assignment(JOHN_ID, role, RG_SCOPE)
    assert direct is None
    assert [a.name for a in inherited] == ["sub-level"]

    ctx.auth_client.role_assignments.add(RG_SCOPE, "Contributor", JOHN_ID, name="rg-level")
    direct, _ = ctx.rbac.find_assignment(JOHN_ID, role, RG_SCOPE)
    assert direct.name == "rg-level"


def test_rg_owner_does_not_count_as_subscription_owner(ctx):
    role = ctx.rbac.resolve_role("Owner", SUB_SCOPE)
    ctx.auth_client.role_assignments.add(RG_SCOPE, "Owner", JOHN_ID)
    direct, inherited = ctx.rbac.find_assignment(JOHN_ID, role, SUB_SCOPE)
    assert direct is None and inherited == []


def test_create_assignment_handles_existing_conflict(ctx):
    role = ctx.rbac.resolve_role("Reader", SUB_SCOPE)
    ctx.auth_client.role_assignments.add(RG_SCOPE, "Reader", JOHN_ID, name="already")
    ctx.auth_client.role_assignments.create_errors.append(http_error(409, "RoleAssignmentExists"))
    assignment, created = ctx.rbac.create_assignment(RG_SCOPE, role, JOHN_ID)
    assert created is False
    assert assignment.name == "already"


def test_create_assignment_retries_principal_not_found(ctx):
    role = ctx.rbac.resolve_role("Reader", SUB_SCOPE)
    ctx.auth_client.role_assignments.create_errors += [http_error(400, "PrincipalNotFound")] * 2
    assignment, created = ctx.rbac.create_assignment(RG_SCOPE, role, JOHN_ID)
    assert created and assignment.principal_id == JOHN_ID
    assert ctx.auth_client.role_assignments.create_calls == 3


def test_create_assignment_permission_error_is_readable(ctx):
    role = ctx.rbac.resolve_role("Contributor", SUB_SCOPE)
    ctx.auth_client.role_assignments.create_errors.append(http_error(403, "AuthorizationFailed", "denied"))
    with pytest.raises(OperationError) as info:
        ctx.rbac.create_assignment(RG_SCOPE, role, JOHN_ID, user_label="john@example.com")
    error = info.value
    assert error.operation == "Assign Contributor role"
    assert error.required_permission == "Microsoft.Authorization/roleAssignments/write"
    assert error.user == "john@example.com"


def test_sdk_model_is_normalised_to_rest_json():
    from azure.mgmt.authorization.models import RoleAssignment as SdkRoleAssignment

    model = SdkRoleAssignment.deserialize({
        "id": "/x/roleAssignments/abc", "name": "abc",
        "properties": {"scope": RG_SCOPE, "roleDefinitionId": "/r/" + ROLES["Reader"], "principalId": JOHN_ID},
    })
    parsed = RoleAssignment.from_json(as_json(model))
    assert (parsed.scope, parsed.principal_id, parsed.role_guid) == (RG_SCOPE, JOHN_ID, ROLES["Reader"])


# --------------------------------------------------------------------------- setup engine (RBAC parts)


def test_first_run_plans_rg_and_assignment_then_second_run_is_noop(ctx):
    ctx.users.add("john@example.com", JOHN_ID)
    engine = SetupEngine(ctx, JOHN)
    plans = engine.plan()
    kinds = [(a.kind, a.title) for a in plans[0].actions]
    assert (ActionKind.NOOP, "User exists") in kinds
    assert (ActionKind.CREATE, "Resource Group: rg-john") in kinds
    assert (ActionKind.ASSIGN, "Contributor") in kinds
    assert not plans[0].actions[-1].high_privilege

    # Planning (and therefore --dry-run) never writes.
    assert ctx.resource_groups.created == [] and ctx.auth_client.role_assignments.create_calls == 0

    engine.execute(plans)
    assert all(a.status == "done" for a in plans[0].changes)
    assert ctx.resource_groups.created == ["rg-john"]

    second = SetupEngine(ctx, JOHN).plan()
    assert second[0].changes == []
    titles = [a.title for a in second[0].actions]
    assert "Resource Group exists: rg-john" in titles
    assert "Contributor role already assigned" in titles


def test_missing_user_is_blocked_without_invite_flag(ctx):
    plans = SetupEngine(ctx, JOHN).plan()
    assert plans[0].blocked
    blocked = [a for a in plans[0].actions if a.kind == ActionKind.BLOCKED][0]
    assert "invitation is required" in blocked.details[0]
    SetupEngine(ctx, JOHN).execute(plans)
    assert ctx.resource_groups.created == []  # nothing applied for a blocked user
    assert ctx.users.invited == []


def test_invite_then_assign(ctx):
    cfg = config("""
users:
  - email: guest@partner.com
    invite_if_missing: true
    access: {scope_type: subscription, role: Reader}
""")
    engine = SetupEngine(ctx, cfg)
    plans = engine.plan()
    assert [a.kind for a in plans[0].changes] == [ActionKind.INVITE, ActionKind.ASSIGN]
    engine.execute(plans)
    assert ctx.users.invited == ["guest@partner.com"]
    created = ctx.auth_client.role_assignments.items[-1]["properties"]
    assert created["principalId"] == "99999999-9999-9999-9999-999999999999"
    assert created["scope"] == SUB_SCOPE


def test_owner_at_subscription_requires_confirmation(ctx):
    ctx.users.add("boss@example.com", JOHN_ID)
    cfg = config("""
users:
  - email: boss@example.com
    access: {scope_type: subscription, role: owner}
""")
    action = SetupEngine(ctx, cfg).plan()[0].changes[0]
    assert action.high_privilege
    assert "full control over the subscription" in action.warnings[0]


def test_inherited_assignment_is_noted(ctx):
    ctx.users.add("john@example.com", JOHN_ID)
    ctx.resource_groups.add("rg-john")
    ctx.auth_client.role_assignments.add(SUB_SCOPE, "Contributor", JOHN_ID)
    action = SetupEngine(ctx, JOHN).plan()[0].changes[0]
    assert action.kind == ActionKind.ASSIGN
    assert any("inherited from Subscription" in d for d in action.details)


def test_unknown_role_and_missing_scopes_are_blocked(ctx):
    ctx.users.add("a@example.com", JOHN_ID)
    cfg = config(f"""
users:
  - email: a@example.com
    access:
      - {{scope_type: subscription, role: Contribtor}}
      - {{scope_type: management_group, management_group_id: mg-missing, role: Reader}}
      - {{scope_type: resource_group, resource_group: rg-missing, role: Reader}}
      - scope: /subscriptions/{SUB}/resourceGroups/rg-ok/providers/Microsoft.Web/sites/missing
        role: Reader
""")
    ctx.resource_groups.add("rg-ok")
    plan = SetupEngine(ctx, cfg).plan()[0]
    blocked = [a for a in plan.actions if a.kind == ActionKind.BLOCKED]
    reasons = " | ".join(a.title + " " + " ".join(a.details) for a in blocked)
    assert "Did you mean: Contributor" in reasons
    assert "mg-missing" in reasons
    assert "Resource Group not found: rg-missing" in reasons
    assert "Resource not found" in reasons
    assert plan.blocked


def test_failure_stops_remaining_actions_for_that_user_only(ctx):
    ctx.users.add("john@example.com", JOHN_ID)
    ctx.users.add("alice@example.com", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    cfg = config("""
users:
  - email: john@example.com
    resource_group: {name: rg-john, location: centralindia, create_if_missing: true}
    access: {role: Contributor}
    budget: {amount: 100, notifications: {emails: [a@example.com]}}
  - email: alice@example.com
    access: {scope_type: subscription, role: Reader}
""")
    engine = SetupEngine(ctx, cfg)
    plans = engine.plan()
    ctx.auth_client.role_assignments.create_errors.append(http_error(403, "AuthorizationFailed"))
    engine.execute(plans)
    john = {a.title: a.status for a in plans[0].changes}
    assert john == {"Resource Group: rg-john": "done", "Contributor": "failed", "Budget: budget-rg-john": "skipped"}
    assert [a.status for a in plans[1].changes] == ["done"]
    assert plans[0].changes[1].error.required_permission == "Microsoft.Authorization/roleAssignments/write"


def test_preflight_requirements_from_plan(ctx):
    ctx.users.add("john@example.com", JOHN_ID)
    engine = SetupEngine(ctx, JOHN)
    req = engine.requirements(engine.plan())
    assert req.rg_create_needed
    # RG does not exist yet, so RBAC permission is checked on the parent subscription
    assert req.rbac_scopes == [SUB_SCOPE]
    assert req.needs_directory
