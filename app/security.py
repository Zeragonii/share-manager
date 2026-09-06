import hmac
from fastapi import Request
from itsdangerous import URLSafeSerializer, BadSignature
from .config import settings

serializer = URLSafeSerializer(settings.app_secret, salt="share-manager-session")

def valid_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(password, settings.admin_password)

def logged_in(request: Request) -> bool:
    raw = request.cookies.get("sm_session")
    if not raw:
        return False
    try:
        return serializer.loads(raw) == settings.admin_username
    except BadSignature:
        return False

def make_session() -> str:
    return serializer.dumps(settings.admin_username)
