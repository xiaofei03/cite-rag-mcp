"""CrossRef API client for DOI lookup."""

import asyncio
import html
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CROSSREF_BASE = "https://api.crossref.org/works"
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5  # seconds


def _extract_four_digit_year(value: object) -> Optional[int]:
    if value is None:
        return None
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value))
    if not match:
        return None
    return int(match.group(1))


class CrossRefResult:
    """Parsed result from CrossRef."""

    def __init__(self, raw: dict):
        msg = raw.get("message", {})
        self.raw = msg
        self.doi: Optional[str] = msg.get("DOI")
        self.title: Optional[str] = _extract_title(msg)
        self.first_author: Optional[str] = _extract_first_author(msg)
        self.authors: list[dict] = _extract_all_authors(msg)
        self.year: Optional[int] = _extract_year(msg)
        self.journal: Optional[str] = _extract_journal(msg)
        self.publisher: Optional[str] = msg.get("publisher")
        self.type: Optional[str] = msg.get("type")
        self.issn: Optional[str] = _extract_issn(msg)
        self.volume: Optional[str] = msg.get("volume")
        self.issue: Optional[str] = msg.get("issue")
        self.pages: Optional[str] = msg.get("page")
        self.abstract: Optional[str] = _extract_abstract(msg)

    def is_valid(self) -> bool:
        return self.title is not None


def _extract_title(msg: dict) -> Optional[str]:
    titles = msg.get("title")
    if titles and isinstance(titles, list) and len(titles) > 0:
        return titles[0]
    return None


def _extract_all_authors(msg: dict) -> list[dict]:
    """Extract all authors as list of {given, family} dicts."""
    result = []
    authors = msg.get("author")
    if authors and isinstance(authors, list):
        for a in authors:
            family = (a.get("family") or "").strip()
            given = (a.get("given") or "").strip()
            if family or given:
                result.append({"family": family, "given": given})
    return result


def _extract_first_author(msg: dict) -> Optional[str]:
    authors = msg.get("author")
    if authors and isinstance(authors, list) and len(authors) > 0:
        first = authors[0]
        family = first.get("family")
        if family:
            return family.strip().lower()
    return None


def _extract_year(msg: dict) -> Optional[int]:
    # Try 'created' date-parts first
    created = msg.get("created", {}).get("date-parts", [])
    if created and isinstance(created, list) and len(created) > 0:
        parts = created[0]
        if parts and len(parts) > 0:
            year = _extract_four_digit_year(parts[0])
            if year is not None:
                return year

    # Fallback to 'published-print' or 'published-online'
    for key in ("published-print", "published-online", "issued"):
        dp = msg.get(key, {}).get("date-parts", [])
        if dp and len(dp) > 0 and dp[0]:
            year = _extract_four_digit_year(dp[0][0])
            if year is not None:
                return year

    for key in ("published", "published-print", "published-online", "issued", "created", "deposited"):
        candidate = msg.get(key)
        if not isinstance(candidate, dict):
            continue
        for field in ("date-time", "timestamp"):
            year = _extract_four_digit_year(candidate.get(field))
            if year is not None:
                return year
    return None


def _extract_journal(msg: dict) -> Optional[str]:
    container = msg.get("container-title")
    if container and isinstance(container, list) and len(container) > 0:
        return container[0]
    return None


def _extract_issn(msg: dict) -> Optional[str]:
    issn_types = msg.get("issn-type")
    if isinstance(issn_types, list):
        for entry in issn_types:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type") or "").strip().lower() == "print":
                value = str(entry.get("value") or "").strip()
                if value:
                    return value

    issn_list = msg.get("ISSN")
    if isinstance(issn_list, list):
        for value in issn_list:
            text = str(value or "").strip()
            if text:
                return text
    return None


def _extract_abstract(msg: dict) -> Optional[str]:
    abstract = msg.get("abstract")
    if not abstract:
        return None
    text = html.unescape(str(abstract))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


async def lookup_crossref(
    doi: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> Optional[CrossRefResult]:
    """Query CrossRef for a single DOI with retries.

    Returns CrossRefResult on success, None if not found or all retries exhausted.
    """
    url = f"{CROSSREF_BASE}/{doi}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with semaphore:
                response = await client.get(url, headers={"User-Agent": "DoiVerify/1.0 (mailto:dev@example.com)"})

            if response.status_code == 404:
                logger.debug("CrossRef 404 for %s", doi)
                return None

            if response.status_code == 429:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("CrossRef rate-limited for %s, retrying in %.1fs (attempt %d/%d)",
                               doi, delay, attempt, MAX_RETRIES)
                await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            return CrossRefResult(response.json())

        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as e:
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("CrossRef error for %s: %s (attempt %d/%d, retrying in %.1fs)",
                               doi, e, attempt, MAX_RETRIES, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("CrossRef exhausted retries for %s: %s", doi, e)

    return None
