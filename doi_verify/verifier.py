"""Core verification logic — cross-validate CrossRef vs OpenAlex."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx
from thefuzz import fuzz

from .crossref import CrossRefResult, lookup_crossref
from .openalex import OpenAlexResult, lookup_openalex
from .cnki import CNKIResult, lookup_cnki_fallback, _looks_like_chinese_doi
from .formatters import ReferenceData, format_citation

logger = logging.getLogger(__name__)

# Matching thresholds
TITLE_THRESHOLD = 85        # token_sort_ratio
JOURNAL_THRESHOLD = 80      # partial_ratio
YEAR_TOLERANCE = 1          # ±1 year
MAX_CONCURRENT = 15           # concurrent DOI lookups


@dataclass
class DiffDetail:
    """A single field difference between CrossRef and OpenAlex."""
    field: str
    crossref_value: Optional[str]
    openalex_value: Optional[str]


@dataclass
class VerifyResult:
    """Result of verifying a single DOI."""
    doi: str
    status: str  # "verified" | "discrepancy" | "ghost" | "single_source" | "chinese_db"
    crossref: Optional[CrossRefResult] = None
    openalex: Optional[OpenAlexResult] = None
    cnki: Optional[CNKIResult] = None
    diffs: list[DiffDetail] = field(default_factory=list)

    @property
    def display_title(self) -> Optional[str]:
        if self.crossref and self.crossref.title:
            return self.crossref.title
        if self.openalex and self.openalex.title:
            return self.openalex.title
        return None

    @property
    def display_author(self) -> Optional[str]:
        if self.crossref and self.crossref.first_author:
            return self.crossref.first_author.title()
        if self.openalex and self.openalex.first_author:
            return self.openalex.first_author.title()
        return None

    @property
    def display_year(self) -> Optional[str]:
        year = None
        if self.crossref and self.crossref.year:
            year = self.crossref.year
        elif self.openalex and self.openalex.year:
            year = self.openalex.year
        return str(year) if year else None

    @property
    def display_journal(self) -> Optional[str]:
        if self.crossref and self.crossref.journal:
            return self.crossref.journal
        if self.openalex and self.openalex.journal:
            return self.openalex.journal
        return None

    @property
    def diff_summary(self) -> str:
        """Human-readable summary of all discrepancies."""
        if not self.diffs:
            return ""
        parts = []
        for d in self.diffs:
            cr = d.crossref_value or "(missing)"
            oa = d.openalex_value or "(missing)"
            parts.append(f"{d.field}: CrossRef={cr}  vs  OpenAlex={oa}")
        return "; ".join(parts)

    @property
    def status_color(self) -> str:
        """CSS class name for the status."""
        return {
            "verified": "green",
            "discrepancy": "yellow",
            "ghost": "red",
            "single_source": "yellow",
            "chinese_db": "blue",
        }.get(self.status, "yellow")

    @property
    def status_label_cn(self) -> str:
        labels = {
            "verified": "一致",
            "discrepancy": "有差异",
            "ghost": "幽灵文献",
            "single_source": "仅单源",
            "chinese_db": "中文库收录",
        }
        return labels.get(self.status, self.status)

    @property
    def row_css_class(self) -> str:
        """CSS classes for the table row."""
        return f"status-{self.status}"

    def get_ref_data(self) -> Optional[ReferenceData]:
        """Build merged reference data from available sources (CrossRef > OpenAlex > CNKI)."""
        cr_data = ReferenceData.from_crossref(self.crossref) if self.crossref else None
        oa_data = ReferenceData.from_openalex(self.openalex) if self.openalex else None
        cnki_data = ReferenceData.from_cnki(self.cnki) if self.cnki else None

        # Priority: CrossRef > OpenAlex > CNKI
        ref = cr_data or oa_data or cnki_data
        if cr_data and oa_data:
            ref = cr_data.merge(oa_data)
        elif cr_data and cnki_data:
            ref = cr_data.merge(cnki_data)
        elif oa_data and cnki_data:
            ref = oa_data.merge(cnki_data)
        return ref

    def get_citation(self, fmt: str = "apa") -> Optional[str]:
        """Generate formatted citation. Returns None for ghost references."""
        ref = self.get_ref_data()
        if ref is None or not ref.title:
            return None
        return format_citation(ref, fmt)


def _compare_field(
    field: str,
    cr_val: Optional[str],
    oa_val: Optional[str],
    threshold: int,
    fuzzy: bool = True,
) -> Optional[DiffDetail]:
    """Compare a single field. Returns DiffDetail if values differ, None if consistent."""
    cr_str = str(cr_val).strip() if cr_val else None
    oa_str = str(oa_val).strip() if oa_val else None

    if cr_str is None and oa_str is None:
        return None  # both missing — not a discrepancy
    if cr_str is None or oa_str is None:
        return DiffDetail(field=field, crossref_value=cr_str, openalex_value=oa_str)

    # Normalize for comparison
    cr_norm = cr_str.lower().strip()
    oa_norm = oa_str.lower().strip()

    if cr_norm == oa_norm:
        return None

    if fuzzy:
        # Use token_sort_ratio for title, partial_ratio for journal
        if field in ("Title", "Journal"):
            ratio = fuzz.token_sort_ratio(cr_norm, oa_norm)
        else:
            ratio = fuzz.token_sort_ratio(cr_norm, oa_norm)
        if ratio >= threshold:
            return None

    return DiffDetail(field=field, crossref_value=cr_str, openalex_value=oa_str)


def cross_validate(cr: CrossRefResult, oa: OpenAlexResult) -> list[DiffDetail]:
    """Compare CrossRef and OpenAlex results, return list of field differences."""
    diffs = []

    # Title comparison
    diff = _compare_field("Title", cr.title, oa.title, TITLE_THRESHOLD, fuzzy=True)
    if diff:
        diffs.append(diff)

    # First author comparison
    diff = _compare_field("First Author", cr.first_author, oa.first_author, 100, fuzzy=True)
    if diff:
        diffs.append(diff)

    # Year comparison — with tolerance
    cr_year_str = str(cr.year) if cr.year else None
    oa_year_str = str(oa.year) if oa.year else None
    if cr.year is not None and oa.year is not None:
        if abs(cr.year - oa.year) > YEAR_TOLERANCE:
            diffs.append(DiffDetail(
                field="Year",
                crossref_value=cr_year_str,
                openalex_value=oa_year_str,
            ))
    elif cr.year is None and oa.year is not None:
        diffs.append(DiffDetail(field="Year", crossref_value=None, openalex_value=oa_year_str))
    elif cr.year is not None and oa.year is None:
        diffs.append(DiffDetail(field="Year", crossref_value=cr_year_str, openalex_value=None))

    # Journal comparison
    diff = _compare_field("Journal", cr.journal, oa.journal, JOURNAL_THRESHOLD, fuzzy=True)
    if diff:
        diffs.append(diff)

    return diffs


async def verify_single(
    doi: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> VerifyResult:
    """Verify a single DOI against both CrossRef and OpenAlex concurrently.

    If both sources return no metadata, try CNKI/Wanfang fallback (for Chinese DOIs)
    and doi.org resolution to distinguish "ghost" from "Chinese DB only".

    Args:
        doi: The DOI to verify.
        client: httpx AsyncClient.
        semaphore: Concurrency limiter.
    """
    cr_task = asyncio.create_task(lookup_crossref(doi, client, semaphore))
    oa_task = asyncio.create_task(lookup_openalex(doi, client, semaphore))

    cr, oa = await asyncio.gather(cr_task, oa_task)

    cr_valid = cr is not None and cr.is_valid()
    oa_valid = oa is not None and oa.is_valid()

    # Both found — cross-validate
    if cr_valid and oa_valid:
        diffs = cross_validate(cr, oa)
        if diffs:
            return VerifyResult(
                doi=doi, status="discrepancy",
                crossref=cr, openalex=oa, diffs=diffs,
            )
        else:
            return VerifyResult(
                doi=doi, status="verified",
                crossref=cr, openalex=oa,
            )

    # Single source only
    if cr_valid or oa_valid:
        return VerifyResult(
            doi=doi, status="single_source",
            crossref=cr if cr_valid else None,
            openalex=oa if oa_valid else None,
        )

    # Neither source found metadata — try CNKI/Wanfang fallback
    cnki = await lookup_cnki_fallback(doi, client, semaphore)

    if cnki is not None:
        return VerifyResult(
            doi=doi, status="chinese_db",
            cnki=cnki,
        )

    # Truly ghost — no source has it
    return VerifyResult(
        doi=doi, status="ghost",
    )


def clean_doi(raw: str) -> str:
    """Normalize a raw DOI string.

    Handles:
    - Full URL: https://doi.org/10.xxx/yyy -> 10.xxx/yyy
    - Bare DOI with whitespace
    - Empty strings
    """
    s = raw.strip()
    if not s:
        return s
    # Remove common prefixes
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip()


def read_dois(filepath: str) -> list[str]:
    """Read DOIs from a file, one per line. Skips comments and empty lines."""
    dois = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cleaned = clean_doi(line)
            if cleaned:
                dois.append(cleaned)
    return dois


async def verify_all(dois: list[str]) -> list[VerifyResult]:
    """Verify a batch of DOIs concurrently.

    Uses trust_env=False to bypass system proxy settings,
    which can interfere with API authentication and rate limiting.

    Args:
        dois: List of DOIs to verify.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
        tasks = [verify_single(doi, client, semaphore)
                 for doi in dois]
        results = await asyncio.gather(*tasks)
    return list(results)
