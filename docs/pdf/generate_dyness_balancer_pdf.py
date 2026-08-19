#!/usr/bin/env python3
"""Render the Dyness implementation manual from its Markdown source."""

from __future__ import annotations

import html
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs" / "dyness-balancer-implementation.md"
OUTPUT = ROOT / "output" / "pdf" / "dyness-balancer-algorithm-reference-with-toc.pdf"

NAVY = colors.HexColor("#17324D")
BLUE = colors.HexColor("#2563A6")
TEAL = colors.HexColor("#0F766E")
SLATE = colors.HexColor("#475569")
PALE = colors.HexColor("#F1F5F9")
GRID = colors.HexColor("#CBD5E1")
WHITE = colors.white


def make_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=23, leading=28, alignment=TA_CENTER, textColor=NAVY,
            spaceAfter=12,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=base["Normal"], fontName="Helvetica",
            fontSize=11, leading=15, alignment=TA_CENTER, textColor=SLATE,
            spaceAfter=10,
        ),
        "h1": ParagraphStyle(
            "H1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=16, leading=20, textColor=NAVY, spaceBefore=9,
            spaceAfter=7, keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=12.5, leading=16, textColor=BLUE, spaceBefore=8,
            spaceAfter=5, keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "H3", parent=base["Heading3"], fontName="Helvetica-Bold",
            fontSize=10.5, leading=14, textColor=TEAL, spaceBefore=7,
            spaceAfter=4, keepWithNext=True,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["BodyText"], fontName="Helvetica",
            fontSize=8.7, leading=12.1, textColor=colors.HexColor("#1E293B"),
            spaceAfter=5,
        ),
        "bullet": ParagraphStyle(
            "Bullet", parent=base["BodyText"], fontName="Helvetica",
            fontSize=8.6, leading=11.8, leftIndent=12, firstLineIndent=-7,
            textColor=colors.HexColor("#1E293B"), spaceAfter=2.5,
        ),
        "code": ParagraphStyle(
            "Code", parent=base["Code"], fontName="Courier", fontSize=7.6,
            leading=10, leftIndent=8, rightIndent=8, borderColor=GRID,
            borderWidth=0.5, borderPadding=7, backColor=PALE, spaceBefore=3,
            spaceAfter=7,
        ),
        "cell": ParagraphStyle(
            "Cell", parent=base["BodyText"], fontName="Helvetica",
            fontSize=7.5, leading=9.5, textColor=colors.HexColor("#1E293B"),
        ),
        "cell_header": ParagraphStyle(
            "CellHeader", parent=base["BodyText"], fontName="Helvetica-Bold",
            fontSize=7.4, leading=9.3, textColor=WHITE,
        ),
    }


STYLES = make_styles()


def inline(text: str) -> str:
    value = html.escape(text, quote=False)
    value = re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r"<u>\1</u> (\2)", value)
    return value


class ManualDocTemplate(SimpleDocTemplate):
    def afterFlowable(self, flowable) -> None:
        if not isinstance(flowable, Paragraph) or flowable.style.name != "H1":
            return
        title = flowable.getPlainText()
        slug = re.sub("[^a-z0-9]+", "-", title.lower()).strip("-")
        key = f"section-{self.page}-{slug}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(title, key, 0, False)
        self.notify("TOCEntry", (0, title, self.page, key))


def header_footer(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(GRID)
    canvas.line(16 * mm, height - 13 * mm, width - 16 * mm, height - 13 * mm)
    canvas.line(16 * mm, 12 * mm, width - 16 * mm, 12 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(SLATE)
    canvas.drawString(16 * mm, height - 10 * mm, "Dyness PowerBrick PRO Balancer")
    canvas.drawRightString(width - 16 * mm, height - 10 * mm, "Implementation and Algorithms")
    canvas.drawString(16 * mm, 8 * mm, "Cerbo GX / Node-RED / RS485 / Guardian 101")
    canvas.drawRightString(width - 16 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def markdown_table(lines: list[str]) -> Table:
    rows: list[list[Paragraph]] = []
    for line in lines:
        cells = [item.strip() for item in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        style = "cell_header" if not rows else "cell"
        rows.append([Paragraph(inline(cell), STYLES[style]) for cell in cells])
    count = max(1, len(rows[0]))
    if count == 2:
        widths = [48 * mm, 130 * mm]
    elif count == 3:
        widths = [29 * mm, 61 * mm, 88 * mm]
    elif count == 4:
        widths = [35 * mm, 38 * mm, 47 * mm, 58 * mm]
    else:
        widths = [178 * mm / count] * count
    result = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.35, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PALE]),
    ]))
    return result


