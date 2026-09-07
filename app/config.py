from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "sqlite:///./sharemanager.db"
    app_secret: str = "dev-only-change-me"
    admin_username: str = "admin"
    admin_password: str = "changeme"
    reconcile_on_assign: bool = True
    billing_check_interval_minutes: int = 15
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
