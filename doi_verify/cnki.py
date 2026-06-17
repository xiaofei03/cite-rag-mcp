"""CNKI / Chinese database fallback lookup.

When CrossRef + OpenAlex both return nothing, try:
1. DOI resolution via doi.org (to confirm DOI exists at all)
2. CNKI DOI resolver (cnki.net / kns.cnki.net)
3. Wanfang Data DOI lookup

Chinese DOIs from CNKI/Wanfang/VIP are often NOT registered with CrossRef,
even though the DOI itself is valid. We distinguish "ghost" (DOI doesn't
exist) from "Chinese only" (DOI resolves but no CrossRef/OpenAlex metadata).
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 1
RETRY_DELAY = 0.5

# DOI prefix patterns that indicate Chinese academic publishers
CHINESE_PREFIXES = (
    "10.7668",   # CNKI / CNKI Journals
    "10.16014",  # CNKI 另一种
    "10.16158",  # CNKI
    "10.16262",  # CNKI
    "10.13613",  # CNKI
    "10.16314",  # CNKI
    "10.15828",  # CNKI
    "10.19697",  # CNKI Science & Technology
)

# DOI substrings indicating Chinese academic databases
CHINESE_PATTERNS = (
    "cnki", "wanfang", "kns.cnki", "d.wanfangdata",
    "qikan.cws", "cqvip", "bjcyck", " periodicals.com.cn",
)

# Known Chinese journal publishers NOT in CrossRef
CHINESE_PUBLISHERS = (
    "cnki", "wanfang", "qinghua", "beihang", "tongji",
    "harbin institute", "nanjing university",
)


@dataclass
class CNKIResult:
    """Parsed result from CNKI/Wanfang fallback lookup."""
    doi: Optional[str]
    title: Optional[str] = None
    authors: list[dict] = None          # [{family, given}]
    year: Optional[int] = None
    journal: Optional[str] = None
    source: str = "cnki"               # "cnki" | "wanfang" | "doi_resolver"
    doi_resolves: bool = False         # True if DOI resolves (exists)
    doi_landing_url: Optional[str] = None

    def __post_init__(self):
        if self.authors is None:
            self.authors = []

    def is_valid(self) -> bool:
        return self.title is not None

    @property
    def first_author(self) -> Optional[str]:
        if self.authors:
            a = self.authors[0]
            return (a.get("family") or "").strip().lower()
        return None


def _is_likely_chinese_doi(doi: str) -> bool:
    """Heuristics: does this DOI look like it belongs to a Chinese academic DB?"""
    doi_lower = doi.lower()
    for pat in CHINESE_PATTERNS:
        if pat in doi_lower:
            return True
    for prefix in CHINESE_PREFIXES:
        if doi_lower.startswith(prefix):
            return True
    return False


async def _resolve_doi_exists(doi: str, client: httpx.AsyncClient) -> tuple[bool, Optional[str]]:
    """Check if a DOI resolves (exists) via doi.org redirect.

    Returns (resolved, final_url). If resolved=True, the DOI exists.
    Follows all redirects to get the actual landing page.
    """
    url = f"https://doi.org/{doi}"
    try:
        response = await client.get(
            url,
            headers={"User-Agent": "DoiVerify/1.0 (mailto:dev@example.com)"},
            follow_redirects=True,
            timeout=8.0,
        )
        final_url = str(response.url)

        # doi.org returns a "not found" page when DOI doesn't exist
        if "doi.org" in final_url.lower():
            # Still on doi.org domain = couldn't resolve
            # Check content for "not found" or "error"
            text_lower = response.text.lower()
            if any(kw in text_lower for kw in ["not found", "not exist", "invalid", "resolver", "error"]):
                return False, None
            # If we're still on doi.org with no error keywords, treat as valid
            return True, final_url

        # Successfully redirected to a publisher/DB site
        return True, final_url
    except (httpx.TimeoutException, httpx.RequestError):
        return False, None


async def _lookup_cnki_doi(doi: str, client: httpx.AsyncClient) -> Optional[CNKIResult]:
    """Try to resolve a CNKI DOI via their DOI redirect service.

    CNKI DOIs redirect through kns.cnki.net or doi.cnki.net.
    """
    # Try CNKI's DOI resolver endpoint
    cnki_resolver_urls = [
        f"https://doi.cnki.net/{doi}",
        f"https://kns.cnki.net/kcms/doi/detail.aspx?dbcode=wijson&dbname=cjfd&filename={doi.split('/')[-1]}",
    ]
    for url in cnki_resolver_urls:
        try:
            response = await client.get(url, timeout=6.0, follow_redirects=True)
            if response.status_code == 200 and len(response.text) > 200:
                # Try to extract metadata from CNKI page
                result = _parse_cnki_page(response.text, doi, url)
                if result and result.title:
                    return result
        except (httpx.TimeoutException, httpx.RequestError):
            continue

    # Try the DOI resolution directly
    resolved, landing_url = await _resolve_doi_exists(doi, client)
    if resolved:
        return CNKIResult(
            doi=doi,
            source="doi_resolver",
            doi_resolves=True,
            doi_landing_url=landing_url,
        )
    return None


def _parse_cnki_page(html: str, doi: str, url: str) -> Optional[CNKIResult]:
    """Parse CNKI page HTML to extract article metadata."""
    import re

    # Extract title — CNKI pages have title in <title> or specific meta tags
    title = None
    title_patterns = [
        r'<h1[^>]*>([^<]+)</h1>',
        r'class="title"[^>]*>([^<]+)</[^>]+>',
        r'meta\s+name="citation_title"\s+content="([^"]+)"',
        r'class="wxTitle"[^>]*>([^<]+)</[^>]+>',
    ]
    for pat in title_patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            break

    # Extract authors
    authors = []
    author_patterns = [
        r'class="author"[^>]*>([^<]+)</[^>]+>',
        r'class="authorName"[^>]*>([^<]+)</[^>]+>',
        r'meta\s+name="citation_author"\s+content="([^"]+)"',
    ]
    for pat in author_patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            name = m.group(1).strip()
            if name:
                parts = name.strip().split()
                if len(parts) >= 2:
                    authors.append({"family": parts[-1], "given": " ".join(parts[:-1])})
                else:
                    authors.append({"family": name, "given": ""})

    # Extract year
    year = None
    year_patterns = [
        r'meta\s+name="citation_date"\s+content="([^"]+)"',
        r'meta\s+name="citation_publication_date"\s+content="([^"]+)"',
        r'(\d{4})年',
        r'class="year"[^>]*>(\d{4})</',
    ]
    for pat in year_patterns:
        m = re.search(pat, html)
        if m:
            yr = re.search(r'\d{4}', m.group(1))
            if yr:
                year = int(yr.group())
                break

    # Extract journal
    journal = None
    journal_patterns = [
        r'class="journal"[^>]*>([^<]+)</[^>]+>',
        r'class="journalName"[^>]*>([^<]+)</[^>]+>',
        r'meta\s+name="citation_journal_title"\s+content="([^"]+)"',
    ]
    for pat in journal_patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            journal = m.group(1).strip()
            break

    result = CNKIResult(
        doi=doi,
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        source="cnki",
        doi_resolves=True,
        doi_landing_url=url,
    )
    return result


async def _lookup_wanfang_doi(doi: str, client: httpx.AsyncClient) -> Optional[CNKIResult]:
    """Try to resolve a Wanfang Data DOI."""
    wanfang_resolver = f"https://d.wanfangdata.com.cn/ExternalUrlParser?doi={doi}"
    try:
        response = await client.get(wanfang_resolver, timeout=10.0, follow_redirects=True)
        if response.status_code == 200 and len(response.text) > 100:
            result = _parse_wanfang_page(response.text, doi)
            if result:
                return result
    except (httpx.TimeoutException, httpx.RequestError):
        pass

    # Fallback: try doi.org resolution
    resolved, landing_url = await _resolve_doi_exists(doi, client)
    if resolved:
        return CNKIResult(
            doi=doi,
            source="doi_resolver",
            doi_resolves=True,
            doi_landing_url=landing_url,
        )
    return None


def _parse_wanfang_page(html: str, doi: str) -> Optional[CNKIResult]:
    """Parse Wanfang Data page HTML."""
    import re

    title = None
    for pat in [r'class="title"[^>]*>([^<]+)</', r'<h1[^>]*>([^<]+)</h1>']:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            break

    authors = []
    for m in re.finditer(r'class="author"[^>]*>([^<]+)</', html, re.IGNORECASE):
        name = m.group(1).strip()
        if name:
            parts = name.strip().split()
            if len(parts) >= 2:
                authors.append({"family": parts[-1], "given": " ".join(parts[:-1])})
            else:
                authors.append({"family": name, "given": ""})

    year = None
    m = re.search(r'(\d{4})年', html)
    if m:
        year = int(m.group(1))

    journal = None
    for pat in [r'class="journal"[^>]*>([^<]+)</', r'class="periodical"[^>]*>([^<]+)</']:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            journal = m.group(1).strip()
            break

    if not title:
        return None
    return CNKIResult(
        doi=doi,
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        source="wanfang",
        doi_resolves=True,
    )


async def lookup_cnki_fallback(
    doi: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> Optional[CNKIResult]:
    """Main entry point: try CNKI/Wanfang fallback when CrossRef+OpenAlex fail.

    This is called only when the DOI is likely Chinese OR when both
    CrossRef and OpenAlex returned no metadata.
    """
    if not _is_likely_chinese_doi(doi) and not _looks_like_chinese_doi(doi):
        # Quick doi.org resolution check — short timeout
        resolved, landing_url = await _resolve_doi_exists(doi, client)
        if resolved:
            return CNKIResult(
                doi=doi,
                source="doi_resolver",
                doi_resolves=True,
                doi_landing_url=landing_url,
            )
        return None

    # Try CNKI first
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with semaphore:
                result = await _lookup_cnki_doi(doi, client)
                if result:
                    return result
        except Exception as e:
            logger.warning("CNKI lookup failed for %s: %s (attempt %d)", doi, e, attempt)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            continue

    # Try Wanfang
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with semaphore:
                result = await _lookup_wanfang_doi(doi, client)
                if result:
                    return result
        except Exception as e:
            logger.warning("Wanfang lookup failed for %s: %s (attempt %d)", doi, e, attempt)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            continue

    # Last resort: doi.org resolution
    try:
        async with semaphore:
            resolved, landing_url = await _resolve_doi_exists(doi, client)
            if resolved:
                return CNKIResult(
                    doi=doi,
                    source="doi_resolver",
                    doi_resolves=True,
                    doi_landing_url=landing_url,
                )
    except Exception as e:
        logger.warning("doi.org resolution failed for %s: %s", doi, e)

    return None


def _looks_like_chinese_doi(doi: str) -> bool:
    """Broader heuristic: is this a Chinese DOI pattern?

    Checks for common Chinese academic DOI prefixes and URL patterns.
    """
    doi_lower = doi.lower()

    # Common Chinese publisher/DB prefixes in DOIs
    # These prefixes are registered with CrossRef by Chinese publishers
    # but often have sparse metadata
    KNOWN_CHINESE_PREFIXES = [
        "10.7668", "10.16014", "10.16158", "10.16262",
        "10.13613", "10.16314", "10.15828", "10.19697",
        "10.12030",  # CNKI Science & Tech
        "10.13939",  # some Chinese journals
        "10.16538",  # Chinese journals (like FEM)
        "10.12677",  # Chinese journals (like FIA)
        "10.15957",  # Chinese journals (like JJDL)
        "10.3969",   # Chinese SSAP
    ]

    for prefix in KNOWN_CHINESE_PREFIXES:
        if doi_lower.startswith(prefix):
            return True

    # Chinese domain patterns
    if any(p in doi_lower for p in CHINESE_PATTERNS):
        return True

    return False
