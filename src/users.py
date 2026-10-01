"""Microsoft Entra ID (Azure AD) user lookup and B2B guest invitations via Microsoft Graph.

Required Graph permissions:
  * read users:    User.Read.All or Directory.Read.All (delegated or application)
  * invite guests: User.Invite.All (or the 'Guest Inviter' directory role for a signed-in user)
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import OperationError, RestError, translate_error
from .rest import RestClient
from .validation import odata_quote

_USER_FIELDS = "id,displayName,userPrincipalName,mail,userType,accountEnabled,externalUserState,otherMails"
READ_PERMISSION = "Microsoft Graph: User.Read.All (or Directory.Read.All)"
INVITE_PERMISSION = "Microsoft Graph: User.Invite.All (or the 'Guest Inviter' Entra ID role)"


@dataclass
class DirectoryUser:
    object_id: str
    display_name: str | None
    user_principal_name: str | None
    mail: str | None
    user_type: str | None = None  # Member | Guest
    account_enabled: bool | None = None
    external_state: str | None = None  # PendingAcceptance | Accepted

    @classmethod
    def from_graph(cls, data: dict) -> "DirectoryUser":
        return cls(
            object_id=data["id"],
            display_name=data.get("displayName"),
            user_principal_name=data.get("userPrincipalName"),
            mail=data.get("mail"),
            user_type=data.get("userType"),
            account_enabled=data.get("accountEnabled"),
            external_state=data.get("externalUserState"),
        )

    @property
    def email(self) -> str | None:
        return self.mail or self.user_principal_name


@dataclass
class PrincipalInfo:
    object_id: str
    display_name: str | None
    email: str | None
    principal_type: str  # User | Group | ServicePrincipal | Unknown
    user_type: str | None = None


class UserService:
    def __init__(self, graph: RestClient):
        self.graph = graph

    # -- lookup ---------------------------------------------------------
    def get_by_id(self, object_id: str) -> DirectoryUser | None:
        try:
            data = self.graph.get(f"/users/{object_id}", params={"$select": _USER_FIELDS})
        except RestError as exc:
            if exc.status == 404:
                return None
            raise translate_error(exc, "Look up user", user=object_id, required_permission=READ_PERMISSION) from None
        return DirectoryUser.from_graph(data)

    def find_by_email(self, email: str) -> DirectoryUser | None:
        """Find a member or guest by mail, UPN, otherMails, or the guest #EXT# UPN form."""
        quoted = odata_quote(email)
        guest_prefix = odata_quote(email.replace("@", "_")) + "#EXT#"
        queries = [
            ({"$filter": f"mail eq '{quoted}' or userPrincipalName eq '{quoted}'"}, None),
            # otherMails and startswith on UPN require advanced query capabilities.
            (
                {"$filter": f"otherMails/any(x:x eq '{quoted}') or startswith(userPrincipalName,'{guest_prefix}')", "$count": "true"},
                {"ConsistencyLevel": "eventual"},
            ),
        ]
        for params, headers in queries:
            params = dict(params, **{"$select": _USER_FIELDS, "$top": "5"})
            try:
                data = self.graph.get("/users", params=params, headers=headers)
            except RestError as exc:
                raise translate_error(exc, "Look up user", user=email, required_permission=READ_PERMISSION) from None
            matches = [DirectoryUser.from_graph(item) for item in (data or {}).get("value", [])]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise OperationError(
                    "Look up user",
                    f"{len(matches)} directory users match {email}: "
                    + ", ".join(f"{m.display_name} ({m.object_id})" for m in matches),
                    user=email,
                    hint="Set 'object_id' for this user in config.yaml to pick the right account.",
                    code="AmbiguousUser",
                )
        return None

    def check_read_access(self) -> None:
        """Cheap probe used by the pre-flight check."""
        try:
            self.graph.get("/users", params={"$top": "1", "$select": "id"})
        except RestError as exc:
            raise translate_error(exc, "Read directory users", required_permission=READ_PERMISSION) from None

    # -- invitations ----------------------------------------------------
    def invite(self, email: str, display_name: str | None, redirect_url: str, send_message: bool) -> DirectoryUser:
        body = {
            "invitedUserEmailAddress": email,
            "inviteRedirectUrl": redirect_url,
            "sendInvitationMessage": bool(send_message),
        }
        if display_name:
            body["invitedUserDisplayName"] = display_name
        try:
            data = self.graph.post("/invitations", json=body)
        except RestError as exc:
            raise translate_error(exc, "Invite guest user", user=email, required_permission=INVITE_PERMISSION) from None
        invited = (data or {}).get("invitedUser") or {}
        if not invited.get("id"):
            raise OperationError("Invite guest user", "Graph did not return the invited user's object ID.", user=email)
        return DirectoryUser(
            object_id=invited["id"],
            display_name=display_name,
            user_principal_name=None,
            mail=email,
            user_type=data.get("invitedUserType") or "Guest",
            external_state=data.get("status"),
        )

    # -- bulk resolution for reports -------------------------------------
    def resolve_principals(self, object_ids: list[str]) -> dict[str, PrincipalInfo]:
        """Resolve object IDs of users, groups and service principals in batches of 1000."""
        result: dict[str, PrincipalInfo] = {}
        ids = sorted({i for i in object_ids if i})
        for start in range(0, len(ids), 1000):
            chunk = ids[start : start + 1000]
            try:
                data = self.graph.post(
                    "/directoryObjects/getByIds",
                    json={"ids": chunk, "types": ["user", "group", "servicePrincipal"]},
                )
            except RestError as exc:
                raise translate_error(exc, "Resolve principals", required_permission="Microsoft Graph: Directory.Read.All") from None
            for item in (data or {}).get("value", []):
                odata_type = item.get("@odata.type", "").rsplit(".", 1)[-1]
                principal_type = {"user": "User", "group": "Group", "servicePrincipal": "ServicePrincipal"}.get(odata_type, "Unknown")
                result[item["id"]] = PrincipalInfo(
                    object_id=item["id"],
                    display_name=item.get("displayName"),
                    email=item.get("mail") or item.get("userPrincipalName") or item.get("appId"),
                    principal_type=principal_type,
                    user_type=item.get("userType"),
                )
        return result
