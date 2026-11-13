import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.db.connection import connection_args

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
        hide_input_in_errors=True,
    )

    runtime_env: Literal["local", "aws"] = "local"
    runtime_role: Literal["api", "collector", "migration", "import"] | None = None
    settings_secret_id: str = ""
    # Bootstrap handoff guard, not an authentication or IAM boundary.
    settings_loaded: bool = Field(default=False, repr=False, exclude=True)

    def __init__(self, **values):
        runtime_env = values.get("runtime_env", os.environ.get("APP_RUNTIME_ENV", "local"))
        if runtime_env == "aws":
            values["_env_file"] = None
        super().__init__(**values)

    api_port: int = Field(default=8000, validation_alias="API_PORT", ge=1, le=65535)
    db_port: int = Field(default=5432, validation_alias="DB_PORT", ge=1, le=65535)

    # 빈 키를 가진 제공처의 정기 수집은 실행하지 않습니다.
    seoul_sdot_api_key: SecretStr = SecretStr("")
    its_api_key: SecretStr = SecretStr("")
    sgis_consumer_key: SecretStr = SecretStr("")
    sgis_consumer_secret: SecretStr = SecretStr("")

    kakao_rest_api_key: SecretStr = SecretStr("")
    # Per-process rolling 24h allowance; shared app usage is still tracked by Kakao.
    mobility_request_budget: int = Field(default=8000, ge=1, le=10000)
    mobility_max_concurrency: int = Field(default=3, ge=1, le=5)
    mobility_requests_per_second: int = Field(default=3, ge=1, le=5)
    ai_provider: Literal["bedrock", "openai"] = "bedrock"
    bedrock_region: str = Field(default="ap-northeast-2", min_length=1)
    # Temporary integration candidate; final model selection belongs to chat evaluation.
    bedrock_model_id: str = Field(
        default="global.anthropic.claude-haiku-4-5-20251001-v1:0", min_length=1
    )
    bedrock_timeout_seconds: float = Field(default=20, gt=0, le=120)
    bedrock_max_tokens: int = Field(default=2000, ge=1, le=8192)
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    openai_timeout_seconds: float = Field(default=20, gt=0, le=120)
    data_go_kr_api_key: SecretStr = SecretStr("")
    seoul_municipal_parking_api_key: SecretStr = SecretStr("")
    seoul_topis_api_key: SecretStr = SecretStr("")
    seoul_api_key: SecretStr = SecretStr("")
    seoul_citydata_api_key: SecretStr = SecretStr("")

    # Enable only once the V2 migrations/permissions and public route are ready.
    chat_v2_enabled: bool = False
    chat_sessions_per_day: int = Field(default=300, ge=1)
    chat_model_calls_per_day: int = Field(default=200, ge=2)
    chat_model_input_bytes_per_day: int = Field(default=6_500_000, ge=1)
    chat_model_output_tokens_per_day: int = Field(default=204_800, ge=1)
    chat_http_calls_per_day: int = Field(default=8000, ge=1)
    chat_http_calls_per_run: int = Field(default=40, ge=1, le=100)

    app_name: str = "서울 지금 어디 API"
    docs_enabled: bool = True
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://seoul:seoul_local@127.0.0.1:5432/seoul_now"
    )
    db_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    db_sslmode: (
        Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] | None
    ) = None
    db_sslrootcert: Path | None = None
    health_timeout_seconds: float = Field(default=5, gt=0, le=30)
    result_context_ttl_seconds: int = Field(default=1800, ge=1, le=86400)
    # Where in-flight API state lives. "database" is shared by every API process and
    # survives a rolling restart; "memory" is one process only (DB-less tests).
    state_backend: Literal["database", "memory"] = "database"

    @model_validator(mode="after")
    def validate_chat_provider(self):
        if self.chat_v2_enabled and self.ai_provider != "bedrock":
            raise ValueError("Chat V2 currently requires the Bedrock provider")
        return self

    @model_validator(mode="after")
    def require_shared_state_on_aws(self):
        if self.runtime_env == "aws" and self.state_backend != "database":
            raise ValueError("AWS API processes must share state through the database")
        return self

    @model_validator(mode="after")
    def require_aws_bootstrap(self):
        if self.runtime_env == "aws" and (
            not self.settings_loaded
            or self.runtime_role is None
            or not self.settings_secret_id
            or "database_url" not in self.model_fields_set
        ):
            raise ValueError("AWS settings must be supplied by app.bootstrap")
        return self

    @model_validator(mode="after")
    def validate_database_transport(self):
        connection_args(self)
        return self

    @field_validator("bedrock_region", "bedrock_model_id", mode="before")
    @classmethod
    def strip_bedrock_identifiers(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL") from None
        if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
            raise ValueError("DATABASE_URL must use postgresql+psycopg with host and database")
        return value
