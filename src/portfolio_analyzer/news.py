"""Deterministic story clustering and editorial ordering; never upgrades evidence."""
import re
from urllib.parse import urlsplit


_MAJOR = {"reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com", "bbc.com"}
_SECONDARY = {"news.google.com", "finance.yahoo.com", "businessinsider.com", "marketbeat.com", "insidermonkey.com", "fool.com", "seekingalpha.com"}
_PUBLISHER_DOMAINS = {"reuters": "reuters.com", "associated press": "apnews.com", "bloomberg": "bloomberg.com",
                      "cnbc": "cnbc.com", "business insider": "businessinsider.com", "yahoo finance": "finance.yahoo.com"}
_EVENTS = {
    "earnings": r"\b(?:earnings|quarterly results|reports? results|beats? .*estimates|misses? .*estimates)\b",
    "guidance": r"\b(?:guidance|outlook (?:cut|raised|lowered)|raises? forecast|cuts? forecast)\b",
    "filing": r"\b(?:8-k|10-q|10-k|sec filing|files? with sec)\b",
    "corporate_action": r"\b(?:acquisition|merger|dividend|stock split|buyback|bankruptcy)\b",
    "legal": r"\b(?:court|ruling|lawsuit|regulator|regulatory|antitrust|settlement|fined)\b",
}
_OPINION = re.compile(r"\b(?:could|should|might|opinion|why|is it time|what .+ telling|next leg|stocks? to buy)\b", re.I)


def publisher_label(source):
    """Small documented tiers: official domains, major newsrooms, secondary, unknown."""
    host = (urlsplit(source.url).hostname or "").lower().removeprefix("www.")
    publisher = (source.publisher or "").lower().strip().removeprefix("www.")
    # This is publisher-declared metadata, never a verified redirect destination.
    # Feed publisher fields commonly contain a domain even when the URL is an aggregator.
    if host in {"news.google.com", "www.bing.com", "bing.com"} and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", publisher):
        host = publisher
    elif host in {"news.google.com", "bing.com"} and publisher in _PUBLISHER_DOMAINS:
        host = _PUBLISHER_DOMAINS[publisher]
    if host.endswith(".gov") or source.content_type == "official_feed":
        tier = "official"
    elif any(host == domain or host.endswith("." + domain) for domain in _MAJOR):
        tier = "major"
    elif any(host == domain or host.endswith("." + domain) for domain in _SECONDARY):
        tier = "secondary"
    else:
        tier = "unknown"
    return host, tier


def normalized_title(source):
    title = source.title.casefold()
    # Strip a trailing feed attribution, preserving hyphenated event names (8-K).
    parts = re.split(r"\s+[|–—-]\s+", title)
    if len(parts) > 1:
        suffix = re.sub(r"\W", "", parts[-1])
        publisher = re.sub(r"\W", "", source.publisher.casefold())
        known = {"reuters", "businessinsider", "yahoofinance", "cnbc", "bloomberg", "marketbeat"}
        if suffix in known or suffix and (suffix in publisher or publisher in suffix):
            title = " ".join(parts[:-1])
    return " ".join(re.findall(r"[\w]+", title))


def event_types(source):
    return {name for name, pattern in _EVENTS.items() if re.search(pattern, source.title, re.I)}


def evidence_grade(source):
    return "full" if source.content_type in {"official_feed", "retrieved_page"} else "headline" if source.content_type == "news_feed" else None


def _rank(source):
    category = bool(source.published_at and event_types(source) and not _OPINION.search(source.title))
    _, tier = publisher_label(source)
    grade = {"full": 0, "headline": 1, None: 2}[evidence_grade(source)]
    return (not category, grade, {"official": 0, "major": 1, "secondary": 2, "unknown": 3}[tier],
            -(source.published_at.timestamp() if source.published_at else 0), source.url, source.id)


def _same_story(left, right):
    if not left.published_at or not right.published_at or abs((left.published_at - right.published_at).total_seconds()) > 48 * 3600:
        return False
    a, b = normalized_title(left), normalized_title(right)
    # Different figures/directions or event classes signal distinct updates.
    if set(re.findall(r"\d+(?:\.\d+)?", a)) != set(re.findall(r"\d+(?:\.\d+)?", b)):
        return False
    if event_types(left) != event_types(right):
        return False
    def directions(text):
        tokens = re.findall(r"\b(?:beats?|misses?|raises?|cuts?|rises?|falls?)\b", text)
        canonical = {"beat": "beat", "beats": "beat", "miss": "miss", "misses": "miss",
                     "raise": "raise", "raises": "raise", "cut": "cut", "cuts": "cut",
                     "rise": "rise", "rises": "rise", "fall": "fall", "falls": "fall"}
        return {canonical[token] for token in tokens}
    if directions(a) != directions(b):
        return False
    if a == b:
        return True
    def shingles(text):
        words = text.split()
        return set(zip(words, words[1:])) if len(words) > 1 else set(words)
    first, second = shingles(a), shingles(b)
    return bool(first | second) and len(first & second) / len(first | second) >= 0.65


def cluster_stories(sources, symbol):
    """Return all ranked stories, retaining every member's evidence reference."""
    groups = []
    for source in sorted(sources, key=_rank):
        group = next((items for items in groups if all(_same_story(source, member) for member in items)), None)
        if group is None:
            groups.append([source])
        else:
            group.append(source)
    stories = []
    for members in groups:
        representative = min(members, key=_rank)
        domain, tier = publisher_label(representative)
        grades = {evidence_grade(member) for member in members}
        stories.append({"representative_source_id": representative.id,
                        "member_source_ids": [member.id for member in members],
                        "related_report_count": len(members) - 1, "affected_symbols": [symbol],
                        "evidence_grade": "full" if "full" in grades else "headline" if "headline" in grades else None,
                        "category": "event" if representative.published_at and event_types(representative) and not _OPINION.search(representative.title) else "also",
                        "publisher_domain": domain, "publisher_tier": tier})
    return stories
