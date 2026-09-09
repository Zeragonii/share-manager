import hashlib
import hmac
from fastapi import Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from .config import settings

serializer = URLSafeTimedSerializer(settings.app_secret, salt="share-manager-session")

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
