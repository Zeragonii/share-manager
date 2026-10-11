from unittest.mock import patch
import httpx
import pytest
from app.integrations.jellyfin import JellyfinIntegration, JellyfinError


def test_discover_users_read_only():
    calls = []
    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json=[
            {"Id": "abc", "Name": "Tester", "Policy": {"IsAdministrator": False, "IsDisabled": True}},
            {"Id": "def", "Name": "Admin", "Policy": {"IsAdministrator": True}},
        ], request=httpx.Request("GET", url))
    with patch("app.integrations.jellyfin.httpx.get", side_effect=fake_get):
        result = JellyfinIntegration("http://jellyfin:8096", "secret").users()
    assert len(result) == 2
    assert result[0] == {"id": "abc", "name": "Tester", "admin": False, "disabled": True}
    assert result[1]["admin"]
    assert calls[0][0] == "http://jellyfin:8096/Users"
    assert calls[0][1]["headers"] == {"Authorization": 'MediaBrowser Token="secret"', "Accept": "application/json"}
    assert calls[0][1]["follow_redirects"] is False


def test_invalid_server_and_payload():
    with pytest.raises(JellyfinError):
        JellyfinIntegration("not a url", "secret")
    with pytest.raises(JellyfinError):
        JellyfinIntegration("http://localhost:8096", "")
    with patch("app.integrations.jellyfin.httpx.get", return_value=httpx.Response(200, json={"Users": []}, request=httpx.Request("GET", "http://test/Users"))):
        with pytest.raises(JellyfinError, match="Unexpected"):
            JellyfinIntegration("http://test", "x").users()


def test_create_user_posts_password_and_returns_identity():
    def fake_post(url, **kwargs):
        assert url == "http://jellyfin:8096/Users/New"
        assert kwargs["headers"]["Authorization"] == 'MediaBrowser Token="secret"'
        assert kwargs["json"] == {"Name": "customer1", "Password": "Changeme!"}
        return httpx.Response(200, json={"Id": "abc-123", "Name": "customer1"}, request=httpx.Request("POST", url))
    with patch("app.integrations.jellyfin.httpx.post", side_effect=fake_post):
        user = JellyfinIntegration("http://jellyfin:8096", "secret").create_user("customer1", "Changeme!")
    assert user == {"id": "abc-123", "name": "customer1"}


def test_create_user_rejection_does_not_leak_password():
    url = "http://jellyfin:8096/Users/New"
    response = httpx.Response(409, text='{"error":"conflict"}', request=httpx.Request("POST", url))
    with patch("app.integrations.jellyfin.httpx.post", return_value=response):
        with pytest.raises(JellyfinError) as exc:
            JellyfinIntegration("http://jellyfin:8096", "secret").create_user("taken", "Changeme!")
    assert "Changeme!" not in str(exc.value)
    assert "409" in str(exc.value)
