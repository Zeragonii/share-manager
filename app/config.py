from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    database_url: str
    app_secret: str = "dev-only-change-me"
    admin_username: str = "admin"
    admin_password: str = "changeme"
    session_max_age_seconds: int = 60 * 60 * 24 * 7
    session_cookie_secure: bool = False
    reconcile_on_assign: bool = True
    billing_check_interval_minutes: int = 15
    notification_due_soon_days: int = 3
    storage_root: str = "/share-manager"
    backup_dir: str = "/share-manager/backups"
    attachment_dir: str = "/share-manager/attachments"
    backup_schedule_hour: int = 3
    backup_check_interval_minutes: int = 5
    backup_retention_daily: int = 7
    backup_retention_weekly: int = 4
    backup_retention_monthly: int = 6
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("DATABASE_URL is required; Share Manager requires PostgreSQL")
        try:
            backend = make_url(value).get_backend_name()
        except Exception as exc:
            raise ValueError(f"DATABASE_URL is invalid: {exc}") from exc
        if backend != "postgresql":
            raise ValueError(
                f"Unsupported database backend '{backend}'. Share Manager requires PostgreSQL."
            )
        return value


settings = Settings()
