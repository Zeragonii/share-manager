from types import SimpleNamespace

from app.main import _portal_username_base, generate_portal_password
from app.security import hash_portal_password, verify_portal_password


def test_generated_portal_password_is_12_char_alphanumeric():
    password = generate_portal_password()
    assert len(password) == 12
    assert password.isalnum()


def test_portal_password_is_hashed_and_verifiable():
    password = "Ab12Cd34Ef56"
    password_hash = hash_portal_password(password)
    assert password not in password_hash
    assert verify_portal_password(password, password_hash)
    assert not verify_portal_password("WrongPassword12", password_hash)


def test_portal_username_defaults_from_plex_username():
    customer = SimpleNamespace(id=7, plex_username="Matt.Brown", email="matt@example.com", name="Matt Brown")
    assert _portal_username_base(customer) == "matt.brown"


def test_portal_username_defaults_from_email_local_part():
    customer = SimpleNamespace(id=8, plex_username="matt+plex@example.com", email=None, name="Matt Brown")
    assert _portal_username_base(customer) == "mattplex"


def test_portal_manifest_is_scoped_to_customer_portal():
    import json
    from pathlib import Path
    manifest = json.loads(Path("app/static/portal-manifest.webmanifest").read_text())
    assert manifest["start_url"] == "/portal"
    assert manifest["scope"] == "/portal"
    assert manifest["display"] == "standalone"


def test_portal_password_change_form_requires_current_and_confirmed_password():
    from pathlib import Path
    template = Path("app/templates/portal_dashboard.html").read_text()
    assert 'action="/portal/password"' in template
    assert 'name="current_password"' in template
    assert 'name="new_password"' in template
    assert 'name="confirm_password"' in template
