# cite-rag-mcp

A local MCP server for evidence-safe academic citation workflows:

- verified-reference retrieval
- Zotero duplicate detection and DOI import
- Better BibTeX citekey extraction
- citekey-constrained writing bundles
- Word export through Pandoc, `zotero.lua`, and `template.docx`

The server supports four workflow modes through the `run_reference_workflow` tool:

1. `retrieve_only`
2. `import_only`
3. `export_only`
4. `full_pipeline`

## Safety Rules

- Never fabricate references, citekeys, DOIs, or citation facts.
- `retrieve_only` must not draft prose.
- `import_only` must not run open-ended literature retrieval.
- `export_only` must not retrieve literature or add references.
- `full_pipeline` keeps the end-to-end path, but still cannot bypass verified references.
- Word export must use Zotero live citations, `zotero.lua`, and `template.docx`.

## Requirements

- Python 3.11+
- Zotero Desktop running locally
- Better BibTeX for Zotero
- Pandoc on `PATH` for Word export

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Codex MCP Configuration

Example `config.toml` entry:

```toml
[mcp_servers.cite-rag-mcp]
command = "python"
args = ["C:\\path\\to\\cite-rag-mcp\\server.py"]
startup_timeout_sec = 20
```

Adjust the path for your local clone.

## Main Tool

Use `run_reference_workflow`.

### `retrieve_only`

Inputs:

- `citation_need_csv_path`, or
- `retrieval_request`

Behavior:

- Searches and ranks literature
- Verifies DOI metadata
- Checks Zotero duplicates
- Imports missing verified DOI items into the selected Zotero collection when needed
- Returns citekeys

It does not write body text and does not export Word.

### `import_only`

Inputs:

- `doi_list`, or
- `title_list` plus optional `authors_list`, or
- `verified_references_csv_path`

Behavior:

- Confirms whether items already exist in Zotero
- Imports DOI-backed items when needed
- Returns citekeys or a clear unresolved status

It does not draft body text and does not export Word.

### `export_only`

Inputs:

- `markdown_path`, or
- `markdown_content`

Behavior:

- Reads finalized Markdown
- Removes any `<Citation_Reasoning>` block before rendering
- Ensures the references anchor is present
- Calls Pandoc with `zotero.lua` and `template.docx`

It does not retrieve literature and does not add references.

### `full_pipeline`

Inputs:

- `retrieval_request` or `citation_need_csv_path`
- Optional finalized `markdown_path` or `markdown_content`

Behavior:

- Runs retrieval and verification
- Produces verified citekeys and a citekey-constrained drafting bundle
- Optionally exports Word if finalized Markdown is provided

The actual drafting step should still be performed by the orchestrator or a writing skill using the returned citekey bundle.

## Notes

- The included `references/custom_journal_catalog.xlsx` is used for journal filtering and ranking.
- The Zotero local connector is expected at `http://127.0.0.1:23119`.
- No Zotero credentials or API secrets are required by this server.
