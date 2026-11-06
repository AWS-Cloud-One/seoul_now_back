"""Recommendation-only configuration; shared application settings stay unchanged."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RecommendationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=Path(__file__).resolve().parents[3] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    openai_timeout_seconds: float = Field(default=20, gt=0, le=120)
    recommendation_state_secret: SecretStr = SecretStr("")
