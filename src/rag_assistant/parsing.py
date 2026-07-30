"""Document parsing → Markdown.

Markdown is the verified intermediate format: structure (headers, tables)
survives into chunking. PDF parsing uses Docling when installed
(`.[parsing]` extra — it downloads layout models on first run); Markdown and
plain text pass through. The worker never guesses at unknown formats.
"""

from __future__ import annotations


class UnsupportedFormatError(ValueError):
    pass


def parse_to_markdown(data: bytes, fmt: str) -> str:
    fmt = fmt.lower().lstrip(".")
    if fmt in ("md", "markdown", "txt"):
        return data.decode("utf-8")
    if fmt == "pdf":
        try:
            from docling.document_converter import DocumentConverter  # heavy: lazy import
        except ImportError as exc:
            raise UnsupportedFormatError(
                "PDF parsing requires the 'parsing' extra: uv sync --extra parsing"
            ) from exc
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(data)
            tmp.flush()
            result = DocumentConverter().convert(tmp.name)
        return result.document.export_to_markdown()
    raise UnsupportedFormatError(f"unsupported format: {fmt} (md, txt, pdf)")
