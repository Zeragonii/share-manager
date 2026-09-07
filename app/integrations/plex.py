from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer


class PlexIntegration:
    """Thin wrapper around PlexAPI for Share Manager's entitlement operations.

    PlexAPI's ``updateFriend(removeSections=True)`` currently has an edge case where
    an existing server share plus an empty sections list becomes a no-op.  For a
    suspended customer we therefore remove the *server share* explicitly and then
    verify the result against a fresh plex.tv user record.
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
        """Return this user's share for one specific PMS, if present."""
        return next(
            (share for share in user.servers if share.machineIdentifier == machine_identifier),
            None,
        )

    def _remove_server_share(self, account: MyPlexAccount, user, server: PlexServer) -> None:
        """Remove access to this server without removing the Plex friend relationship."""
        share = self._server_share(user, server.machineIdentifier)
        if share is None:
            # Desired state already achieved.
            return

        # This is the same endpoint PlexAPI uses when it successfully removes a
        # server share, but calling it directly avoids the empty-sections branch
        # in updateFriend() that can otherwise silently do nothing.
        url = account.FRIENDSERVERS.format(
            machineId=server.machineIdentifier,
            serverId=share.id,
        )
        account.query(url, account._session.delete)

        # Never report success unless plex.tv now agrees that the share is gone.
        refreshed = account.user(user.id)
        if self._server_share(refreshed, server.machineIdentifier) is not None:
            raise RuntimeError(
                f"Plex reported success but server access still exists for {user.title}"
            )

    def _verify_shared_libraries(
        self,
        account: MyPlexAccount,
        user_id,
        machine_identifier: str,
        expected_names: set[str],
    ) -> None:
        """Verify a library update using fresh plex.tv share state."""
        refreshed = account.user(user_id)
        share = self._server_share(refreshed, machine_identifier)
        if share is None:
            raise RuntimeError("Plex server share is missing after applying libraries")

        actual = {section.title for section in share.sections() if section.shared}
        if actual != expected_names:
            raise RuntimeError(
                "Plex library verification failed: "
                f"expected {sorted(expected_names)}, got {sorted(actual)}"
            )

    def apply_libraries(self, plex_username: str, library_names: list[str]):
        account = self.account()
        server = self.server()
        user = account.user(plex_username)

        if not library_names:
            self._remove_server_share(account, user, server)
            return

        sections = [server.library.section(name) for name in library_names]
        account.updateFriend(user=user, server=server, sections=sections)
        self._verify_shared_libraries(
            account,
            user.id,
            server.machineIdentifier,
            set(library_names),
        )
