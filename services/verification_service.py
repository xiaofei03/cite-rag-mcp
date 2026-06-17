from __future__ import annotations

import asyncio
import logging
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MCP_ROOT = SCRIPT_DIR.parent

if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from doi_verify.crossref import lookup_crossref
from doi_verify.openalex import lookup_openalex
from doi_verify.verifier import verify_all
from models import VerifiedDoiRecord
from schema import CandidatePaper, ResearchIntent, SearchQueries, VariableSet
from journal_catalog import DEFAULT_CATALOG_PATH, filter_and_label, load_catalog
from ranking import rank_candidates
from retrieval import search_papers

logger = logging.getLogger(__name__)
SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1/paper"


def extract_four_digit_year(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(value))
    if not match:
        return None
    return int(match.group(1))


class VerificationService:
    def __init__(self, catalog_path: Path | None = None) -> None:
        self.catalog_path = catalog_path or DEFAULT_CATALOG_PATH

    def derive_intent(self, topic: str, requirement: str) -> ResearchIntent:
        tokens = [part.strip() for part in topic.split() + requirement.split() if part.strip()]
        keywords = list(dict.fromkeys(tokens))[:12]
        return ResearchIntent(
            topic=topic,
            writing_goal=requirement,
            keywords=keywords,
            theories=[],
            variables=VariableSet(context=keywords[:4]),
            search_queries=SearchQueries(
                broad=topic,
                balanced=" ".join(keywords[:6]) if keywords else topic,
                precise=f"{topic} {requirement}"[:240],
            ),
        )

    async def retrieve_and_rank(
        self,
        topic: str,
        requirement: str,
        *,
        top_k: int = 25,
        per_query: int = 20,
        allow_classics: bool = False,
    ) -> list[dict]:
        intent = self.derive_intent(topic, requirement)
        candidates = await search_papers(intent, per_query=per_query)
        catalog = load_catalog(self.catalog_path)
        matched, _ = filter_and_label(candidates, catalog)
        ranked = rank_candidates(matched, allow_classics=allow_classics)
        return [paper.to_dict() for paper in ranked[:top_k]]

    async def verify_dois(self, dois: list[str]) -> list[VerifiedDoiRecord]:
        clean = [doi.strip() for doi in dois if doi and doi.strip()]
        results = await verify_all(clean)
        verified: list[VerifiedDoiRecord] = []
        for result in results:
            if result.status != "verified":
                continue
            year = int(result.display_year) if result.display_year else None
            verified.append(
                VerifiedDoiRecord(
                    doi=result.doi,
                    title=result.display_title,
                    journal=result.display_journal,
                    year=year,
                    verification_status=result.status,
                )
            )
        return verified

    async def _fetch_semantic_scholar_abstract(
        self,
        doi: str,
        client,
        semaphore: asyncio.Semaphore,
    ) -> str | None:
        url = f"{SEMANTIC_SCHOLAR_BASE}/DOI:{doi}"
        try:
            async with semaphore:
                response = await client.get(
                    url,
                    params={"fields": "abstract"},
                    headers={"Accept": "application/json"},
                )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Semantic Scholar abstract fetch failed for %s: %s", doi, exc)
            return None

        abstract = payload.get("abstract")
        if abstract is None:
            return None
        text = str(abstract).strip()
        return text or None

    def _restore_openalex_abstract_from_inverted_index(
        self,
        inverted_index: dict | None,
    ) -> str | None:
        if not inverted_index or not isinstance(inverted_index, dict):
            return None

        positions_to_words: dict[int, str] = {}
        for word, positions in inverted_index.items():
            if not isinstance(word, str) or not isinstance(positions, list):
                continue
            for position in positions:
                if isinstance(position, int):
                    positions_to_words[position] = word

        if not positions_to_words:
            return None

        words = [positions_to_words[index] for index in sorted(positions_to_words)]
        text = " ".join(words).strip()
        return text or None

    def _extract_openalex_abstract(self, openalex) -> str | None:
        if not openalex:
            return None

        raw = getattr(openalex, "raw", None)
        if not isinstance(raw, dict):
            return None

        direct_abstract = raw.get("abstract")
        if isinstance(direct_abstract, str):
            text = direct_abstract.strip()
            if text:
                return text

        return self._restore_openalex_abstract_from_inverted_index(
            raw.get("abstract_inverted_index")
        )

    async def fetch_doi_metadata_async(self, doi: str) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            semaphore = asyncio.Semaphore(4)
            crossref_task = asyncio.create_task(lookup_crossref(doi, client, semaphore))
            openalex_task = asyncio.create_task(lookup_openalex(doi, client, semaphore))
            semantic_scholar_task = asyncio.create_task(
                self._fetch_semantic_scholar_abstract(doi, client, semaphore)
            )
            crossref, openalex, semantic_scholar_abstract = await asyncio.gather(
                crossref_task,
                openalex_task,
                semantic_scholar_task,
            )

        title = None
        if crossref and crossref.is_valid():
            title = crossref.title
        elif openalex and openalex.is_valid():
            title = openalex.title

        authors: list[dict] = []
        if crossref and crossref.authors:
            authors = crossref.authors
        elif openalex and openalex.authors:
            authors = openalex.authors

        year = None
        if crossref and crossref.year:
            year = extract_four_digit_year(crossref.year)
        elif openalex and openalex.year:
            year = extract_four_digit_year(openalex.year)

        journal = None
        if crossref and crossref.journal:
            journal = crossref.journal
        elif openalex and openalex.journal:
            journal = openalex.journal

        issn = None
        if crossref and crossref.issn:
            issn = crossref.issn
        elif openalex and openalex.issn:
            issn = openalex.issn

        volume = crossref.volume if crossref and crossref.volume else getattr(openalex, "volume", None)
        issue = crossref.issue if crossref and crossref.issue else getattr(openalex, "issue", None)
        pages = crossref.pages if crossref and crossref.pages else getattr(openalex, "pages", None)
        url = f"https://doi.org/{doi}"
        openalex_abstract = self._extract_openalex_abstract(openalex)
        crossref_abstract = crossref.abstract if crossref and crossref.abstract else None
        abstract = semantic_scholar_abstract or openalex_abstract or crossref_abstract or "Abstract not available"
        abstract_source = (
            "Semantic Scholar"
            if semantic_scholar_abstract
            else "OpenAlex"
            if openalex_abstract
            else "CrossRef"
            if crossref_abstract
            else "unavailable"
        )
        return {
            "doi": doi,
            "title": title,
            "authors": authors,
            "year": year,
            "date": str(year) if year is not None else "",
            "journal": journal,
            "issn": issn,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "url": url,
            "abstract": abstract,
            "abstract_source": abstract_source,
        }
