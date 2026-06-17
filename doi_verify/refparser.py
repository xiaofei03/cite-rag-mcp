"""Parse Chinese academic reference strings to extract metadata.

Supports common formats:
- Author1, Author2. (Year). Title [J]. Journal, Vol(Issue): Pages.
- Author1, Author2. Year. Title [J]. Journal, Vol(Issue): Pages.
- Author1, Author2. Title [J]. Journal, Year, Vol(Issue): Pages.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedReference:
    """Metadata extracted from a reference string."""
    raw: str
    authors: list[str] = None
    year: Optional[int] = None
    title: Optional[str] = None
    journal: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None

    def __post_init__(self):
        if self.authors is None:
            self.authors = []

    @property
    def first_author(self) -> Optional[str]:
        return self.authors[0] if self.authors else None


def parse_reference(ref: str) -> ParsedReference:
    """Parse a reference string and extract metadata.

    Args:
        ref: Raw reference string, e.g.:
             "徐志向, 罗圣杰. (2026). 关税冲击... [J]. 成都理工大学学报(社会科学版), 34(2): 98-111."

    Returns:
        ParsedReference with extracted fields (may be partial if parsing fails).
    """
    ref = ref.strip()
    if not ref:
        return ParsedReference(raw=ref)

    result = ParsedReference(raw=ref)

    # --- Extract year: (YYYY) or [YYYY] or standalone YYYY ---
    # Chinese format: "Authors. (2026). Title [J]. Journal, 34(2): 98-111."
    year_m = re.search(r"\((\d{4})\)|\[(\d{4})\].|(?<![年份])\b(\d{4})\b(?!\s*年)", ref)
    if year_m:
        y = year_m.group(1) or year_m.group(2) or year_m.group(3)
        if y:
            result.year = int(y)

    # --- Extract title: between year and [J] or Journal start ---
    # Title is often the longest sentence-like part between year and [J]
    title_patterns = [
        # (YYYY). Title [J].   — Chinese format: Authors. (2026). Title [J].
        r"\)\.\s*(.+?)\s*\[J\]\.",
        # Author. Title [J].
        r"\.\s*([^[]+?)\s*\[J\]\.",
    ]
    for pat in title_patterns:
        m = re.search(pat, ref, re.DOTALL)
        if m:
            groups = [g for g in m.groups() if g is not None]
            title = groups[-1].strip() if groups else None
            if title and len(title) > 5:
                result.title = title
                break

    # --- Extract journal: between [J]. and volume/page info ---
    # [J]. Journal Name, 34(2): 98-111.
    journal_m = re.search(r'\[J\]\.\s*([^,\d]+)', ref)
    if journal_m:
        journal = journal_m.group(1).strip()
        journal = re.sub(r'[,.，、;；:：]+$', '', journal)
        if journal and len(journal) > 2:
            result.journal = journal

    # --- Extract volume, issue, pages ---
    # Common: 34(2): 98-111 or 34(2):98-111 or 34(2): 98-111
    vp_m = re.search(r"(\d+)\((\d+)\):\s*(\d+[-–]\d+)", ref)
    if vp_m:
        result.volume = vp_m.group(1)
        result.issue = vp_m.group(2)
        result.pages = vp_m.group(3)

    # --- Extract authors: everything before the first period followed by year ---
    # Chinese format: "徐志向, 罗圣杰. (2026)." or "Zhang, W., Li, Y. (2020)."
    author_m = re.match(r"^([^.]+?)\.\s*\(?\d{4}", ref)
    if not author_m:
        # Fallback: before first ]
        author_m = re.match(r"^([^\]]+)\]", ref)

    if author_m:
        authors_str = author_m.group(1).strip()
        # Split by common separators: comma, Chinese comma, semicolon, &
        parts = re.split(r'[,，;；&和]\s*', authors_str)
        authors = []
        for p in parts:
            p = p.strip()
            # Filter out pure numbers or very short strings
            if p and len(p) > 1 and not re.match(r"^\d{4}$", p):
                authors.append(p)
        if authors:
            result.authors = authors

    return result


def read_input_with_refs(filepath: str) -> list[tuple[str, Optional[str]]]:
    """Read DOIs and optional reference text from an input file.

    Format:
      doi                          # bare DOI
      doi | reference text          # DOI + reference (pipe-separated)
      # comment                     # comment

    Args:
        filepath: Path to input file.

    Returns:
        List of (doi, reference_or_none) tuples.
    """
    results = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Split by pipe: DOI | reference
            if "|" in line:
                parts = line.split("|", 1)
                doi = parts[0].strip()
                ref = parts[1].strip() if len(parts) > 1 else None
            else:
                doi = line
                ref = None

            if doi:
                results.append((doi, ref))

    return results


def format_authors_short(authors: list[str]) -> str:
    """Format authors for display: 'Zhang, Y.; Li, W.' style."""
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]}; {authors[1]}"
    return f"{authors[0]} 等" if authors else ""


if __name__ == "__main__":
    # Test
    refs = [
        "徐志向, 罗圣杰. (2026). 关税冲击下数字化转型对企业组织韧性的影响研究 [J]. 成都理工大学学报(社会科学版), 34(2): 98-111.",
        "Zhang, W., Li, Y. (2020). Deep learning for NLP [J]. Nature, 15(3): 123-145.",
        "Smith, J. A. (2019). Climate change effects on agriculture. Journal of Environmental Science, 28(2): 45-67.",
        "10.16538/j.cnki.fem.20230318.103 | 王浩, 李明. (2023). 数字经济研究 [J]. 经济研究, 45(3): 12-25.",
    ]
    for r in refs:
        p = parse_reference(r)
        print(f"Raw: {r[:80]}")
        print(f"  Authors: {p.authors}")
        print(f"  Year: {p.year}")
        print(f"  Title: {p.title}")
        print(f"  Journal: {p.journal}")
        print(f"  Vol/Issue/Pages: {p.volume}({p.issue}): {p.pages}")
        print()
