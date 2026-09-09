from types import SimpleNamespace
from unittest.mock import Mock

from app.integrations.plex import PlexIntegration


def make_client():
    integration = PlexIntegration("http://plex.invalid", "token")
    account = Mock()
    account.FRIENDSERVERS = "https://plex.test/api/servers/{machineId}/shared_servers/{serverId}"
    account._session.delete = Mock()
    invite_server = SimpleNamespace(machineIdentifier="machine-1", id=77)
    invite = SimpleNamespace(id=123, username="user@example.com", email="user@example.com", friendlyName="User", servers=[invite_server])
    account.pendingInvites.return_value = [invite]
    account.user.side_effect = Exception("unused")
    server = SimpleNamespace(machineIdentifier="machine-1", library=SimpleNamespace(section=Mock(side_effect=lambda name: SimpleNamespace(title=name, key=name))))
    integration.account = Mock(return_value=account)
    integration.server = Mock(return_value=server)
    integration._find_user = Mock(return_value=None)
    return integration, account, server


def test_suspension_removes_server_specific_pending_invite():
    integration, account, server = make_client()
    result = integration.apply_libraries("user@example.com", [])
    assert result == {"state": "removed", "libraries": []}
    account.query.assert_called_once()
    assert "/servers/machine-1/shared_servers/77" in account.query.call_args.args[0]


def test_package_change_replaces_pending_invite_with_current_libraries():
    integration, account, server = make_client()
    result = integration.apply_libraries("user@example.com", ["Movies", "TV"])
    assert result == {"state": "pending", "libraries": ["Movies", "TV"]}
    account.query.assert_called_once()
    account.inviteFriend.assert_called_once()
    sections = account.inviteFriend.call_args.kwargs["sections"]
    assert [section.title for section in sections] == ["Movies", "TV"]
