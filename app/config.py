from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://gen:gen@localhost:5432/gen"
    webhook_secret: str = "test-secret"
    provider_timeout: float = 60
    http_timeout: float = 5
    watchdog_interval: float = 1
    alpha_url: str = "http://localhost:18001"
    beta_url: str = "http://localhost:18002"
    public_base_url: str = "http://localhost:18000"
    max_active_tasks: int = 3


settings = Settings()


def provider_url(name: str) -> str:
    mapping = {"alpha": settings.alpha_url, "beta": settings.beta_url}
    if name not in mapping:
        raise KeyError(name)
    return mapping[name]
