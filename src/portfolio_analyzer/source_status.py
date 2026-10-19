"""Safe source availability metadata and SEC request identification checks."""
from datetime import datetime, timezone
import re


def valid_sec_contact(value: str | None) -> bool:
    return bool(isinstance(value, str) and value and not any(ord(char) < 32 or ord(char) == 127 for char in value)
                and re.search(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value))


def source_outcome(status: str, reason: str = "") -> dict:
    category = ""
    if reason in {"HTTP 401", "HTTP 403"}:
        category = "blocked"
    elif "quota" in reason.lower() or reason == "HTTP 429":
        category = "quota"
    elif "subscription" in reason.lower():
        category = "entitlement"
    elif "configured" in reason.lower() or "key is invalid" in reason.lower() or "contact" in reason.lower():
        category = "configuration"
    elif "timed out" in reason.lower():
        category = "timeout"
    elif reason.startswith("HTTP "):
        category = "http"
    elif status == "failed":
        category = "request"
    return {"status": status, "reason": reason, "category": category, "tested_at": datetime.now(timezone.utc).isoformat()}


def safe_source_error(error: Exception) -> str:
    """Do not expose URLs, response bodies, credentials, or arbitrary exception text."""
    safe_reason = getattr(error, "safe_reason", None)
    if safe_reason:
        return safe_reason
    if isinstance(error, TimeoutError):
        return "Source request timed out"
    status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
    return f"HTTP {status}" if isinstance(status, int) else "Source request failed"


def record_outcome(provider, source: str, status: str, reason: str = "") -> None:
    if not hasattr(provider, "source_outcomes"):
        provider.source_outcomes = {}
    old = provider.source_outcomes.get(source)
    # A partial failure must remain visible when a later request succeeds.
    if old and ((old["status"] == "failed" and status != "failed")
                or (old["status"] == "passed" and status == "empty")):
        return
    provider.source_outcomes[source] = source_outcome(status, reason)
