from __future__ import annotations

import base64
import zipfile
from dataclasses import dataclass
from io import BytesIO

import pytest

from omnigent.entities import ConversationItem, MessageData, StoredFile
from omnigent.runtime.content_resolver import extract_text_attachments, resolve_content_references
from omnigent.runtime.xlsx import XLSX_MIME, xlsx_to_text


@dataclass
class _Files:
    file: StoredFile

    def get(self, file_id: str) -> StoredFile | None:
        return self.file if file_id == self.file.id else None


@dataclass
class _Artifacts:
    content: bytes

    def get(self, file_id: str) -> bytes:
        return self.content


def _item() -> ConversationItem:
    return ConversationItem(
        id="msg_1",
        type="message",
        status="completed",
        response_id="resp_1",
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_file", "file_id": "file_xlsx", "filename": "sales.xlsx"}],
        ),
    )


def _stores() -> tuple[_Files, _Artifacts]:
    content = workbook_bytes()
    return (
        _Files(
            StoredFile(
                id="file_xlsx",
                filename="sales.xlsx",
                bytes=len(content),
                content_type=XLSX_MIME,
                created_at=1,
            )
        ),
        _Artifacts(content),
    )


def workbook_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as workbook:
        workbook.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                <sheets><sheet name="Sales" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        )
        workbook.writestr(
            "xl/sharedStrings.xml",
            """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <si><t>Item</t></si><si><t>Revenue</t></si><si><t>Widget</t></si>
            </sst>""",
        )
        workbook.writestr(
            "xl/worksheets/sheet1.xml",
            """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                <sheetData>
                  <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
                  <row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>42</v></c></row>
                </sheetData>
            </worksheet>""",
        )
    return output.getvalue()


def test_xlsx_to_text_preserves_sheet_and_cells() -> None:
    assert xlsx_to_text(workbook_bytes()).decode() == "Sheet: Sales\nItem\tRevenue\nWidget\t42"


def test_xlsx_to_text_rejects_invalid_archive() -> None:
    with pytest.raises(ValueError, match="Invalid XLSX workbook"):
        xlsx_to_text(b"not a workbook")


def test_xlsx_to_text_rejects_expanded_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.runtime.xlsx.MAX_XLSX_XML_BYTES", 1)
    with pytest.raises(ValueError, match="expands beyond the supported limit"):
        xlsx_to_text(workbook_bytes())


def test_xlsx_to_text_rejects_xml_declarations() -> None:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as workbook:
        workbook.writestr(
            "xl/workbook.xml",
            b'<!DOCTYPE workbook [<!ENTITY x "value">]><workbook>&x;</workbook>',
        )
        workbook.writestr("xl/_rels/workbook.xml.rels", b"<Relationships/>")

    with pytest.raises(ValueError, match="unsupported XML declarations"):
        xlsx_to_text(output.getvalue())


def test_content_resolver_sends_xlsx_as_text() -> None:
    files, artifacts = _stores()
    [resolved] = resolve_content_references(
        [_item()],
        files,  # type: ignore[arg-type]
        artifacts,  # type: ignore[arg-type]
    )
    assert isinstance(resolved.data, MessageData)
    block = resolved.data.content[0]
    assert block["filename"] == "sales.xlsx.txt"
    data_uri = block["file_data"]
    assert data_uri.startswith("data:text/plain;base64,")
    assert base64.b64decode(data_uri.split(",", 1)[1]).decode() == (
        "Sheet: Sales\nItem\tRevenue\nWidget\t42"
    )


def test_policy_scans_xlsx_text() -> None:
    files, artifacts = _stores()
    result = extract_text_attachments(
        [{"type": "input_file", "file_id": "file_xlsx"}],
        files,  # type: ignore[arg-type]
        artifacts,  # type: ignore[arg-type]
    )
    assert result == [
        {
            "filename": "sales.xlsx",
            "content_type": "text/tab-separated-values",
            "text": "Sheet: Sales\nItem\tRevenue\nWidget\t42",
        }
    ]
