from __future__ import annotations

import asyncio
from typing import Any

import httpx

from doi_verify.crossref import lookup_crossref
from schema import CandidatePaper, ResearchIntent

OPENALEX_WORKS = "https://api.openalex.org/works"
DEFAULT_PER_QUERY = 20
MAX_CONCURRENT_CROSSREF = 8


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _extract_abstract(raw: dict[str, Any]) -> str | None:
    inverted = raw.get("abstract_inverted_index")
    if not inverted:
        return None
    positions: list[tuple[int, str]] = []
    for token, indexes in inverted.items():
        for idx in indexes:
            positions.append((idx, token))
    positions.sort(key=lambda item: item[0])
    return " ".join(token for _, token in positions)


def _extract_issn(raw: dict[str, Any]) -> str | None:
    locations = _safe_list(raw.get("locations"))
    for location in locations:
        source = location.get("source") or {}
        issn_l = _safe_list(source.get("issn_l"))
        if issn_l:
            return str(issn_l[0])
        issn = _safe_list(source.get("issn"))
        if issn:
            return str(issn[0])
    primary = (raw.get("primary_location") or {}).get("source") or {}
    issn = _safe_list(primary.get("issn"))
    if issn:
        return str(issn[0])
    issn_l = primary.get("issn_l")
    return str(issn_l) if issn_l else None


def _extract_journal(raw: dict[str, Any]) -> str | None:
    primary = (raw.get("primary_location") or {}).get("source") or {}
    if primary.get("display_name"):
        return primary["display_name"]
    locations = _safe_list(raw.get("locations"))
    for location in locations:
        source = location.get("source") or {}
        if source.get("display_name"):
            return source["display_name"]
    return None


def _extract_authors(raw: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for authorship in _safe_list(raw.get("authorships")):
        author = authorship.get("author") or {}
        display_name = author.get("display_name")
        if display_name:
            authors.append(display_name)
    return authors


def _normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    prefix = "https://doi.org/"
    if doi.startswith(prefix):
        return doi[len(prefix):]
    return doi


async def _search_openalex_query(
    query: str,
    per_query: int,
    client: httpx.AsyncClient,
) -> list[CandidatePaper]:
    response = await client.get(
        OPENALEX_WORKS,
        params={"search": query, "per-page": per_query, "mailto": "dev@example.com"},
    )
    response.raise_for_status()
    payload = response.json()
    results: list[CandidatePaper] = []
    for item in payload.get("results", []):
        doi = _normalize_doi(item.get("doi"))
        paper = CandidatePaper(
            title=item.get("title") or "",
            authors=_extract_authors(item),
            year=item.get("publication_year"),
            journal=_extract_journal(item),
            issn=_extract_issn(item),
            doi=doi,
            abstract=_extract_abstract(item),
            source="openalex",
            cited_by_count=int(item.get("cited_by_count") or 0),
            url=item.get("id"),
        )
        if paper.title:
            results.append(paper)
    return results


async def _enrich_with_crossref(
    candidates: list[CandidatePaper],
    client: httpx.AsyncClient,
) -> list[CandidatePaper]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CROSSREF)

    async def enrich_one(candidate: CandidatePaper) -> CandidatePaper:
        if not candidate.doi:
            return candidate
        crossref = await lookup_crossref(candidate.doi, client, semaphore)
        if crossref is None or not crossref.is_valid():
            return candidate
        if not candidate.journal:
            candidate.journal = crossref.journal
        if not candidate.year:
            candidate.year = crossref.year
        if not candidate.issn:
            candidate.issn = None
        if not candidate.authors and crossref.authors:
            candidate.authors = [
                " ".join(part for part in (author.get("given"), author.get("family")) if part).strip()
                for author in crossref.authors
            ]
        if not candidate.url and candidate.doi:
            candidate.url = f"https://doi.org/{candidate.doi}"
        return candidate

    return list(await asyncio.gather(*[enrich_one(candidate) for candidate in candidates]))


def dedupe_candidates(candidates: list[CandidatePaper]) -> list[CandidatePaper]:
    seen: dict[str, CandidatePaper] = {}
    for candidate in candidates:
        key = candidate.doi or candidate.title.lower().strip()
        if not key:
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = candidate
            continue
        if candidate.cited_by_count > existing.cited_by_count:
            seen[key] = candidate
    return list(seen.values())


async def search_papers(
    intent: ResearchIntent,
    per_query: int = DEFAULT_PER_QUERY,
) -> list[CandidatePaper]:
    queries = [
        intent.search_queries.broad,
        intent.search_queries.balanced,
        intent.search_queries.precise,
    ]
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        query_results = await asyncio.gather(
            *[_search_openalex_query(query, per_query, client) for query in queries if query]
        )
        candidates = [candidate for group in query_results for candidate in group]
        candidates = dedupe_candidates(candidates)
        candidates = await _enrich_with_crossref(candidates, client)
    return candidates
