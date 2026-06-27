from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


def _to_serializable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _to_serializable(val) for key, val in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _to_serializable(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_to_serializable(item) for item in value]
    return value


@dataclass
class VerifiedDoiRecord:
    doi: str
    title: str | None = None
    journal: str | None = None
    year: int | None = None
    verification_status: str = "verified"

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class SelectedTarget:
    library_id: int
    library_name: str
    editable: bool
    collection_id: int | None = None
    collection_name: str | None = None
    collection_key: str | None = None
    collection_level: int | None = None
    attach_to_library_root: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class ZoteroMatch:
    doi: str
    item_key: str
    title: str | None = None
    item_type: str | None = None
    issn: str | None = None
    abstract_note: str | None = None
    collections: list[str] = field(default_factory=list)
    duplicate_warning: bool = False
    metadata_hygiene_status: str = "not_checked"
    normalized_creators: list[str] = field(default_factory=list)
    hygiene_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class ZoteroImportResult:
    doi: str
    title: str | None = None
    item_key: str | None = None
    collection_key: str | None = None
    status: str = "failed"
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class CitekeyResult:
    doi: str | None = None
    title: str | None = None
    item_key: str | None = None
    citekey: str | None = None
    citation_anchor: str | None = None
    status: str = "failed"
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class CitekeyMapRecord:
    doi: str
    title: str | None
    item_key: str
    citekey: str
    citation_anchor: str

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class ImportBatchReport:
    selected_target: SelectedTarget
    requested_count: int
    imported_count: int
    reused_count: int
    failed_count: int
    results: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class ImportedMetadataRecord:
    doi: str
    title: str
    item_key: str
    citekey: str
    issn: str | None
    abstract_note: str | None
    citation_anchor: str
    collection_name: str
    collection_key: str
    library_id: int
    write_route: str = "connector"
    verification_status: str = "confirmed_in_target_collection"
    metadata_hygiene_status: str = "not_checked"
    normalized_creators: list[str] = field(default_factory=list)
    hygiene_warnings: list[str] = field(default_factory=list)
    metadata_source: str | None = None
    fulltext_status: str | None = None
    source_identifier: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class MetadataImportReport:
    selected_target: SelectedTarget
    requested_count: int
    imported_count: int
    reused_count: int
    write_route: str
    results: list[ImportedMetadataRecord] = field(default_factory=list)
    metadata_hygiene_status: str = "not_checked"
    hygiene_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class CreatorNormalizationResult:
    item_key: str
    doi: str | None = None
    title: str | None = None
    status: str = "not_checked"
    metadata_hygiene_status: str = "not_checked"
    original_creators: list[str] = field(default_factory=list)
    normalized_creators: list[str] = field(default_factory=list)
    hygiene_warnings: list[str] = field(default_factory=list)
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class CreatorNormalizationBatchReport:
    requested_count: int
    updated_count: int
    unchanged_count: int
    failed_count: int
    results: list[CreatorNormalizationResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class ZoteroMetadataAuditRecord:
    citekey: str | None = None
    item_key: str | None = None
    doi: str | None = None
    zotero_title: str | None = None
    verified_title: str | None = None
    zotero_year: int | None = None
    verified_year: int | None = None
    zotero_journal: str | None = None
    verified_journal: str | None = None
    status: str = "not_checked"
    action_required: str = ""
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class ZoteroMetadataAuditReport:
    requested_count: int
    checked_count: int
    mismatch_count: int
    missing_count: int
    records: list[ZoteroMetadataAuditRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class WorkflowArtifact:
    label: str
    path: str | None = None
    generated: bool = False
    row_count: int | None = None
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class VerifiedReferenceRecord:
    need_id: str
    query: str
    doi: str
    title: str
    authors: str = ""
    year: int | None = None
    journal: str | None = None
    journal_level: str | None = None
    journal_level_raw: str | None = None
    catalog_match_type: str | None = None
    verification_status: str = "verified"
    zotero_status: str = "not_checked"
    item_key: str | None = None
    citekey: str | None = None
    citation_anchor: str | None = None
    abstract_note: str | None = None
    abstract_source: str | None = None
    url: str | None = None
    source: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class CitekeyMappingRecord:
    source_type: str
    doi: str | None = None
    title: str | None = None
    authors: str = ""
    zotero_status: str = "unresolved"
    item_key: str | None = None
    citekey: str | None = None
    citation_anchor: str | None = None
    notes: str | None = None
    metadata_hygiene_status: str | None = None
    normalized_creators: list[str] = field(default_factory=list)
    hygiene_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)


@dataclass
class WorkflowResponse:
    mode: str
    status: str
    summary: str
    warnings: list[str] = field(default_factory=list)
    artifacts: list[WorkflowArtifact] = field(default_factory=list)
    verified_references: list[VerifiedReferenceRecord] = field(default_factory=list)
    citekey_mapping: list[CitekeyMappingRecord] = field(default_factory=list)
    generation_bundle: dict[str, Any] | None = None
    export_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _to_serializable(self)
