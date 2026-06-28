from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.document_service import FinalWordDocumentService


def main() -> None:
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="cite-rag-mcp-deleted-cwd-") as temp_dir:
        deleted_cwd = Path(temp_dir) / "gone"
        deleted_cwd.mkdir()
        os.chdir(deleted_cwd)
        deleted_cwd.rmdir()

        service = FinalWordDocumentService()
        runtime_dir = service._safe_runtime_dir()
        output_path = service._resolve_output_path("relative-output.docx")

        assert runtime_dir == service.project_dir
        assert output_path == service.project_dir / "relative-output.docx"

        result = service.generate_final_word_document(
            "Grouped suppress-author citations should fail [-@one; -@two].",
            "relative-output.docx",
        )
        assert result["success"] is False
        assert result["status"] == "error"
        assert result["output_path"] == str(service.project_dir / "relative-output.docx")
        assert "Citation syntax validation failed" in result["details"]

    os.chdir(original_cwd)
    (FinalWordDocumentService().project_dir / "relative-output.docx").unlink(missing_ok=True)
    print("[OK] document service handles deleted runtime cwd")


if __name__ == "__main__":
    main()