def parse_markdown(text: str) -> tuple[str, str, list]:
    lines = text.splitlines()
    title = lines[0].removeprefix("# ").strip()
    subtitle = lines[2].strip() if len(lines) > 2 else "Engineering reference"
    story: list = []
    index = 3
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            joined = " ".join(part.strip() for part in paragraph)
            story.append(Paragraph(inline(joined), STYLES["body"]))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            flush()
            index += 1
            continue
        if line.startswith("```"):
            flush()
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            story.append(Preformatted("\n".join(block), STYLES["code"], maxLineLength=100))
            index += 1
            continue
        if line.startswith("|") and index + 1 < len(lines) and lines[index + 1].startswith("|"):
            flush()
            table_lines = []
            while index < len(lines) and lines[index].startswith("|"):
                table_lines.append(lines[index])
                index += 1
            story.extend([markdown_table(table_lines), Spacer(1, 6)])
            continue
        if line.startswith("## "):
            flush()
            story.append(Paragraph(inline(line[3:]), STYLES["h1"]))
            index += 1
            continue
        if line.startswith("### "):
            flush()
            story.append(Paragraph(inline(line[4:]), STYLES["h2"]))
            index += 1
            continue
        if line.startswith("#### "):
            flush()
            story.append(Paragraph(inline(line[5:]), STYLES["h3"]))
            index += 1
            continue
        if line.startswith("- "):
            flush()
            story.append(Paragraph("&bull; " + inline(line[2:]), STYLES["bullet"]))
            index += 1
            continue
        match = re.match(r"(\d+)\.\s+(.*)", line)
        if match:
            flush()
            story.append(Paragraph(f"{match.group(1)}. " + inline(match.group(2)), STYLES["bullet"]))
            index += 1
            continue
        paragraph.append(line)
        index += 1
    flush()
    return title, subtitle, story


def build() -> Path:
    title, subtitle, body = parse_markdown(SOURCE.read_text(encoding="utf-8"))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = ManualDocTemplate(
        str(OUTPUT), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm, title=title,
        author="Cerbo GX Dyness Balancer project",
        subject="Complete implementation and algorithm reference",
    )
    toc = TableOfContents()
    toc.dotsMinLevel = 0
    toc.levelStyles = [ParagraphStyle(
        "TOC1", fontName="Helvetica", fontSize=9, leading=12,
        leftIndent=4, firstLineIndent=-4, textColor=NAVY,
    )]
    cover = [
        Spacer(1, 38 * mm),
        Paragraph(inline(title), STYLES["title"]),
        Paragraph(inline(subtitle), STYLES["subtitle"]),
        Spacer(1, 9 * mm),
        Paragraph(
            "Current deployed architecture: telemetry worker, Node-RED controller, "
            "and persistent guardian DeviceInstance 101.", STYLES["subtitle"],
        ),
        Spacer(1, 16 * mm),
        markdown_table([
            "| Item | Value |",
            "| --- | --- |",
            "| Hardware profile | Dyness PowerBrick PRO DIP 00110 |",
            "| Serial protocol | Dyness/Pylon ASCII, 115200 8N1, read-only |",
            "| Safety fallback | 54.0 V CVL / 20.0 A CCL / 100.0 A DCL |",
            "| Document status | Implementation reference dated 2026-08-19 |",
        ]),
        PageBreak(),
        Paragraph("Contents", STYLES["h1"]),
        Spacer(1, 4),
        toc,
        PageBreak(),
    ]
    doc.multiBuild(cover + body, onFirstPage=header_footer, onLaterPages=header_footer)
    return OUTPUT


if __name__ == "__main__":
    print(build())
