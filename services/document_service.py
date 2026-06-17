from __future__ import annotations

import os
import shutil
import re
import subprocess
import tempfile
from pathlib import Path


class FinalWordDocumentService:
    """Render citekey-based Markdown into a Word document via Pandoc.

    Sync this repository's template.docx into the current working directory
    before rendering, then use that local template for Pandoc.
    """

    def __init__(self, script_dir: Path | None = None) -> None:
        self.script_dir = (script_dir or Path(__file__).resolve().parent).resolve()
        self.project_dir = self.script_dir.parent
        self.lua_filter_path = self.script_dir / "zotero.lua"
        self.reference_doc_path = self.project_dir / "template.docx"

    def _sync_reference_doc_to_cwd(self) -> Path:
        cwd_template = (Path.cwd() / "template.docx").resolve()
        canonical_template = self.reference_doc_path.resolve()
        if not canonical_template.exists():
            return cwd_template

        cwd_template.parent.mkdir(parents=True, exist_ok=True)
        if canonical_template != cwd_template:
            shutil.copy2(canonical_template, cwd_template)
        return cwd_template

    def _strip_process_leakage(self, text: str) -> str:
        forbidden_patterns = [
            r"^.*摘要未返回.*$",
            r"^.*abstractNote缺失.*$",
            r"^.*Abstract not available.*$",
            r"^.*根据系统要求.*$",
            r"^.*检索动作.*$",
            r"^.*数据读取状态.*$",
            r"^.*我们可以谨慎推断.*$",
            r"^.*由于摘要缺失.*$",
        ]
        for pattern in forbidden_patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)
        return text

    def _remove_citation_reasoning_block(self, text: str) -> str:
        return re.sub(
            r"(?:^|\n)<Citation_Reasoning>[\s\S]*?</Citation_Reasoning>(?=\n|$)",
            "\n\n",
            text,
            flags=re.IGNORECASE,
        )

    def _collect_distinct_citekeys(self, text: str) -> list[str]:
        return sorted(set(re.findall(r"@([A-Za-z0-9_:.+/\-]+)", text)))

    def _extract_introduction_section(self, text: str) -> str:
        lines = text.split("\n")
        intro_start = None
        intro_level = None

        for index, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.strip())
            if not match:
                continue
            hashes, heading_text = match.groups()
            if heading_text.strip().casefold() == "introduction":
                intro_start = index + 1
                intro_level = len(hashes)
                break

        if intro_start is None:
            return ""

        intro_lines: list[str] = []
        for line in lines[intro_start:]:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.strip())
            if match and len(match.group(1)) <= (intro_level or 6):
                break
            intro_lines.append(line)
        return "\n".join(intro_lines).strip()

    def _count_introduction_paragraphs(self, text: str) -> int | None:
        intro_section = self._extract_introduction_section(text)
        if not intro_section:
            return None
        blocks = [
            block.strip()
            for block in re.split(r"\n\s*\n", intro_section)
            if block.strip()
        ]
        paragraphs = [
            block
            for block in blocks
            if not block.startswith("#") and block != '<div id="refs"></div>'
        ]
        return len(paragraphs)

    def _last_sentence_fragment(self, text: str) -> str:
        boundary_candidates = [
            text.rfind("!"),
            text.rfind("?"),
            text.rfind("。"),
            text.rfind("！"),
            text.rfind("？"),
            text.rfind("\n"),
        ]
        boundary = max(boundary_candidates)
        return text[boundary + 1 :].strip()

    def _has_narrative_author_phrase(self, text: str) -> bool:
        fragment = self._last_sentence_fragment(text)
        fragment = re.sub(r"\s+", " ", fragment).strip()
        if not fragment:
            return False

        author_token = r"(?:[A-Z][A-Za-z'`.-]+|[A-Z]\.)"
        author_name = rf"{author_token}(?:\s+{author_token})*"
        english_phrase = rf"(?:{author_name})(?:\s+(?:and|&)\s+{author_name})?(?:\s+(?:et al\.|and colleagues))?"
        english_pattern = re.compile(
            rf"(?:^|.*\bby\s+)(?:{english_phrase})$",
            flags=re.IGNORECASE,
        )
        if english_pattern.search(fragment):
            return True

        chinese_name = r"(?:[A-Za-z][A-Za-z'`.-]*|[一-龥]+)"
        chinese_pattern = re.compile(
            rf"(?:{chinese_name})(?:\s*(?:和|与|及)\s*{chinese_name})?(?:\s*(?:等人|及其同事))?$"
        )
        return bool(chinese_pattern.search(fragment))

    def validate_citation_syntax(self, markdown_content: str) -> dict:
        text = markdown_content.replace("\r\n", "\n").replace("\r", "\n")
        errors: list[dict[str, str]] = []

        for match in re.finditer(r"\[([^\]]*-\@[^\]]+)\]", text):
            raw_citation = match.group(0)
            inner = match.group(1)
            citekeys = re.findall(r"-@([A-Za-z0-9_:.+/\-]+)", inner)
            if not citekeys:
                continue

            sentence_start = max(
                text.rfind("!", 0, match.start()),
                text.rfind("?", 0, match.start()),
                text.rfind("。", 0, match.start()),
                text.rfind("！", 0, match.start()),
                text.rfind("？", 0, match.start()),
                text.rfind("\n", 0, match.start()),
            )
            sentence_end_candidates = [
                value
                for value in (
                    text.find("!", match.end()),
                    text.find("?", match.end()),
                    text.find("。", match.end()),
                    text.find("！", match.end()),
                    text.find("？", match.end()),
                    text.find("\n", match.end()),
                )
                if value != -1
            ]
            sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
            sentence = text[sentence_start + 1 : sentence_end].strip()

            if ";" in inner:
                errors.append(
                    {
                        "citekey": ", ".join(citekeys),
                        "citation": raw_citation,
                        "sentence": sentence,
                        "suggestion": (
                            "Grouped suppress-author citations are not allowed. Use [@citekey] for grouped "
                            "parenthetical citations, or write each author explicitly before its own [-@citekey]."
                        ),
                    }
                )
                continue

            if not self._has_narrative_author_phrase(text[: match.start()]):
                errors.append(
                    {
                        "citekey": citekeys[0],
                        "citation": raw_citation,
                        "sentence": sentence,
                        "suggestion": (
                            "Use [@citekey] for a parenthetical citation, or write the author name explicitly "
                            "in the same sentence before keeping [-@citekey]."
                        ),
                    }
                )

        distinct_citekeys = self._collect_distinct_citekeys(text)
        introduction_paragraph_count = self._count_introduction_paragraphs(text)
        introduction_citation_target_met = None
        if self._extract_introduction_section(text):
            introduction_citation_target_met = len(distinct_citekeys) >= 15

        return {
            "citation_syntax_passed": not errors,
            "errors": errors,
            "distinct_cited_sources": len(distinct_citekeys),
            "distinct_citekeys": distinct_citekeys,
            "introduction_paragraph_count": introduction_paragraph_count,
            "introduction_citation_target_met": introduction_citation_target_met,
        }

    def _normalize_heading_syntax(self, text: str) -> str:
        lines = text.split("\n")
        normalized_lines: list[str] = []
        heading_pattern = re.compile(r"^(#{1,6})[ \t]*(.+?)\s*$")

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                normalized_lines.append("")
                continue
            match = heading_pattern.match(stripped)
            if match:
                hashes, heading_text = match.groups()
                normalized_lines.append(f"{hashes} {heading_text.strip()}")
                continue
            normalized_lines.append(raw_line.rstrip())

        output: list[str] = []
        for line in normalized_lines:
            if re.match(r"^#{1,6}\s+\S", line):
                while output and output[-1] == "":
                    output.pop()
                if output:
                    output.append("")
                output.append(line)
                output.append("")
                continue
            output.append(line)

        return "\n".join(output)

    def _ensure_heading_structure(self, text: str) -> str:
        lines = text.split("\n")
        body_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped == "# 参考文献":
                break
            body_lines.append(line)

        headings = [line.strip() for line in body_lines if re.match(r"^#{1,6}\s+\S", line.strip())]
        has_h1 = any(line.startswith("# ") for line in headings)
        has_h2 = any(line.startswith("## ") for line in headings)

        if not has_h1:
            first_content_index = next(
                (
                    idx
                    for idx, line in enumerate(lines)
                    if line.strip() and line.strip() != "# 参考文献"
                ),
                None,
            )
            if first_content_index is None:
                raise ValueError("markdown_content must contain academic body text.")
            lines.insert(first_content_index, "# 正文")
            if first_content_index + 1 < len(lines) and lines[first_content_index + 1].strip():
                lines.insert(first_content_index + 1, "")

        if not has_h2:
            insertion_index = next(
                (
                    idx
                    for idx, line in enumerate(lines)
                    if line.strip() and not line.lstrip().startswith("#")
                ),
                None,
            )
            if insertion_index is None:
                raise ValueError("markdown_content must contain paragraph text under the title hierarchy.")
            lines.insert(insertion_index, "## 主体分析")
            if insertion_index + 1 < len(lines) and lines[insertion_index + 1].strip():
                lines.insert(insertion_index + 1, "")

        return "\n".join(lines)

    def _ensure_reference_anchor(self, text: str) -> str:
        text = re.sub(
            r"\n*#\s*参考文献\s*(?:\n\s*\n\s*|\n\s*)<div\s+id=[\"']refs[\"']\s*></div>\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\n*#\s*参考文献\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        ).rstrip()
        return f"{text}\n\n# 参考文献\n\n<div id=\"refs\"></div>\n"

    def _sanitize_markdown_content(self, markdown_content: str) -> str:
        text = markdown_content.replace("\r\n", "\n").replace("\r", "\n")
        text = self._remove_citation_reasoning_block(text)
        text = self._strip_process_leakage(text)
        text = self._normalize_heading_syntax(text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if not text:
            raise ValueError("markdown_content must contain academic body text after removing <Citation_Reasoning>.")

        text = self._ensure_heading_structure(text)
        text = self._normalize_heading_syntax(text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return self._ensure_reference_anchor(text)

    def generate_final_word_document(
        self,
        markdown_content: str,
        output_filename: str = "final_paper.docx",
    ) -> dict:
        if not markdown_content or not markdown_content.strip():
            raise ValueError("markdown_content must not be empty.")
        validation = self.validate_citation_syntax(markdown_content)
        if not validation["citation_syntax_passed"]:
            return {
                "success": False,
                "status": "error",
                "output_path": str((Path(output_filename).expanduser() if output_filename else Path("final_paper.docx")).resolve()),
                "details": "Citation syntax validation failed. Fix suppress-author citations before Word export.",
                **validation,
            }
        markdown_content = self._sanitize_markdown_content(markdown_content)

        output_path = Path(output_filename).expanduser()
        if not output_path.is_absolute():
            output_path = Path.cwd() / output_path
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.lua_filter_path.exists():
            return {
                "success": False,
                "status": "error",
                "output_path": str(output_path),
                "details": f"Missing Lua filter: {self.lua_filter_path}",
            }
        reference_doc_path = self._sync_reference_doc_to_cwd()
        if not reference_doc_path.exists():
            return {
                "success": False,
                "status": "error",
                "output_path": str(output_path),
                "details": (
                    "Missing reference document after template sync. Checked canonical and local "
                    f"template paths; expected synced template at: {reference_doc_path}"
                ),
            }

        temp_root = Path("C:/tmp")
        if not temp_root.exists():
            temp_root = output_path.parent

        with tempfile.TemporaryDirectory(prefix="pandoc-render-", dir=str(temp_root)) as temp_dir:
            temp_md_path = Path(temp_dir) / "temp.md"
            temp_md_path.write_text(markdown_content, encoding="utf-8")

            command = [
                "pandoc",
                str(temp_md_path),
                f"--lua-filter={self.lua_filter_path}",
                "--metadata=zotero_client:zotero",
                f"--reference-doc={reference_doc_path}",
                "-o",
                str(output_path),
            ]

            startupinfo = None
            creationflags = 0
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            try:
                completed = subprocess.run(
                    command,
                    cwd=self.script_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
            except FileNotFoundError as exc:
                return {
                    "success": False,
                    "status": "error",
                    "output_path": str(output_path),
                    "details": f"Pandoc executable not found: {exc}",
                }

        success = completed.returncode == 0 and output_path.exists()
        return {
            "success": success,
            "status": "ok" if success else "error",
            "output_path": str(output_path),
            "reference_doc_used": str(reference_doc_path),
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip() or None,
            "stderr": completed.stderr.strip() or None,
            **validation,
        }
