from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import requests

from models import (
    CreatorNormalizationBatchReport,
    CreatorNormalizationResult,
    ImportedMetadataRecord,
    MetadataImportReport,
    SelectedTarget,
    ZoteroMatch,
    ZoteroMetadataAuditRecord,
    ZoteroMetadataAuditReport,
)
from services.citekey_service import CitekeyService
from services.verification_service import VerificationService, extract_four_digit_year

LOCAL_BASE_URL = "http://127.0.0.1:23119"
API_HEADERS = {"Zotero-API-Version": "3"}
CONNECTOR_HEADERS = {
    "Content-Type": "application/json",
    "X-Zotero-Connector-API-Version": "3",
}
NON_REGULAR_TYPES = {"attachment", "note", "annotation"}
DOI_FAILURE_CACHE_PATH = Path(__file__).resolve().parent.parent / "references" / "doi_metadata_failure_cache.json"
DOI_FAILURE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


class ZoteroImportError(RuntimeError):
    def __init__(self, phase: str, message: str) -> None:
        super().__init__(f"{phase}: {message}")
        self.phase = phase
        self.message = message


@dataclass
class CollectionCandidate:
    key: str
    name: str
    parent: str | None
    level: int


def normalize_doi(doi: str) -> str:
    value = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    return value.strip()


