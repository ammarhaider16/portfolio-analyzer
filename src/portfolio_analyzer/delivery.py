"""Report rendering and explicit, owner-only SMTP delivery."""
from __future__ import annotations

from email.message import EmailMessage
from html import escape
import re
import smtplib
import ssl
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SMTPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = Field(default=465, ge=1, le=65535)
    username: str = ""
    sender: str
    recipient: str
    tls_mode: Literal["implicit", "starttls"] = "implicit"
    plain_text_only: bool = False
    percent_only: bool = False

    @field_validator("sender", "recipient")
    @classmethod
    def single_address(cls, value: str) -> str:
        # Bare addresses only: no lists, display names, or header injection.
        if not re.fullmatch(r"[^\s@<>,;:\"\\]+@[^\s@<>,;:\"\\]+\.[^\s@<>,;:\"\\]+", value):
            raise ValueError("Enter one bare email address")
        return value

    @field_validator("host", "username")
    @classmethod
    def no_newlines(cls, value: str) -> str:
        if any(c in value for c in "\r\n\x00"):
            raise ValueError("Invalid connection setting")
        return value


class DeliveryOutcome(BaseModel):
    status: Literal["sent", "failed", "uncertain"]
    message: str


def _data(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _safe_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username
    except ValueError:
        return False


def _percent_metrics(value: Any, percentage_context: bool = False) -> Any:
    """Keep only explicitly identified percentage/weight fields; fail closed."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            is_percentage = percentage_context or any(token in key.lower() for token in ("percent", "weight", "_pct", "ratio"))
            if is_percentage and isinstance(item, (int, float)) and not isinstance(item, bool):
                out[key] = item
            elif isinstance(item, (dict, list)):
                filtered = _percent_metrics(item, is_percentage)
                if filtered:
                    out[key] = filtered
        return out
    if isinstance(value, list):
        return [filtered for item in value if (filtered := _percent_metrics(item, percentage_context))]
    return {}


_LABELS = {
    "known_value": "Portfolio value (priced holdings and cash)",
    "cash": "Cash balance", "cash_weight_pct": "Cash allocation",
    "position_values": "Holding values", "weights_pct": "Holding allocations",
    "sector_weights_pct": "Sector allocations", "largest_position_pct": "Largest holding allocation",
    "daily_price_contributions": "Estimated daily price contributions",
    "daily_price_change_estimate": "Estimated daily price change",
    "priced_positions": "Positions with prices", "total_positions": "Total positions",
    "missing_prices": "Positions missing prices", "unsupported": "Unsupported holdings",
    "coverage_complete": "Complete valuation coverage", "cash_weight": "Cash allocation",
    "annualized_volatility_pct": "Estimated annualized volatility",
    "maximum_drawdown_pct": "Estimated maximum drawdown",
    "covered_known_value_pct": "History coverage of priced portfolio",
}


def _metric_rows(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    rows = []
    if isinstance(value, dict):
        for key, item in value.items():
            rows.extend(_metric_rows(item, (*path, str(key))))
    elif isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            rows.append((" / ".join(_LABELS.get(key, key.replace("_", " ").capitalize()) for key in path), ", ".join(map(str, value)) or "None"))
        else:
            for index, item in enumerate(value, 1):
                rows.extend(_metric_rows(item, (*path, str(index))))
    else:
        label = " / ".join(_LABELS.get(key, key if key.isupper() else key.replace("_", " ").capitalize()) for key in path)
        percentage = any("pct" in key or "percent" in key for key in path)
        money = any(key in {"known_value", "cash", "position_values", "daily_price_contributions", "daily_price_change_estimate", "total_value"} for key in path)
        if value is None:
            display = "Not available"
        elif isinstance(value, bool):
            display = "Yes" if value else "No — review coverage notes"
        elif percentage or money:
            try:
                display = f"{float(value):,.2f}%" if percentage else f"${float(value):,.2f}"
            except (ValueError, TypeError):
                display = str(value)
        else:
            display = str(value)
        rows.append((label, display))
    return rows


def render_report(result: Any, percent_only: bool = False) -> tuple[str, str]:
    """Return escaped standalone HTML and equivalent plain text, without remote assets."""
    data = _data(result)
    heading = "Portfolio briefing"
    as_of = str(data.get('snapshot_as_of', 'Unknown'))
    status = str(data.get('status', 'unknown')).capitalize()
    lines = [heading, f"As of: {as_of}", f"Status: {status}"]
    body = f'<p style="color:#526577">As of {escape(as_of)}<br>Status: {escape(status)}</p>'

    def paragraph(text: str) -> None:
        nonlocal body
        lines.append(text)
        body += f'<p style="line-height:1.6">{escape(text)}</p>'

    def section(title: str) -> None:
        nonlocal body
        lines.append(title)
        body += f'<h2 style="font-size:20px;margin-top:28px;color:#173f55">{escape(title)}</h2>'

    metrics = data.get("metrics", {})
    if percent_only:
        metrics = _percent_metrics(metrics)
        paragraph("Percentage-only view. Narrative and sources are omitted to protect portfolio amounts.")
        if data.get("warnings"):
            paragraph("Coverage or freshness limitations apply. Review the dashboard before relying on these figures.")
    section("Portfolio at a glance")
    rows = _metric_rows(metrics)
    if rows:
        body += '<table style="border-collapse:collapse;width:100%;font-size:14px"><caption style="text-align:left;padding:0 0 12px;color:#526577">Calculated portfolio metrics</caption><thead><tr><th scope="col" style="text-align:left;padding:10px;background:#edf4f6">Measure</th><th scope="col" style="text-align:right;padding:10px;background:#edf4f6">Value</th></tr></thead><tbody>'
        for label, value in rows:
            lines.append(f"{label}: {value}")
            body += f'<tr><th scope="row" style="text-align:left;font-weight:normal;padding:10px;border-bottom:1px solid #dce5ea">{escape(label)}</th><td style="text-align:right;padding:10px;border-bottom:1px solid #dce5ea">{escape(value)}</td></tr>'
        body += '</tbody></table>'
    else:
        paragraph("No calculated metrics are available for this report.")
    if not percent_only:
        findings = [_data(item) for item in data.get("findings", [])]
        for title, selected in (("Research findings", [item for item in findings if "risk" not in str(item.get("kind", "")).lower()]), ("Risks to review", [item for item in findings if "risk" in str(item.get("kind", "")).lower()])):
            if selected:
                section(title)
                for item in selected:
                    finding_title = str(item.get("title", "Finding"))
                    lines.append(finding_title)
                    body += f'<h3 style="font-size:16px;margin-bottom:6px">{escape(finding_title)}</h3>'
                    paragraph(str(item.get("detail", "")))
                    if item.get("evidence_ids"):
                        paragraph("Evidence: " + ", ".join(map(str, item["evidence_ids"])))
        if data.get("suggestions"):
            section("Possible adjustments")
            for suggestion in data["suggestions"]:
                paragraph(str(suggestion))
        if data.get("warnings"):
            section("Coverage and freshness")
            for warning in data["warnings"]:
                paragraph(str(warning))
        if data.get("error"):
            paragraph("Analysis could not be completed. Check the dashboard for recovery steps.")
    if not percent_only:
        sources_started = False
        for source in data.get("sources", []):
            source = _data(source)
            url = str(source.get("url", ""))
            if _safe_url(url):
                if not sources_started:
                    section("Evidence and sources")
                    sources_started = True
                title = str(source.get("title", "Source"))
                description = f"[{source.get('id', '')}] {title} — {source.get('publisher', 'Source')}"
                lines.append(f"{description}: {url}")
                body += f'<p style="line-height:1.6"><a style="color:#126181" href="{escape(url, quote=True)}">{escape(description)}</a></p>'
    html = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{heading}</title></head><body style="font-family:Arial,sans-serif;margin:0;padding:24px;background:#f3f6f8;color:#172b3a"><main style="max-width:760px;margin:auto;background:#ffffff;padding:28px;border-top:5px solid #146575"><p style="font-size:12px;letter-spacing:2px;color:#146575">PORTFOLIO RESEARCH</p><h1 style="font-size:30px;margin-bottom:12px">{heading}</h1>{body}</main></body></html>'
    return html, "\n\n".join(lines)


def _send(message: EmailMessage, config: SMTPConfig, password: str) -> DeliveryOutcome:
    client = None
    submitting = False
    accepted = False
    try:
        context = ssl.create_default_context()
        if config.tls_mode == "implicit":
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=30, context=context)
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=30)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if config.username:
            client.login(config.username, password)
        submitting = True
        refused = client.send_message(message, from_addr=config.sender, to_addrs=[config.recipient])
        if refused:
            return DeliveryOutcome(status="failed", message="The mail server refused the recipient.")
        accepted = True
        return DeliveryOutcome(status="sent", message="The mail server accepted the message.")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
        return DeliveryOutcome(status="failed", message="The mail server rejected the message.")
    except (OSError, smtplib.SMTPException):
        return DeliveryOutcome(status="uncertain" if submitting and not accepted else "failed", message="Delivery acceptance is uncertain; check your inbox before resending." if submitting else "Unable to connect or authenticate securely. Check email settings.")
    finally:
        if client is not None:
            # Closing locally avoids interpreting a QUIT failure as failed delivery.
            try:
                client.close()
            except OSError:
                pass


def send_report(result: Any, config: SMTPConfig, password: str, verified_recipient: str | None) -> DeliveryOutcome:
    if not verified_recipient or config.recipient != verified_recipient:
        return DeliveryOutcome(status="failed", message="Verify the configured owner email address before sending reports.")
    html, plain = render_report(result, config.percent_only)
    message = EmailMessage()
    message["Subject"] = "Your portfolio briefing"
    message["From"] = config.sender
    message["To"] = config.recipient
    message.set_content(plain)
    if not config.plain_text_only:
        message.add_alternative(html, subtype="html")
    return _send(message, config, password)


def send_verification(config: SMTPConfig, password: str, code: str) -> DeliveryOutcome:
    if not re.fullmatch(r"[A-Za-z0-9-]{6,64}", code):
        raise ValueError("Invalid verification code")
    message = EmailMessage()
    message["Subject"] = "Verify your portfolio briefing email"
    message["From"] = config.sender
    message["To"] = config.recipient
    message.set_content(f"Your verification code is: {code}\n\nEnter it in the dashboard to enable report delivery.")
    return _send(message, config, password)
