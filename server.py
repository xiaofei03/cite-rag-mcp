from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from models import (
    CitekeyMappingRecord,
    ImportedMetadataRecord,
    MetadataImportReport,
    VerifiedReferenceRecord,
    WorkflowArtifact,
    WorkflowResponse,
)
from prompts import (
    SMJ_SKILL_WORKFLOW_GUIDANCE,
    THEORY_WRITER_CITEKEY_SYSTEM_PROMPT,
    build_citekey_generation_bundle,
    build_workflow_mode_bundle,
)
from services.citekey_service import CitekeyService
from services.document_service import FinalWordDocumentService
from services.verification_service import VerificationService
from services.zotero_service import NON_REGULAR_TYPES, ZoteroService, normalize_doi

mcp = FastMCP(
    name="cite-rag-mcp",
    instructions=(
        "Local MCP server for verified-reference retrieval, Zotero dedupe/import, Better BibTeX citekey "
        "extraction, citekey-constrained academic writing bundles, and Pandoc-based Word rendering. "
        "This server supports four modes in one MCP: retrieve_only, import_only, export_only, and "
        "full_pipeline. retrieve_only must not draft prose or export Word. import_only must not search "
        "for new literature beyond the provided identifiers. export_only must not retrieve literature or "
        "change references. full_pipeline preserves the end-to-end path while still forbidding fabricated "
        "citations. All Word output must use generate_final_word_document, zotero.lua, and template.docx."
    ),
)

zotero_service = ZoteroService()
verification_service = VerificationService()
citekey_service = CitekeyService()
document_service = FinalWordDocumentService()

WORKFLOW_MODES = {"retrieve_only", "import_only", "export_only", "full_pipeline"}
CSV_ENCODING = "utf-8-sig"
DOI_IMPORT_BATCH_SIZE = 5
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
FORMAL_PUBLICATION_YEAR_OVERRIDES: dict[str, tuple[int, str]] = {
    "10.1037/0033-295X.94.3.319": (
        1987,
        "Formal publication year from Psychological Review 94(3), not PsycNet/Crossref database year.",
    ),
    "10.1037/0033-295X.92.4.548": (
        1985,
        "Formal publication year from Psychological Review 92(4), not PsycNet/Crossref database year.",
    ),
    "10.1037/0022-3514.83.6.1423": (
        2002,
        "Formal publication year from Journal of Personality and Social Psychology 83(6), not PsycNet/Crossref database year.",
    ),
    "10.1002/JOB.737": (2012, "Formal publication year from Journal of Organizational Behavior 33(1)."),
    "10.5465/AMJ.2012.0122": (2014, "Formal publication year from Academy of Management Journal 57(1)."),
    "10.5465/AMJ.2020.1627": (
        2022,
        "Formal publication year from Academy of Management Journal issue/year, not online-first or aggregator database year.",
    ),
    "10.1037/XGE0000033": (
        2015,
        "Formal publication year from Journal of Experimental Psychology: General 144(1).",
    ),
    "10.1287/MNSC.2016.2643": (2018, "Formal publication year from Management Science 64(3)."),
    "10.25300/MISQ/2021/16274": (
        2021,
        "Formal publication year from MIS Quarterly 45(3), not aggregator database year.",
    ),
    "10.5465/AMR.2013.0318": (2015, "Formal publication year from Academy of Management Review 40(1)."),
    "10.1016/J.IM.2019.103174": (2020, "Formal publication year from Information & Management 57."),
}


def _normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _normalize_title(value: str | None) -> str:
    text = _normalize_space(value).casefold()
    return re.sub(r"[^0-9a-z]+", "", text)


def _normalize_person_name(value: str | None) -> str:
    return re.sub(r"[^0-9a-z]+", "", _normalize_space(value).casefold())


def _split_names(value: str | None) -> list[str]:
    if not value:
        return []
    return [
        _normalize_person_name(part)
        for part in re.split(r"[;,|\n]+", value)
        if _normalize_person_name(part)
    ]


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _chunked(values: list[str], size: int = DOI_IMPORT_BATCH_SIZE) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


