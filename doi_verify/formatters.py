"""Citation formatters — APA 7th, GB/T 7714-2015, MLA 9th.

Each formatter takes a unified reference dict and returns a formatted string.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ReferenceData:
    """Unified reference data from CrossRef or OpenAlex."""
    authors: list[dict] = field(default_factory=list)  # [{family, given}]
    title: str = ""
    year: Optional[int] = None
    journal: Optional[str] = None
    volume: Optional[str] = None
    issue: Optional[str] = None
    pages: Optional[str] = None
    doi: Optional[str] = None
    publisher: Optional[str] = None

    @classmethod
    def from_crossref(cls, cr) -> "ReferenceData":
        """Build from CrossRefResult."""
        return cls(
            authors=cr.authors,
            title=cr.title or "",
            year=cr.year,
            journal=cr.journal,
            volume=cr.volume,
            issue=cr.issue,
            pages=cr.pages,
            doi=cr.doi,
            publisher=cr.publisher,
        )

    @classmethod
    def from_openalex(cls, oa) -> "ReferenceData":
        """Build from OpenAlexResult."""
        return cls(
            authors=oa.authors,
            title=oa.title or "",
            year=oa.year,
            journal=oa.journal,
            volume=oa.volume,
            issue=oa.issue,
            pages=oa.pages,
            doi=oa.doi,
        )

    @classmethod
    def from_cnki(cls, cnki) -> "ReferenceData":
        """Build from CNKIResult (Chinese database fallback)."""
        return cls(
            authors=cnki.authors or [],
            title=cnki.title or "",
            year=cnki.year,
            journal=cnki.journal or "",
            doi=cnki.doi,
        )

    def merge(self, other: "ReferenceData") -> "ReferenceData":
        """Merge two ReferenceData, preferring non-empty values from self."""
        return ReferenceData(
            authors=self.authors or other.authors,
            title=self.title or other.title,
            year=self.year or other.year,
            journal=self.journal or other.journal,
            volume=self.volume or other.volume,
            issue=self.issue or other.issue,
            pages=self.pages or other.pages,
            doi=self.doi or other.doi,
            publisher=self.publisher or other.publisher,
        )


# ──── APA 7th Edition ────

def format_apa(ref: ReferenceData) -> str:
    """Format as APA 7th edition journal article.

    Example:
        Charness, G., & Rabin, M. (2002). Understanding social preferences...
        Quarterly Journal of Economics, 117(3), 817–869. https://doi.org/10.xxx
    """
    parts = []

    # Authors
    if ref.authors:
        author_strs = []
        for a in ref.authors:
            family = a.get("family", "")
            given = a.get("given", "")
            initials = " ".join(p[0].upper() + "." for p in given.split() if p)
            author_strs.append(f"{family}, {initials}" if initials else family)
        if len(author_strs) == 1:
            parts.append(author_strs[0])
        elif len(author_strs) == 2:
            parts.append(f"{author_strs[0]}, & {author_strs[1]}")
        elif len(author_strs) <= 20:
            parts.append(", ".join(author_strs[:-1]) + f", & {author_strs[-1]}")
        else:
            parts.append(", ".join(author_strs[:19]) + ", ... " + author_strs[-1])
    else:
        parts.append("[No author]")

    # Year
    year_str = f"({ref.year})." if ref.year else "(n.d.)."
    parts.append(year_str)

    # Title (sentence case, no quotes)
    title = _sentence_case(ref.title) if ref.title else "[No title]"
    parts.append(f"{title}.")

    # Journal (italic in real output)
    if ref.journal:
        journal_part = _title_case(ref.journal)
        if ref.volume:
            journal_part += f", {ref.volume}"
            if ref.issue:
                journal_part += f"({ref.issue})"
        if ref.pages:
            journal_part += f", {ref.pages.replace('-', '–')}"
        parts.append(f"{journal_part}.")
    elif ref.publisher:
        parts.append(f"{ref.publisher}.")

    # DOI
    if ref.doi:
        parts.append(f"https://doi.org/{ref.doi}")

    return " ".join(parts)


# ──── GB/T 7714-2015 ────

def format_gbt(ref: ReferenceData) -> str:
    """Format as GB/T 7714-2015 (Chinese national standard).

    Example:
        CHARNESS G, RABIN M. Understanding social preferences with simple
        tests[J]. Quarterly Journal of Economics, 2002, 117(3): 817-869.
    """
    parts = []

    # Authors (ALL CAPS, given initials)
    if ref.authors:
        author_strs = []
        for a in ref.authors:
            family = a.get("family", "").upper()
            given = a.get("given", "")
            initials = " ".join(p[0].upper() for p in given.split() if p)
            name = f"{family} {initials}" if initials else family
            author_strs.append(name.strip())
        if len(author_strs) <= 3:
            parts.append(", ".join(author_strs))
        else:
            parts.append(", ".join(author_strs[:3]) + ", et al")
    else:
        parts.append("[NO AUTHOR]")

    # Title
    title = ref.title or "[No title]"
    parts.append(f"{title}[J].")

    # Journal
    if ref.journal:
        journal_str = ref.journal
        parts.append(journal_str + ",")

    # Year
    if ref.year:
        parts.append(str(ref.year) + ",")

    # Volume / Issue
    vol_issue = ""
    if ref.volume:
        vol_issue += ref.volume
    if ref.issue:
        vol_issue += f"({ref.issue})"
    if vol_issue:
        parts.append(vol_issue + ":" if ref.pages else vol_issue + ".")

    # Pages
    if ref.pages:
        parts.append(ref.pages + ".")

    return " ".join(parts)


# ──── MLA 9th Edition ────

def format_mla(ref: ReferenceData) -> str:
    """Format as MLA 9th edition.

    Example:
        Charness, Gary, and Matthew Rabin. "Understanding Social Preferences..."
        Quarterly Journal of Economics, vol. 117, no. 3, 2002, pp. 817-869.
    """
    parts = []

    # Authors (full given names, first author inverted)
    if ref.authors:
        if len(ref.authors) == 1:
            a = ref.authors[0]
            parts.append(f'{a["family"]}, {a["given"]}.')
        elif len(ref.authors) == 2:
            a0, a1 = ref.authors
            parts.append(f'{a0["family"]}, {a0["given"]}, and {a1["given"]} {a1["family"]}.')
        else:
            a0 = ref.authors[0]
            others = ", ".join(f'{a["given"]} {a["family"]}' for a in ref.authors[1:])
            parts.append(f'{a0["family"]}, {a0["given"]}, et al.')
    else:
        parts.append('[No author].')

    # Title (in quotes)
    title = ref.title or "[No title]"
    parts.append(f'"{title}."')

    # Journal (italic)
    if ref.journal:
        parts.append(f"{ref.journal},")

    # Volume / Issue
    if ref.volume:
        parts.append(f"vol. {ref.volume},")
    if ref.issue:
        parts.append(f"no. {ref.issue},")

    # Year
    if ref.year:
        parts.append(f"{ref.year},")

    # Pages
    if ref.pages:
        parts.append(f"pp. {ref.pages.replace('-', '–')}.")

    return " ".join(parts)


# ──── Helpers ────

def _sentence_case(text: str) -> str:
    """Convert title to APA-style sentence case (capitalize first word + proper nouns)."""
    if not text:
        return text
    # Lowercase everything, then capitalize first char
    result = text[0].upper() + text[1:]
    # Words after colon/dash get capitalized
    for punct in (":", "–", "—", "?", "!"):
        idx = result.find(f"{punct} ")
        while idx != -1 and idx + 2 < len(result):
            result = result[:idx + 2] + result[idx + 2].upper() + result[idx + 3:]
            idx = result.find(f"{punct} ", idx + 2)
    return result


def _title_case(text: str) -> str:
    """Convert journal name to title case."""
    minors = {"a", "an", "the", "and", "but", "or", "for", "nor", "on", "at",
              "to", "by", "in", "of", "with"}
    words = text.split()
    result = []
    for i, w in enumerate(words):
        if i == 0 or i == len(words) - 1 or w.lower() not in minors:
            result.append(w[0].upper() + w[1:] if len(w) > 1 else w.upper())
        else:
            result.append(w.lower())
    return " ".join(result)


# ──── Format registry ────

FORMATTERS = {
    "apa": ("APA 7th", format_apa),
    "gbt": ("GB/T 7714-2015", format_gbt),
    "mla": ("MLA 9th", format_mla),
}


def format_citation(ref: ReferenceData, fmt: str = "apa") -> str:
    """Format a reference with the specified citation style.

    Args:
        ref: Unified ReferenceData
        fmt: One of 'apa', 'gbt', 'mla'

    Returns:
        Formatted citation string.
    """
    if fmt not in FORMATTERS:
        raise ValueError(f"Unknown format '{fmt}'. Options: {list(FORMATTERS)}")
    return FORMATTERS[fmt][1](ref)


def get_format_label(fmt: str) -> str:
    """Get the human-readable label for a format."""
    return FORMATTERS.get(fmt, (fmt,))[0]
