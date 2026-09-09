from plexapi.exceptions import NotFound
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer


class PlexIntegration:
    """Thin wrapper around PlexAPI for Share Manager entitlement operations.

    Suspension removes the share to this PMS explicitly. Reactivation must not rely
    only on ``account.user(username)`` because Plex may stop returning that user once
    their final server share is removed.

    When restoring access, Share Manager creates the Plex invitation using the
    username/email form of ``inviteFriend()`` and supplies the desired sections in the
    invitation itself. This is important: a pending invite is a legitimate state and
    should not require a second reconciliation after acceptance just to add sections.
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

    @staticmethod
    def _find_pending_invite(account: MyPlexAccount, machine_identifier: str, *identifiers):
        """Find a sent pending invitation for this user/server, if one exists."""
        wanted = {str(value).lower() for value in identifiers if value not in (None, "")}
        if not wanted:
            return None
        try:
            invites = account.pendingInvites(includeSent=True, includeReceived=False)
        except Exception:
            # Pending-invite discovery is a guard against duplicate invitations, not
            # a prerequisite for normal reconciliation. Let the invite call surface
            # any authoritative Plex error instead of masking it here.
            return None

        for invite in invites:
            candidates = {
                str(value).lower()
                for value in (
                    getattr(invite, "id", None),
                    getattr(invite, "username", None),
                    getattr(invite, "email", None),
                    getattr(invite, "friendlyName", None),
                )
                if value not in (None, "")
            }
            if not wanted.intersection(candidates):
                continue
            servers = getattr(invite, "servers", [])
            if callable(servers):
                servers = servers()
            if any(getattr(server, "machineIdentifier", None) == machine_identifier for server in servers):
                return invite
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

    @staticmethod
    def _pending_server_share(invite, machine_identifier: str):
        servers = getattr(invite, "servers", [])
        if callable(servers):
            servers = servers()
        return next((item for item in servers if getattr(item, "machineIdentifier", None) == machine_identifier), None)

    def _remove_pending_server_invite(self, account: MyPlexAccount, invite, server: PlexServer) -> None:
        """Remove only this server's pending share when Plex exposes a share id."""
        pending_server = self._pending_server_share(invite, server.machineIdentifier)
        if pending_server is not None and getattr(pending_server, "id", None) is not None:
            url = account.FRIENDSERVERS.format(machineId=server.machineIdentifier, serverId=pending_server.id)
            account.query(url, account._session.delete)
            return

        servers = getattr(invite, "servers", [])
        if callable(servers):
            servers = servers()
        if len(servers) <= 1:
            account.cancelInvite(invite)
            return
        raise RuntimeError("Plex pending invitation could not be removed safely without affecting another server")

    def _replace_pending_server_invite(self, account: MyPlexAccount, invite, server: PlexServer, sections, plex_username: str, plex_user_id: str | None, email: str | None) -> None:
        """Replace a pending entitlement so accepting the invite grants current libraries."""
        self._remove_pending_server_invite(account, invite, server)
        self._create_server_share(
            account, server, sections, plex_username=plex_username, plex_user_id=plex_user_id, email=email
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
        """Create a server invitation with the desired libraries already attached.

        PlexAPI's inviteFriend() accepts a username/email string without first
        resolving it through account.user(). That makes it suitable for users whose
        last server share was removed during suspension. More importantly, it sends
        the requested section IDs as part of the invite, so accepting the invitation
        should immediately grant the intended package libraries.
        """
        # An explicitly stored Plex username/account email is authoritative. The
        # customer's contact email may be different and should only be a fallback.
        target = plex_username or email
        if target:
            account.inviteFriend(user=target, server=server, sections=sections)
            return

        # Very old/manual records may theoretically contain only a Plex numeric ID.
        # Retain a best-effort fallback, although normal synced users always have a
        # username/email available and therefore use inviteFriend() above.
        if plex_user_id:
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

        raise RuntimeError("Cannot restore Plex access: no Plex user ID, username, or email is stored")

    def _verify_shared_libraries(
        self,
        account: MyPlexAccount,
        identifiers,
        machine_identifier: str,
        expected_names: set[str],
    ) -> None:
        refreshed = self._find_user(account, *identifiers)
        if refreshed is None:
            raise RuntimeError("Plex server share is missing after applying libraries")

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
    ) -> dict:
        """Apply desired Plex access and return a truthful reconciliation state.

        Returned ``state`` values:
        - ``removed``: no share exists for this server.
        - ``invited``: a new Plex invitation was sent with desired libraries attached.
        - ``pending``: an existing server invitation is awaiting acceptance.
        - ``applied``: an accepted share exists and its libraries were verified.
        """
        account = self.account()
        server = self.server()
        identifiers = (plex_user_id, plex_username, email)
        user = self._find_user(account, *identifiers)

        pending_invite = self._find_pending_invite(account, server.machineIdentifier, *identifiers)

        if not library_names:
            if user is not None:
                self._remove_server_share(account, user, server)
            if pending_invite is not None:
                self._remove_pending_server_invite(account, pending_invite, server)
            return {"state": "removed", "libraries": []}

        sections = [server.library.section(name) for name in library_names]
        share = self._server_share(user, server.machineIdentifier) if user is not None else None

        if share is None and pending_invite is not None:
            # A pending invitation is entitlement state too. Recreate this server's
            # invitation with the current package libraries instead of assuming the
            # old invite is still correct.
            self._replace_pending_server_invite(
                account, pending_invite, server, sections, plex_username, plex_user_id, email
            )
            return {"state": "pending", "libraries": list(library_names)}

        # A pending invitation is already carrying the desired entitlement from the
        # initial invite. Do not spam another invite or pretend access is verified.
        if share is not None and getattr(share, "pending", False):
            return {"state": "pending", "libraries": list(library_names)}

        # If no accepted share exists, create an invitation containing the desired
        # libraries. We deliberately do not try to verify usable access until the
        # recipient accepts it.
        if share is None:
            self._create_server_share(
                account,
                server,
                sections,
                plex_username=plex_username,
                plex_user_id=plex_user_id,
                email=email,
            )
            return {"state": "invited", "libraries": list(library_names)}

        account.updateFriend(user=user, server=server, sections=sections)
        self._verify_shared_libraries(
            account,
            identifiers,
            server.machineIdentifier,
            set(library_names),
        )
        return {"state": "applied", "libraries": list(library_names)}
