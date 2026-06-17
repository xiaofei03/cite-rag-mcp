"""HTML report generator for DOI verification results."""

import datetime
from pathlib import Path
from typing import Optional

from .verifier import VerifyResult
from .formatters import get_format_label, ReferenceData

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DOI Verification Report</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: #f5f5f5;
    color: #333;
    line-height: 1.6;
}}
.container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}
h1 {{
    font-size: 24px;
    font-weight: 700;
    margin-bottom: 8px;
    color: #1a1a2e;
}}
.subtitle {{
    color: #888;
    font-size: 14px;
    margin-bottom: 24px;
}}

/* Summary cards */
.summary {{
    display: flex;
    gap: 16px;
    margin-bottom: 24px;
    flex-wrap: wrap;
}}
.card {{
    background: #fff;
    border-radius: 12px;
    padding: 20px 24px;
    flex: 1;
    min-width: 160px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    text-align: center;
    transition: transform 0.15s;
}}
.card:hover {{ transform: translateY(-2px); }}
.card .count {{
    font-size: 36px;
    font-weight: 800;
    line-height: 1.2;
}}
.card .label {{
    font-size: 13px;
    color: #888;
    margin-top: 4px;
}}
.card.total .count {{ color: #1a1a2e; }}
.card.green .count {{ color: #16a34a; }}
.card.yellow .count {{ color: #ca8a04; }}
.card.red .count {{ color: #dc2626; }}
.card.blue .count {{ color: #2563eb; }}

/* Filters */
.filters {{
    display: flex;
    gap: 8px;
    margin-bottom: 20px;
    flex-wrap: wrap;
}}
.filter-btn {{
    border: 1px solid #ddd;
    background: #fff;
    padding: 8px 20px;
    border-radius: 20px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: all 0.15s;
    color: #555;
}}
.filter-btn:hover {{ border-color: #999; }}
.filter-btn.active {{
    background: #1a1a2e;
    color: #fff;
    border-color: #1a1a2e;
}}

/* Table */
.table-wrap {{
    background: #fff;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}}
table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}}
thead {{
    background: #fafafa;
    border-bottom: 2px solid #eee;
}}
th {{
    padding: 12px 16px;
    text-align: left;
    font-weight: 600;
    color: #555;
    white-space: nowrap;
}}
td {{
    padding: 12px 16px;
    border-bottom: 1px solid #f0f0f0;
    vertical-align: top;
}}
tr.status-verified {{ background: #f0fdf4; }}
tr.status-discrepancy {{ background: #fefce8; }}
tr.status-ghost {{ background: #fef2f2; }}
tr.status-single_source {{ background: #fefce8; }}
tr.status-chinese_db {{ background: #eff6ff; }}
tr:hover {{ filter: brightness(0.97); }}

/* Ghost with reference — orange tint */
tr.status-ghost.has-ref {{ background: #fff7ed; }}

/* Status badge */
.badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
}}
.badge.green {{ background: #dcfce7; color: #166534; }}
.badge.yellow {{ background: #fef9c3; color: #854d0e; }}
.badge.red {{ background: #fee2e2; color: #991b1b; }}
.badge.blue {{ background: #dbeafe; color: #1d4ed8; }}
.badge.orange {{ background: #ffedd5; color: #9a3412; }}

.doi {{ font-family: "SF Mono", "Fira Code", monospace; font-size: 12px; word-break: break-all; }}
.title {{ max-width: 350px; }}
.diff-detail {{ font-size: 12px; color: #b45309; }}
.diff-detail .field-name {{ font-weight: 600; }}
.nodata {{ color: #bbb; font-style: italic; }}

.citation {{
    font-size: 12px;
    line-height: 1.5;
    max-width: 400px;
    word-break: break-word;
}}

.copy-btn {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border: 1px solid #ddd;
    background: #fff;
    border-radius: 4px;
    cursor: pointer;
    color: #888;
    margin-left: 6px;
    vertical-align: middle;
    transition: all 0.15s;
}}
.copy-btn:hover {{
    background: #1a1a2e;
    color: #fff;
    border-color: #1a1a2e;
}}
.copy-btn.copied {{
    background: #16a34a;
    color: #fff;
    border-color: #16a34a;
}}

.footer {{
    text-align: center;
    color: #aaa;
    font-size: 12px;
    margin-top: 32px;
    padding: 16px;
}}

/* Responsive */
@media (max-width: 768px) {{
    .summary {{ flex-direction: column; }}
    .card {{ min-width: auto; }}
    th, td {{ padding: 8px 10px; font-size: 12px; }}
}}
</style>
</head>
<body>
<div class="container">
<h1>DOI Verification Report</h1>
<p class="subtitle">Generated: {timestamp} | Sources: CrossRef &amp; OpenAlex</p>

<!-- Summary Cards -->
<div class="summary">
    <div class="card total">
        <div class="count">{total}</div>
        <div class="label">Total</div>
    </div>
    <div class="card green">
        <div class="count">{verified}</div>
        <div class="label">Consistent</div>
    </div>
    <div class="card yellow">
        <div class="count">{discrepancy}</div>
        <div class="label">Discrepancies</div>
    </div>
    <div class="card blue">
        <div class="count">{chinese_db}</div>
        <div class="label">Chinese DB</div>
    </div>
    <div class="card red">
        <div class="count">{ghost}</div>
        <div class="label">Ghost Refs</div>
    </div>
</div>

<!-- Filter Buttons -->
<div class="filters">
    <button class="filter-btn active" onclick="filterTable('all', this)">All</button>
    <button class="filter-btn" onclick="filterTable('verified', this)">Consistent</button>
    <button class="filter-btn" onclick="filterTable('discrepancy', this)">Discrepancies</button>
    <button class="filter-btn" onclick="filterTable('single_source', this)">Single Source</button>
    <button class="filter-btn" onclick="filterTable('chinese_db', this)">Chinese DB</button>
    <button class="filter-btn" onclick="filterTable('ghost', this)">Ghost</button>
</div>

<!-- Results Table -->
<div class="table-wrap">
<table>
<thead>
<tr>
    <th>#</th>
    <th>DOI</th>
    <th>Status</th>
    <th>Title</th>
    <th>Author</th>
    <th>Year</th>
    <th>Journal</th>
    <th>{citation_header}</th>
    <th>Details</th>
</tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</div>

<div class="footer">
    doi-verify &mdash; CrossRef + OpenAlex Cross-Validation Tool
</div>
</div>

<script>
function filterTable(status, btn) {{
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    document.querySelectorAll('tbody tr').forEach(row => {{
        if (status === 'all') {{
            row.style.display = '';
        }} else {{
            row.style.display = row.dataset.status === status ? '' : 'none';
        }}
    }});
}}

function copyCitation(btn) {{
    var citation = btn.getAttribute('data-citation');
    var textarea = document.createElement('textarea');
    textarea.value = citation;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try {{
        document.execCommand('copy');
        btn.classList.add('copied');
        setTimeout(function() {{ btn.classList.remove('copied'); }}, 1500);
    }} catch(e) {{
        alert('Copy failed, please copy manually');
    }}
    document.body.removeChild(textarea);
}}
</script>
</body>
</html>"""


def _escape(s: Optional[str]) -> str:
    """Basic HTML entity escaping."""
    if s is None:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _escape_attr(s: str) -> str:
    """Escape a string for use in an HTML attribute (data-*)."""
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("'", "&#39;").replace("<", "&lt;").replace(">", "&gt;")


def generate_report(results: list[VerifyResult], output_path: str, fmt: str = "apa") -> str:
    """Generate an HTML report and write to output_path.

    Args:
        results: List of VerifyResult objects.
        output_path: Path for the output HTML file.
        fmt: Citation format ('apa', 'gbt', 'mla').

    Returns the absolute path of the generated file.
    """
    total = len(results)
    verified = sum(1 for r in results if r.status == "verified")
    discrepancy = sum(1 for r in results if r.status in ("discrepancy", "single_source"))
    ghost = sum(1 for r in results if r.status == "ghost")
    chinese_db = sum(1 for r in results if r.status == "chinese_db")

    fmt_label = get_format_label(fmt)

    rows_parts = []
    for i, r in enumerate(results, 1):
        # Citation — prefer DOI metadata > user reference
        citation = r.get_citation(fmt)
        if citation and r.status not in ("ghost",):
            citation_html = f'<div class="citation">{_escape(citation)}</div>'
            # Add copy button
            citation_html += (
                f'<button class="copy-btn" onclick="copyCitation(this)" '
                f'data-citation="{_escape_attr(citation)}" title="Copy citation">'
                f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                f'stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>'
                f'<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
                f'</button>'
            )
        elif r.status == "chinese_db" and r.cnki and r.cnki.doi_landing_url:
            citation_html = (
                f'<span class="nodata">Chinese DB — </span>'
                f'<a href="{_escape_attr(r.cnki.doi_landing_url)}" target="_blank">Open</a>'
            )
        else:
            citation_html = '<span class="nodata">&mdash;</span>'

        # Title cell — prefer CrossRef > OpenAlex > CNKI
        title_cr = r.crossref.title if r.crossref and r.crossref.title else None
        title_oa = r.openalex.title if r.openalex and r.openalex.title else None
        title_cn = r.cnki.title if r.cnki and r.cnki.title else None
        title_display = title_cr or title_oa or title_cn

        if r.status == "discrepancy" and title_cr and title_oa and title_cr.lower() != title_oa.lower():
            title_html = (
                f'<div><strong>CrossRef:</strong> {_escape(title_cr)}</div>'
                f'<div><strong>OpenAlex:</strong> {_escape(title_oa)}</div>'
            )
        elif title_display:
            title_html = _escape(title_display)
        else:
            title_html = '<span class="nodata">&mdash;</span>'

        # Author cell
        author_cr = r.crossref.first_author.title() if r.crossref and r.crossref.first_author else None
        author_oa = r.openalex.first_author.title() if r.openalex and r.openalex.first_author else None
        author_cn = r.cnki.first_author.title() if r.cnki and r.cnki.first_author else None
        author_display = author_cr or author_oa or author_cn
        author_html = author_display or '<span class="nodata">&mdash;</span>'

        # Year cell
        year_cr = str(r.crossref.year) if r.crossref and r.crossref.year else None
        year_oa = str(r.openalex.year) if r.openalex and r.openalex.year else None
        year_cn = str(r.cnki.year) if r.cnki and r.cnki.year else None
        year_display = year_cr or year_oa or year_cn
        year_html = year_display or '<span class="nodata">&mdash;</span>'

        # Journal cell
        journal_cr = r.crossref.journal if r.crossref and r.crossref.journal else None
        journal_oa = r.openalex.journal if r.openalex and r.openalex.journal else None
        journal_cn = r.cnki.journal if r.cnki and r.cnki.journal else None
        journal_display = journal_cr or journal_oa or journal_cn
        journal_html = journal_display or '<span class="nodata">&mdash;</span>'

        # Details cell
        if r.diffs:
            diff_items = []
            for d in r.diffs:
                cr_v = d.crossref_value or "<em>missing</em>"
                oa_v = d.openalex_value or "<em>missing</em>"
                diff_items.append(
                    f'<span class="field-name">{_escape(d.field)}:</span> '
                    f'CR={cr_v} vs OA={oa_v}'
                )
            diff_html = "<br>".join(diff_items)
            detail_html = f'<div class="diff-detail">{diff_html}</div>'
        elif r.status == "ghost":
            detail_html = '<span class="nodata">Not found in any source</span>'
        elif r.status == "single_source":
            source = "CrossRef" if r.crossref and r.crossref.is_valid() else "OpenAlex"
            detail_html = f'<span class="nodata">Only in {source}</span>'
        elif r.status == "chinese_db":
            source_tag = ""
            if r.cnki:
                source_tag = f'<span style="color:#2563eb">Found via CNKI/Wanfang</span>'
                if r.cnki.doi_resolves and not r.cnki.title:
                    source_tag += '<br><span class="nodata">DOI resolves but metadata not extracted</span>'
                if r.cnki.doi_landing_url:
                    source_tag += f'<br><a href="{_escape_attr(r.cnki.doi_landing_url)}" target="_blank">Open landing page</a>'
            detail_html = source_tag or '<span class="nodata">Chinese database literature</span>'
        else:
            detail_html = '<span style="color:#16a34a">All fields match</span>'

        row = (
            f'<tr class="{r.row_css_class}" data-status="{r.status}">'
            f'<td>{i}</td>'
            f'<td class="doi">{_escape(r.doi)}</td>'
            f'<td><span class="badge {r.status_color}">{r.status_label_cn}</span></td>'
            f'<td class="title">{title_html}</td>'
            f'<td>{author_html}</td>'
            f'<td>{year_html}</td>'
            f'<td>{journal_html}</td>'
            f'<td>{citation_html}</td>'
            f'<td>{detail_html}</td>'
            f'</tr>'
        )
        rows_parts.append(row)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = TEMPLATE.format(
        timestamp=timestamp,
        total=total,
        verified=verified,
        discrepancy=discrepancy,
        chinese_db=chinese_db,
        ghost=ghost,
        citation_header=fmt_label,
        rows="\n".join(rows_parts),
    )

    out = Path(output_path)
    out.write_text(html, encoding="utf-8")
    return str(out.resolve())
