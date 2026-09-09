from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./sharemanager.db"
    app_secret: str = "dev-only-change-me"
    admin_username: str = "admin"
    admin_password: str = "changeme"
    session_max_age_seconds: int = 60 * 60 * 24 * 7
    session_cookie_secure: bool = False
    reconcile_on_assign: bool = True
    billing_check_interval_minutes: int = 15
    notification_due_soon_days: int = 3
    backup_dir: str = "/backups"
    backup_schedule_hour: int = 3
    backup_check_interval_minutes: int = 5
    backup_retention_daily: int = 7
    backup_retention_weekly: int = 4
    backup_retention_monthly: int = 6
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
