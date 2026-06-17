from __future__ import annotations

import httpx
import re
import urllib.parse
import urllib.request

from models import CitekeyResult

LOCAL_BASE_URL = "http://127.0.0.1:23119"
API_HEADERS = {"Zotero-API-Version": "3"}


class CitekeyExtractionError(RuntimeError):
    """Raised when Better BibTeX citekey extraction fails."""


class CitekeyService:
    def __init__(self, base_url: str = LOCAL_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    async def export_item_bibtex_async(self, item_key: str) -> str:
        query = {"itemKey": item_key, "format": "bibtex", "limit": 100}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{self.base_url}/api/users/0/items", params=query, headers=API_HEADERS)
            response.raise_for_status()
            return response.text

    def export_item_bibtex(self, item_key: str) -> str:
        query = urllib.parse.urlencode({"itemKey": item_key, "format": "bibtex", "limit": 100})
        url = f"{self.base_url}/api/users/0/items?{query}"
        request = urllib.request.Request(url, headers=API_HEADERS, method="GET")
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.read().decode("utf-8", errors="replace")

    def extract_citekey(self, bibtex_text: str) -> str:
        matches = re.findall(r"@\w+\s*\{\s*([^,\s]+)", bibtex_text)
        if not matches:
            raise CitekeyExtractionError(
                "Could not extract a Better BibTeX citekey from exported BibTeX."
            )
        return matches[0]

    def export_item_citekey(
        self,
        item_key: str,
        *,
        doi: str | None = None,
        title: str | None = None,
    ) -> CitekeyResult:
        try:
            bibtex_text = self.export_item_bibtex(item_key)
            citekey = self.extract_citekey(bibtex_text)
            return CitekeyResult(
                doi=doi,
                title=title,
                item_key=item_key,
                citekey=citekey,
                citation_anchor=f"[@{citekey}]",
                status="ok",
                details="Better BibTeX export succeeded.",
            )
        except Exception as exc:
            return CitekeyResult(
                doi=doi,
                title=title,
                item_key=item_key,
                status="failed",
                details=str(exc),
            )

    async def export_item_citekey_async(
        self,
        item_key: str,
        *,
        doi: str | None = None,
        title: str | None = None,
    ) -> CitekeyResult:
        try:
            bibtex_text = await self.export_item_bibtex_async(item_key)
            citekey = self.extract_citekey(bibtex_text)
            return CitekeyResult(
                doi=doi,
                title=title,
                item_key=item_key,
                citekey=citekey,
                citation_anchor=f"[@{citekey}]",
                status="ok",
                details="Better BibTeX export succeeded.",
            )
        except Exception as exc:
            return CitekeyResult(
                doi=doi,
                title=title,
                item_key=item_key,
                status="failed",
                details=str(exc),
            )
