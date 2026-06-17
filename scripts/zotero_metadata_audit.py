from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
MCP_ROOT = SCRIPT_DIR.parent
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from services.zotero_service import ZoteroService


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
    "10.1002/JOB.737": (
        2012,
        "Formal publication year from Journal of Organizational Behavior 33(1).",
    ),
    "10.5465/AMJ.2012.0122": (
        2014,
        "Formal publication year from Academy of Management Journal 57(1).",
    ),
    "10.5465/AMJ.2020.1627": (
        2022,
        "Formal publication year from Academy of Management Journal issue/year, not online-first or aggregator database year.",
    ),
    "10.1037/XGE0000033": (
        2015,
        "Formal publication year from Journal of Experimental Psychology: General 144(1).",
    ),
    "10.1287/MNSC.2016.2643": (
        2018,
        "Formal publication year from Management Science 64(3).",
    ),
    "10.25300/MISQ/2021/16274": (
        2021,
        "Formal publication year from MIS Quarterly 45(3), not aggregator database year.",
    ),
    "10.5465/AMR.2013.0318": (
        2015,
        "Formal publication year from Academy of Management Review 40(1).",
    ),
    "10.1016/J.IM.2019.103174": (
        2020,
        "Formal publication year from Information & Management 57.",
    ),
}


def extract_citekeys(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for citekey in re.findall(r"@([A-Za-z0-9_:.+/\-]+)", text):
        if citekey not in seen:
            seen.add(citekey)
            result.append(citekey)
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "citekey",
        "item_key",
        "doi",
        "zotero_title",
        "verified_title",
        "zotero_year",
        "verified_year",
        "zotero_journal",
        "verified_journal",
        "status",
        "action_required",
        "details",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict[str, Any]], warnings: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Zotero Metadata Audit",
        "",
        "| citekey | Zotero year | Verified year | Status | Action |",
        "|---|---:|---:|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {citekey} | {zotero_year} | {verified_year} | {status} | {action_required} |".format(
                citekey=row.get("citekey") or "",
                zotero_year=row.get("zotero_year") or "",
                verified_year=row.get("verified_year") or "",
                status=row.get("status") or "",
                action_required=(row.get("action_required") or "").replace("|", "/"),
            )
        )
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_year_overrides(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    result: dict[str, int] = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            citekey = (row.get("citekey") or "").strip().lstrip("@")
            year = (row.get("year") or row.get("authoritative_year") or "").strip()
            if citekey and year.isdigit():
                result[citekey] = int(year)
    return result


def normalize_doi(value: Any) -> str:
    doi = str(value or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
            break
    return doi.upper()


def apply_formal_publication_year_rule(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        doi = normalize_doi(row.get("doi"))
        if doi not in FORMAL_PUBLICATION_YEAR_OVERRIDES:
            continue
        formal_year, basis = FORMAL_PUBLICATION_YEAR_OVERRIDES[doi]
        if row.get("zotero_year") == formal_year:
            row["verified_year"] = formal_year
            row["status"] = "verified_formal_publication_year"
            row["action_required"] = ""
            row["details"] = basis
        elif row.get("status") == "verified":
            row["status"] = "metadata_mismatch"
            row["action_required"] = "Use the formal journal issue publication year, not the PsycNet/Crossref database year."
            row["details"] = f"Zotero year {row.get('zotero_year')} does not match formal publication year {formal_year}. {basis}"


def apply_year_overrides(rows: list[dict[str, Any]], overrides: dict[str, int]) -> None:
    for row in rows:
        citekey = (row.get("citekey") or "").strip().lstrip("@")
        if citekey not in overrides:
            continue
        override_year = overrides[citekey]
        if row.get("zotero_year") == override_year:
            row["verified_year"] = override_year
            row["status"] = "verified_manual_year"
            row["action_required"] = ""
            row["details"] = "Authoritative publication year supplied by local override table."


def refresh_counts_and_warnings(payload: dict[str, Any]) -> None:
    rows = payload["records"]
    payload["mismatch_count"] = sum(
        1
        for row in rows
        if row.get("status") in {"metadata_mismatch", "doi_missing", "verification_failed", "verification_failed_cached"}
    )
    payload["missing_count"] = sum(1 for row in rows if row.get("status") == "missing_in_zotero")
    if payload["mismatch_count"] == 0 and payload["missing_count"] == 0:
        payload["warnings"] = []


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit Zotero live-citation metadata by citekeys before Word export."
    )
    parser.add_argument("--markdown", help="Markdown file containing Pandoc/Zotero citekeys.")
    parser.add_argument(
        "--markdown-glob",
        help="Glob pattern resolved from the current directory; useful for non-ASCII filenames.",
    )
    parser.add_argument("--citekey", action="append", default=[], help="A citekey to audit. Repeatable.")
    parser.add_argument("--csv", help="Optional CSV output path.")
    parser.add_argument("--md", help="Optional Markdown summary output path.")
    parser.add_argument(
        "--year-override-csv",
        help="CSV with citekey,year for authoritative publication-year overrides.",
    )
    parser.add_argument(
        "--zotero-only",
        action="store_true",
        help="Skip DOI metadata verification and only resolve citekeys in Zotero.",
    )
    parser.add_argument(
        "--external-concurrency",
        type=int,
        default=4,
        help="Maximum concurrent external DOI metadata checks for non-overridden DOI records.",
    )
    parser.add_argument(
        "--no-failure-cache",
        action="store_true",
        help="Disable the local DOI metadata failure cache for this run.",
    )
    args = parser.parse_args()

    citekeys = list(args.citekey)
    if args.markdown:
        citekeys.extend(extract_citekeys(Path(args.markdown).read_text(encoding="utf-8")))
    if args.markdown_glob:
        matches = sorted(Path.cwd().glob(args.markdown_glob), key=lambda path: path.stat().st_mtime, reverse=True)
        if not matches:
            parser.error(f"No Markdown files matched --markdown-glob {args.markdown_glob!r}.")
        citekeys.extend(extract_citekeys(matches[0].read_text(encoding="utf-8")))
    if not citekeys:
        parser.error("Provide --markdown or at least one --citekey.")

    service = ZoteroService()
    report = await service.audit_metadata_by_citekeys(
        citekeys,
        verify_doi_metadata=not args.zotero_only,
        formal_year_overrides=FORMAL_PUBLICATION_YEAR_OVERRIDES,
        external_concurrency=args.external_concurrency,
        use_failure_cache=not args.no_failure_cache,
    )
    payload = report.to_dict()
    rows = payload["records"]
    overrides = read_year_overrides(args.year_override_csv)
    if overrides:
        apply_year_overrides(rows, overrides)
    refresh_counts_and_warnings(payload)

    if args.csv:
        write_csv(Path(args.csv), rows)
    if args.md:
        write_markdown(Path(args.md), rows, payload.get("warnings") or [])

    print(
        "checked={checked_count} mismatches={mismatch_count} missing={missing_count}".format(
            **payload
        )
    )
    for warning in payload.get("warnings") or []:
        print(f"WARNING: {warning}")
    return 1 if payload["mismatch_count"] or payload["missing_count"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
