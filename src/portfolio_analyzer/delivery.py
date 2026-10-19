"""Report rendering and explicit, owner-only SMTP delivery."""
from __future__ import annotations

from email.message import EmailMessage
from datetime import date, datetime
from html import escape
import math
import re
import smtplib
import ssl
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def human_date(value: Any, timezone: str = "America/New_York") -> str:
    """Format presentation dates without moving date-only market observations."""
    raw = str(value or "")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed_date = date.fromisoformat(raw)
            return f"{parsed_date:%b} {parsed_date.day}, {parsed_date.year}"
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return raw
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("America/New_York")
        local = parsed.astimezone(zone)
        return f"{local:%b} {local.day}, {local.year}, {local.hour % 12 or 12}:{local:%M %p %Z}"
    except (ValueError, TypeError):
        return raw or "Unknown"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _price_change(detail: dict) -> float | None:
    price, previous = _number(detail.get("price")), _number(detail.get("previous_close"))
    return (price / previous - 1) * 100 if price is not None and previous is not None and previous > 0 else None


def _opaque_aggregator(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
        return host in {"news.google.com", "www.bing.com", "bing.com"}
    except ValueError:
        return True


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


def _percentage_rows(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [row for key, item in value.items() for row in _percentage_rows(item, (*path, str(key)))]
    if isinstance(value, list):
        return [row for index, item in enumerate(value, 1) for row in _percentage_rows(item, (*path, str(index)))]
    number = _number(value)
    if number is None or isinstance(value, bool):
        return []
    if not any("pct" in key.lower() or "percent" in key.lower() for key in path):
        number *= 100
    label = " / ".join(_LABELS.get(key, key if key.isupper() else key.replace("_", " ").capitalize()) for key in path)
    if any(key in {"historical_risk", "benchmark_comparison"} for key in path):
        label += " (historical estimate)"
    return [(label, f"{number:.1f}".rstrip("0").rstrip(".") + "%")]


def render_report(result: Any, percent_only: bool = False, embed: bool = False,
                  timezone: str = "America/New_York") -> tuple[str, str]:
    """Render a short decision-focused briefing; raw metrics stay in the JSON API.

    ``embed`` drops the leading page title when the HTML is shown inside the
    dashboard card, which already carries its own heading. Plain-text output and
    email delivery keep the title (``embed`` never affects ``lines``).
    """
    data = _data(result)
    metrics = data.get("metrics", {})
    coverage = data.get("coverage") or {}
    as_of = human_date(data.get("snapshot_as_of"), timezone)
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
        rows = _percentage_rows(_percent_metrics(metrics))
        if rows:
            table(rows)
        if data.get("warnings"):
            paragraph("Coverage or freshness limitations apply. Review the dashboard before relying on these figures.")
    else:
        if coverage.get("summary"):
            section("Bottom line")
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
                    context.append(f"Event date: {human_date(item['event_date'], timezone)}")
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
                        lines.append(f"Source: {label}: " + ("link available in the dashboard" if _opaque_aggregator(url) else url))
                        body += f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a>'
                body += '</article>'
        else:
            section("What matters now")
            paragraph("No supported portfolio insight is available for this run. Review the portfolio measures and data limitations below.")
        position_news = coverage.get("position_news") or metrics.get("position_news", [])
        detailed = data.get("report_length") == "detailed" or metrics.get("report_length") == "detailed"

        def status_sentence(symbol: str, item: dict) -> str:
            return {
                "search_failed": f"{symbol}: news search failed in this run",
                "not_checked": f"{symbol}: news was not checked in this run",
                "discovery_only": f"{symbol}: relevant headlines were found but not verified",
            }.get(item.get("status"), f"{symbol}: no verified recent position news found")

        details = {item["symbol"]: item for item in metrics.get("position_details", [])}
        order = {symbol: index for index, symbol in enumerate(metrics.get("position_news_display_order", []))}
        position_news = sorted(position_news, key=lambda item: order.get(item["symbol"], len(order)))
        if detailed and details:
            section("Holding details")
            columns = ("Symbol", "Price", "Previous close", "Change vs prior close", "Type", "Sector", "Quote date")
            body += '<div class="table-scroll"><table><caption>Holding prices and classifications</caption><thead><tr>'
            body += ''.join(f'<th scope="col">{escape(label)}</th>' for label in columns) + '</tr></thead><tbody>'
            symbols = [item["symbol"] for item in position_news] + [symbol for symbol in details if symbol not in {item["symbol"] for item in position_news}]
            for symbol in symbols:
                detail = details.get(symbol, {})
                price, previous = _number(detail.get("price")), _number(detail.get("previous_close"))
                change = _price_change(detail)
                quote_date = metrics.get("market_data_as_of", {}).get(symbol, {}).get("trading_date")
                values = [symbol, f"${price:,.2f}" if price is not None else "—",
                          f"${previous:,.2f}" if previous is not None else "—",
                          f"{change:+.2f}%" if change is not None else "—",
                          str(detail.get("asset_type") or "—"), str(detail.get("sector") or "—"),
                          human_date(quote_date, timezone) if quote_date else "—"]
                body += '<tr><th scope="row">' + escape(symbol) + '</th>' + ''.join(f'<td>{escape(value)}</td>' for value in values[1:]) + '</tr>'
                sentences = [f"{label.lower()} {value}" for label, value in zip(columns[1:], values[1:]) if value != "—"]
                lines.append(f"{symbol}: " + "; ".join(sentences) + ".")
            body += '</tbody></table></div>'

        def source_link(source: dict, prefix: str = "", story: dict | None = None) -> bool:
            nonlocal body
            url = str(source.get("url", ""))
            if not _safe_url(url):
                return False
            story = story or {}
            title = str(source.get("title") or source.get("publisher") or "Source")
            domain = str(story.get("publisher_domain") or source.get("publisher") or urlsplit(url).hostname or "Unknown publisher")
            tier = story.get("publisher_tier")
            # Presentation must never promote an excerpt after failed retrieval.
            # A cluster may only carry the best grade of evidence it retains.
            content_type = source.get("content_type")
            grade = ("full" if content_type in {"official_feed", "retrieved_page"}
                     else "headline" if content_type == "news_feed" else "discovery")
            evidence_label = {"full": "full source", "headline": "headline only, not verified",
                              "discovery": "discovery only, not verified"}[grade]
            published = human_date(source.get("published_at"), timezone) if source.get("published_at") else "date unavailable"
            context = f"{domain}" + (f" · {tier}" if tier else "") + f" · {published} · {evidence_label}"
            destination = "link available in the dashboard" if _opaque_aggregator(url) else url
            lines.append(f"{prefix}{title} ({context}): {destination}")
            body += f'<p><a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(title)}</a> <small>{escape(context)}</small></p>'
            return True

        def story_entry(story: dict, symbol: str) -> None:
            nonlocal body
            source_id = story.get("representative_source_id")
            source = source_map.get(source_id)
            if source:
                source_link(source, f"{symbol}: ", story)
            related = [source_map[source_id] for source_id in story.get("member_source_ids", []) if source_id in source_map and source_id != story.get("representative_source_id")]
            if related:
                body += f'<details><summary>plus {len(related)} related reports</summary>'
                for source in related:
                    source_link(source, "Related report: ")
                body += '</details>'

        if position_news:
            section("News affecting your holdings")
            for item in position_news:
                symbol = item["symbol"]
                change = _price_change(details.get(symbol, {}))
                headline = symbol + (f" {change:+.1f}% vs prior close" if change is not None and not detailed else "")
                body += f'<h3>{escape(headline)}</h3>'
                lines.append(headline)
                stories = item.get("stories")
                linked = False
                if stories is not None:
                    for category, label in (("event", "Events"), ("also", "Also in the news")):
                        selected_stories = [story for story in stories if story.get("category", "also") == category]
                        if selected_stories:
                            body += f'<h4>{label}</h4>'
                            lines.append(label)
                            for story in selected_stories:
                                story_entry(story, symbol)
                                linked = True
                    if item.get("additional_stories"):
                        body += '<details><summary>More stories</summary>'
                        lines.append(f"More stories for {symbol}")
                        for story in item["additional_stories"]:
                            story_entry(story, symbol)
                        body += '</details>'
                else:
                    for source_id in item.get("source_ids", []):
                        source = source_map.get(source_id)
                        if source and source.get("content_type") in {"retrieved_page", "official_feed", "news_feed"}:
                            linked = source_link(source, f"{symbol}: ") or linked
                if not linked or item.get("status") == "discovery_only":
                    paragraph(status_sentence(symbol, item))
        section("Market and policy developments")
        setup_ids = metrics.get("setup_news_source_ids", [])
        if setup_ids:
            paragraph("These developments may matter to portfolio exposure; the source alone does not establish a price effect.")
        for source_id in setup_ids:
            source = source_map.get(source_id)
            if source:
                source_link(source)
        if not setup_ids:
            status = metrics.get("setup_news_status")
            reasons = {
                "insufficient_holdings": "Market and policy news was not checked because broad search requires at least two holdings.",
                "budget_exhausted": "Market and policy news was not checked because the search or time budget was exhausted.",
                "source_limit": "Market and policy news was not checked because the source limit was reached.",
                "unsupported_provider": "The configured provider cannot serve market-wide news queries.",
                "research_disabled": "Market and policy news was not checked because current-event research is disabled.",
                "request_failed": "Market and policy news search failed for this run.",
            }
            paragraph(reasons.get(metrics.get("setup_news_reason")) or {"unavailable": "Market and policy news search was unavailable for this run.",
                       "not_checked": "Market and policy news was not checked within this run's search limit.",
                       "search_failed": "Market and policy news search failed for this run."}.get(
                           status, "No verified recent market or policy development was found for this run."))
        rows = []
        missing = coverage.get("missing_price_symbols") or metrics.get("missing_prices", [])
        unsupported = coverage.get("unsupported_symbols") or metrics.get("unsupported", [])
        excluded = list(dict.fromkeys([*missing, *unsupported]))
        for key, label in (("known_value", "Known portfolio value"), ("cash_weight_pct", "Cash share of holdings we could price"),
                           ("largest_position_pct", "Largest holding we could price"), ("total_positions", "Holdings")):
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
        notes = []
        covered_messages = set(issues)
        seen_steps = set()
        for issue in sorted(coverage.get("issues", []), key=lambda item: not bool(item.get("action"))):
            step = {"portfolio": "Review holdings and cash, then add any missing prices.",
                    "research": "Set up and test current-event search in the research connections.",
                    "model": "Configure and test a research model for written event analysis."}.get(issue.get("action"))
            message = str(issue.get("message") or "")
            note = (message + (" " + step if step and step not in seen_steps else "")).strip()
            if step:
                seen_steps.add(step)
            if note and note not in notes:
                notes.append(note)
        for warning in warnings:
            if warning not in covered_messages and warning not in notes:
                notes.append(warning)
        if data.get("error"):
            notes.insert(0, "Analysis could not be completed. Check the dashboard for recovery steps.")
        if notes:
            section("Data notes")
            for note in notes[:2]:
                paragraph(note)
            if len(notes) > 2:
                body += f'<details><summary>{len(notes) - 2} more data notes</summary>'
                for note in notes[2:]:
                    body += f'<p>{escape(note)}</p>'
                body += '</details>'
                lines.append("Additional data notes:\n" + "\n".join(f"- {note}" for note in notes[2:]))

    style = ('body{font:16px/1.55 system-ui,sans-serif;color:#182629;background:#f4f6f3;margin:0;padding:20px}'
             'main{max-width:760px;margin:auto;background:white;padding:clamp(20px,5vw,42px)}'
             'h1{font-size:2rem;line-height:1.15;margin:.2em 0}h2{font-size:1.2rem;margin:2em 0 .6em}'
             'h3{font-size:1rem;margin:0 0 .4em}.date,caption{color:#536366}p{margin:.4em 0 1em}'
             '.finding{border-top:1px solid #cbd5d0;padding:1em 0}.finding a{display:inline-block;margin:.2em .7em .3em 0}'
             '.finding-context{font-size:.9rem;color:#536366}'
             'table{border-collapse:collapse;width:100%}caption{text-align:left}th,td{padding:.55em 0;border-bottom:1px solid #dbe2df}'
             '.table-scroll{overflow-x:auto}.table-scroll th,.table-scroll td{padding:.55em .65em}.table-scroll th:first-child{padding-left:0}'
             'th{text-align:left;font-weight:500}td{text-align:right;font-variant-numeric:tabular-nums}'
             'a{color:#155c64}a:focus-visible{outline:3px solid #c47528;outline-offset:3px}')
    heading = "" if embed else "<h1>Betafolio briefing</h1>"
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Betafolio briefing</title><style>{style}</style></head><body><main>{heading}{body}</main></body></html>')
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


def send_report(result: Any, config: SMTPConfig, password: str, verified_recipient: str | None,
                timezone: str = "America/New_York") -> DeliveryOutcome:
    if not verified_recipient or config.recipient != verified_recipient:
        return DeliveryOutcome(status="failed", message="Verify the configured owner email address before sending reports.")
    html, plain = render_report(result, config.percent_only, timezone=timezone)
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
