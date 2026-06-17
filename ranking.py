from __future__ import annotations

from datetime import datetime

from journal_catalog import CatalogMatch
from schema import CandidatePaper, RankedPaper

LEVEL_SCORES = {"A": 300, "B": 200, "C": 100, "Other": 0}
RECENT_WINDOW_YEARS = 5
CLASSIC_CITATION_THRESHOLD = 500


def compute_year_score(year: int | None, current_year: int | None = None) -> int:
    if year is None:
        return 0
    current_year = current_year or datetime.now().year
    age = current_year - year
    if age <= 2:
        return 60
    if age <= RECENT_WINDOW_YEARS:
        return 35
    if age <= 10:
        return 5
    return -20


def is_classic_candidate(
    paper: CandidatePaper,
    current_year: int | None = None,
    citation_threshold: int = CLASSIC_CITATION_THRESHOLD,
) -> bool:
    if paper.year is None:
        return False
    current_year = current_year or datetime.now().year
    return (current_year - paper.year) > RECENT_WINDOW_YEARS and paper.cited_by_count >= citation_threshold


def rank_candidates(
    matched_candidates: list[tuple[CandidatePaper, CatalogMatch]],
    allow_classics: bool = False,
    current_year: int | None = None,
    citation_threshold: int = CLASSIC_CITATION_THRESHOLD,
) -> list[RankedPaper]:
    current_year = current_year or datetime.now().year
    ranked: list[RankedPaper] = []
    for candidate, match in matched_candidates:
        classic_candidate = is_classic_candidate(
            candidate,
            current_year=current_year,
            citation_threshold=citation_threshold,
        )
        if classic_candidate and not allow_classics:
            year_score = -10
        else:
            year_score = compute_year_score(candidate.year, current_year=current_year)
        level_score = LEVEL_SCORES.get(match.entry.level_group, 0)
        citation_bonus = min(candidate.cited_by_count, 1000) / 100.0
        final_score = level_score + year_score + citation_bonus
        reason_parts = [
            f"level={match.entry.level_group}({match.entry.level_raw})",
            f"match={match.match_type}",
            f"year_score={year_score}",
            f"citations={candidate.cited_by_count}",
        ]
        if classic_candidate:
            reason_parts.append("classic_candidate=true")
        ranked.append(
            RankedPaper(
                **candidate.to_dict(),
                journal_level=match.entry.level_group,
                journal_level_raw=match.entry.level_raw,
                catalog_match_type=match.match_type,
                level_score=level_score,
                year_score=year_score,
                classic_candidate=classic_candidate,
                final_score=final_score,
                rank_reason=", ".join(reason_parts),
            )
        )
    ranked.sort(
        key=lambda paper: (
            LEVEL_SCORES.get(paper.journal_level, 0),
            paper.final_score,
            paper.year or 0,
            paper.cited_by_count,
        ),
        reverse=True,
    )
    return ranked
