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
    """Render a short decision-focused briefing; raw metrics stay in the JSON API."""
    data = _data(result)
    metrics = data.get("metrics", {})
    coverage = data.get("coverage") or {}
    as_of = str(data.get("snapshot_as_of", "Unknown"))
    lines = ["Betafolio briefing", f"Holdings as of {as_of}"]
    body = f'<p class="date">Holdings as of {escape(as_of)}</p>'

    def section(title: str) -> None:
        nonlocal body
        lines.append(title)
        body += f'<h2>{escape(title)}</h2>'

    def paragraph(value: str) -> None:
        nonlocal body
        lines.append(value)
        body += f'<p>{escape(value)}</p>'

    def table(rows: list[tuple[str, str]]) -> None:
        nonlocal body
        body += '<table><caption>Portfolio measures</caption><tbody>'
        for label, value in rows:
            lines.append(f"{label}: {value}")
            body += f'<tr><th scope="row">{escape(label)}</th><td>{escape(value)}</td></tr>'
        body += '</tbody></table>'

    if percent_only:
        section("Portfolio percentages")
        paragraph("Percentage-only view. Narrative and sources are omitted to protect portfolio amounts.")
        rows = _metric_rows(_percent_metrics(metrics))
        if rows:
            table(rows)
        if data.get("warnings"):
            paragraph("Coverage or freshness limitations apply. Review the dashboard before relying on these figures.")
    else:
        if coverage.get("summary"):
            paragraph(str(coverage["summary"]))
        findings = [_data(item) for item in data.get("findings", [])]
        substantive = [item for item in findings if item.get("title") != "Portfolio overview"]
        substantive.sort(key=lambda item: (not bool(item.get("evidence_ids")), str(item.get("kind", "")) != "risk"))
        selected = substantive[:4]
        source_map = {source["id"]: source for entry in data.get("sources", []) if (source := _data(entry)).get("id")}
        if selected:
            section("What matters now")
            for item in selected:
                title, detail = str(item.get("title", "Finding")), str(item.get("detail", ""))
                lines.extend((title, detail))
                body += f'<article class="finding"><h3>{escape(title)}</h3><p>{escape(detail)}</p>'
                context = []
                if item.get("event_date"):
                    context.append(f"Event date: {item['event_date']}")
                if item.get("affected_symbols"):
                    context.append("Affected: " + ", ".join(item["affected_symbols"]))
                if item.get("portfolio_relevance"):
                    context.append(str(item["portfolio_relevance"]))
                if item.get("uncertainty"):
                    context.append("Limit: " + str(item["uncertainty"]))
                for note in context:
                    lines.append(note)
                    body += f'<p class="finding-context">{escape(note)}</p>'
                for source_id in item.get("evidence_ids", []):
                    source = source_map.get(source_id)
                    if source and _safe_url(str(source.get("url", ""))):
                        url = str(source["url"])
                        label = str(source.get("title") or source.get("publisher") or "Source")
                        lines.append(f"Source: {label}: {url}")
                        body += f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a>'
                body += '</article>'
        else:
            section("What matters now")
            paragraph("No supported portfolio insight is available for this run. Review the portfolio measures and data limitations below.")
        position_news = coverage.get("position_news") or metrics.get("position_news", [])
        if position_news:
            section("News affecting your holdings")
            details = {item["symbol"]: item for item in metrics.get("position_details", [])}
            detailed = data.get("report_length") == "detailed" or metrics.get("report_length") == "detailed"
            entries = position_news if detailed else [item for item in position_news if item.get("status") == "verified"]
            for item in entries:
                symbol = item["symbol"]
                detail = details.get(symbol, {})
                if detailed:
                    context = [f"price ${detail['price']}" if detail.get("price") is not None else "price unavailable",
                               f"previous close ${detail['previous_close']}" if detail.get("previous_close") is not None else "previous close unavailable",
                               detail.get("asset_type") or "type unknown"]
                    if detail.get("sector"):
                        context.append(f"sector {detail['sector']}")
                    quote_date = metrics.get("market_data_as_of", {}).get(symbol, {}).get("trading_date")
                    if quote_date:
                        context.append(f"closing price dated {quote_date}")
                    try:
                        price, previous = float(detail["price"]), float(detail["previous_close"])
                        if previous > 0:
                            context.append(f"change from previous close {(price / previous - 1) * 100:+.2f}%")
                    except (TypeError, ValueError, KeyError):
                        pass
                    paragraph(f"{symbol}: {', '.join(context)}.")
                linked = False
                for source_id in item.get("source_ids", []):
                    source = source_map.get(source_id)
                    if not source or source.get("content_type") not in {"retrieved_page", "official_feed"} or not _safe_url(str(source.get("url", ""))):
                        continue
                    date = str(source.get("published_at") or "date unavailable")[:10]
                    label = str(source.get("title") or source.get("publisher") or "Source")
                    lines.append(f"{symbol}: {label} ({date}): {source['url']}")
                    body += f'<p><strong>{escape(symbol)}</strong>: <a href="{escape(source["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a> <small>{escape(date)}</small></p>'
                    linked = True
                if detailed and not linked:
                    paragraph(f"{symbol}: no verified recent position news found" if item.get("status") != "not_checked" else f"{symbol}: news was not checked in this run")
            quiet = [item["symbol"] for item in position_news if item.get("status") != "verified"]
            if not detailed and quiet:
                paragraph(f"No verified recent position news for {', '.join(quiet[:12])}{' and others' if len(quiet) > 12 else ''}. Check coverage notes for search limits and connection status.")
            section("Market and policy developments")
            setup_ids = metrics.get("setup_news_source_ids", [])
            if setup_ids:
                paragraph("These developments may matter to portfolio exposure; the source alone does not establish a price effect.")
            for source_id in setup_ids:
                source = source_map.get(source_id)
                if source and _safe_url(str(source.get("url", ""))):
                    label = str(source.get("title") or "Source")
                    lines.append(f"{label}: {source['url']}")
                    body += f'<p><a href="{escape(source["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a></p>'
            if not setup_ids:
                status = metrics.get("setup_news_status")
                paragraph({"unavailable": "Market and policy news search was unavailable for this run.",
                           "not_checked": "Market and policy news was not checked within this run's search limit.",
                           "search_failed": "Market and policy news search failed for this run."}.get(
                               status, "No verified recent market or policy development was found for this run."))
        rows = []
        missing = coverage.get("missing_price_symbols") or metrics.get("missing_prices", [])
        unsupported = coverage.get("unsupported_symbols") or metrics.get("unsupported", [])
        excluded = list(dict.fromkeys([*missing, *unsupported]))
        for key, label in (("known_value", "Known valued assets"), ("cash_weight_pct", "Cash allocation of valued assets"),
                           ("largest_position_pct", "Largest valued holding"), ("total_positions", "Holdings")):
            if key == "largest_position_pct" and not metrics.get("total_positions"):
                continue
            if key in metrics and metrics[key] is not None:
                try:
                    number = float(metrics[key])
                    value = f"${number:,.2f}" if key == "known_value" else (f"{number:.2f}%" if key.endswith("pct") else str(int(number)))
                    if excluded and key in {"known_value", "cash_weight_pct", "largest_position_pct"}:
                        value += f"; {', '.join(excluded[:5])} excluded"
                    rows.append((label, value))
                except (TypeError, ValueError, OverflowError):
                    continue
        if rows:
            section("Portfolio at a glance")
            table(rows)
        if data.get("suggestions"):
            section("Possible adjustments")
            for suggestion in data["suggestions"][:4]:
                paragraph(str(suggestion))
        issues = [str(item.get("message", "")) for item in coverage.get("issues", []) if item.get("message")]
        warnings = list(dict.fromkeys(str(warning) for warning in data.get("warnings", [])))
        actionable = issues if coverage else [warning for warning in warnings if any(word in warning.lower() for word in ("unpriced", "unsupported", "stale", "unavailable", "disabled", "identity", "research coverage"))]
        if actionable:
            section("Limits of this briefing")
            for warning in actionable[:4]:
                paragraph(warning)
        if data.get("error"):
            paragraph("Analysis could not be completed. Check the dashboard for recovery steps.")

    style = ('body{font:16px/1.55 system-ui,sans-serif;color:#182629;background:#f4f6f3;margin:0;padding:20px}'
             'main{max-width:760px;margin:auto;background:white;padding:clamp(20px,5vw,42px)}'
             'h1{font-size:2rem;line-height:1.15;margin:.2em 0}h2{font-size:1.2rem;margin:2em 0 .6em}'
             'h3{font-size:1rem;margin:0 0 .4em}.date,caption{color:#536366}p{margin:.4em 0 1em}'
             '.finding{border-top:1px solid #cbd5d0;padding:1em 0}.finding a{display:inline-block;margin:.2em .7em .3em 0}'
             '.finding-context{font-size:.9rem;color:#536366}'
             'table{border-collapse:collapse;width:100%}caption{text-align:left}th,td{padding:.55em 0;border-bottom:1px solid #dbe2df}'
             'th{text-align:left;font-weight:500}td{text-align:right;font-variant-numeric:tabular-nums}'
             'a{color:#155c64}a:focus-visible{outline:3px solid #c47528;outline-offset:3px}')
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Betafolio briefing</title><style>{style}</style></head><body><main><h1>Betafolio briefing</h1>{body}</main></body></html>')
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
    message["Subject"] = "Your Betafolio briefing"
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
