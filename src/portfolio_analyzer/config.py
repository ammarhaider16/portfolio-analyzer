"""Validated, nonsecret configuration. Secrets live in the credential store."""
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .delivery import SMTPConfig
from .scheduling import SchedulePolicy
from .source_status import valid_sec_contact


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["colgate", "openai", "anthropic", "custom"] = "colgate"
    model: str = "openai/gpt-oss-120b"
    write_model: str | None = None
    base_url: str = "https://litellm.colgate.edu"
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=3000, ge=256, le=16000)
    request_timeout_seconds: int = Field(default=90, ge=10, le=300)

    @model_validator(mode="before")
    @classmethod
    def provider_endpoint(cls, data):
        if isinstance(data, dict) and "base_url" not in data:
            endpoints = {"colgate": "https://litellm.colgate.edu", "openai": "https://api.openai.com/v1",
                         "anthropic": "https://api.anthropic.com", "custom": "http://127.0.0.1:4000/v1"}
            data = {**data, "base_url": endpoints.get(data.get("provider", "colgate"), "https://litellm.colgate.edu")}
        return data

    @field_validator("write_model", mode="before")
    @classmethod
    def validate_write_model(cls, value):
        if value is None:
            return value
        if not isinstance(value, str):
            raise ValueError("Write model must be a model identifier")
        value = value.strip()
        if not value:
            raise ValueError("Write model must not be empty")
        if len(value) > 100:
            raise ValueError("Write model must be at most 100 characters")
        return value

    @field_validator("base_url")
    @classmethod
    def safe_endpoint(cls, value):
        parsed = urlsplit(value)
        if parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("Enter an API base URL without credentials")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("API connections require HTTPS, except an explicitly configured local model server")
        return value.rstrip("/")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelConfig = Field(default_factory=ModelConfig)
    model_enabled: bool = False
    search_provider: Literal["tavily", "keyless", "disabled"] = "keyless"
    # market_data_enabled controls research-time enrichment (daily closes and history
    # during an analysis run); auto_prices_enabled instead polls quotes on a schedule
    # and keeps the saved portfolio's prices current. Manual/CSV prices remain an
    # offline fallback when both are off.
    market_data_enabled: bool = False
    auto_prices_enabled: bool = True
    price_refresh_minutes: int = Field(default=15, ge=1, le=1440)
    primary_feeds_enabled: bool = True
    sec_company_filings_enabled: bool = False
    ticker_news_enabled: bool = True
    news_feed_sources: list[str] = Field(default_factory=lambda: ["google_news", "nasdaq"])
    sec_contact: str = ""
    max_searches: int = Field(default=100, ge=0, le=500)
    max_model_calls: int = Field(default=64, ge=1, le=256)
    max_tool_calls: int = Field(default=200, ge=0, le=1000)
    max_seconds: int = Field(default=600, ge=1, le=1800)
    max_data_age_hours: int = Field(default=72, ge=1, le=8760)
    benchmark: str = "SPY"
    position_review_threshold_pct: float = Field(default=25, gt=0, le=100)
    sector_review_threshold_pct: float = Field(default=40, gt=0, le=100)
    investment_profile: dict | None = None
    report_length: Literal["brief", "detailed"] = "brief"
    retention_days: int = Field(default=90, ge=1, le=3650)
    schedule: SchedulePolicy = Field(default_factory=SchedulePolicy)
    scheduled_email: bool = False
    smtp: SMTPConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def default_sec_for_contact(cls, data):
        if isinstance(data, dict) and "sec_company_filings_enabled" not in data and valid_sec_contact(data.get("sec_contact")):
            data = {**data, "sec_company_filings_enabled": True}
        return data

    @field_validator("sec_contact")
    @classmethod
    def validate_sec_contact(cls, value):
        if value and not valid_sec_contact(value):
            raise ValueError("Enter an identifying contact containing an email address, e.g. Your name contact@example.com")
        return value.strip()

    @field_validator("investment_profile")
    @classmethod
    def validate_profile(cls, value):
        if value is not None:
            from .models import InvestmentProfile
            return InvestmentProfile.model_validate(value).model_dump()
        return value

    @field_validator("news_feed_sources")
    @classmethod
    def validate_news_feed_sources(cls, value):
        allowed = ("google_news", "nasdaq", "bing_news")
        normalized = []
        for item in value:
            name = str(item).strip().lower()
            if name not in allowed:
                raise ValueError(f"Unknown news feed source: {item}")
            if name not in normalized:
                normalized.append(name)
        return normalized


def data_directory() -> Path:
    return Path(os.environ["PORTFOLIO_DATA_DIR"]) if "PORTFOLIO_DATA_DIR" in os.environ else user_data_path("portfolio-analyzer", appauthor=False)


def externally_managed() -> set[str]:
    return {key[len("PORTFOLIO_SETTING_"):].lower() for key in os.environ if key.startswith("PORTFOLIO_SETTING_")}


def load_settings(directory: Path) -> Settings:
    path = directory / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for key in externally_managed():
        value = os.environ["PORTFOLIO_SETTING_" + key.upper()]
        try:
            data[key] = json.loads(value)
        except json.JSONDecodeError:
            data[key] = value
    return Settings.model_validate(data)


def save_settings(directory: Path, settings: Settings) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "settings.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    temp.replace(target)
