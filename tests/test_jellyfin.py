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
