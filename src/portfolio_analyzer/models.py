"""Versioned, serializable contracts shared by all research callers."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Position(Contract):
    symbol: str = Field(min_length=1, max_length=40)
    quantity: Decimal = Field(ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    asset_type: str = "stock"
    sector: str | None = None
    previous_close: Decimal | None = Field(default=None, ge=0)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if not value or not all(c.isalnum() or c in ".-^=/" for c in value):
            raise ValueError("Symbol contains unsupported characters")
        return value


class PortfolioSnapshot(Contract):
    as_of: datetime
    positions: list[Position] = Field(default_factory=list, max_length=1000)
    cash: Decimal = Field(default=Decimal("0"), ge=0)
    source: str = "supplied"
    account_id: str = "portfolio"
    unsupported: list[str] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Snapshot timestamp must include a timezone")
        return value


class InvestmentProfile(Contract):
    objectives: str = Field(min_length=1, max_length=2000)
    horizon: str = Field(min_length=1, max_length=500)
    risk_tolerance: str = Field(min_length=1, max_length=500)
    liquidity_needs: str = Field(min_length=1, max_length=1000)
    allocation_targets: dict[str, float] = Field(min_length=1, max_length=30)
    restrictions: list[str] = Field(max_length=30)

    @field_validator("objectives", "horizon", "risk_tolerance", "liquidity_needs")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Investment profile values cannot be blank")
        return value.strip()

    @field_validator("allocation_targets")
    @classmethod
    def valid_targets(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not name.strip() or len(name) > 100 or not 0 <= weight <= 100 for name, weight in value.items()) or sum(value.values()) > 100.00001:
            raise ValueError("Allocation target percentages must be between 0 and 100 and total at most 100")
        return value

    @field_validator("restrictions")
    @classmethod
    def bounded_restrictions(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("Each restriction must be nonblank and at most 500 characters")
        return value


class AnalysisRequest(Contract):
    snapshot: PortfolioSnapshot
    previous_snapshot: PortfolioSnapshot | None = None
    question: str = Field(default="", max_length=4000)
    include_suggestions: bool = False
    investment_profile: dict[str, Any] | None = None
    benchmark: str = Field(default="SPY", max_length=40)
    report_length: Literal["brief", "detailed"] = "brief"
    max_searches: int = Field(default=8, ge=0, le=30)
    max_seconds: int = Field(default=120, ge=1, le=600)
    max_data_age_hours: int = Field(default=72, ge=1, le=8760)
    position_review_threshold_pct: float = Field(default=25, gt=0, le=100)
    sector_review_threshold_pct: float = Field(default=40, gt=0, le=100)
    reporting_cutoff: datetime | None = None

    @field_validator("benchmark")
    @classmethod
    def benchmark_symbol(cls, value: str) -> str:
        return Position.normalize_symbol(value) if value.strip() else ""

    @field_validator("reporting_cutoff")
    @classmethod
    def aware_cutoff(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Reporting cutoff must include a timezone")
        return value


class Evidence(Contract):
    id: str
    url: str
    title: str
    publisher: str
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    excerpt: str = ""
    content_type: str = "search_excerpt"


class Finding(Contract):
    title: str
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)
    kind: str = "observation"


class AnalysisResult(Contract):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["success", "partial", "failed"] = "success"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    snapshot_as_of: datetime
    metrics: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    sources: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    error: str | None = None


class ResearchNarrative(Contract):
    findings: list[Finding] = Field(default_factory=list, max_length=30)
    suggestions: list[str] = Field(default_factory=list, max_length=10)
