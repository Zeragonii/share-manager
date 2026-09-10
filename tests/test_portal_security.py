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
