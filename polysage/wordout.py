"""所有给人看的输出都能导成 Word（.docx）：试验方案、本轮报告、分档结果、对话回答、调研报告。

- new_doc(标题, 副标题)：中文字体（正文等线、标题微软雅黑）、页边距、可选横向；
- add_markdown(doc, 文本)：把智能体 / 报告里的 Markdown（标题、列表、表格、加粗、代码）排成 Word；
- add_table(doc, DataFrame 或 行列表)：带表头底色的表格，可指定列宽；
- md_to_docx(文本, 标题) → bytes：页面上“下载 Word”直接用。
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BODY_FONT = "等线"
HEAD_FONT = "微软雅黑"
MONO_FONT = "Consolas"
ACCENT = RGBColor(0x1E, 0x4B, 0x7A)
HEADER_FILL = "DCE6F2"


# ---------------- 文档与样式 ----------------

def _fonts(element_rpr_owner, latin: str, east: str) -> None:
    rpr = element_rpr_owner.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.insert(0, rf)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        rf.attrib.pop(qn(attr), None)
    rf.set(qn("w:ascii"), latin)
    rf.set(qn("w:hAnsi"), latin)
    rf.set(qn("w:eastAsia"), east)


def _style(doc, name: str, latin: str, east: str, size: float | None = None, bold: bool | None = None,
           color: RGBColor | None = None) -> None:
    try:
        st = doc.styles[name]
    except KeyError:
        return
    _fonts(st.element, latin, east)
    if size:
        st.font.size = Pt(size)
    if bold is not None:
        st.font.bold = bold
    if color is not None:
        st.font.color.rgb = color


def new_doc(title: str, subtitle: str = "", landscape: bool = False) -> Any:
    doc = Document()
    sec = doc.sections[0]
    if landscape:
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = sec.page_height, sec.page_width
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Cm(1.8))
    sec.top_margin = sec.bottom_margin = Cm(1.8)
    _style(doc, "Normal", BODY_FONT, BODY_FONT, 10.5)
    doc.styles["Normal"].paragraph_format.space_after = Pt(4)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.25
    _style(doc, "Title", HEAD_FONT, HEAD_FONT, 20, True, ACCENT)
    for lvl, size in ((1, 15), (2, 13), (3, 11.5), (4, 10.5)):
        _style(doc, f"Heading {lvl}", HEAD_FONT, HEAD_FONT, size, True, ACCENT)
    for name in ("List Bullet", "List Bullet 2", "List Number", "Table Grid"):
        _style(doc, name, BODY_FONT, BODY_FONT)
    doc.core_properties.title = title
    doc.core_properties.author = "HDU×恒诺 包装膜配方助手"
    doc.add_paragraph(title, style="Title")
    meta = subtitle or ""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    p = doc.add_paragraph()
    r = p.add_run((meta + "　·　" if meta else "") + f"生成时间 {stamp}")
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    return doc


def usable_width_cm(doc) -> float:
    sec = doc.sections[-1]
    return (sec.page_width - sec.left_margin - sec.right_margin) / 360000


# ---------------- 行内格式 ----------------

_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def add_inline(p, text: str, size: float | None = None, bold: bool = False) -> None:
    """**加粗**、`代码`、[文字](链接) → Word 行内格式。"""
    for part in _INLINE.split(text or ""):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            r = p.add_run(part[2:-2])
            r.bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            r = p.add_run(part[1:-1])
            _fonts(r._element, MONO_FONT, BODY_FONT)
        elif part.startswith("[") and "](" in part:
            label, url = part[1:].split("](", 1)
            r = p.add_run(f"{label}（{url[:-1]}）")
        else:
            r = p.add_run(part)
            r.bold = bold or None
        if size:
            r.font.size = Pt(size)


def para(doc, text: str, *, size: float | None = None, bold: bool = False, color: RGBColor | None = None,
         align: str | None = None, style: str | None = None):
    p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    add_inline(p, text, size=size, bold=bold)
    if color is not None:
        for r in p.runs:
            r.font.color.rgb = color
    if align == "center":
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    return p


def bullets(doc, items: Iterable[str], numbered: bool = False) -> None:
    for it in items:
        para(doc, str(it), style="List Number" if numbered else "List Bullet")


def note(doc, text: str) -> None:
    para(doc, text, size=9, color=RGBColor(0x55, 0x55, 0x55))


# ---------------- 表格 ----------------

def _shade(cell, fill: str) -> None:
    tcpr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _repeat_header(row) -> None:
    trpr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trpr.append(el)


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:                       # NaN
            return ""
        if abs(v) >= 100:
            return f"{v:,.0f}"
        return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:.1f}".rstrip("0").rstrip(".")
    return str(v)


def add_table(doc, data: Any, *, widths_cm: list[float] | None = None, font_size: float = 9.0,
              header_fill: str = HEADER_FILL, empty_rows: int = 0, row_height_cm: float | None = None):
    """data：DataFrame，或 [{列: 值}]，或 (表头, [行])。empty_rows：末尾加几行空白（回填用）。"""
    if hasattr(data, "columns"):
        header = [str(c) for c in data.columns]
        rows = [[_fmt(v) for v in r] for r in data.itertuples(index=False, name=None)]
    elif isinstance(data, tuple):
        header, rows = [str(h) for h in data[0]], [[_fmt(v) for v in r] for r in data[1]]
    else:
        data = list(data)
        header = list(dict.fromkeys(k for r in data for k in r))
        rows = [[_fmt(r.get(h)) for h in header] for r in data]
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        c.text = ""
        add_inline(c.paragraphs[0], h, size=font_size, bold=True)
        _shade(c, header_fill)
    _repeat_header(t.rows[0])
    for r in rows + [[""] * len(header) for _ in range(empty_rows)]:
        cells = t.add_row().cells
        for i, v in enumerate(r[:len(header)]):
            cells[i].text = ""
            add_inline(cells[i].paragraphs[0], v, size=font_size)
    if widths_cm:
        for row in t.rows:
            for i, w in enumerate(widths_cm[:len(header)]):
                row.cells[i].width = Cm(w)
    if row_height_cm:
        for row in t.rows[1:]:
            row.height = Cm(row_height_cm)
    doc.add_paragraph()
    return t


# ---------------- Markdown ----------------

_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def add_markdown(doc, text: str, base_level: int = 1) -> None:
    """常见 Markdown → Word。base_level：# 对应几级标题。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            p = doc.add_paragraph()
            r = p.add_run("\n".join(code))
            _fonts(r._element, MONO_FONT, BODY_FONT)
            r.font.size = Pt(9)
            continue
        if s.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            header = _cells(s)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            add_table(doc, (header, rows))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = min(4, len(m.group(1)) + base_level - 1)
            doc.add_heading(m.group(2).strip().strip("#").strip(), level=lvl)
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            i += 1
            continue
        m = re.match(r"^(\s*)([-*+·•])\s+(.*)$", line)
        if m:
            style = "List Bullet 2" if len(m.group(1)) >= 2 else "List Bullet"
            para(doc, m.group(3), style=style)
            i += 1
            continue
        m = re.match(r"^\s*(\d+)[.、)]\s+(.*)$", line)
        if m:
            para(doc, f"{m.group(1)}. {m.group(2)}")
            i += 1
            continue
        if s.startswith(">"):
            note(doc, s.lstrip("> ").strip())
            i += 1
            continue
        # 普通段落：连续的非空行并成一段
        buf = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(\s*[-*+·•]\s|\s*\d+[.、)]\s|#{1,6}\s|\||```|>)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        para(doc, " ".join(buf) if all(ord(c) < 128 for c in "".join(buf)) else "".join(buf))


# ---------------- 输出 ----------------

def to_bytes(doc) -> bytes:
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


def save(doc, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def md_to_docx(text: str, title: str, subtitle: str = "") -> bytes:
    doc = new_doc(title, subtitle)
    add_markdown(doc, text)
    return to_bytes(doc)


def safe_name(s: str, n: int = 40) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", s or "").strip("_")[:n] or "导出"


def read_tables(source: Any) -> list[list[list[str]]]:
    """读回 Word 里的所有表格（回填用）：[[行[单元格文本]]]。source：路径 / 上传文件 / bytes。"""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    doc = Document(source)
    out = []
    for t in doc.tables:
        out.append([[c.text.strip() for c in row.cells] for row in t.rows])
    return out
