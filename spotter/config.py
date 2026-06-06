"""Load and validate Spotter configuration with Pydantic."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from spotter.errors import ConfigError

DEFAULT_MODEL = "claude-sonnet-4-6"


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from a local .env file into the process environment."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


def require_non_empty_path(value: Any) -> Any:
    """Reject empty configured paths before Pydantic converts them to Path."""
    if isinstance(value, str) and not value.strip():
        raise ValueError("Path must be a non-empty string.")
    return value


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
UnitFloat = Annotated[float, Field(ge=0, le=1)]
ConfiguredPath = Annotated[
    Path,
    BeforeValidator(require_non_empty_path),
    AfterValidator(lambda value: value.expanduser()),
]


class ConfigModel(BaseModel):
    """Base configuration model with strict, immutable, closed schemas."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class Topic(ConfigModel):
    id: NonEmptyStr
    name: NonEmptyStr
    description: NonEmptyStr
    threshold: UnitFloat = 0.75


class WhatsAppConfig(ConfigModel):
    db_path: ConfiguredPath
    initial_backfill_days: PositiveInt = 14
    include_own_messages: bool = False
    max_messages_per_run: PositiveInt = 2000
    batch_size: PositiveInt = 200


class LlmConfig(ConfigModel):
    model: NonEmptyStr = DEFAULT_MODEL
    max_tokens: PositiveInt = 4000
    retry_max_tokens: PositiveInt = 4000
    temperature: float | None = 0
    timeout_seconds: PositiveFloat = 120
    max_retries: NonNegativeInt = 3
    use_output_config: bool = True

    @model_validator(mode="after")
    def validate_retry_max_tokens(self) -> Self:
        if self.retry_max_tokens < self.max_tokens:
            raise ValueError("llm.retry_max_tokens must be greater than or equal to llm.max_tokens.")
        return self


class NotificationConfig(ConfigModel):
    macos: bool = True
    pushover: bool = False
    title: NonEmptyStr = "WhatsApp topic match"
    sound_name: str | None = None
    max_body_chars: PositiveInt = 180
    pushover_device: str | None = None
    pushover_priority: int | None = None
    pushover_sound_name: str | None = None
    pushover_url: str | None = None
    pushover_url_title: str | None = None


class LaunchAgentConfig(ConfigModel):
    label: NonEmptyStr = "com.example.spotter"
    start_interval_seconds: PositiveInt = 1800
    run_at_load: bool = True


class LoggingConfig(ConfigModel):
    dir: ConfiguredPath = Path("~/Library/Logs/spotter")
    file: NonEmptyStr = "spotter.log"
    level: NonEmptyStr = "INFO"

    @property
    def path(self) -> Path:
        return self.dir / self.file


class FilesConfig(ConfigModel):
    state: ConfiguredPath
    alerts: ConfiguredPath
    errors: ConfiguredPath
    usage: ConfiguredPath | None = None


class AppConfig(ConfigModel):
    whatsapp: WhatsAppConfig
    llm: LlmConfig = Field(default_factory=LlmConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    launch_agent: LaunchAgentConfig = Field(default_factory=LaunchAgentConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    files: FilesConfig
    topics: Annotated[tuple[Topic, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_unique_topic_ids(self) -> Self:
        topic_ids = [topic.id for topic in self.topics]
        duplicates = sorted({topic_id for topic_id in topic_ids if topic_ids.count(topic_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate topic ids: {', '.join(duplicates)}")
        return self


def load_config(path: Path) -> AppConfig:
    """Read and validate a Spotter configuration JSON file."""
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}. Copy config.example.json to {path} and edit your topics.")
    try:
        return AppConfig.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {path}: {exc}") from exc


def parse_config(raw: Any) -> AppConfig:
    """Validate a decoded JSON-compatible value into typed configuration."""
    try:
        encoded = json.dumps(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Config must be JSON-compatible: {exc}") from exc
    try:
        return AppConfig.model_validate_json(encoded)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config: {exc}") from exc
