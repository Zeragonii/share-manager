"""Phase 1 must remain read-only and never mutate a provider account."""
import httpx
import pytest

from app.integrations.nexa import (
    BASE_URL, NexaError, NexaIntegration, encrypt_api_key, decrypt_api_key,
)

SECRET = "stable-production-app-secret-for-tests"


def test_key_encrypted_at_rest_and_roundtrips():
    encrypted = encrypt_api_key("nexa-secret-token", SECRET)
    assert "nexa-secret-token" not in encrypted
    assert decrypt_api_key(encrypted, SECRET) == "nexa-secret-token"
    with pytest.raises(NexaError, match="decrypt"):
        decrypt_api_key(encrypted, "different-secret")
    with pytest.raises(NexaError, match="APP_SECRET"):
        encrypt_api_key("token", "dev-only-change-me")


def test_packages_read_only_and_credit_balance(monkeypatch):
    def fake_get(url, **kwargs):
        assert url == BASE_URL + "/api/reseller/v1/packages"
        assert kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert kwargs["follow_redirects"] is False
        return httpx.Response(200, json={"success": True, "balance": 85, "packages": [
            {"id": 3, "name": "Standard 6 months", "months": 6, "credits": 6, "connections": 1, "account_types": ["username", "mag"]}
        ]})
    monkeypatch.setattr(httpx, "get", fake_get)
    result = NexaIntegration("test-key").packages()
    assert result["balance"] == 85
    assert result["packages"][0]["id"] == "3"
    assert result["packages"][0]["credits"] == 6


def test_templates_support_string_ids_and_shared_source(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={
        "templates": [{"id": "37b2f69aad23dfb3", "name": "UK & Sport", "source": "owner"}]}
    ))
    assert NexaIntegration("key").templates()[0] == {
        "id": "37b2f69aad23dfb3", "name": "UK & Sport", "source": "owner"
    }


def test_lookup_uses_one_encoded_query_parameter_and_unix_seconds(monkeypatch):
    def fake_get(url, **kwargs):
        assert kwargs["params"] == {"username": "Some+User"}
        return httpx.Response(200, json={"account": {
            "id": 123, "username": "Some+User", "type": "username", "status": "active", "exp_date": 1798761600,
        }})
    monkeypatch.setattr(httpx, "get", fake_get)
    account = NexaIntegration("key").lookup(username="Some+User")
    assert account["id"] == "123"
    assert account["expiry_label"].endswith("UTC")
    assert account["exp_date"] == 1798761600


def test_lookup_rejects_ambiguous_input_before_network(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: pytest.fail("Network called"))
    with pytest.raises(NexaError, match="exactly one"):
        NexaIntegration("key").lookup(username="a", mac="00:1A:79:42:56:78")
    with pytest.raises(NexaError, match="exactly one"):
        NexaIntegration("key").lookup()
    with pytest.raises(NexaError, match="MAC address"):
        NexaIntegration("key").lookup(mac="not-a-mac")


@pytest.mark.parametrize("status,fragment", [(401, "invalid or revoked"), (404, "No matching"), (409, "ambiguous"), (429, "rate limit"), (503, "unavailable")])
def test_safe_http_errors(monkeypatch, status, fragment):
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(status, json={"message": "secret-key-test"}))
    with pytest.raises(NexaError, match=fragment) as exc:
        NexaIntegration("test-key").packages()
    assert "secret-key-test" not in str(exc.value)


def test_no_non_get_operations_exposed():
    assert not hasattr(NexaIntegration, "extend")
    assert not hasattr(NexaIntegration, "create")
    with pytest.raises(NexaError, match="Unsupported"):
        NexaIntegration("key")._get("/accounts/123/extend")