async def _import_doi_metadata_in_batches(dois: list[str]) -> MetadataImportReport:
    clean_dois = _dedupe_preserve_order([normalize_doi(doi) for doi in dois if normalize_doi(doi)])
    if not clean_dois:
        raise ValueError("At least one DOI is required for import.")

    reports = []
    for batch in _chunked(clean_dois):
        reports.append(await zotero_service.import_doi_metadata_to_selected_collection(batch))

    first = reports[0]
    results = []
    hygiene_warnings = []
    statuses = []
    for report in reports:
        results.extend(report.results)
        hygiene_warnings.extend(report.hygiene_warnings)
        statuses.append(report.metadata_hygiene_status)

    write_route = first.write_route if len(reports) == 1 else f"batched_{first.write_route}"
    metadata_hygiene_status = "clean" if all(status == "clean" for status in statuses) else "not_checked"
    if len(reports) > 1:
        hygiene_warnings.append(
            f"DOI imports were split into {len(reports)} batches of at most {DOI_IMPORT_BATCH_SIZE} records."
        )

    return MetadataImportReport(
        selected_target=first.selected_target,
        requested_count=len(clean_dois),
        imported_count=sum(report.imported_count for report in reports),
        reused_count=sum(report.reused_count for report in reports),
        write_route=write_route,
        results=results,
        metadata_hygiene_status=metadata_hygiene_status,
        hygiene_warnings=hygiene_warnings,
    )


def _extract_dois_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return _dedupe_preserve_order([normalize_doi(match) for match in DOI_PATTERN.findall(text)])


def _normalize_doi_key(value: Any) -> str:
    return normalize_doi(str(value or "")).upper()


def _extract_citekeys_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    matches = re.findall(r"@([A-Za-z0-9_:.+/\-]+)", text)
    return _dedupe_preserve_order(matches)


