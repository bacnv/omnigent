"""Convert XLSX workbooks to bounded plain text for model attachments."""

from __future__ import annotations

import posixpath
import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_XLSX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_XLSX_XML_BYTES = 50 * 1024 * 1024
MAX_XLSX_TEXT_CHARS = 2_000_000

_XML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF_RE = re.compile(r"([A-Z]+)")


def is_xlsx_filename(filename: str | None) -> bool:
    """Return whether *filename* has an XLSX extension."""
    return bool(filename and filename.lower().endswith(".xlsx"))


def is_xlsx_attachment(content_type: str, filename: str | None) -> bool:
    """Return whether MIME type or filename identifies an XLSX workbook."""
    return content_type.split(";", 1)[0].lower() == XLSX_MIME or is_xlsx_filename(filename)


def xlsx_text_filename(filename: str | None) -> str:
    """Return a text filename for workbook content converted for a model."""
    return f"{filename or 'attachment.xlsx'}.txt"


def xlsx_to_text(content: bytes) -> bytes:
    """Return workbook cells as UTF-8 TSV sections, bounded for model context."""
    try:
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            _validate_archive(workbook)
            shared = _shared_strings(workbook)
            sheets = _sheet_paths(workbook)
            parts: list[str] = []
            chars = 0
            for name, path in sheets:
                lines = _sheet_lines(workbook.read(path), shared)
                section = f"Sheet: {name}\n" + "\n".join(lines)
                if chars + len(section) > MAX_XLSX_TEXT_CHARS:
                    section = section[: MAX_XLSX_TEXT_CHARS - chars]
                parts.append(section)
                chars += len(section)
                if chars >= MAX_XLSX_TEXT_CHARS:
                    break
    except (KeyError, OSError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
        raise ValueError("Invalid XLSX workbook") from exc
    return "\n\n".join(parts).encode("utf-8")


def _validate_archive(workbook: zipfile.ZipFile) -> None:
    total = 0
    for info in workbook.infolist():
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_XLSX_XML_BYTES:
            raise ValueError("XLSX workbook expands beyond the supported limit")
        if info.flag_bits & 0x1:
            raise ValueError("Encrypted XLSX workbooks are not supported")


def _xml(content: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("XLSX workbook contains unsupported XML declarations")
    return ElementTree.fromstring(content)


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    try:
        root = _xml(workbook.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(text.text or "" for text in item.iter(f"{_XML_NS}t")) for item in root]


def _sheet_paths(workbook: zipfile.ZipFile) -> list[tuple[str, str]]:
    root = _xml(workbook.read("xl/workbook.xml"))
    rels = _xml(workbook.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.iter(f"{_PACKAGE_REL_NS}Relationship")
        if "Id" in rel.attrib and "Target" in rel.attrib
    }
    sheets: list[tuple[str, str]] = []
    for sheet in root.iter(f"{_XML_NS}sheet"):
        target = targets.get(sheet.attrib.get(f"{_REL_NS}id", ""))
        if not target:
            continue
        path = posixpath.normpath(posixpath.join("xl", target.lstrip("/")))
        if target.startswith("/xl/"):
            path = target.lstrip("/")
        if not path.startswith("xl/") or path.startswith("xl/../"):
            raise ValueError("XLSX workbook contains an invalid worksheet path")
        sheets.append((sheet.attrib.get("name", "Sheet"), path))
    return sheets


def _sheet_lines(content: bytes, shared: list[str]) -> list[str]:
    root = _xml(content)
    lines: list[str] = []
    for row in root.iter(f"{_XML_NS}row"):
        values: list[str] = []
        column = 0
        for cell in row.findall(f"{_XML_NS}c"):
            match = _CELL_REF_RE.match(cell.attrib.get("r", ""))
            target_column = _column_index(match.group(1)) if match else column
            values.extend([""] * max(0, target_column - column))
            values.append(_cell_value(cell, shared))
            column = target_column + 1
        lines.append("\t".join(values).rstrip())
    return lines


def _column_index(label: str) -> int:
    value = 0
    for char in label:
        value = value * 26 + ord(char) - ord("A") + 1
    return value - 1


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{_XML_NS}t"))
    value = cell.findtext(f"{_XML_NS}v", default="")
    if cell_type == "s" and value:
        try:
            return shared[int(value)]
        except (IndexError, ValueError):
            return ""
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value
