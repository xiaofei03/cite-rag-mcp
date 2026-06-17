"""OpenAlex API client for DOI lookup."""

import asyncio
import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org/works"
MAX_RETRIES = 2
RETRY_BASE_DELAY = 0.5  # seconds


def _extract_four_digit_year(value: object) -> Optional[int]:
    if value is None:
        return None
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value))
    if not match:
        return None
    return int(match.group(1))


class OpenAlexResult:
    """Parsed result from OpenAlex."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.doi: Optional[str] = _normalize_doi(raw.get("doi"))
        self.title: Optional[str] = raw.get("title")
        self.first_author: Optional[str] = _extract_first_author(raw)
        self.authors: list[dict] = _extract_all_authors(raw)
        self.year: Optional[int] = _extract_year(raw)
        self.journal: Optional[str] = _extract_journal(raw)
        self.type: Optional[str] = raw.get("type")
        self.issn: Optional[str] = _extract_issn(raw)
        self.volume: Optional[str] = _extract_volume(raw)
        self.issue: Optional[str] = _extract_issue(raw)
        self.pages: Optional[str] = _extract_pages(raw)

    def is_valid(self) -> bool:
        return self.title is not None


def _normalize_doi(doi_str: Optional[str]) -> Optional[str]:
    if not doi_str:
        return None
    # OpenAlex returns DOI as full URL: https://doi.org/10.xxx/yyy
    prefix = "https://doi.org/"
    if doi_str.startswith(prefix):
        return doi_str[len(prefix):]
    return doi_str


def _extract_all_authors(raw: dict) -> list[dict]:
    """Extract all authors as list of {given, family} dicts."""
    result = []
    authors = raw.get("authorships")
    if authors and isinstance(authors, list):
        for a in authors:
            author_obj = a.get("author", {})
            display = author_obj.get("display_name", "")
            if display:
                parts = display.strip().split()
                if len(parts) == 1:
                    result.append({"family": parts[0], "given": ""})
                else:
                    result.append({"family": parts[-1], "given": " ".join(parts[:-1])})
    return result


def _extract_first_author(raw: dict) -> Optional[str]:
    authors = raw.get("authorships")
    if authors and isinstance(authors, list) and len(authors) > 0:
        first = authors[0]
        author_obj = first.get("author", {})
        display = author_obj.get("display_name", "")
        # Extract surname: "Gary Charness" -> "charness"
        if display:
            parts = display.strip().split()
            if parts:
                return parts[-1].strip().lower()
    return None


def _extract_journal(raw: dict) -> Optional[str]:
    primary = raw.get("primary_location")
    if primary:
        source = primary.get("source")
        if source:
            return source.get("display_name")
    return None


def _extract_issn(raw: dict) -> Optional[str]:
    primary = (raw.get("primary_location") or {}).get("source") or {}
    issn = primary.get("issn")
    if isinstance(issn, list):
        for value in issn:
            text = str(value or "").strip()
            if text:
                return text

    issn_l = primary.get("issn_l")
    if issn_l:
        text = str(issn_l).strip()
        if text:
            return text

    locations = raw.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            source = location.get("source") or {}
            issn = source.get("issn")
            if isinstance(issn, list):
                for value in issn:
                    text = str(value or "").strip()
                    if text:
                        return text
            issn_l = source.get("issn_l")
            if issn_l:
                text = str(issn_l).strip()
                if text:
                    return text
    return None


def _extract_volume(raw: dict) -> Optional[str]:
    biblio = raw.get("biblio", {})
    v = biblio.get("volume")
    return str(v) if v is not None else None


def _extract_issue(raw: dict) -> Optional[str]:
    biblio = raw.get("biblio", {})
    i = biblio.get("issue")
    return str(i) if i is not None else None


def _extract_pages(raw: dict) -> Optional[str]:
    biblio = raw.get("biblio", {})
    first = biblio.get("first_page")
    last = biblio.get("last_page")
    if first and last:
        return f"{first}-{last}"
    if first:
        return str(first)
    return None


def _extract_year(raw: dict) -> Optional[int]:
    for key in ("publication_year", "publication_date"):
        year = _extract_four_digit_year(raw.get(key))
        if year is not None:
            return year

    for location_key in ("primary_location",):
        location = raw.get(location_key)
        if not isinstance(location, dict):
            continue
        year = _extract_four_digit_year(location.get("published_date"))
        if year is not None:
            return year

    locations = raw.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if not isinstance(location, dict):
                continue
            year = _extract_four_digit_year(location.get("published_date"))
            if year is not None:
                return year
    return None


async def lookup_openalex(
    doi: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> Optional[OpenAlexResult]:
    """Query OpenAlex for a single DOI with retries.

    Returns OpenAlexResult on success, None if not found or all retries exhausted.
    """
    url = f"{OPENALEX_BASE}/doi:https://doi.org/{doi}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with semaphore:
                response = await client.get(url, params={"mailto": "dev@example.com"})

            if response.status_code == 404:
                logger.debug("OpenAlex 404 for %s", doi)
                return None

            if response.status_code == 429:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("OpenAlex rate-limited for %s, retrying in %.1fs (attempt %d/%d)",
                               doi, delay, attempt, MAX_RETRIES)
                await asyncio.sleep(delay)
                continue

            response.raise_for_status()
            return OpenAlexResult(response.json())

        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as e:
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning("OpenAlex error for %s: %s (attempt %d/%d, retrying in %.1fs)",
                               doi, e, attempt, MAX_RETRIES, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("OpenAlex exhausted retries for %s: %s", doi, e)

    return None
