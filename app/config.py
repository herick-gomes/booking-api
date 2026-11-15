from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+psycopg://booking:local-only@localhost:5432/booking"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "local-demo-key-change-before-deployment-2026"
    access_minutes: int = 30
    cors_origins: list[str] = []

    @model_validator(mode="after")
    def production_secret(self):
        if self.environment == "production" and (
            len(self.jwt_secret) < 32 or self.jwt_secret.startswith("local-")
        ):
            raise ValueError("Production requires an independent JWT_SECRET with at least 32 characters.")
        return self


@lru_cache
def get_settings():
    return Settings()