class ZoteroService:
    def __init__(self, base_url: str = LOCAL_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.citekey_service = CitekeyService(base_url=base_url)
        self.verification_service = VerificationService()
        self._item_writeback_supported: bool | None = None
        self.manual_creator_fix_guidance = (
            "Manual Zotero fix: open the item in Zotero, edit each author in the right-hand metadata pane, "
            "and change all-caps Latin names to normal capitalization, such as 'TIM MARTENS' -> 'Tim Martens' "
            "and 'CHRISTOPH J. SEXTROH' -> 'Christoph J. Sextroh'."
        )

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{self.base_url}{path}", params=params, headers=API_HEADERS)
            response.raise_for_status()
            return response.json()

    async def _post_connector_json(self, path: str, payload: dict[str, Any]) -> Any:
        def _request() -> Any:
            response = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                headers=CONNECTOR_HEADERS,
                timeout=120,
            )
            response.raise_for_status()
            if not response.text.strip():
                return {}
            try:
                return response.json()
            except ValueError:
                return {"raw_text": response.text}

        return await asyncio.to_thread(_request)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> tuple[Any, dict[str, str]]:
        def _request() -> tuple[Any, dict[str, str]]:
            response = requests.request(
                method=method,
                url=f"{self.base_url}{path}",
                json=payload,
                headers=headers or API_HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            parsed: Any
            if not response.text.strip():
                parsed = {}
            else:
                try:
                    parsed = response.json()
                except ValueError:
                    parsed = {"raw_text": response.text}
            return parsed, dict(response.headers)

        return await asyncio.to_thread(_request)

    async def _ping_connector(self) -> None:
        def _request() -> None:
            response = requests.get(f"{self.base_url}/connector/ping", timeout=10)
            response.raise_for_status()

        try:
            await asyncio.to_thread(_request)
        except Exception as exc:
            raise ZoteroImportError("本地 Connector 检查失败", str(exc)) from exc

    async def get_selected_target(self) -> SelectedTarget:
        try:
            payload = await self._post_connector_json("/connector/getSelectedCollection", {})
        except Exception as exc:
            if isinstance(exc, ZoteroImportError):
                raise
            raise ZoteroImportError("选中集合获取失败", str(exc)) from exc

        selected_id = payload.get("id")
        level = None
        if selected_id is not None:
            target_id = f"C{selected_id}"
            for target in payload.get("targets", []):
                if target.get("id") == target_id:
                    level = target.get("level")
                    break

        target = SelectedTarget(
            library_id=int(payload.get("libraryID", 0)),
            library_name=payload.get("libraryName") or "",
            editable=bool(payload.get("editable")),
            collection_id=int(selected_id) if selected_id is not None else None,
            collection_name=payload.get("name"),
            collection_level=level,
            attach_to_library_root=selected_id is None,
        )
        if target.collection_name:
            target.collection_key = await self.resolve_collection_key(
                target.collection_name,
                target.collection_level,
            )
        return target

    async def _collection_candidates(self) -> list[CollectionCandidate]:
        rows = await self._get_json("/api/users/0/collections", params={"limit": 10000})
        raw: dict[str, dict[str, Any]] = {}
        for row in rows:
            data = row.get("data", {})
            key = row.get("key") or data.get("key")
            if not key:
                continue
            raw[key] = {
                "name": data.get("name"),
                "parent": data.get("parentCollection") or None,
            }

        def level_for(key: str) -> int:
            level = 1
            parent = raw[key]["parent"]
            while parent:
                level += 1
                parent = raw[parent]["parent"] if parent in raw else None
            return level

        return [
            CollectionCandidate(
                key=key,
                name=data["name"],
                parent=data["parent"],
                level=level_for(key),
            )
            for key, data in raw.items()
        ]

    async def resolve_collection_key(self, collection_name: str, collection_level: int | None) -> str | None:
        matches = [
            candidate
            for candidate in await self._collection_candidates()
            if candidate.name == collection_name
        ]
        if not matches:
            return None
        if collection_level is not None:
            leveled = [candidate for candidate in matches if candidate.level == collection_level]
            if len(leveled) == 1:
                return leveled[0].key
            if leveled:
                return leveled[0].key
        return matches[0].key

    def _looks_like_latin_name(self, value: str) -> bool:
        letters = re.sub(r"[^A-Za-z]", "", value or "")
        return bool(letters)

    def _normalize_creator_part(self, value: str) -> tuple[str, bool, str | None]:
        raw = (value or "").strip()
        if not raw:
            return raw, False, None
        if not self._looks_like_latin_name(raw):
            return raw, False, None

        letters = re.sub(r"[^A-Za-z]", "", raw)
        if not letters or not letters.isupper():
            return raw, False, None

        tokens = re.split(r"(\s+|-)", raw)
        normalized_parts: list[str] = []
        for token in tokens:
            if not token or token.isspace() or token == "-":
                normalized_parts.append(token)
                continue
            token_letters = re.sub(r"[^A-Za-z]", "", token)
            if not token_letters:
                normalized_parts.append(token)
                continue
            if len(token_letters) <= 2 and token.replace(".", "").isalpha():
                normalized_parts.append(token.upper())
                continue
            if "'" in token:
                subtokens = token.split("'")
                normalized_parts.append("'".join(part[:1].upper() + part[1:].lower() if part else part for part in subtokens))
                continue
            normalized_parts.append(token[:1].upper() + token[1:].lower())
        normalized = "".join(normalized_parts)
        warning = None
        if normalized == raw:
            return raw, False, None
        if len(letters) <= 2:
            warning = f"Skipped aggressive normalization for short all-caps token '{raw}'."
            return raw, False, warning
        return normalized, True, None

    def _normalize_creators_for_hygiene(
        self,
        creators: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], str, list[str], list[str]]:
        normalized_creators: list[dict[str, Any]] = []
        normalized_names: list[str] = []
        warnings: list[str] = []
        changed = False
        checked_any = False

        for creator in creators or []:
            if not isinstance(creator, dict):
                continue
            checked_any = True
            updated = dict(creator)
            first_name, first_changed, first_warning = self._normalize_creator_part(str(updated.get("firstName") or ""))
            last_name, last_changed, last_warning = self._normalize_creator_part(str(updated.get("lastName") or ""))
            if first_warning:
                warnings.append(first_warning)
            if last_warning:
                warnings.append(last_warning)
            updated["firstName"] = first_name
            updated["lastName"] = last_name
            changed = changed or first_changed or last_changed
            normalized_creators.append(updated)
            display_name = " ".join(part for part in [first_name, last_name] if part).strip()
            if display_name:
                normalized_names.append(display_name)

        status = "normalized" if changed else "clean" if checked_any else "not_checked"
        return normalized_creators, status, normalized_names, warnings

    def _row_to_match(self, row: dict[str, Any], normalized: str) -> ZoteroMatch | None:
        data = row.get("data", {})
        item_doi = normalize_doi(data.get("DOI", "")) if data.get("DOI") else ""
        if item_doi != normalized:
            return None
        if data.get("itemType") in NON_REGULAR_TYPES:
            return None
        if data.get("parentItem"):
            return None
        creators, hygiene_status, normalized_creators, hygiene_warnings = self._normalize_creators_for_hygiene(
            data.get("creators", [])
        )
        return ZoteroMatch(
            doi=normalized,
            item_key=row.get("key") or data.get("key") or "",
            title=data.get("title"),
            item_type=data.get("itemType"),
            issn=data.get("ISSN"),
            abstract_note=data.get("abstractNote"),
            collections=data.get("collections", []),
            duplicate_warning=False,
            metadata_hygiene_status=hygiene_status,
            normalized_creators=normalized_creators,
            hygiene_warnings=hygiene_warnings,
        )

    def _creator_display_names(self, creators: list[dict[str, Any]] | None) -> list[str]:
        names: list[str] = []
        for creator in creators or []:
            if not isinstance(creator, dict):
                continue
            display_name = (
                str(creator.get("name") or "").strip()
                or " ".join(
                    part.strip()
                    for part in [str(creator.get("firstName") or ""), str(creator.get("lastName") or "")]
                    if part and part.strip()
                ).strip()
            )
            if display_name:
                names.append(display_name)
        return names

    def _item_year(self, data: dict[str, Any]) -> int | None:
        return extract_four_digit_year(data.get("date") or data.get("year"))

    async def _all_regular_top_items(self) -> list[dict[str, Any]]:
        rows = await self._get_json("/api/users/0/items/top", params={"limit": 10000})
        return [
            row
            for row in rows
            if row.get("data", {}).get("itemType") not in NON_REGULAR_TYPES
            and not row.get("data", {}).get("parentItem")
        ]

    async def _fetch_csl_items_by_citekeys(self, citekeys: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        request = {
            "jsonrpc": "2.0",
            "method": "item.pandoc_filter",
            "params": {
                "style": "apa",
                "citekeys": citekeys,
                "asCSL": True,
            },
        }
        url = f"{self.base_url}/better-bibtex/json-rpc?{urllib.parse.quote(json.dumps(request))}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        if payload.get("error"):
            raise ZoteroImportError("元数据审计失败", str(payload["error"]))
        result = payload.get("result") or {}
        return result.get("items") or {}, result.get("errors") or {}

    def _csl_item_key(self, item: dict[str, Any]) -> str | None:
        uri = ((item.get("custom") or {}).get("uri") or "").strip()
        match = re.search(r"/items/([A-Z0-9]+)$", uri)
        return match.group(1) if match else None

    def _csl_year(self, item: dict[str, Any]) -> int | None:
        date_parts = (item.get("issued") or {}).get("date-parts") or []
        if date_parts and date_parts[0]:
            return extract_four_digit_year(date_parts[0][0])
        return None

    async def _find_item_by_citekey(self, citekey: str, candidate_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        clean = (citekey or "").strip().lstrip("@")
        if not clean:
            return None
        for row in candidate_rows:
            data = row.get("data", {})
            item_key = row.get("key") or data.get("key")
            if not item_key:
                continue
            exported = await self.citekey_service.export_item_citekey_async(
                item_key,
                doi=data.get("DOI"),
                title=data.get("title"),
            )
            if exported.status == "ok" and exported.citekey == clean:
                return row
        return None

    def _titles_match(self, left: str | None, right: str | None) -> bool:
        def normalize(value: str | None) -> str:
            return re.sub(r"[^0-9a-z]+", "", (value or "").casefold())

        left_norm = normalize(left)
        right_norm = normalize(right)
        return bool(left_norm and right_norm and left_norm == right_norm)

    def _doi_cache_key(self, doi: str) -> str:
        return normalize_doi(doi).upper()

    def _read_doi_failure_cache(self) -> dict[str, dict[str, Any]]:
        try:
            if not DOI_FAILURE_CACHE_PATH.exists():
                return {}
            payload = json.loads(DOI_FAILURE_CACHE_PATH.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_doi_failure_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        DOI_FAILURE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOI_FAILURE_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _cached_doi_failure(self, cache: dict[str, dict[str, Any]], doi: str) -> dict[str, Any] | None:
        entry = cache.get(self._doi_cache_key(doi))
        if not isinstance(entry, dict):
            return None
        timestamp = float(entry.get("timestamp") or 0)
        if time.time() - timestamp > DOI_FAILURE_CACHE_TTL_SECONDS:
            return None
        return entry

    def _store_doi_failure(self, cache: dict[str, dict[str, Any]], doi: str, error: str) -> None:
        cache[self._doi_cache_key(doi)] = {
            "timestamp": time.time(),
            "error": error,
        }

    def _clear_doi_failure(self, cache: dict[str, dict[str, Any]], doi: str) -> None:
        cache.pop(self._doi_cache_key(doi), None)

    def _apply_verified_metadata_to_audit_record(
        self,
        record: ZoteroMetadataAuditRecord,
        metadata: dict[str, Any],
    ) -> None:
        record.verified_title = metadata.get("title")
        record.verified_year = extract_four_digit_year(metadata.get("date") or metadata.get("year"))
        record.verified_journal = metadata.get("journal")
        year_mismatch = (
            record.zotero_year is not None
            and record.verified_year is not None
            and record.zotero_year != record.verified_year
        )
        title_mismatch = (
            record.zotero_title is not None
            and record.verified_title is not None
            and not self._titles_match(record.zotero_title, record.verified_title)
        )
        if year_mismatch or title_mismatch:
            record.status = "metadata_mismatch"
            problems = []
            if year_mismatch:
                problems.append(f"year {record.zotero_year} != {record.verified_year}")
            if title_mismatch:
                problems.append("title mismatch")
            record.details = "; ".join(problems)
            record.action_required = "Fix the Zotero item metadata or replace the item before Word export."
        else:
            record.status = "verified"

    async def audit_metadata_by_citekeys(
        self,
        citekeys: list[str],
        *,
        verify_doi_metadata: bool = True,
        formal_year_overrides: dict[str, tuple[int, str]] | None = None,
        external_concurrency: int = 4,
        use_failure_cache: bool = True,
    ) -> ZoteroMetadataAuditReport:
        clean_citekeys = []
        for citekey in citekeys:
            clean = (citekey or "").strip().lstrip("@")
            if clean and clean not in clean_citekeys:
                clean_citekeys.append(clean)
        if not clean_citekeys:
            raise ZoteroImportError("元数据审计失败", "Provide at least one citekey.")

        csl_items, csl_errors = await self._fetch_csl_items_by_citekeys(clean_citekeys)
        records: list[ZoteroMetadataAuditRecord] = []
        warnings: list[str] = []
        pending_external_checks: list[tuple[ZoteroMetadataAuditRecord, str]] = []

        for citekey in clean_citekeys:
            item = csl_items.get(citekey)
            if item is None:
                details = ""
                if citekey in csl_errors:
                    details = f"Better BibTeX error marker: {csl_errors[citekey]}"
                records.append(
                    ZoteroMetadataAuditRecord(
                        citekey=citekey,
                        status="missing_in_zotero",
                        action_required="Locate or re-import the cited source before generating live citations.",
                        details=details,
                    )
                )
                continue

            doi = normalize_doi(item.get("DOI", "")) if item.get("DOI") else ""
            item_key = self._csl_item_key(item)
            record = ZoteroMetadataAuditRecord(
                citekey=citekey,
                item_key=item_key,
                doi=doi or None,
                zotero_title=item.get("title"),
                zotero_year=self._csl_year(item),
                zotero_journal=item.get("container-title"),
                status="checked_zotero_only",
            )

            formal_year_rule = None
            if formal_year_overrides and doi:
                formal_year_rule = formal_year_overrides.get(normalize_doi(doi).upper())
            if formal_year_rule is not None:
                formal_year, basis = formal_year_rule
                record.verified_year = formal_year
                if record.zotero_year == formal_year:
                    record.status = "verified_formal_publication_year"
                    record.details = basis
                else:
                    record.status = "metadata_mismatch"
                    record.details = (
                        f"Zotero year {record.zotero_year} does not match formal publication year "
                        f"{formal_year}. {basis}"
                    )
                    record.action_required = (
                        "Use the formal journal issue publication year, not the PsycNet/Crossref database year."
                    )
                records.append(record)
                continue

            if verify_doi_metadata and doi:
                pending_external_checks.append((record, doi))
            elif not doi:
                record.status = "doi_missing"
                record.action_required = "Add DOI or manually verify title/year/journal before final citation refresh."

            records.append(record)

        if pending_external_checks:
            failure_cache = self._read_doi_failure_cache() if use_failure_cache else {}
            cache_changed = False
            semaphore = asyncio.Semaphore(max(1, external_concurrency))

            async def verify_pending(record: ZoteroMetadataAuditRecord, doi: str) -> None:
                nonlocal cache_changed
                if use_failure_cache:
                    cached_failure = self._cached_doi_failure(failure_cache, doi)
                    if cached_failure is not None:
                        record.status = "verification_failed_cached"
                        record.details = str(cached_failure.get("error") or "Cached DOI metadata lookup failure.")
                        record.action_required = (
                            "Manually verify DOI publisher metadata before final citation refresh, or clear the "
                            "failure cache if the external service has recovered."
                        )
                        return
                try:
                    async with semaphore:
                        metadata = await self.verification_service.fetch_doi_metadata_async(doi)
                    self._apply_verified_metadata_to_audit_record(record, metadata)
                    if use_failure_cache:
                        before = dict(failure_cache)
                        self._clear_doi_failure(failure_cache, doi)
                        cache_changed = cache_changed or before != failure_cache
                except Exception as exc:
                    record.status = "verification_failed"
                    record.details = str(exc)
                    record.action_required = "Manually verify DOI publisher metadata before final citation refresh."
                    if use_failure_cache:
                        self._store_doi_failure(failure_cache, doi, str(exc))
                        cache_changed = True

            await asyncio.gather(*(verify_pending(record, doi) for record, doi in pending_external_checks))
            if use_failure_cache and cache_changed:
                self._write_doi_failure_cache(failure_cache)

        mismatch_count = sum(1 for record in records if record.status in {"metadata_mismatch", "doi_missing", "verification_failed", "verification_failed_cached"})
        missing_count = sum(1 for record in records if record.status == "missing_in_zotero")
        if mismatch_count or missing_count:
            warnings.append(
                "Do not generate or refresh a Zotero live-citation Word document until all missing or mismatched items are resolved."
            )

        return ZoteroMetadataAuditReport(
            requested_count=len(clean_citekeys),
            checked_count=len(records) - missing_count,
            mismatch_count=mismatch_count,
            missing_count=missing_count,
            records=records,
            warnings=warnings,
        )

    async def lookup_existing_items_by_doi(self, dois: list[str]) -> list[ZoteroMatch]:
        matches: list[ZoteroMatch] = []
        for doi in dois:
            normalized = normalize_doi(doi)
            rows = await self._get_json(
                "/api/users/0/items/top",
                params={"q": normalized, "qmode": "everything", "limit": 100},
            )
            exact: list[ZoteroMatch] = []
            for row in rows:
                match = self._row_to_match(row, normalized)
                if match:
                    exact.append(match)
            if not exact:
                continue
            exact[0].duplicate_warning = len(exact) > 1
            matches.append(exact[0])
        return matches

    async def lookup_existing_items_map_by_doi(self, dois: list[str]) -> dict[str, ZoteroMatch]:
        matches = await self.lookup_existing_items_by_doi(dois)
        return {match.doi: match for match in matches}

    async def _get_item_row(self, item_key: str) -> dict[str, Any]:
        row = await self._get_json(f"/api/users/0/items/{item_key}")
        if not isinstance(row, dict):
            raise ZoteroImportError("条目读取失败", f"{item_key}: local API returned non-object payload.")
        return row

    async def _put_item_row(self, item_key: str, item_data: dict[str, Any], version: int | None) -> dict[str, Any]:
        request_headers = dict(API_HEADERS)
        request_headers["Content-Type"] = "application/json"
        if version is not None:
            request_headers["If-Unmodified-Since-Version"] = str(version)
        try:
            parsed, _headers = await self._request_json(
                "PUT",
                f"/api/users/0/items/{item_key}",
                payload=item_data,
                headers=request_headers,
            )
            self._item_writeback_supported = True
        except Exception as exc:
            if "501" in str(exc) or "Not Implemented" in str(exc):
                self._item_writeback_supported = False
                raise ZoteroImportError(
                    "已有条目作者回写不受支持",
                    f"{item_key}: Zotero local API is read-only for item updates on this installation.",
                ) from exc
            raise ZoteroImportError("已有条目作者回写失败", f"{item_key}: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
        return {}

    async def normalize_existing_item_creators(
        self,
        item_key: str,
        *,
        dry_run: bool = False,
    ) -> CreatorNormalizationResult:
        row = await self._get_item_row(item_key)
        data = row.get("data", {})
        creators = data.get("creators", [])
        original_creators = self._creator_display_names(creators)
        normalized_creators_payload, hygiene_status, normalized_names, hygiene_warnings = (
            self._normalize_creators_for_hygiene(creators)
        )

        result = CreatorNormalizationResult(
            item_key=item_key,
            doi=data.get("DOI"),
            title=data.get("title"),
            metadata_hygiene_status=hygiene_status,
            original_creators=original_creators,
            normalized_creators=normalized_names or original_creators,
            hygiene_warnings=hygiene_warnings,
        )

        if hygiene_status != "normalized":
            result.status = "unchanged"
            result.details = "Existing Zotero item creators were already clean."
            return result

        if dry_run:
            result.status = "manual_review_required"
            result.details = (
                "Detected creator case issues. This MCP is configured to report the issue for manual review "
                f"instead of writing back changes. {self.manual_creator_fix_guidance}"
            )
            return result

        result.status = "manual_review_required"
        result.details = (
            "Detected creator case issues. Automatic write-back is disabled by policy. "
            f"{self.manual_creator_fix_guidance}"
        )
        return result

        try:
            await self._put_item_row(item_key, updated_data, version if isinstance(version, int) else None)
        except ZoteroImportError as exc:
            if exc.phase == "已有条目作者回写不受支持":
                result.status = "unsupported_by_local_api"
                result.details = exc.message
                return result
            raise
        refreshed = await self._get_item_row(item_key)
        refreshed_creators = refreshed.get("data", {}).get("creators", [])
        refreshed_names = self._creator_display_names(refreshed_creators)
        _, refreshed_hygiene_status, _, refreshed_warnings = self._normalize_creators_for_hygiene(refreshed_creators)

        result.normalized_creators = refreshed_names or result.normalized_creators
        result.hygiene_warnings = list(dict.fromkeys(result.hygiene_warnings + refreshed_warnings))
        if refreshed_hygiene_status == "normalized":
            result.status = "verification_failed"
            result.metadata_hygiene_status = "normalized"
            result.details = "Write-back completed, but creators still appear to need normalization after re-read."
            return result

        result.status = "updated"
        result.metadata_hygiene_status = "repaired_existing_item"
        result.details = "Existing Zotero item creators were normalized and written back successfully."
        return result

    async def normalize_existing_items_creators(
        self,
        *,
        item_keys: list[str] | None = None,
        dois: list[str] | None = None,
        dry_run: bool = False,
    ) -> CreatorNormalizationBatchReport:
        resolved_item_keys: list[str] = []
        warnings: list[str] = []

        for item_key in item_keys or []:
            clean = (item_key or "").strip()
            if clean and clean not in resolved_item_keys:
                resolved_item_keys.append(clean)

        if dois:
            matches = await self.lookup_existing_items_by_doi(dois)
            doi_map = {match.doi: match for match in matches}
            for doi in dois:
                normalized = normalize_doi(doi)
                match = doi_map.get(normalized)
                if match is None:
                    warnings.append(f"No existing Zotero top-level item found for DOI {normalized}.")
                    continue
                if match.item_key not in resolved_item_keys:
                    resolved_item_keys.append(match.item_key)

        if not resolved_item_keys:
            raise ZoteroImportError("已有条目作者回写失败", "Provide item_keys or DOI values that resolve to existing Zotero items.")

        results: list[CreatorNormalizationResult] = []
        updated_count = 0
        unchanged_count = 0
        failed_count = 0

        for item_key in resolved_item_keys:
            try:
                result = await self.normalize_existing_item_creators(item_key, dry_run=dry_run)
            except Exception as exc:
                failed_count += 1
                results.append(
                    CreatorNormalizationResult(
                        item_key=item_key,
                        status="failed",
                        metadata_hygiene_status="error",
                        details=str(exc),
                    )
                )
                continue

            results.append(result)
            if result.status == "updated":
                updated_count += 1
            elif result.status in {"unchanged", "manual_review_required", "dry_run_change_detected", "unsupported_by_local_api"}:
                unchanged_count += 1
            else:
                failed_count += 1

        return CreatorNormalizationBatchReport(
            requested_count=len(resolved_item_keys),
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            failed_count=failed_count,
            results=results,
            warnings=warnings,
        )

    async def _collection_item_rows(self, collection_key: str) -> list[dict[str, Any]]:
        try:
            rows = await self._get_json(
                f"/api/users/0/collections/{collection_key}/items/top",
                params={"limit": 10000},
            )
        except Exception as exc:
            raise ZoteroImportError("集合回读失败", f"无法读取集合 {collection_key}: {exc}") from exc
        if not isinstance(rows, list):
            raise ZoteroImportError("集合回读失败", f"集合 {collection_key} 返回了非列表结果。")
        return rows

    async def _collection_matches_by_doi(self, collection_key: str, dois: list[str]) -> dict[str, list[ZoteroMatch]]:
        normalized_set = {normalize_doi(doi) for doi in dois}
        grouped = {doi: [] for doi in normalized_set}
        for row in await self._collection_item_rows(collection_key):
            data = row.get("data", {})
            item_doi = normalize_doi(data.get("DOI", "")) if data.get("DOI") else ""
            if item_doi not in normalized_set:
                continue
            match = self._row_to_match(row, item_doi)
            if match:
                grouped[item_doi].append(match)
        return grouped

    def _prepare_creators_from_metadata(
        self,
        metadata: dict[str, Any],
    ) -> tuple[list[dict[str, str]], str, list[str], list[str]]:
        creators: list[dict[str, str]] = []
        for author in metadata.get("authors", []):
            family = (author.get("family") or "").strip()
            given = (author.get("given") or "").strip()
            if family or given:
                creators.append(
                    {
                        "creatorType": "author",
                        "lastName": family or given,
                        "firstName": given if family else "",
                    }
                )
        exported_creators = [
            {
                "creatorType": str(creator.get("creatorType") or "author"),
                "lastName": str(creator.get("lastName") or ""),
                "firstName": str(creator.get("firstName") or ""),
            }
            for creator in creators
        ]
        _, hygiene_status, normalized_names, hygiene_warnings = self._normalize_creators_for_hygiene(exported_creators)
        return exported_creators, hygiene_status, normalized_names, hygiene_warnings

    def _creators_from_metadata(self, metadata: dict[str, Any]) -> list[dict[str, str]]:
        creators, _, _, _ = self._prepare_creators_from_metadata(metadata)
        return creators

    def _make_connector_item(self, doi: str, metadata: dict[str, Any], index: int) -> dict[str, Any]:
        clean_year = extract_four_digit_year(metadata.get("date") or metadata.get("year"))
        item = {
            "id": f"codex-doi-{index}-{uuid.uuid4().hex[:10]}",
            "itemType": "journalArticle",
            "title": metadata.get("title") or doi,
            "creators": self._creators_from_metadata(metadata),
            "publicationTitle": metadata.get("journal") or "",
            "date": str(clean_year) if clean_year is not None else "",
            "DOI": doi,
            "url": metadata.get("url") or f"https://doi.org/{doi}",
        }
        if metadata.get("issn"):
            item["ISSN"] = str(metadata["issn"])
        if metadata.get("volume"):
            item["volume"] = str(metadata["volume"])
        if metadata.get("issue"):
            item["issue"] = str(metadata["issue"])
        if metadata.get("pages"):
            item["pages"] = str(metadata["pages"])
        if metadata.get("abstract"):
            item["abstractNote"] = str(metadata["abstract"])
        return item

    async def _fetch_metadata_or_raise(self, doi: str) -> dict[str, Any]:
        try:
            metadata = await self.verification_service.fetch_doi_metadata_async(doi)
        except Exception as exc:
            raise ZoteroImportError("DOI 元数据获取失败", f"{doi}: {exc}") from exc
        if not metadata.get("title"):
            raise ZoteroImportError("DOI 元数据获取失败", f"{doi}: 未解析到题名。")
        if not metadata.get("issn"):
            raise ZoteroImportError(
                "DOI 元数据获取失败",
                f"{doi}: 未成功提取并映射期刊 ISSN，已按强约束停止导入。",
            )
        return metadata

    async def _save_items_to_selected_collection(
        self,
        target: SelectedTarget,
        metadata_rows: list[tuple[str, dict[str, Any]]],
    ) -> None:
        payload = {
            "sessionID": f"codex-doi-batch-{uuid.uuid4().hex}",
            "target": {
                "libraryID": target.library_id,
                "collectionKey": target.collection_key,
            },
            "items": [
                self._make_connector_item(doi, metadata, index)
                for index, (doi, metadata) in enumerate(metadata_rows, start=1)
            ],
        }
        try:
            await self._post_connector_json("/connector/saveItems", payload)
        except Exception as exc:
            raise ZoteroImportError("本地 Connector 写入失败", str(exc)) from exc

    async def import_doi_metadata_to_selected_collection(self, dois: list[str]) -> MetadataImportReport:
        normalized: list[str] = []
        for doi in dois:
            clean = normalize_doi(doi)
            if clean:
                normalized.append(clean)
        if not normalized:
            raise ZoteroImportError("输入校验失败", "未提供有效 DOI。")

        await self._ping_connector()
        selected_target = await self.get_selected_target()
        if selected_target.attach_to_library_root or not selected_target.collection_name:
            raise ZoteroImportError("选中集合获取失败", "Zotero 当前未选中具体集合。")
        if not selected_target.collection_key:
            raise ZoteroImportError(
                "选中集合获取失败",
                f"无法将集合 {selected_target.collection_name} 映射为本地 API collection key。",
            )
        if not selected_target.editable:
            raise ZoteroImportError("选中集合获取失败", "当前 Zotero 选中集合不可写。")

        existing_matches = await self.lookup_existing_items_map_by_doi(normalized)
        before_matches = await self._collection_matches_by_doi(selected_target.collection_key, normalized)
        missing_dois = [doi for doi in normalized if doi not in existing_matches]
        metadata_rows = [
            (doi, await self._fetch_metadata_or_raise(doi))
            for doi in missing_dois
        ]
        hygiene_warnings: list[str] = []
        overall_hygiene_status = "not_checked"
        metadata_hygiene_map = {
            doi: self._prepare_creators_from_metadata(metadata)[1:]
            for doi, metadata in metadata_rows
        }

        if metadata_rows:
            await self._save_items_to_selected_collection(selected_target, metadata_rows)

        after_matches = await self._collection_matches_by_doi(selected_target.collection_key, normalized)
        results: list[ImportedMetadataRecord] = []
        reused_count = 0

        for doi in normalized:
            existing = existing_matches.get(doi)
            if existing is None:
                continue
            if existing.metadata_hygiene_status == "normalized":
                repair_result = await self.normalize_existing_item_creators(existing.item_key)
                refreshed_existing_map = await self.lookup_existing_items_map_by_doi([doi])
                existing = refreshed_existing_map.get(doi) or existing
                if repair_result.hygiene_warnings:
                    hygiene_warnings.extend(repair_result.hygiene_warnings)
                if repair_result.status == "manual_review_required":
                    hygiene_warnings.append(f"{existing.item_key}: {repair_result.details}")
                elif repair_result.status == "unsupported_by_local_api":
                    hygiene_warnings.append(
                        f"{existing.item_key}: creator normalization detected; please review and fix manually in Zotero."
                    )
                elif repair_result.status != "updated":
                    hygiene_warnings.append(
                        f"{existing.item_key}: creator normalization review returned status {repair_result.status}."
                    )
                else:
                    existing.metadata_hygiene_status = repair_result.metadata_hygiene_status
                    existing.normalized_creators = repair_result.normalized_creators
                    existing.hygiene_warnings = repair_result.hygiene_warnings

            citekey_result = await self.citekey_service.export_item_citekey_async(
                existing.item_key,
                doi=doi,
                title=existing.title,
            )
            if citekey_result.status != "ok" or not citekey_result.citekey:
                raise ZoteroImportError(
                    "BBT 提取失败",
                    f"{doi} / {existing.item_key}: {citekey_result.details}",
                )

            results.append(
                ImportedMetadataRecord(
                    doi=doi,
                    title=existing.title or doi,
                    item_key=existing.item_key,
                    citekey=citekey_result.citekey,
                    issn=existing.issn,
                    abstract_note=existing.abstract_note,
                    citation_anchor=citekey_result.citation_anchor or f"[@{citekey_result.citekey}]",
                    collection_name=selected_target.collection_name or selected_target.library_name,
                    collection_key=selected_target.collection_key,
                    library_id=selected_target.library_id,
                    write_route="existing_library_item",
                    verification_status="reused_existing_library_item",
                    metadata_hygiene_status=existing.metadata_hygiene_status,
                    normalized_creators=existing.normalized_creators,
                    hygiene_warnings=existing.hygiene_warnings,
                )
            )
            hygiene_warnings.extend(existing.hygiene_warnings)
            reused_count += 1

        for doi, metadata in metadata_rows:
            before_keys = {match.item_key for match in before_matches.get(doi, []) if match.item_key}
            after_rows = after_matches.get(doi, [])
            if not after_rows:
                raise ZoteroImportError(
                    "集合回读失败",
                    f"{doi}: 写入后未在集合 {selected_target.collection_name}({selected_target.collection_key}) 中找到条目。",
                )

            new_rows = [match for match in after_rows if match.item_key not in before_keys]
            chosen = new_rows[0] if new_rows else after_rows[0]
            if not new_rows:
                reused_count += 1
            if chosen.metadata_hygiene_status == "normalized":
                repair_result = await self.normalize_existing_item_creators(chosen.item_key)
                refreshed_after_rows = await self._collection_matches_by_doi(selected_target.collection_key, [doi])
                chosen = (refreshed_after_rows.get(doi) or [chosen])[0]
                hygiene_warnings.extend(repair_result.hygiene_warnings)
                if repair_result.status == "manual_review_required":
                    hygiene_warnings.append(f"{chosen.item_key}: {repair_result.details}")
                elif repair_result.status == "unsupported_by_local_api":
                    hygiene_warnings.append(
                        f"{chosen.item_key}: creator normalization detected; please review and fix manually in Zotero."
                    )
                elif repair_result.status != "updated":
                    hygiene_warnings.append(
                        f"{chosen.item_key}: creator normalization review returned status {repair_result.status}."
                    )
                else:
                    chosen.metadata_hygiene_status = repair_result.metadata_hygiene_status
                    chosen.normalized_creators = repair_result.normalized_creators
                    chosen.hygiene_warnings = repair_result.hygiene_warnings

            citekey_result = await self.citekey_service.export_item_citekey_async(
                chosen.item_key,
                doi=doi,
                title=metadata.get("title"),
            )
            if citekey_result.status != "ok" or not citekey_result.citekey:
                raise ZoteroImportError(
                    "BBT 提取失败",
                    f"{doi} / {chosen.item_key}: {citekey_result.details}",
                )

            results.append(
                ImportedMetadataRecord(
                    doi=doi,
                    title=metadata.get("title") or chosen.title or doi,
                    item_key=chosen.item_key,
                    citekey=citekey_result.citekey,
                    issn=metadata.get("issn") or chosen.issn,
                    abstract_note=metadata.get("abstract") or chosen.abstract_note,
                    citation_anchor=citekey_result.citation_anchor or f"[@{citekey_result.citekey}]",
                    collection_name=selected_target.collection_name or selected_target.library_name,
                    collection_key=selected_target.collection_key,
                    library_id=selected_target.library_id,
                    metadata_hygiene_status=(
                        metadata_hygiene_map.get(doi, ("not_checked", [], []))[0]
                        if metadata_hygiene_map.get(doi, ("not_checked", [], []))[0] == "normalized"
                        else chosen.metadata_hygiene_status
                    ),
                    normalized_creators=(
                        metadata_hygiene_map.get(doi, ("not_checked", [], []))[1]
                        or chosen.normalized_creators
                    ),
                    hygiene_warnings=(
                        metadata_hygiene_map.get(doi, ("not_checked", [], []))[2]
                        + chosen.hygiene_warnings
                    ),
                )
            )
            hygiene_warnings.extend(metadata_hygiene_map.get(doi, ("not_checked", [], []))[2])
            hygiene_warnings.extend(chosen.hygiene_warnings)

        statuses = {record.metadata_hygiene_status for record in results if record.metadata_hygiene_status}
        if "normalized" in statuses:
            overall_hygiene_status = "normalized"
        elif "clean" in statuses:
            overall_hygiene_status = "clean"

        return MetadataImportReport(
            selected_target=selected_target,
            requested_count=len(normalized),
            imported_count=len(metadata_rows),
            reused_count=reused_count,
            write_route=(
                "existing_library_item"
                if reused_count and not metadata_rows
                else "mixed"
                if reused_count and metadata_rows
                else "connector"
            ),
            results=results,
            metadata_hygiene_status=overall_hygiene_status,
            hygiene_warnings=hygiene_warnings,
        )