def _resolve_path(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _read_csv_rows(csv_path: str) -> list[dict[str, str]]:
    path = _resolve_path(csv_path)
    if path is None or not path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    with path.open("r", encoding=CSV_ENCODING, newline="") as handle:
        return list(csv.DictReader(handle))


def _read_year_override_csv(csv_path: str | None) -> dict[str, int]:
    if not csv_path:
        return {}
    overrides: dict[str, int] = {}
    for row in _read_csv_rows(csv_path):
        key = (row.get("citekey") or row.get("citation_key") or "").strip().lstrip("@")
        doi_key = _normalize_doi_key(row.get("doi"))
        year = (row.get("year") or row.get("authoritative_year") or "").strip()
        if not year.isdigit():
            continue
        if key:
            overrides[key] = int(year)
        if doi_key:
            overrides[doi_key] = int(year)
    return overrides


def _refresh_audit_counts_and_warnings(payload: dict[str, Any]) -> None:
    rows = payload.get("records") or []
    payload["mismatch_count"] = sum(
        1
        for row in rows
        if row.get("status") in {"metadata_mismatch", "doi_missing", "verification_failed", "verification_failed_cached"}
    )
    payload["missing_count"] = sum(1 for row in rows if row.get("status") == "missing_in_zotero")
    if payload["mismatch_count"] == 0 and payload["missing_count"] == 0:
        payload["warnings"] = []


def _apply_formal_publication_year_rules(payload: dict[str, Any]) -> None:
    for row in payload.get("records") or []:
        doi_key = _normalize_doi_key(row.get("doi"))
        if doi_key not in FORMAL_PUBLICATION_YEAR_OVERRIDES:
            continue
        formal_year, basis = FORMAL_PUBLICATION_YEAR_OVERRIDES[doi_key]
        if row.get("zotero_year") == formal_year:
            row["verified_year"] = formal_year
            row["status"] = "verified_formal_publication_year"
            row["action_required"] = ""
            row["details"] = basis
        elif row.get("status") == "verified":
            row["status"] = "metadata_mismatch"
            row["action_required"] = (
                "Use the formal journal issue publication year, not the PsycNet/Crossref database year."
            )
            row["details"] = (
                f"Zotero year {row.get('zotero_year')} does not match formal publication year {formal_year}. {basis}"
            )


def _apply_year_override_csv(payload: dict[str, Any], year_override_csv: str | None) -> None:
    overrides = _read_year_override_csv(year_override_csv)
    if not overrides:
        return
    for row in payload.get("records") or []:
        citekey = (row.get("citekey") or "").strip().lstrip("@")
        doi_key = _normalize_doi_key(row.get("doi"))
        year = overrides.get(citekey) or overrides.get(doi_key)
        if year is None:
            continue
        if row.get("zotero_year") == year:
            row["verified_year"] = year
            row["status"] = "verified_manual_year"
            row["action_required"] = ""
            row["details"] = "Authoritative publication year supplied by year_override_csv."


def _formal_year_overrides_for_service(year_override_csv: str | None) -> dict[str, tuple[int, str]]:
    overrides = dict(FORMAL_PUBLICATION_YEAR_OVERRIDES)
    for row in _read_csv_rows(year_override_csv) if year_override_csv else []:
        doi_key = _normalize_doi_key(row.get("doi"))
        year = (row.get("year") or row.get("authoritative_year") or "").strip()
        basis = (
            row.get("basis")
            or row.get("source")
            or row.get("notes")
            or "Authoritative publication year supplied by year_override_csv."
        )
        if doi_key and year.isdigit():
            overrides[doi_key] = (int(year), basis)
    return overrides


def _write_csv(path_str: str, fieldnames: list[str], rows: list[dict[str, Any]]) -> WorkflowArtifact:
    path = _resolve_path(path_str)
    if path is None:
        raise ValueError("Output path is required for CSV export.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return WorkflowArtifact(
        label=path.name,
        path=str(path),
        generated=True,
        row_count=len(rows),
    )


def _records_to_csv_text(fieldnames: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _authors_to_string(authors: list[str] | None) -> str:
    return "; ".join(author for author in (authors or []) if author)


def _build_retrieve_queries(
    retrieval_request: str | None,
    citation_need_csv_path: str | None,
) -> list[dict[str, str]]:
    queries: list[dict[str, str]] = []
    if retrieval_request:
        queries.append(
            {
                "need_id": "need_1",
                "query": _normalize_space(retrieval_request),
                "topic": _normalize_space(retrieval_request),
                "requirement": _normalize_space(retrieval_request),
            }
        )
    if citation_need_csv_path:
        for index, row in enumerate(_read_csv_rows(citation_need_csv_path), start=1):
            topic = _normalize_space(
                row.get("topic") or row.get("theme") or row.get("construct") or row.get("query")
            )
            requirement = _normalize_space(
                row.get("requirement") or row.get("need") or row.get("goal") or row.get("notes")
            )
            query = _normalize_space(
                row.get("query")
                or row.get("search_query")
                or row.get("keywords")
                or " ".join(part for part in [topic, requirement] if part)
            )
            if query:
                queries.append(
                    {
                        "need_id": row.get("need_id") or f"need_{index}",
                        "query": query,
                        "topic": topic or query,
                        "requirement": requirement or query,
                    }
                )
    if not queries:
        raise ValueError("retrieve_only requires retrieval_request or citation_need_csv_path.")
    return queries


def _rank_key(row: dict[str, Any]) -> tuple[float, int]:
    return (float(row.get("final_score") or 0.0), int(row.get("year") or 0))


async def _lookup_existing_by_title_and_authors(
    title: str,
    authors: str = "",
) -> CitekeyMappingRecord | None:
    normalized_title = _normalize_title(title)
    if not normalized_title:
        return None

    rows = await zotero_service._get_json(
        "/api/users/0/items/top",
        params={"q": title, "qmode": "titleCreatorYear", "limit": 50},
    )
    target_authors = set(_split_names(authors))
    for row in rows:
        data = row.get("data", {})
        if data.get("itemType") in NON_REGULAR_TYPES or data.get("parentItem"):
            continue
        if _normalize_title(data.get("title")) != normalized_title:
            continue
        row_authors = {
            _normalize_person_name(
                " ".join(
                    part
                    for part in [creator.get("firstName"), creator.get("lastName")]
                    if part
                )
            )
            for creator in data.get("creators", [])
            if isinstance(creator, dict)
        }
        if target_authors and row_authors and target_authors.isdisjoint(row_authors):
            continue
        item_key = row.get("key") or data.get("key")
        if not item_key:
            continue
        citekey = await citekey_service.export_item_citekey_async(
            item_key,
            doi=data.get("DOI"),
            title=data.get("title"),
        )
        if citekey.status != "ok" or not citekey.citekey:
            return CitekeyMappingRecord(
                source_type="title_authors",
                doi=data.get("DOI"),
                title=data.get("title"),
                authors=authors,
                zotero_status="matched_without_citekey",
                item_key=item_key,
                notes=citekey.details,
            )
        return CitekeyMappingRecord(
            source_type="title_authors",
            doi=data.get("DOI"),
            title=data.get("title"),
            authors=authors,
            zotero_status="existing_item",
            item_key=item_key,
            citekey=citekey.citekey,
            citation_anchor=citekey.citation_anchor,
            metadata_hygiene_status="not_checked",
        )
    return None


def _verified_reference_fields() -> list[str]:
    return [
        "need_id",
        "query",
        "doi",
        "title",
        "authors",
        "year",
        "journal",
        "journal_level",
        "journal_level_raw",
        "catalog_match_type",
        "verification_status",
        "zotero_status",
        "item_key",
        "citekey",
        "citation_anchor",
        "abstract_note",
        "abstract_source",
        "url",
        "source",
        "notes",
    ]


def _citekey_mapping_fields() -> list[str]:
    return [
        "source_type",
        "doi",
        "title",
        "authors",
        "zotero_status",
        "item_key",
        "citekey",
        "citation_anchor",
        "notes",
        "metadata_hygiene_status",
        "normalized_creators",
        "hygiene_warnings",
    ]


def _bundle_citekeys_from_verified(records: list[VerifiedReferenceRecord]) -> list[dict[str, Any]]:
    citekeys: list[dict[str, Any]] = []
    for record in records:
        if not record.citekey or not record.item_key:
            continue
        citekeys.append(
            {
                "doi": record.doi,
                "title": record.title,
                "item_key": record.item_key,
                "citekey": record.citekey,
                "citation_anchor": record.citation_anchor or f"[@{record.citekey}]",
                "abstractNote": record.abstract_note,
                "authors": record.authors,
                "year": record.year,
                "journal": record.journal,
            }
        )
    return citekeys


async def _run_retrieve_only(
    *,
    retrieval_request: str | None,
    citation_need_csv_path: str | None,
    verified_output_csv_path: str | None,
    top_k_per_need: int,
) -> WorkflowResponse:
    queries = _build_retrieve_queries(retrieval_request, citation_need_csv_path)
    ranked_by_doi: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for query in queries:
        ranked = await verification_service.retrieve_and_rank(
            query["topic"],
            query["requirement"],
            top_k=top_k_per_need,
        )
        for row in ranked:
            doi = normalize_doi(row.get("doi") or "")
            if not doi:
                continue
            enriched = dict(row)
            enriched["need_id"] = query["need_id"]
            enriched["query"] = query["query"]
            existing = ranked_by_doi.get(doi)
            if existing is None or _rank_key(enriched) > _rank_key(existing):
                ranked_by_doi[doi] = enriched

    verified_metadata = await verification_service.verify_dois(list(ranked_by_doi.keys()))
    verified_map = {normalize_doi(row.doi): row for row in verified_metadata}
    ranked_verified = [row for doi, row in ranked_by_doi.items() if doi in verified_map]
    existing_map = await zotero_service.lookup_existing_items_map_by_doi([row["doi"] for row in ranked_verified])

    missing_dois = [row["doi"] for row in ranked_verified if row["doi"] not in existing_map]
    imported_map: dict[str, ImportedMetadataRecord] = {}
    if missing_dois:
        import_report = await _import_doi_metadata_in_batches(missing_dois)
        imported_map = {record.doi: record for record in import_report.results}

    records: list[VerifiedReferenceRecord] = []
    citekey_map: list[CitekeyMappingRecord] = []

    for row in ranked_verified:
        doi = row["doi"]
        verified = verified_map[doi]
        existing = existing_map.get(doi)
        imported = imported_map.get(doi)
        zotero_status = "existing_item"
        item_key = None
        citekey = None
        citation_anchor = None
        abstract_note = None
        abstract_source = None
        notes = None

        if existing is not None:
            citekey_result = await citekey_service.export_item_citekey_async(
                existing.item_key,
                doi=doi,
                title=existing.title,
            )
            item_key = existing.item_key
            abstract_note = existing.abstract_note
            if citekey_result.status == "ok":
                citekey = citekey_result.citekey
                citation_anchor = citekey_result.citation_anchor
            else:
                zotero_status = "existing_item_missing_citekey"
                notes = citekey_result.details
        elif imported is not None:
            zotero_status = imported.write_route
            item_key = imported.item_key
            citekey = imported.citekey
            citation_anchor = imported.citation_anchor
            abstract_note = imported.abstract_note
            notes = imported.verification_status
        else:
            zotero_status = "verified_without_zotero_item"
            warnings.append(f"{doi} was verified but no Zotero item or citekey could be confirmed.")

        record = VerifiedReferenceRecord(
            need_id=row["need_id"],
            query=row["query"],
            doi=doi,
            title=verified.title or row.get("title") or doi,
            authors=_authors_to_string(row.get("authors")),
            year=verified.year or row.get("year"),
            journal=verified.journal or row.get("journal"),
            journal_level=row.get("journal_level"),
            journal_level_raw=row.get("journal_level_raw"),
            catalog_match_type=row.get("catalog_match_type"),
            verification_status=verified.verification_status,
            zotero_status=zotero_status,
            item_key=item_key,
            citekey=citekey,
            citation_anchor=citation_anchor,
            abstract_note=abstract_note,
            abstract_source=abstract_source,
            url=row.get("url"),
            source=row.get("source"),
            notes=notes,
        )
        records.append(record)
        citekey_map.append(
            CitekeyMappingRecord(
                source_type="retrieved_reference",
                doi=record.doi,
                title=record.title,
                authors=record.authors,
                zotero_status=record.zotero_status,
                item_key=record.item_key,
                citekey=record.citekey,
                citation_anchor=record.citation_anchor,
                notes=record.notes,
                metadata_hygiene_status=None,
            )
        )

    artifact_list: list[WorkflowArtifact] = []
    row_dicts = [record.to_dict() for record in records]
    if verified_output_csv_path:
        artifact_list.append(_write_csv(verified_output_csv_path, _verified_reference_fields(), row_dicts))
    else:
        artifact_list.append(
            WorkflowArtifact(
                label="verified_references.csv",
                generated=False,
                row_count=len(row_dicts),
                details=_records_to_csv_text(_verified_reference_fields(), row_dicts),
            )
        )

    return WorkflowResponse(
        mode="retrieve_only",
        status="ok",
        summary=(
            f"Verified {len(records)} references across {len(queries)} retrieval need(s). "
            "No body text was drafted and no Word export was performed."
        ),
        warnings=warnings,
        artifacts=artifact_list,
        verified_references=records,
        citekey_mapping=citekey_map,
    )


def _parse_import_rows(
    *,
    doi_list: list[str] | None,
    title_list: list[str] | None,
    authors_list: list[str] | None,
    verified_references_csv_path: str | None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for doi in doi_list or []:
        clean = normalize_doi(doi)
        if clean:
            rows.append({"doi": clean, "title": "", "authors": "", "source_type": "doi"})
    titles = title_list or []
    authors = authors_list or []
    for index, title in enumerate(titles):
        rows.append(
            {
                "doi": "",
                "title": _normalize_space(title),
                "authors": _normalize_space(authors[index] if index < len(authors) else ""),
                "source_type": "title_authors",
            }
        )
    if verified_references_csv_path:
        for row in _read_csv_rows(verified_references_csv_path):
            rows.append(
                {
                    "doi": normalize_doi(row.get("doi") or "") if row.get("doi") else "",
                    "title": _normalize_space(row.get("title")),
                    "authors": _normalize_space(row.get("authors")),
                    "source_type": row.get("source_type") or "verified_references_csv",
                }
            )
    cleaned = [
        row
        for row in rows
        if row.get("doi") or row.get("title")
    ]
    if not cleaned:
        raise ValueError("import_only requires DOI, title/authors, or verified_references_csv_path.")
    return cleaned


async def _run_import_only(
    *,
    doi_list: list[str] | None,
    title_list: list[str] | None,
    authors_list: list[str] | None,
    verified_references_csv_path: str | None,
    mapping_output_csv_path: str | None,
) -> WorkflowResponse:
    rows = _parse_import_rows(
        doi_list=doi_list,
        title_list=title_list,
        authors_list=authors_list,
        verified_references_csv_path=verified_references_csv_path,
    )
    warnings: list[str] = []
    results: list[CitekeyMappingRecord] = []

    doi_rows = [row for row in rows if row.get("doi")]
    if doi_rows:
        report = await _import_doi_metadata_in_batches([row["doi"] for row in doi_rows])
        imported_map = {record.doi: record for record in report.results}
        for row in doi_rows:
            imported = imported_map.get(row["doi"])
            if imported is None:
                results.append(
                    CitekeyMappingRecord(
                        source_type=row["source_type"],
                        doi=row["doi"],
                        title=row.get("title"),
                        authors=row.get("authors", ""),
                        zotero_status="import_failed",
                        notes="No Zotero item was returned for this DOI.",
                    )
                )
                continue
            results.append(
                CitekeyMappingRecord(
                    source_type=row["source_type"],
                    doi=imported.doi,
                    title=imported.title,
                    authors=row.get("authors", ""),
                    zotero_status=imported.verification_status,
                    item_key=imported.item_key,
                    citekey=imported.citekey,
                    citation_anchor=imported.citation_anchor,
                    metadata_hygiene_status=imported.metadata_hygiene_status,
                    normalized_creators=imported.normalized_creators,
                    hygiene_warnings=imported.hygiene_warnings,
                )
            )

    for row in rows:
        if row.get("doi"):
            continue
        matched = await _lookup_existing_by_title_and_authors(row.get("title", ""), row.get("authors", ""))
        if matched is not None:
            results.append(matched)
            continue
        warning = f"Could not resolve Zotero item from title/authors only: {row.get('title')}"
        warnings.append(warning)
        results.append(
            CitekeyMappingRecord(
                source_type=row["source_type"],
                title=row.get("title"),
                authors=row.get("authors", ""),
                zotero_status="doi_required_for_import",
                notes="No existing Zotero item matched the provided title/authors, and DOI is required for import.",
            )
        )

    artifact_list: list[WorkflowArtifact] = []
    row_dicts = [record.to_dict() for record in results]
    if mapping_output_csv_path:
        artifact_list.append(_write_csv(mapping_output_csv_path, _citekey_mapping_fields(), row_dicts))
    else:
        artifact_list.append(
            WorkflowArtifact(
                label="citekey_mapping.csv",
                generated=False,
                row_count=len(row_dicts),
                details=_records_to_csv_text(_citekey_mapping_fields(), row_dicts),
            )
        )

    return WorkflowResponse(
        mode="import_only",
        status="ok",
        summary="Resolved Zotero citekeys without drafting body text or exporting Word.",
        warnings=warnings,
        artifacts=artifact_list,
        citekey_mapping=results,
    )


def _run_export_only(
    *,
    markdown_path: str | None,
    markdown_content: str | None,
    output_filename: str,
) -> WorkflowResponse:
    if markdown_content is None:
        resolved_markdown = _resolve_path(markdown_path)
        if resolved_markdown is None or not resolved_markdown.exists():
            raise FileNotFoundError("export_only requires markdown_path or markdown_content.")
        markdown_content = resolved_markdown.read_text(encoding="utf-8")

    export_result = document_service.generate_final_word_document(
        markdown_content=markdown_content,
        output_filename=output_filename,
    )
    return WorkflowResponse(
        mode="export_only",
        status="ok" if export_result.get("success") else "error",
        summary="Rendered the provided Markdown to Word without retrieving literature or changing references.",
        artifacts=[
            WorkflowArtifact(
                label=Path(output_filename).name,
                path=str(_resolve_path(output_filename)),
                generated=bool(export_result.get("success")),
                details=export_result.get("stderr") or export_result.get("stdout"),
            )
        ],
        export_result=export_result,
    )


async def _run_full_pipeline(
    *,
    retrieval_request: str | None,
    citation_need_csv_path: str | None,
    verified_output_csv_path: str | None,
    top_k_per_need: int,
    markdown_path: str | None,
    markdown_content: str | None,
    output_filename: str,
) -> WorkflowResponse:
    retrieve_response = await _run_retrieve_only(
        retrieval_request=retrieval_request,
        citation_need_csv_path=citation_need_csv_path,
        verified_output_csv_path=verified_output_csv_path,
        top_k_per_need=top_k_per_need,
    )
    citekey_map = _bundle_citekeys_from_verified(retrieve_response.verified_references)
    user_requirement = retrieval_request or "Full pipeline academic drafting request"
    bundle = build_citekey_generation_bundle(user_requirement, citekey_map)
    bundle["workflow_mode"] = "full_pipeline"
    bundle["workflow_contract"] = build_workflow_mode_bundle("full_pipeline")
    bundle["smj_coordination"] = SMJ_SKILL_WORKFLOW_GUIDANCE

    export_response = None
    artifacts = list(retrieve_response.artifacts)
    if markdown_path or markdown_content:
        export_workflow = _run_export_only(
            markdown_path=markdown_path,
            markdown_content=markdown_content,
            output_filename=output_filename,
        )
        export_response = export_workflow.export_result
        artifacts.extend(export_workflow.artifacts)

    return WorkflowResponse(
        mode="full_pipeline",
        status="ok" if (export_response is None or export_response.get("success")) else "error",
        summary=(
            "Completed reference retrieval and citekey bundle assembly for the full pipeline. "
            "Word export was executed only when finalized Markdown was provided."
        ),
        warnings=retrieve_response.warnings,
        artifacts=artifacts,
        verified_references=retrieve_response.verified_references,
        citekey_mapping=retrieve_response.citekey_mapping,
        generation_bundle=bundle,
        export_result=export_response,
    )


@mcp.tool(
    name="get_selected_zotero_target",
    description="Return the currently selected Zotero library or collection target.",
)
async def get_selected_zotero_target() -> dict:
    return (await zotero_service.get_selected_target()).to_dict()


@mcp.tool(
    name="lookup_existing_items_by_doi",
    description="Find existing Zotero top-level regular items by DOI without writing to the library.",
)
async def lookup_existing_items_by_doi(dois: list[str]) -> list[dict]:
    return [match.to_dict() for match in await zotero_service.lookup_existing_items_by_doi(dois)]


@mcp.tool(
    name="export_item_citekey",
    description="Export a single Zotero item to BibTeX and extract its Better BibTeX citekey.",
)
def export_item_citekey(item_key: str, doi: str | None = None, title: str | None = None) -> dict:
    return citekey_service.export_item_citekey(item_key, doi=doi, title=title).to_dict()


@mcp.tool(
    name="import_doi_metadata_to_selected_collection",
    description=(
        "Fetch DOI metadata, enrich abstracts, inject the metadata into the currently selected local "
        "Zotero collection via the local Connector, verify the item appears in that collection, and "
        "return Better BibTeX citekeys. DOI imports are internally split into batches of at most 5 records."
    ),
)
async def import_doi_metadata_to_selected_collection(dois: list[str]) -> dict:
    return (await _import_doi_metadata_in_batches(dois)).to_dict()


@mcp.tool(
    name="import_manual_metadata_to_selected_collection",
    description=(
        "Import user-verified or Chinese-language metadata into the currently selected Zotero collection "
        "without requiring a downloaded PDF or DOI. Use this for CNKI/Chinese journal items, metadata-only "
        "references, or sources whose DOI metadata cannot be resolved. The tool writes Zotero items, verifies "
        "collection read-back, and returns Better BibTeX citekeys for live-citation Word export."
    ),
)
async def import_manual_metadata_to_selected_collection(
    metadata_items: list[dict],
    metadata_source: str = "manual_metadata",
    fulltext_status: str = "metadata_only",
) -> dict:
    return (
        await zotero_service.import_manual_metadata_to_selected_collection(
            metadata_items,
            metadata_source=metadata_source,
            fulltext_status=fulltext_status,
        )
    ).to_dict()


@mcp.tool(
    name="import_dois_and_get_citekeys",
    description=(
        "Backward-compatible alias for import_doi_metadata_to_selected_collection. Uses only the local "
        "Zotero Connector write route and enforces read-back verification."
    ),
)
async def import_dois_and_get_citekeys(
    dois: list[str],
    collection_mode: str = "selected",
    collection_name: str | None = None,
    reuse_existing: bool = True,
    attach_to_collection: bool = True,
) -> dict:
    if collection_mode != "selected":
        raise ValueError("Only collection_mode='selected' is supported.")
    if collection_name is not None:
        raise ValueError("collection_name is no longer supported; use the currently selected Zotero collection.")
    if not reuse_existing:
        raise ValueError("reuse_existing=False is no longer supported in the local-connector import path.")
    if not attach_to_collection:
        raise ValueError("attach_to_collection=False is no longer supported in the local-connector import path.")
    return (await _import_doi_metadata_in_batches(dois)).to_dict()


@mcp.tool(
    name="normalize_existing_item_creators",
    description=(
        "Inspect creator name casing for existing Zotero top-level items, report any all-caps Latin names, "
        "and return manual-fix guidance. Supports item keys directly or DOI resolution first."
    ),
)
async def normalize_existing_item_creators(
    item_keys: list[str] | None = None,
    dois: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    return (
        await zotero_service.normalize_existing_items_creators(
            item_keys=item_keys,
            dois=dois,
            dry_run=dry_run,
        )
    ).to_dict()


@mcp.tool(
    name="audit_zotero_metadata_by_citekeys",
    description=(
        "Audit Zotero live-citation metadata before Word export. Resolves Better BibTeX citekeys to Zotero "
        "items, compares Zotero title/year/journal with DOI metadata, and flags mismatches that must be "
        "fixed in Zotero before refreshing APA citations. Supports year_override_csv for authoritative "
        "formal publication-year overrides. External DOI metadata checks run concurrently and failed DOI "
        "lookups are cached locally to avoid repeated slow retries."
    ),
)
async def audit_zotero_metadata_by_citekeys(
    citekeys: list[str] | None = None,
    markdown_path: str | None = None,
    markdown_content: str | None = None,
    year_override_csv: str | None = None,
    verify_doi_metadata: bool = True,
    allow_metadata_only_without_doi: bool = False,
    external_concurrency: int = 4,
    use_failure_cache: bool = True,
) -> dict:
    resolved_citekeys = list(citekeys or [])
    if markdown_content:
        resolved_citekeys.extend(_extract_citekeys_from_text(markdown_content))
    if markdown_path:
        path = _resolve_path(markdown_path)
        if path is None or not path.exists():
            raise FileNotFoundError(f"Markdown file not found: {markdown_path}")
        resolved_citekeys.extend(_extract_citekeys_from_text(path.read_text(encoding="utf-8")))
    payload = (
        await zotero_service.audit_metadata_by_citekeys(
            _dedupe_preserve_order(resolved_citekeys),
            verify_doi_metadata=verify_doi_metadata,
            allow_metadata_only_without_doi=allow_metadata_only_without_doi,
            formal_year_overrides=_formal_year_overrides_for_service(year_override_csv),
            external_concurrency=external_concurrency,
            use_failure_cache=use_failure_cache,
        )
    ).to_dict()
    _apply_formal_publication_year_rules(payload)
    _apply_year_override_csv(payload, year_override_csv)
    _refresh_audit_counts_and_warnings(payload)
    return payload


@mcp.tool(
    name="build_citekey_generation_bundle",
    description=(
        "Build a citekey-only writing bundle for the drafting node. The bundle enforces mandatory "
        "<Citation_Reasoning>, abstract-first verification, dual-track citation syntax, and anti-fabrication rules."
    ),
)
def build_citekey_generation_bundle_tool(user_requirement: str, citekey_map: list[dict]) -> dict:
    return build_citekey_generation_bundle(user_requirement, citekey_map)


@mcp.tool(
    name="get_theory_writer_citekey_system_prompt",
    description="Return the strict citekey-only drafting system prompt.",
)
def get_theory_writer_citekey_system_prompt() -> dict:
    return {"system_prompt": THEORY_WRITER_CITEKEY_SYSTEM_PROMPT}


@mcp.tool(
    name="get_workflow_mode_contract",
    description="Return the allowed and forbidden actions for one of the four workflow modes.",
)
def get_workflow_mode_contract(mode: str) -> dict:
    return build_workflow_mode_bundle(mode)


@mcp.tool(
    name="generate_final_word_document",
    description=(
        "Render citekey-based Markdown into a Word document via Pandoc using zotero.lua and template.docx. "
        "The Markdown must already be finalized and must not include <Citation_Reasoning>."
    ),
)
def generate_final_word_document(markdown_content: str, output_filename: str = "final_paper.docx") -> dict:
    return document_service.generate_final_word_document(
        markdown_content=markdown_content,
        output_filename=output_filename,
    )


@mcp.tool(
    name="run_reference_workflow",
    description=(
        "Unified staged workflow entrypoint for cite-rag-mcp. Supports retrieve_only, import_only, "
        "export_only, and full_pipeline without splitting the MCP."
    ),
)
async def run_reference_workflow(
    mode: str,
    retrieval_request: str | None = None,
    citation_need_csv_path: str | None = None,
    verified_references_csv_path: str | None = None,
    doi_list: list[str] | None = None,
    title_list: list[str] | None = None,
    authors_list: list[str] | None = None,
    markdown_path: str | None = None,
    markdown_content: str | None = None,
    verified_output_csv_path: str | None = None,
    mapping_output_csv_path: str | None = None,
    output_filename: str = "final_paper.docx",
    top_k_per_need: int = 8,
) -> dict:
    if mode not in WORKFLOW_MODES:
        raise ValueError(f"Unsupported mode: {mode}. Expected one of {sorted(WORKFLOW_MODES)}.")

    if mode == "retrieve_only":
        return (
            await _run_retrieve_only(
                retrieval_request=retrieval_request,
                citation_need_csv_path=citation_need_csv_path,
                verified_output_csv_path=verified_output_csv_path,
                top_k_per_need=top_k_per_need,
            )
        ).to_dict()

    if mode == "import_only":
        return (
            await _run_import_only(
                doi_list=doi_list or _extract_dois_from_text(retrieval_request),
                title_list=title_list,
                authors_list=authors_list,
                verified_references_csv_path=verified_references_csv_path,
                mapping_output_csv_path=mapping_output_csv_path,
            )
        ).to_dict()

    if mode == "export_only":
        return _run_export_only(
            markdown_path=markdown_path,
            markdown_content=markdown_content,
            output_filename=output_filename,
        ).to_dict()

    return (
        await _run_full_pipeline(
            retrieval_request=retrieval_request,
            citation_need_csv_path=citation_need_csv_path,
            verified_output_csv_path=verified_output_csv_path,
            top_k_per_need=top_k_per_need,
            markdown_path=markdown_path,
            markdown_content=markdown_content,
            output_filename=output_filename,
        )
    ).to_dict()


if __name__ == "__main__":
    mcp.run(transport="stdio")
