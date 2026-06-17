THEORY_WRITER_CITEKEY_SYSTEM_PROMPT = (
    "You are a rigorous academic writing assistant.\n"
    "Your arguments must rely only on the verified reference list, citekey map, and abstractNote fields provided to you.\n\n"
    "Hard constraints:\n"
    "1. Never use literature outside the supplied verified set.\n"
    "2. Never fabricate a DOI, author, year, title, journal, abstract claim, or citekey.\n"
    "3. Never output bare @citekey. Use only dual-track citation syntax.\n"
    "4. [@citekey] is for parenthetical citations only.\n"
    "5. [-@citekey] is for narrative citations only, and only when the author name is already written explicitly in the same sentence.\n"
    "6. Never use [-@citekey] as a shortcut for an authorless parenthetical citation. For example, '... resilience [-@foo2015]' and '... governance dynamics [-@bar2020]' are forbidden.\n"
    "7. Allowed examples: 'Martens and Sextroh [-@martensAnalystCoverageOverlaps2021] show ...' and 'Prior work links signaling ambiguity to stakeholder miscalibration [@connellySignalingTheoryReview2010].'\n"
    "8. Before any cited prose, emit a <Citation_Reasoning> block that maps each planned claim to the exact citekey and explains how abstractNote supports that claim.\n"
    "9. If a reference abstract does not directly support a claim, do not cite it. Say so explicitly inside <Citation_Reasoning>.\n"
    "10. Do not hand-write bibliography entries. The bibliography must be generated later by Zotero and Pandoc.\n"
    "11. If abstractNote is 'Abstract not available', stop extending claim-level reasoning from that source.\n"
    "12. Do not browse for missing abstracts.\n"
    "13. Never create temporary scripts to generate or modify Word documents. The only allowed Markdown-to-Word route is generate_final_word_document.\n"
    "14. <Citation_Reasoning> must never be passed into generate_final_word_document.\n"
    "15. The final Markdown body must use strict heading hierarchy: # for the paper title, ## for core sections.\n"
    "16. The final Markdown body must end with '# References' followed by '<div id=\"refs\"></div>'.\n"
)


WORKFLOW_MODE_RULES = {
    "retrieve_only": {
        "allowed": [
            "search literature",
            "verify DOI metadata",
            "check Zotero duplicates",
            "import missing verified items into Zotero when needed",
            "return citekeys and verified_references.csv-style output",
        ],
        "forbidden": [
            "draft body text",
            "rewrite Markdown",
            "export Word documents",
            "invent citations",
        ],
    },
    "import_only": {
        "allowed": [
            "confirm existing Zotero items",
            "import DOI-backed items into Zotero",
            "return citekey_mapping.csv-style output",
        ],
        "forbidden": [
            "search for new literature beyond the provided identifiers",
            "draft body text",
            "export Word documents",
            "invent citations",
        ],
    },
    "export_only": {
        "allowed": [
            "read existing Markdown",
            "render Word via Pandoc, zotero.lua, and template.docx",
        ],
        "forbidden": [
            "search literature",
            "add or swap references",
            "rewrite body text",
            "invent citations",
        ],
    },
    "full_pipeline": {
        "allowed": [
            "retrieve and verify references",
            "confirm Zotero citekeys",
            "build the citekey-constrained drafting bundle",
            "export Word from finalized Markdown",
        ],
        "forbidden": [
            "invent citations",
            "bypass generate_final_word_document for Word output",
        ],
    },
}


SMJ_SKILL_WORKFLOW_GUIDANCE = (
    "Coordination contract with SMJ_empirical_writing_skill:\n"
    "1. SMJ_empirical_writing_skill is responsible for argument planning and SMJ-style drafting.\n"
    "2. cite-rag-mcp is responsible for reference verification, Zotero citekeys, and Word export.\n"
    "3. Codex acts as the orchestrator and should call the MCP in stages: retrieve_only or import_only first, then hand verified citekeys to the SMJ skill for drafting, then call export_only for Word rendering.\n"
    "4. full_pipeline keeps the end-to-end one-stop path available, but it still must not fabricate references and it still uses generate_final_word_document plus template.docx for final Word output.\n"
    "5. If the task is an introduction draft, default to a five-paragraph structure and target 15-20 distinct cited sources before finalization.\n"
    "6. For introduction drafting, retrieval should explicitly cover AI washing or machinewashing, AI narrative-action divergence, signaling theory, organizational resilience, trade credit or external resource relationships, and analyst coverage or information intermediation.\n"
)


def build_citekey_generation_bundle(
    user_requirement: str,
    citekey_map: list[dict],
) -> dict:
    distinct_citekeys = len(
        {
            (record.get("citekey") or "").strip()
            for record in citekey_map
            if (record.get("citekey") or "").strip()
        }
    )
    introduction_like = "introduction" in (user_requirement or "").casefold()
    return {
        "user_requirement": user_requirement,
        "citation_format": "dual_track_citekey_only",
        "allowed_citations": citekey_map,
        "system_prompt": THEORY_WRITER_CITEKEY_SYSTEM_PROMPT,
        "anti_fabrication_rule": "Only use citekeys and reference facts that exist in allowed_citations.",
        "introduction_contract": {
            "default_paragraph_count": 5,
            "target_distinct_cited_sources_min": 15,
            "target_distinct_cited_sources_max": 20,
            "distinct_cited_sources_available": distinct_citekeys,
            "target_met": (distinct_citekeys >= 15) if introduction_like else None,
            "retrieval_buckets": [
                "AI washing / machinewashing",
                "AI narrative-action divergence",
                "signaling theory",
                "organizational resilience",
                "trade credit / external resource relationships",
                "analyst coverage / information intermediation",
            ],
        },
    }


def build_workflow_mode_bundle(mode: str) -> dict:
    if mode not in WORKFLOW_MODE_RULES:
        raise ValueError(f"Unsupported workflow mode: {mode}")
    return {
        "mode": mode,
        "rules": WORKFLOW_MODE_RULES[mode],
        "smj_coordination": SMJ_SKILL_WORKFLOW_GUIDANCE,
    }
