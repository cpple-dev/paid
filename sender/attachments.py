#!/usr/bin/env python3
"""In-memory PDF / XLSX attachment generation (no disk writes).

PDF pages are filled with words from words.txt and padded with comment lines
just before the trailer, so the byte count is exact and the file still ends
with `%%EOF`.
XLSX rows are filled with words and the final bytes are an incompressible
stored part (`pad.bin`) declared in [Content_Types].xml, so every build hits
the exact requested byte size without breaking Excel's package rules.
"""
from __future__ import annotations

import io
import os
import random
import string
import zipfile
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen.canvas import Canvas

WORDS_FILE = os.environ.get(
    "WORDS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "words.txt"))

_WORDS = None


def load_words():
    global _WORDS
    if _WORDS is None:
        try:
            with open(WORDS_FILE, "r", encoding="utf-8",
                      errors="replace") as fh:
                _WORDS = [w.strip() for w in fh if w.strip()]
        except OSError:
            _WORDS = []
    return _WORDS


# ------------------------------------------------------------------- pdf
def _pdf_content_chunks(canvas_obj, n_chunks, words, y_state):
    width, height = letter
    x_margin = y_margin = 1 * inch
    line_height = 14
    chunk_words = 100
    for _ in range(n_chunks):
        line = " ".join(random.choices(words, k=chunk_words))[:70]
        if y_state[0] < y_margin:
            canvas_obj.showPage()
            y_state[0] = height - y_margin
        canvas_obj.drawString(x_margin, y_state[0], line)
        y_state[0] -= line_height


def _pad_pdf(data, target_bytes):
    """Grow the file to `target_bytes` with comment lines placed *before*
    `startxref`, so the last bytes of the file stay `%%EOF` (a reader that
    only looks at the tail still finds the trailer).  The xref table sits
    earlier, so its offsets stay valid."""
    need = target_bytes - len(data)
    if need <= 0:
        return data
    idx = data.rfind(b"startxref")
    if idx < 0:                                    # unexpected layout
        return data + os.urandom(need)
    chars = string.ascii_letters + string.digits
    line_len = 73                                  # '%' + 71 chars + '\n'
    n_lines, rest = divmod(need, line_len)
    if rest == 1:                                  # '%' + '\n' is 2 bytes
        n_lines, rest = n_lines - 1, rest + line_len
    lines = [b"%" + "".join(random.choices(chars, k=line_len - 2)).encode()
             + b"\n" for _ in range(n_lines)]
    if rest:
        lines.append(b"%" + "".join(random.choices(chars, k=rest - 2)).encode()
                     + b"\n")
    pad = b"".join(lines)
    return data[:idx] + pad + data[idx:]


def generate_pdf_bytes(target_bytes):
    words = load_words()
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 12)
    y_state = [letter[1] - 1 * inch]
    if words:
        _pdf_content_chunks(c, max(target_bytes // 1000, 1), words, y_state)
    else:
        width, height = letter
        x_margin = y_margin = 1 * inch
        y = y_state[0]
        for _ in range(max(target_bytes // 1024, 1)):
            chunk = "".join(random.choices(
                string.ascii_letters + string.digits + string.punctuation
                + " \n", k=200))
            for line in chunk.split("\n"):
                line = line[:70]
                if y < y_margin:
                    c.showPage()
                    y = height - y_margin
                c.drawString(x_margin, y, line)
                y -= 14
            y_state[0] = y
    c.save()
    return _pad_pdf(buf.getvalue(), target_bytes)


# ------------------------------------------------------------------ xlsx
_XLSX_PARTS = [
    ("[Content_Types].xml",
     '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
     '<Default Extension="xml" ContentType="application/xml"/>'
     '<Default Extension="bin" ContentType="application/octet-stream"/>'
     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
     '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
     "</Types>"),
    ("_rels/.rels",
     '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
     "</Relationships>"),
    ("xl/workbook.xml",
     '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
     '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
     'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
     "<sheets><sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>"),
    ("xl/_rels/workbook.xml.rels",
     '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
     "</Relationships>"),
    ("xl/worksheets/sheet1.xml",
     '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
     '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
     "<sheetData>"
     "<row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>generated attachment</t></is></c></row>"
     "</sheetData></worksheet>"),
]

_SHEET_MAX_ROWS = 1000000


def _sheet_xml(n_rows):
    words = load_words()
    rows = ['<row r="1"><c r="A1" t="inlineStr"><is>'
            "<t>generated attachment</t></is></c></row>"]
    n_rows = max(min(n_rows, _SHEET_MAX_ROWS), 0)
    if words:
        picked = random.choices(words, k=12 * n_rows)
        for r in range(2, n_rows + 2):
            line = " ".join(picked[(r - 2) * 12:(r - 1) * 12])[:70]
            rows.append(f'<row r="{r}"><c r="A{r}" t="inlineStr"><is>'
                        f"<t>{escape(line)}</t></is></c></row>")
    else:
        for r in range(2, n_rows + 2):
            line = "".join(random.choices(string.ascii_lowercase, k=50))
            rows.append(f'<row r="{r}"><c r="A{r}" t="inlineStr"><is>'
                        f"<t>{line}</t></is></c></row>")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<cols><col min="1" max="1" width="80" customWidth="1"/></cols>'
            "<sheetData>" + "".join(rows) + "</sheetData></worksheet>")


def _build_xlsx(n_rows, pad_bytes=0, sheet=None):
    """Build the package.  `sheet` is passed in when the same content is
    measured and then rebuilt - otherwise every build would randomize the
    rows and the compressed size would drift."""
    if sheet is None:
        sheet = _sheet_xml(n_rows)
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for name, xml in _XLSX_PARTS:
            if name == "xl/worksheets/sheet1.xml":
                xml = sheet
            z.writestr(name, xml)
        if pad_bytes > 0:
            # stored (not deflated) -> exactly pad_bytes output bytes
            z.writestr("pad.bin", os.urandom(pad_bytes),
                       compress_type=zipfile.ZIP_STORED)
    return bio.getvalue()


def generate_xlsx_bytes(target_bytes):
    base = _build_xlsx(0, 0)
    if len(base) >= target_bytes:
        return base

    base_len = len(base)
    probe_rows = 2000
    per_row = (len(_build_xlsx(probe_rows, 0)) - base_len) / probe_rows
    if per_row <= 0:
        per_row = 1.0
    rows = min(max(int((target_bytes - base_len) / per_row * 0.98), 1),
               _SHEET_MAX_ROWS)
    sheet = _sheet_xml(rows)
    data = _build_xlsx(rows, 0, sheet)

    # word rows compress, so the first estimate can overshoot; shrink until
    # the remaining gap is small enough for stored padding
    for _ in range(4):
        if len(data) <= target_bytes or rows == 0:
            break
        excess = len(data) - target_bytes
        rows = max(rows - max(int(excess / per_row), 1) - 1, 0)
        sheet = _sheet_xml(rows)
        data = _build_xlsx(rows, 0, sheet)
    if len(data) > target_bytes:
        return data

    # adding pad.bin costs a fixed local+central header; measure it once
    # against the SAME sheet bytes, then store exactly the remaining gap
    probe = _build_xlsx(rows, 1, sheet)
    overhead = len(probe) - len(data) - 1
    need = target_bytes - len(data) - overhead
    if need <= 0:
        return data
    data = _build_xlsx(rows, need, sheet)
    if len(data) != target_bytes:                 # header size drifted
        need += target_bytes - len(data)
        if need > 0:
            data = _build_xlsx(rows, need, sheet)
    return data


def build_attachment(ext, target_bytes):
    if ext == "pdf":
        return generate_pdf_bytes(target_bytes)
    if ext == "xlsx":
        return generate_xlsx_bytes(target_bytes)
    # Never hand out bytes under an extension they are not: a .docx that is
    # really an xlsx zip would not open.  filenames.attachment_name() refuses
    # the same extensions, so the two stay in step.
    raise ValueError(f"no generator for attachment type {ext!r}")
