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
    account.users.return_value = []
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


def test_existing_share_is_discovered_by_listing_without_new_invite():
    integration, account, server = make_client()
    share = SimpleNamespace(machineIdentifier="machine-1", pending=False)
    person = SimpleNamespace(id=123, username="user@example.com", email="user@example.com", title="User", servers=[share])
    account.pendingInvites.return_value = []
    account.users.return_value = [person]
    integration._find_user = Mock(return_value=None)
    integration._verify_shared_libraries = Mock()
    result = integration.apply_libraries("user@example.com", ["Movies"])
    assert result["state"] == "applied"
    account.updateFriend.assert_called_once()
    account.inviteFriend.assert_not_called()


def test_duplicate_share_response_is_recovered_by_refreshing_user_listing():
    integration, account, server = make_client()
    account.pendingInvites.return_value = []
    share = SimpleNamespace(machineIdentifier="machine-1", pending=False)
    person = SimpleNamespace(id=123, username="user@example.com", email="user@example.com", title="User", servers=[share])
    account.users.side_effect = [[], [person]]
    integration._find_user = Mock(return_value=None)
    account.inviteFriend.side_effect = RuntimeError("400 You're already sharing this server with user@example.com")
    integration._verify_shared_libraries = Mock()
    result = integration.apply_libraries("user@example.com", ["Movies"])
    assert result["state"] == "applied"
    account.updateFriend.assert_called_once()
    account.inviteFriend.assert_called_once()


def test_duplicate_error_without_discoverable_share_fails_without_retrying_invite():
    integration, account, server = make_client()
    import pytest
    account.pendingInvites.return_value = []
    account.users.return_value = []
    integration._find_user = Mock(return_value=None)
    account.inviteFriend.side_effect = RuntimeError("You're already sharing this server with user@example.com")
    with pytest.raises(RuntimeError, match="not discoverable"):
        integration.apply_libraries("user@example.com", ["Movies"])
    account.inviteFriend.assert_called_once()
    account.updateFriend.assert_not_called()


def test_ambiguous_identity_does_not_update_any_share():
    integration, account, server = make_client()
    import pytest
    account.users.return_value = [
        SimpleNamespace(id=1, username="different1", email="user@example.com"),
        SimpleNamespace(id=2, username="different2", email="user@example.com"),
    ]
    with pytest.raises(RuntimeError, match="Multiple Plex users"):
        integration.apply_libraries("user@example.com", ["Movies"])
    account.inviteFriend.assert_not_called()
    account.updateFriend.assert_not_called()
