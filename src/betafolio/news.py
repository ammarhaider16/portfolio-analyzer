"""Source text cleanup and duplicate removal before analyst research."""

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


EXCERPT_LIMIT = 5000
_TAG = re.compile(r"<!--.*?-->|</?[A-Za-z][^>]{0,500}>", re.S)
_WHITESPACE = re.compile(r"\s+")
_SEPARATOR = re.compile(r"^(.*\S)\s+[|–—-]\s+(\S(?:.*\S)?)$")


def clean_text(value: str) -> str:
    """Plain visible text: markup and entities are never part of an evidence field."""
    if not value:
        return ""
    text = html.unescape(str(value))
    text = _WHITESPACE.sub(" ", _TAG.sub(" ", html.unescape(_TAG.sub(" ", text))))
    return text.strip()


def _alnum(value: str) -> str:
    return re.sub(r"\W", "", (value or "").casefold())


_ATTRIBUTION_QUALIFIERS = {
    "the",
    "and",
    "com",
    "www",
    "ca",
    "uk",
    "au",
    "in",
    "us",
    "usa",
    "america",
    "canada",
    "australia",
    "india",
    "europe",
    "international",
    "global",
    "staff",
    "wire",
    "news",
}


def clean_feed_title(title: str, publisher: str = "") -> str:
    """Item title with a trailing feed attribution removed only when it duplicates the publisher."""
    cleaned = clean_text(title)[:500]
    match = _SEPARATOR.match(cleaned)
    if not match or not publisher:
        return cleaned
    suffix, owner = _alnum(match.group(2)), _alnum(publisher)
    words = set(re.findall(r"[a-z0-9]+", match.group(2).casefold()))
    duplicated = bool(suffix and owner and (suffix in owner or owner in suffix)) or (
        bool(words)
        and words <= set(re.findall(r"[a-z0-9]+", (publisher or "").casefold())) | _ATTRIBUTION_QUALIFIERS
    )
    return match.group(1).strip() if duplicated else cleaned


# Ingestion hygiene (FR-16): quote widgets, derivatives pages, 13F scraps, mirror duplicates, staleness.
_MIRROR_BASES = (
    "finance.yahoo.com",
    "fool.com",
    "seekingalpha.com",
    "marketwatch.com",
    "investing.com",
    "benzinga.com",
    "zacks.com",
)
_SOURCE_WORDS = {
    "yahoo",
    "finance",
    "fool",
    "motley",
    "marketwatch",
    "seekingalpha",
    "seeking",
    "alpha",
    "investing",
    "benzinga",
    "zacks",
    "reuters",
    "cnbc",
    "bloomberg",
    "ap",
    "insider",
    "monkey",
    "nasdaq",
    "barrons",
    "edition",
} | _ATTRIBUTION_QUALIFIERS
_WIDGET_TITLE = re.compile(r"options? chain|interactive (?:stock )?chart|stock quote", re.I)
_QUOTE_LABEL = re.compile(r"^\s*[A-Z][A-Z.\-]{0,7}\s+(?:Stock\s+)?(?:Quote|Chart|Options?)\b")
_WIDGET_PATH = re.compile(r"/(?:options?-?chain|options?|quotes?|charts?)(?:/|$)", re.I)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def mirror_family(url: str) -> str | None:
    """Publisher base whose country mirrors repeat the same story, or None for independent hosts."""
    host = _host(url)
    for base in _MIRROR_BASES:
        if host == base or host.endswith("." + base):
            return base
    return None


def story_key(source, family: str = "") -> str:
    """Normalized title for duplicate-story comparison, dropping feed attribution tails."""
    parts = re.split(r"\s+[|–—-]\s+", clean_text(source.title).casefold())
    family_words = set(re.findall(r"[a-z0-9]+", family or ""))
    while len(parts) > 1 and len(parts[-1].split()) <= 4:
        words = set(re.findall(r"[a-z0-9]+", parts[-1]))
        if words <= _SOURCE_WORDS or (family_words and words & family_words):
            parts.pop()
        else:
            break
    return " ".join(re.findall(r"[\w]+", " ".join(parts)))


def _junk_reason(source):
    # Exclude quote/derivative widgets, not analyst opinions or research topics.
    probe = f"{source.title or ''} {source.url or ''}"
    return (
        "quote_pages"
        if _WIDGET_TITLE.search(probe)
        or _QUOTE_LABEL.search(source.title or "")
        or _WIDGET_PATH.search(source.url or "")
        else None
    )


def _mirror_dedupe(sources):
    kept, dropped, seen = [], 0, {}
    for source in sources:
        family = mirror_family(source.url)
        if family is None:
            kept.append(source)
            continue
        key = (family, story_key(source, family))
        previous = seen.get(key)
        if previous is None:
            seen[key] = source
            kept.append(source)
            continue
        # Keep one copy per story, preferring the canonical host over country mirrors.
        if _host(source.url) == family and _host(previous.url) != family:
            kept[kept.index(previous)] = source
            seen[key] = source
        else:
            dropped += 1
    return kept, dropped


def source_hygiene(sources, *, reference=None, max_age_hours=None):
    """Clean duplicate and non-article results without imposing editorial relevance."""
    reference = reference or datetime.now(timezone.utc)
    counts = {}
    kept = []
    for source in sources:
        reason = _junk_reason(source)
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
        else:
            kept.append(source)
    kept, mirror_dropped = _mirror_dedupe(kept)
    if mirror_dropped:
        counts["mirror_duplicates"] = mirror_dropped
    # Keep dated background research available; the analyst decides its relevance.
    # Exclude future publications rather than presenting them as known news.
    result = [item for item in kept if not item.published_at or item.published_at <= reference]
    if len(result) != len(kept):
        counts["future_publications"] = len(kept) - len(result)
    return result, counts
