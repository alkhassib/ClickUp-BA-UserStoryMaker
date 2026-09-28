"""Turn an attached User Story document into compact plain text for the LLM."""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import PurePath

SUPPORTED_EXTENSIONS = {"docx", "pdf", "md", "txt"}


class DocumentError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extension_of(filename: str) -> str:
    return PurePath(filename).suffix.lower().lstrip(".")


def parse_document(data: bytes, filename: str) -> str:
    ext = extension_of(filename)
    if ext == "docx":
        text = _parse_docx(data)
    elif ext == "pdf":
        text = _parse_pdf(data)
    elif ext in ("md", "txt"):
        text = data.decode("utf-8-sig", errors="replace")
    else:
        raise DocumentError(f"unsupported file type '.{ext}' (supported: {sorted(SUPPORTED_EXTENSIONS)})")
    text = clean_text(text)
    if not text:
        raise DocumentError(f"no text could be extracted from '{filename}'")
    return text


def _parse_docx(data: bytes) -> str:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    out: list[str] = []
    # Walk the body in order so tables stay next to the text that introduces them.
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name or "").lower() if p.style is not None else ""
            if style.startswith("heading"):
                level = next((int(c) for c in style if c.isdigit()), 1)
                out.append(f"{'#' * min(level, 6)} {text}")
            elif style.startswith("list"):
                out.append(f"- {text}")
            else:
                out.append(text)
        elif tag == "tbl":
            out.extend(_table_rows(Table(child, doc)))
    return "\n".join(out)


def _table_rows(table) -> list[str]:
    """Rows as `| a | b |` lines. Tables nested inside a cell (e.g. field tables) follow their row."""
    rows: list[str] = []
    last_line = None
    for row in table.rows:
        cells: list[str] = []
        nested: list = []
        seen: set[int] = set()
        for cell in row.cells:
            # Horizontally merged cells return the same underlying element; keep one copy.
            if id(cell._tc) in seen:
                continue
            seen.add(id(cell._tc))
            cells.append(" ".join(cell.text.split()))  # direct paragraphs only, nested tables excluded
            nested.extend(cell.tables)
        non_empty = [c for c in cells if c]
        if non_empty:
            # A row whose cells all carry the same text is a full-width label (e.g. a section title).
            line = f"| {non_empty[0]} |" if len(set(non_empty)) == 1 else "| " + " | ".join(cells) + " |"
            if line != last_line:  # vertically merged cells repeat whole rows
                rows.append(line)
                last_line = line
        for inner in nested:
            rows.extend(_table_rows(inner))
    return rows


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


_MULTI_BLANK = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t ]+")


def clean_text(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    # Drop lines that are pure page furniture (page numbers, lone separators).
    lines = [ln for ln in lines if not re.fullmatch(r"(page\s*)?\d+(\s*(of|/)\s*\d+)?|[-_=•·]+", ln, re.I)]
    return _MULTI_BLANK.sub("\n\n", "\n".join(lines)).strip()


def estimate_tokens(text: str) -> int:
    """Rough upper-bound estimate; Arabic tokenizes denser than English, so be conservative."""
    return len(text) // 3 + 1
