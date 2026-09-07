from plexapi.exceptions import NotFound
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer


class PlexIntegration:
    """Thin wrapper around PlexAPI for Share Manager entitlement operations.

    Suspension removes the share to this PMS explicitly. Reactivation must not rely
    only on ``account.user(username)`` because Plex may stop returning that user once
    their final server share is removed. Share Manager therefore prefers the stored
    Plex account ID when recreating a share, with username/email invitation as a
    fallback for older/manual customer records.
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def server(self) -> PlexServer:
        return PlexServer(self.base_url, self.token)

    def account(self) -> MyPlexAccount:
        return MyPlexAccount(token=self.token)

    def test(self) -> dict:
        server = self.server()
        account = self.account()
        return {
            "server_name": server.friendlyName,
            "machine_identifier": server.machineIdentifier,
            "account": account.username,
        }

    def libraries(self) -> list[dict]:
        return [{"id": str(s.key), "name": s.title, "type": s.type} for s in self.server().library.sections()]

    def users(self) -> list[dict]:
        result = []
        for u in self.account().users():
            result.append({
                "id": str(getattr(u, "id", "")),
                "username": getattr(u, "username", None) or getattr(u, "title", None) or "Unknown",
                "email": getattr(u, "email", None),
            })
        return result

    @staticmethod
    def _server_share(user, machine_identifier: str):
        return next(
            (share for share in user.servers if share.machineIdentifier == machine_identifier),
            None,
        )

    @staticmethod
    def _find_user(account: MyPlexAccount, *identifiers):
        """Resolve a currently visible Plex user using any stored identity value."""
        seen = set()
        for identifier in identifiers:
            if identifier in (None, ""):
                continue
            identifier = str(identifier)
            if identifier.lower() in seen:
                continue
            seen.add(identifier.lower())
            try:
                return account.user(identifier)
            except NotFound:
                continue
        return None

    def _remove_server_share(self, account: MyPlexAccount, user, server: PlexServer) -> None:
        """Remove access to this server without removing the Plex account itself."""
        share = self._server_share(user, server.machineIdentifier)
        if share is None:
            return

        url = account.FRIENDSERVERS.format(
            machineId=server.machineIdentifier,
            serverId=share.id,
        )
        account.query(url, account._session.delete)

        # The user can disappear from /api/users after their final share is removed.
        # If they remain visible, verify that this PMS is no longer attached.
        refreshed = self._find_user(account, user.id, user.username, user.email, user.title)
        if refreshed is not None and self._server_share(refreshed, server.machineIdentifier) is not None:
            raise RuntimeError(
                f"Plex reported success but server access still exists for {user.title}"
            )

    def _create_server_share(
        self,
        account: MyPlexAccount,
        server: PlexServer,
        sections,
        plex_username: str,
        plex_user_id: str | None = None,
        email: str | None = None,
    ) -> None:
        """Recreate a server share for a user who is no longer visible in account.users()."""
        if plex_user_id:
            # This mirrors the POST branch used by PlexAPI.updateFriend() for a
            # known user without a share, but uses the ID persisted during sync.
            section_ids = account._getSectionIds(server.machineIdentifier, sections)
            params = {
                "server_id": server.machineIdentifier,
                "shared_server": {
                    "library_section_ids": section_ids,
                    "invited_id": int(plex_user_id),
                },
            }
            headers = {"Content-Type": "application/json"}
            url = account.FRIENDINVITE.format(machineId=server.machineIdentifier)
            account.query(url, account._session.post, json=params, headers=headers)
            return

        # Legacy/manual customer records may pre-date stored Plex IDs. PlexAPI's
        # inviteFriend accepts a Plex username or email directly.
        target = email or plex_username
        if not target:
            raise RuntimeError("Cannot restore Plex access: no Plex user ID, username, or email is stored")
        account.inviteFriend(user=target, server=server, sections=sections)

    def _verify_shared_libraries(
        self,
        account: MyPlexAccount,
        identifiers,
        machine_identifier: str,
        expected_names: set[str],
    ) -> None:
        refreshed = self._find_user(account, *identifiers)
        if refreshed is None:
            raise RuntimeError("Plex server share was created but the user is still not visible via plex.tv")

        share = self._server_share(refreshed, machine_identifier)
        if share is None:
            raise RuntimeError("Plex server share is missing after applying libraries")

        actual = {section.title for section in share.sections() if section.shared}
        if actual != expected_names:
            raise RuntimeError(
                "Plex library verification failed: "
                f"expected {sorted(expected_names)}, got {sorted(actual)}"
            )

    def apply_libraries(
        self,
        plex_username: str,
        library_names: list[str],
        plex_user_id: str | None = None,
        email: str | None = None,
    ):
        account = self.account()
        server = self.server()
        identifiers = (plex_user_id, plex_username, email)
        user = self._find_user(account, *identifiers)

        if not library_names:
            # If Plex no longer lists the user, their share is already absent.
            if user is not None:
                self._remove_server_share(account, user, server)
            return

        sections = [server.library.section(name) for name in library_names]

        if user is None:
            self._create_server_share(
                account,
                server,
                sections,
                plex_username=plex_username,
                plex_user_id=plex_user_id,
                email=email,
            )
        else:
            account.updateFriend(user=user, server=server, sections=sections)

        self._verify_shared_libraries(
            account,
            identifiers,
            server.machineIdentifier,
            set(library_names),
        )
