import hashlib
import hmac
from fastapi import Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext
from .config import settings

serializer = URLSafeTimedSerializer(settings.app_secret, salt="share-manager-session")
portal_serializer = URLSafeTimedSerializer(settings.app_secret, salt="share-manager-portal-session")
password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _session_version() -> str:
    material = f"{settings.admin_username}\0{settings.admin_password}\0{settings.app_secret}".encode()
    return hashlib.sha256(material).hexdigest()[:24]


def valid_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(password, settings.admin_password)


def logged_in(request: Request) -> bool:
    raw = request.cookies.get("sm_session")
    if not raw:
        return False
    try:
        payload = serializer.loads(raw, max_age=settings.session_max_age_seconds)
        return (
            isinstance(payload, dict)
            and payload.get("username") == settings.admin_username
            and hmac.compare_digest(str(payload.get("version", "")), _session_version())
        )
    except (BadSignature, SignatureExpired):
        return False


def make_session() -> str:
    return serializer.dumps({"username": settings.admin_username, "version": _session_version()})


def hash_portal_password(password: str) -> str:
    return password_context.hash(password)


def verify_portal_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return password_context.verify(password, password_hash)
    except Exception:
        return False


def make_portal_session(customer_id: int, version: int) -> str:
    return portal_serializer.dumps({"customer_id": int(customer_id), "version": int(version)})


def read_portal_session(request: Request) -> dict | None:
    raw = request.cookies.get("sm_portal_session")
    if not raw:
        return None
    try:
        payload = portal_serializer.loads(raw, max_age=settings.session_max_age_seconds)
        if not isinstance(payload, dict):
            return None
        return payload
    except (BadSignature, SignatureExpired):
        return None
