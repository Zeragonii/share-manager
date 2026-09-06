from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

class PlexIntegration:
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

    def apply_libraries(self, plex_username: str, library_names: list[str]):
        account = self.account()
        server = self.server()
        user = account.user(plex_username)
        if not library_names:
            account.updateFriend(user=user, server=server, removeSections=True)
            return
        sections = [server.library.section(name) for name in library_names]
        account.updateFriend(user=user, server=server, sections=sections)
