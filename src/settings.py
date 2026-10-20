"""Validated configuration loaded from environment variables."""

from __future__ import annotations

from functools import cached_property

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime service configuration; secrets are never given unsafe defaults."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="SON_", extra="ignore"
    )

    database_url: str = Field(min_length=1)
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_issuer: str = "ethio-telecom-son"
    access_token_minutes: int = Field(default=20, ge=5, le=60)
    cors_origins: str = "http://localhost:8501"
    ingestion_api_token: SecretStr | None = None
    son_netconf_enabled: bool = False
    netconf_username: str | None = None
    netconf_password: SecretStr | None = None
    netconf_known_hosts: str | None = None
    netconf_action_templates_json: str | None = None
    allowed_device_cidrs: str = ""
    netconf_port: int = Field(default=830, ge=1, le=65535)
    netconf_timeout_seconds: int = Field(default=20, ge=5, le=60)
    confirmed_commit_timeout_seconds: int = Field(default=180, ge=60, le=600)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """Allow PostgreSQL in deployments and SQLite for local development."""
        if not value.startswith(("postgresql+psycopg://", "sqlite:///")):
            raise ValueError(
                "DATABASE_URL must use postgresql+psycopg:// or sqlite:///."
            )
        return value

    @cached_property
    def allowed_device_networks(self) -> tuple[str, ...]:
        """Return configured network allowlist entries."""
        return tuple(
            network.strip()
            for network in self.allowed_device_cidrs.split(",")
            if network.strip()
        )

    @cached_property
    def netconf_action_templates(self) -> dict[str, str]:
        """Parse the server-side vendor YANG XML templates."""
        import json

        if not self.netconf_action_templates_json:
            return {}
        payload = json.loads(self.netconf_action_templates_json)
        if not isinstance(payload, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in payload.items()
        ):
            raise ValueError("NETCONF_ACTION_TEMPLATES_JSON must map action names to XML.")
        return payload


settings = Settings()
