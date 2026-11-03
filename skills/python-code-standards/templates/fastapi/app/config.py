from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    database_url: str
    http_timeout_seconds: float = 10.0
    api_version: int = 1

    @property
    def api_prefix(self) -> str:
        return f"/api/v{self.api_version}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
