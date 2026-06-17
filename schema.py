from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _serialize(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    return value


@dataclass
class VariableSet:
    independent: list[str] = field(default_factory=list)
    dependent: list[str] = field(default_factory=list)
    mediator: list[str] = field(default_factory=list)
    moderator: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)


@dataclass
class SearchQueries:
    broad: str
    balanced: str
    precise: str


@dataclass
class ResearchIntent:
    topic: str
    writing_goal: str
    keywords: list[str] = field(default_factory=list)
    theories: list[str] = field(default_factory=list)
    variables: VariableSet = field(default_factory=VariableSet)
    search_queries: SearchQueries | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class CandidatePaper:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    issn: str | None = None
    doi: str | None = None
    abstract: str | None = None
    source: str = "openalex"
    cited_by_count: int = 0
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class RankedPaper(CandidatePaper):
    journal_level: str = "Other"
    journal_level_raw: str | None = None
    catalog_match_type: str | None = None
    level_score: int = 0
    year_score: int = 0
    classic_candidate: bool = False
    final_score: float = 0.0
    rank_reason: str = ""


@dataclass
class VerifiedMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    abstract: str | None = None
    url: str | None = None


@dataclass
class VerifiedPaper(RankedPaper):
    verify_status: str = "verified"
    verified_metadata: VerifiedMetadata | None = None
    doi_url: str | None = None
    evidence_sources: list[str] = field(default_factory=list)


@dataclass
class ExceptionRecord:
    input_doi: str | None
    input_title: str | None
    reason: str
    crossref_hit: bool = False
    openalex_hit: bool = False
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class GenerationBundle:
    user_requirement: str
    verified_library: list[VerifiedPaper] = field(default_factory=list)
    citation_style: str = "author_year"
    outline_constraints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


def dump_records(records: list[Any]) -> list[dict[str, Any]]:
    return [_serialize(item) for item in records]
