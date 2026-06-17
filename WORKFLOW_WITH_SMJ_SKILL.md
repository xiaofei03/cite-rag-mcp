# WORKFLOW_WITH_SMJ_SKILL

## Role split

`SMJ_empirical_writing_skill` is responsible for:

- argument planning
- hypothesis logic
- SMJ-style empirical drafting

`cite-rag-mcp` is responsible for:

- reference verification
- Zotero duplicate detection
- Zotero citekeys
- Word export with live Zotero citations

Codex is the orchestrator and should call them in stages.

## Recommended staged workflow

### Stage 1: Reference retrieval

Call:

- `run_reference_workflow(mode="retrieve_only", retrieval_request=...)`, or
- `run_reference_workflow(mode="retrieve_only", citation_need_csv_path=...)`

Output:

- verified reference records
- citekeys
- optional `verified_references.csv`

Rules:

- no body drafting
- no Markdown rewriting
- no Word export
- no fabricated references

### Stage 2: SMJ planning and drafting

Pass the verified citekey set to `SMJ_empirical_writing_skill`.

The SMJ skill should:

- plan the argument
- draft the paper in SMJ style
- use only the verified citekeys returned by `cite-rag-mcp`
- avoid inventing any citation not present in the verified set

`cite-rag-mcp` can also provide the strict citekey system prompt and citekey bundle for this stage.

### Stage 3: Optional import-only refresh

If the drafting stage introduces a known DOI or a manually curated verified list, Codex can call:

- `run_reference_workflow(mode="import_only", ...)`

This stage is only for Zotero confirmation or DOI-backed import. It must not perform open-ended retrieval.

### Stage 4: Word export

Call:

- `run_reference_workflow(mode="export_only", markdown_path=..., output_filename=...)`

or:

- `run_reference_workflow(mode="full_pipeline", ..., markdown_path=..., output_filename=...)`

Rules:

- do not rewrite the draft during export
- do not add references during export
- always use Zotero live citations
- always use `template.docx`
- always render through Pandoc plus `zotero.lua`

## Why this split works

- The SMJ skill stays focused on theory, argument, and style.
- `cite-rag-mcp` stays focused on evidence control, citekeys, and document rendering.
- Codex can orchestrate them safely without splitting `cite-rag-mcp` into multiple MCP servers.

## Full pipeline note

`full_pipeline` remains available for the one-stop path. In that mode, the MCP still:

- verifies references
- prepares citekeys
- preserves anti-fabrication rules
- exports Word only through `generate_final_word_document`

The staged mode is recommended when working with `SMJ_empirical_writing_skill`, because it keeps drafting and reference verification cleanly separated.
