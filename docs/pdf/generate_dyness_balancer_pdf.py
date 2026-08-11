#!/usr/bin/env python3
"""Generate the Dyness Balancer engineering algorithm reference PDF.

The document intentionally mirrors the deployed controller and RS485 service.
It uses only ReportLab vector primitives so UML-style diagrams remain sharp at
any zoom level.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents


ROOT = Path(__file__).absolute().parents[2]
OUTPUT = ROOT / "output" / "pdf" / "dyness-balancer-algorithm-reference-with-toc.pdf"

NAVY = colors.HexColor("#17324D")
BLUE = colors.HexColor("#2563A6")
TEAL = colors.HexColor("#0F766E")
GREEN = colors.HexColor("#2F855A")
AMBER = colors.HexColor("#B7791F")
RED = colors.HexColor("#B83232")
SLATE = colors.HexColor("#475569")
PALE_BLUE = colors.HexColor("#E8F1FA")
PALE_GREEN = colors.HexColor("#E7F5ED")
PALE_AMBER = colors.HexColor("#FFF6DB")
PALE_RED = colors.HexColor("#FCEBEC")
GRID = colors.HexColor("#CBD5E1")


def styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "Title", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=24, leading=29, textColor=NAVY, spaceAfter=10,
        ),
        "subtitle": ParagraphStyle(
            "Subtitle", parent=base["Normal"], fontName="Helvetica",
            fontSize=11, leading=15, textColor=SLATE, spaceAfter=14,
        ),
        "h1": ParagraphStyle(
            "H1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=17, leading=21, textColor=NAVY, spaceBefore=10, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=12.5, leading=16, textColor=BLUE, spaceBefore=9, spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["BodyText"], fontName="Helvetica",
            fontSize=8.8, leading=12.2, textColor=colors.HexColor("#1E293B"),
            spaceAfter=5,
        ),
        "small": ParagraphStyle(
            "Small", parent=base["BodyText"], fontName="Helvetica",
            fontSize=7.4, leading=9.5, textColor=SLATE, spaceAfter=3,
        ),
        "code": ParagraphStyle(
            "Code", parent=base["BodyText"], fontName="Courier",
            fontSize=7.3, leading=9.4, textColor=colors.HexColor("#1E293B"),
            backColor=colors.HexColor("#F1F5F9"), borderPadding=4, spaceAfter=5,
        ),
        "cell": ParagraphStyle(
            "Cell", parent=base["BodyText"], fontName="Helvetica",
            fontSize=7.1, leading=8.6, textColor=colors.HexColor("#1E293B"),
        ),
        "cell_bold": ParagraphStyle(
            "CellBold", parent=base["BodyText"], fontName="Helvetica-Bold",
            fontSize=7.1, leading=8.6, textColor=NAVY,
        ),
        "diagram": ParagraphStyle(
            "Diagram", parent=base["BodyText"], fontName="Helvetica",
            fontSize=7.2, leading=8.2, textColor=colors.white, alignment=TA_CENTER,
        ),
        "diagram_dark": ParagraphStyle(
            "DiagramDark", parent=base["BodyText"], fontName="Helvetica",
            fontSize=7.2, leading=8.2, textColor=NAVY, alignment=TA_CENTER,
        ),
    }


S = styles()


def p(text: str, style: str = "body") -> Paragraph:
    return Paragraph(text, S[style])


def bullet(text: str) -> Paragraph:
    return p(f"&bull; {text}")


def header_footer(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(GRID)
    canvas.line(16 * mm, height - 13 * mm, width - 16 * mm, height - 13 * mm)
    canvas.setFillColor(SLATE)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(16 * mm, height - 10 * mm, "Dyness Balancer - Engineering Algorithm Reference")
    canvas.drawRightString(width - 16 * mm, height - 10 * mm, "Current implementation")
    canvas.line(16 * mm, 12 * mm, width - 16 * mm, 12 * mm)
    canvas.drawString(16 * mm, 8 * mm, "Cerbo GX / Node-RED / RS485 Dyness virtual BMS")
    canvas.drawRightString(width - 16 * mm, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


class ReferenceDocTemplate(SimpleDocTemplate):
    """Adds named destinations, outline entries, and linked TOC rows."""

    def afterFlowable(self, flowable) -> None:
        if not isinstance(flowable, Paragraph) or flowable.style.name != "H1":
            return
        title = flowable.getPlainText()
        key = f"section-{self.page}-{title.replace(' ', '-').lower()}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(title, key, 0, False)
        self.notify("TOCEntry", (0, title, self.page, key))


def contents() -> TableOfContents:
    toc = TableOfContents()
    toc.dotsMinLevel = 0
    toc.levelStyles = [
        ParagraphStyle(
            "TOCLevel1", parent=S["body"], fontName="Helvetica",
            fontSize=9.5, leading=17, leftIndent=5, rightIndent=14,
            firstLineIndent=0, textColor=NAVY,
        )
    ]
    return toc


class UmlDiagram(Flowable):
    """Small vector UML-style diagrams rendered directly into the PDF."""

    def __init__(self, kind: str, height: float = 210):
        super().__init__()
        self.kind = kind
        self.width = 178 * mm
        self.height = height

    def _box(self, c, x, y, w, h, title, lines=(), fill=PALE_BLUE, stroke=BLUE):
        c.setStrokeColor(stroke)
        c.setFillColor(fill)
        c.roundRect(x, y, w, h, 5, fill=1, stroke=1)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawCentredString(x + w / 2, y + h - 12, title)
        c.setFont("Helvetica", 6.4)
        text_y = y + h - 23
        for line in lines:
            c.drawCentredString(x + w / 2, text_y, line)
            text_y -= 8

    def _note(self, c, x, y, w, text, color=SLATE):
        c.setFillColor(color)
        c.setFont("Helvetica", 6.3)
        c.drawCentredString(x + w / 2, y, text)

    def _arrow(self, c, x1, y1, x2, y2, label=None, color=SLATE, dashed=False):
        c.saveState()
        c.setStrokeColor(color)
        c.setFillColor(color)
        c.setLineWidth(1)
        if dashed:
            c.setDash(3, 2)
        c.line(x1, y1, x2, y2)
        dx, dy = x2 - x1, y2 - y1
        length = max((dx * dx + dy * dy) ** 0.5, 1)
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        size = 5
        c.line(x2, y2, x2 - size * ux + size * 0.55 * px, y2 - size * uy + size * 0.55 * py)
        c.line(x2, y2, x2 - size * ux - size * 0.55 * px, y2 - size * uy - size * 0.55 * py)
        c.restoreState()
        if label:
            self._note(c, (x1 + x2) / 2 - 25, (y1 + y2) / 2 + 4, 50, label, color)

    def _lifeline(self, c, x, title, subtitle):
        self._box(c, x - 39, self.height - 42, 78, 27, title, (subtitle,), PALE_BLUE, BLUE)
        c.saveState()
        c.setStrokeColor(GRID)
        c.setDash(2, 2)
        c.line(x, self.height - 45, x, 18)
        c.restoreState()

    def draw(self):
        c = self.canv
        if self.kind == "architecture":
            self._box(c, 4, 135, 108, 48, "Dyness B3 batteries", ("RS485 A/B", "CID2 42/44/61/63"), PALE_GREEN, GREEN)
            self._box(c, 128, 135, 108, 48, "RS485 service", ("read-only poller", "validation + inventory"), PALE_BLUE, BLUE)
            self._box(c, 252, 135, 108, 48, "Node-RED controller", ("selection", "feed-forward + slow PI"), PALE_BLUE, BLUE)
            self._box(c, 376, 135, 108, 48, "Virtual BMS", ("D-Bus instance 100", "effective arbitration"), PALE_BLUE, BLUE)
            self._box(c, 190, 48, 108, 48, "Cerbo DVCC", ("manual BMS selection", "system-wide limits"), PALE_AMBER, AMBER)
            self._box(c, 326, 48, 128, 48, "MPPTs + MultiPlus", ("managed chargers", "actual charging"), PALE_AMBER, AMBER)
            self._arrow(c, 112, 159, 128, 159, "RS485")
            self._arrow(c, 236, 159, 252, 159, "snapshot")
            self._arrow(c, 360, 159, 376, 159, "command")
            self._arrow(c, 430, 135, 298, 96, "D-Bus limits")
            self._arrow(c, 298, 72, 326, 72, "DVCC")
            self._arrow(c, 190, 96, 160, 135, "GX settings", dashed=True)
            self._note(c, 4, 15, 484, "Arrows show data/control direction. The service never writes Dyness RS485, Cerbo charge-control settings, or device-specific charger paths.")
        elif self.kind == "states":
            self._box(c, 12, 118, 130, 58, "NORMAL", ("Cerbo UI CVL/CCL", "select first eligible pack"), PALE_BLUE, BLUE)
            self._box(c, 182, 118, 130, 58, "BALANCING", ("target selected pack 2 A", "feed-forward + slow PI"), PALE_GREEN, GREEN)
            self._box(c, 352, 118, 130, 58, "SAFETY_STOP", ("55 V / 10 A request", "release selection + reset PI"), PALE_RED, RED)
            self._arrow(c, 142, 147, 182, 147, "eligible pack")
            self._arrow(c, 312, 147, 352, 147, "stale/incomplete telemetry\nor output failure", RED)
            self._arrow(c, 352, 128, 142, 128, "fresh telemetry recovered", GREEN)
            self._arrow(c, 247, 118, 77, 84, "full SOC latch, discharge completion,\nno candidate, manual stop")
            self._arrow(c, 77, 84, 77, 118, None)
            self._note(c, 4, 35, 478, "Selected battery MOSFET OFF or local protection: release only that battery, then reselect another eligible battery. Non-selected interruptions do not disable parallel charging.")
        elif self.kind == "control":
            self._box(c, 12, 132, 104, 48, "New 8 s telemetry", ("selected current", "positive total current"), PALE_BLUE, BLUE)
            self._box(c, 142, 132, 104, 48, "Share estimate", ("Iselected / Itotal", "EWMA alpha = 0.20"), PALE_BLUE, BLUE)
            self._box(c, 272, 132, 104, 48, "Feed-forward", ("2 A / filtered share", "2 A fallback"), PALE_GREEN, GREEN)
            self._box(c, 402, 132, 104, 48, "Slow PI", ("Kp 0.20", "Ki 0.002 A/(A*s)"), PALE_GREEN, GREEN)
            self._box(c, 206, 50, 104, 48, "Aggregate request", ("rise <= 10 A/min", "downward immediate"), PALE_AMBER, AMBER)
            self._box(c, 370, 50, 142, 48, "Final arbitration", ("Cerbo UI, Dyness CVL/CCL", "thermal factor, permissions"), PALE_AMBER, AMBER)
            self._arrow(c, 116, 156, 142, 156)
            self._arrow(c, 246, 156, 272, 156)
            self._arrow(c, 376, 156, 402, 156)
            self._arrow(c, 454, 132, 290, 98, "request")
            self._arrow(c, 310, 74, 370, 74, "candidate CCL")
            self._note(c, 8, 20, 530, "Freeze PI/feed-forward when BMS-limited, CCL is zero, permission is off, selected current is non-positive, thermal derating applies, or solar-limited pause is active.")
        elif self.kind == "arbitration":
            xs = [48, 150, 252, 354, 456]
            labels = [("Node-RED", "controller"), ("RS485 service", "arbitration"), ("GX settings", "UI caps"), ("Dyness BMS", "CVL/CCL/DCL"), ("Virtual BMS", "D-Bus output")]
            for x, (title, sub) in zip(xs, labels):
                self._lifeline(c, x, title, sub)
            steps = [
                (165, 0, 1, "request CVL/CCL + mode"),
                (140, 2, 1, "read MaxChargeVoltage / Current"),
                (115, 3, 1, "read RS485 limits + permissions"),
                (90, 1, 4, "publish effective CVL/CCL/DCL"),
                (65, 4, 0, "readback: requested vs effective"),
            ]
            for y, left, right, label in steps:
                self._arrow(c, xs[left], y, xs[right], y, label, BLUE)
            self._note(c, 4, 18, 484, "effectiveCVL = min(request, Dyness CVL, enabled Cerbo UI CVL, 56.5 V); effectiveCCL = min(request, Dyness CCL, enabled UI CCL) * thermal factor.")
        elif self.kind == "telemetry":
            xs = [48, 150, 252, 354, 456]
            labels = [("Poller", "persistent serial"), ("Dyness B3", "addressed pack"), ("Validator", "framing/range"), ("Inventory", "expected packs"), ("Publisher", "snapshot + D-Bus")]
            for x, (title, sub) in zip(xs, labels):
                self._lifeline(c, x, title, sub)
            steps = [
                (166, 0, 1, "CID2 61 / 63"),
                (145, 1, 0, "system summary + limits"),
                (124, 0, 1, "CID2 42 / 44"),
                (103, 1, 0, "cells, temperatures, status"),
                (82, 0, 2, "validate + construct 16 cells"),
                (61, 2, 3, "complete/inventory status"),
                (40, 3, 4, "fresh parsed snapshot"),
            ]
            for y, left, right, label in steps:
                self._arrow(c, xs[left], y, xs[right], y, label, TEAL)
            self._note(c, 4, 18, 484, "Normal discovery scans 2-16 every 60 s. A missing known pack triggers 10 s recovery scans and is removed only after 10 failed complete scans.")
        elif self.kind == "completion":
            self._box(c, 10, 135, 112, 43, "BALANCING", ("selection held",), PALE_GREEN, GREEN)
            self._box(c, 158, 135, 142, 43, "All expected SOC = 100", ("set completion latch", "reset PI / release"), PALE_BLUE, BLUE)
            self._box(c, 344, 135, 112, 43, "NORMAL", ("wait for rearm",), PALE_BLUE, BLUE)
            self._arrow(c, 122, 156, 158, 156, "all integers 100")
            self._arrow(c, 300, 156, 344, 156, "normal UI command")
            self._arrow(c, 400, 135, 66, 72, "permission OFF, then ON\nfor 5 seconds", TEAL)
            self._arrow(c, 66, 72, 66, 135, "rearmed")
            self._box(c, 155, 35, 150, 48, "Alternative completion", ("effective discharging = ON", "SOC < 100 and spread < 30 mV"), PALE_AMBER, AMBER)
            self._arrow(c, 66, 135, 230, 83, "selected pack only", AMBER)
            self._arrow(c, 305, 59, 344, 135, "release; no latch", AMBER)
        elif self.kind == "csv":
            self._box(c, 4, 130, 108, 52, "Start CSV logging", ("validate filename", "capture initial inventory"), PALE_BLUE, BLUE)
            self._box(c, 126, 130, 108, 52, "Create metadata", ("# schema + status legend", "single header row"), PALE_BLUE, BLUE)
            self._box(c, 248, 130, 108, 52, "Append valid sample", ("local HH:MM:SS", "monotonic sample number"), PALE_GREEN, GREEN)
            self._box(c, 370, 130, 108, 52, "Resume after restart", ("read inventory + header", "continue sample number"), PALE_GREEN, GREEN)
            self._box(c, 176, 48, 144, 52, "Stop recording", ("initial pack disappears", "or user stops session"), PALE_RED, RED)
            self._arrow(c, 112, 156, 126, 156)
            self._arrow(c, 234, 156, 248, 156)
            self._arrow(c, 356, 156, 370, 156, "service restart")
            self._arrow(c, 302, 130, 248, 100, "missing initial battery", RED)
            self._note(c, 4, 18, 474, "Batteries added after recording starts are ignored. An existing file is appended only when its generated header matches the session schema.")
        else:
            c.setFillColor(RED)
            c.drawString(10, 10, f"Unknown diagram: {self.kind}")


def table(rows, widths, header=True):
    content = []
    for row in rows:
        content.append([item if isinstance(item, Paragraph) else p(str(item), "cell") for item in row])
    result = Table(content, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    style = [
        ("GRID", (0, 0), (-1, -1), 0.35, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    result.setStyle(TableStyle(style))
    return result


def section(title: str):
    return [Spacer(1, 3), p(title, "h1")]


def field_rows(items):
    return [[p(name, "cell_bold"), p(description, "cell")] for name, description in items]


def build() -> Path:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = ReferenceDocTemplate(
        str(OUTPUT), pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm, title="Dyness Balancer Algorithm Reference",
        author="Cerbo GX Dyness Balancer project",
    )
    story = []
    story += [
        Spacer(1, 18), p("Dyness Balancer", "title"),
        p("Engineering algorithm and operations reference", "subtitle"),
        p("Cerbo GX / Node-RED / Dyness RS485 / virtual BMS / DVCC", "subtitle"),
        p(f"Implementation reference generated {date.today().isoformat()}. This document describes the deployed three-state controller and read-only RS485 service. It is not an authorization to enable physical charger control.", "body"),
        Spacer(1, 8),
        table([
            [p("Document scope", "cell_bold"), p("Complete telemetry, controller, virtual-BMS, dashboard, persistence, and CSV recording behavior.", "cell")],
            [p("Physical authority", "cell_bold"), p("Cerbo BMS selection remains manual. TEST is shadow arbitration; ACTIVE requires explicit commissioning gates.", "cell")],
            [p("Control target", "cell_bold"), p("Hold the first selected unbalanced battery near 2.0 A by changing the aggregate charge-current request without assuming equal current sharing.", "cell")],
            [p("Safety fallback", "cell_bold"), p("Stale/incomplete telemetry or output-readback failure requests 55.0 V and 10.0 A; valid BMS and thermal limits can reduce it further.", "cell")],
        ], [42 * mm, 136 * mm], header=False),
        Spacer(1, 10), p("Contents", "h2"),
        contents(),
        PageBreak(),
    ]

    story += section("1. Architecture and authority")
    story += [
        p("The RS485 service is the only serial owner. It polls the Dyness/Pylon-compatible protocol, validates a complete snapshot, records parsed telemetry, publishes the virtual BMS, and exposes the latest snapshot to Node-RED. Node-RED calculates a versioned controller command. The service independently applies BMS, thermal, and Cerbo UI constraints before publishing virtual-BMS output."),
        UmlDiagram("architecture", 205),
        p("Figure 1 - System component and control data flow.", "small"),
        p("Authority hierarchy", "h2"),
        table([
            ["Layer", "Authority and rule"],
            ["Cerbo Charge Control UI", "Operator-owned maximum charge voltage and current. NORMAL requests these values; enabled limits remain final ceilings during balancing."],
            ["Dyness BMS", "Authoritative CVL, CCL, DCL, charge/discharge permission, protections, and MOSFET states."],
            ["Controller", "Selects an eligible battery and proposes an aggregate CCL. It never assumes equal current sharing."],
            ["Virtual BMS", "Publishes final effective limits on standard D-Bus paths. It does not write charger-specific paths or GX settings."],
        ], [45 * mm, 133 * mm]),
    ]

    story += section("2. RS485 telemetry and validation")
    story += [
        p("All protocol requests are read-only at 115200 baud, 8N1. The polling interval is eight seconds. The controller requires complete fresh addressed CID2 0x61 telemetry for every expected battery, with a 20-second freshness limit."),
        table([
            ["CID2", "Purpose", "Controller authority"],
            ["0x42", "Addressed cell array, temperatures, signed current, battery voltage; optional capacity/lifetime tail.", "Authoritative for per-battery current and logical 16-cell diagnostic vector."],
            ["0x61", "Addressed integer SOC, Vmin/Vmax, packed cell locations, system voltage/current, BMS temperatures and health summary.", "Sole authority for controller SOC, Vmin/Vmax, and spread."],
            ["0x63", "CVL, DVL, CCL, signed DCL, permission/state bits.", "Master global charge/discharge constraints and permissions."],
            ["0x44", "Status1-Status5, alarms, charge/discharge/precharge MOSFETs, module power, effective charge/discharge.", "Local pack eligibility/diagnostics; not a voltage-extrema source."],
        ], [18 * mm, 72 * mm, 88 * mm]),
        UmlDiagram("telemetry", 205),
        p("Figure 2 - RS485 polling, validation, inventory, and publication sequence.", "small"),
        p("Cell reconstruction and inventory", "h2"),
        bullet("Exactly 16 reported cells: retain them all. Exactly 15: derive cell 16 from that same battery's CID2 0x42 voltage minus the sum of cells 1-15. Any other count makes that battery's cell telemetry invalid."),
        bullet("CID2 0x61 pack extrema are not recalculated from the 0x42 array. Invalid/unphysical CID2 0x61 extrema remain unavailable and block elevated control."),
        bullet("A full address scan covers 2-16 every 60 seconds. A previously known missing battery becomes pending removal, triggers 10-second recovery scans, and is removed only after 10 failed complete scans."),
    ]

    story += [PageBreak()] + section("3. Controller state machine")
    story += [
        p("The controller has three states. It evaluates on telemetry, operator actions, and periodic ticks. Only a new telemetry timestamp advances the feed-forward/PI calculation; additional evaluations use the held request and report BALANCING_DUPLICATE_SAMPLE."),
        UmlDiagram("states", 205),
        p("Figure 3 - Main controller state transitions.", "small"),
        table([
            ["State", "Operation", "Entry and exit"],
            ["NORMAL", "Publish normal Cerbo UI charge limits and seek the first eligible battery in ascending address order.", "Enter on disabled controller, no candidate, normal completion, manual stop, or safety recovery. Enter BALANCING when a candidate is selected."],
            ["BALANCING", "Hold the selected battery, target its measured current near 2.0 A, and adjust only aggregate CCL.", "Exit to NORMAL on full-SOC completion, discharge completion, manual stop, or no replacement candidate. Enter SAFETY_STOP on telemetry/output failure."],
            ["SAFETY_STOP", "Release selection and reset control; request conservative 55.0 V / 10.0 A while retaining charge-capable fallback semantics.", "Enter on stale/incomplete telemetry, invalid configuration, or output-readback failure. Return to NORMAL only after required telemetry/control checks recover."],
        ], [29 * mm, 68 * mm, 81 * mm]),
        p("Eligibility and selection", "h2"),
        bullet("A candidate needs valid addressed CID2 0x61 data, finite current, spread strictly above 30 mV, charge MOSFET ON, and no local hard protection."),
        bullet("The first qualifying address is selected and stays locked even if another battery later has a larger spread."),
        bullet("If the selected battery loses its charge MOSFET or enters local protection, only it is released; the controller immediately seeks the next eligible connected battery without disabling the remaining parallel batteries."),
    ]

    story += section("4. Balancing current control")
    story += [
        p("The controlled variable is the selected battery's measured positive current. The manipulated variable is the aggregate charge-current request. This works with unequal current sharing by estimating the selected battery's observed fraction of the positive pack current."),
        UmlDiagram("control", 205),
        p("Figure 4 - Feed-forward and slow PI current-control activity.", "small"),
        p("Core calculation", "h2"),
        p("positiveTotalCurrent = sum(max(0, current) for valid, charge-MOSFET-on, unprotected batteries)<br/>selectedShare = selectedCurrent / positiveTotalCurrent<br/>feedForward = targetCurrent / filteredSelectedShare<br/>aggregateRequest = feedForward + Kp * (targetCurrent - selectedCurrent) + integral", "code"),
        table([
            ["Parameter", "Current default", "Meaning"],
            ["Selected-current target", "2.0 A", "Desired measured current in the locked selected battery."],
            ["Spread threshold", "30 mV strict", "A battery is eligible only above this value."],
            ["Useful share sample", "selected >= 0.25 A; total >= 0.5 A", "Minimum measurement quality needed to update current-share estimate."],
            ["Feed-forward EWMA", "0.20", "Smooths observed sharing fraction."],
            ["Kp / Ki", "0.20 A/A / 0.002 A/(A*s)", "Slow correction around feed-forward estimate."],
            ["Integral bound", "+/- 10 A", "Limits accumulated correction."],
            ["Upward slew", "10 A/min", "Limits increases; decreases apply immediately."],
            ["Solar detection", "4 consecutive samples; 2.0 A tolerance", "Approximately 32 seconds before pause/resume classification."],
        ], [42 * mm, 42 * mm, 94 * mm]),
        p("Solar-limited pause", "h2"),
        p("When selected current is below target by more than 0.25 A and positive total current is more than 2.0 A below the held request for four samples, with no BMS/thermal/MOSFET reason, the controller remains in BALANCING but freezes share, feed-forward, P, and integral updates. It resumes after four recovery samples without resetting the held control values."),
    ]

    story += [PageBreak()] + section("5. Virtual BMS / DVCC arbitration")
    story += [
        p("The service accepts a versioned Node-RED command containing requested voltage, aggregate current, charge-enable intent, timestamp, mode, and reason. It publishes the final effective result on standard virtual-BMS paths and retains requested-versus-effective diagnostics separately."),
        UmlDiagram("arbitration", 205),
        p("Figure 5 - Virtual-BMS arbitration sequence.", "small"),
        table([
            ["Output", "Effective behavior"],
            ["CVL", "minimum of controller request, Dyness CVL, enabled Cerbo UI maximum charge voltage, and 56.5 V balancing ceiling."],
            ["CCL", "minimum of controller aggregate request, Dyness CCL, enabled Cerbo UI maximum charge current; then multiplied by thermal factor."],
            ["DCL", "Dyness DCL magnitude only while Dyness discharge permission is valid. The controller never invents an unavailable DCL."],
            ["Charge enable", "Requires fresh valid context, controller charge intent in ACTIVE, positive effective CCL, and Dyness charge permission."],
            ["TEST", "Publishes conservative shadow output and diagnostics; selected virtual BMS and commissioning gate are still required before ACTIVE."],
        ], [42 * mm, 136 * mm]),
        p("Normal and safety behavior", "h2"),
        bullet("NORMAL follows the Cerbo values at com.victronenergy.settings /Settings/SystemSetup/MaxChargeVoltage and MaxChargeCurrent. The service only reads these settings."),
        bullet("CCL = 0 and permission OFF are valid BMS constraints, not a battery disconnection. They freeze control and force effective CCL to zero."),
        bullet("If telemetry is invalid, the conservative fallback is 55.0 V / 10.0 A before any valid BMS constraint can reduce it further."),
    ]

    story += section("6. Completion, operations, and persistence")
    story += [
        UmlDiagram("completion", 205),
        p("Figure 6 - Completion, latch rearm, and effective-discharge completion.", "small"),
        p("Completion rules", "h2"),
        bullet("Full-SOC completion: all expected batteries report integer SOC 100. The controller latches completion, resets PI, releases selection, and returns to NORMAL."),
        bullet("Full-SOC latch rearm: master charge permission must be observed OFF, then remain ON for five seconds. This prevents repeated selection while batteries remain reported as full."),
        bullet("Discharge completion: selected effective-discharging bit ON, selected integer SOC below 100, and selected addressed CID2 0x61 spread strictly below 30 mV. It returns to NORMAL without a full-SOC latch."),
        p("Operator controls", "h2"),
        table([
            ["Control", "Effect"],
            ["Enable / Disable", "Enable permits normal evaluation; disable releases selection, resets control, and restores NORMAL output."],
            ["TEST / ACTIVE", "ACTIVE requires virtual BMS selected manually in Cerbo, output readback verified, and valid configuration. TEST remains shadow mode."],
            ["Start / Stop balancing", "Start selects an eligible battery, optionally chosen manually; stop releases selection and resets PI without disabling charging."],
            ["Reset control", "Clears share, feed-forward, integral, solar-pause counters, and returns aggregate request to fallback."],
            ["Configuration", "Validates thresholds, bounds, gains, slew, and timing before replacing active controller settings."],
            ["CSV logging", "Starts/stops a fixed-inventory file under /data/home/nodered/cerbo-balancer-csv/."],
        ], [48 * mm, 130 * mm]),
        p("Persistence and retention", "h2"),
        p("Controller state/configuration, command, inventory, CSV setting, events, sessions, newest snapshot, detailed parsed telemetry, and concise monthly summaries are stored below /data/home/nodered/. Detailed parsed telemetry rolls at 24 hours; the concise summary retains 30 days. Raw RS485 protocol payloads are removed before publication/persistence."),
    ]

    story += [PageBreak()] + section("7. CSV recording format")
    story += [
        p("CSV logging is intended for balancing analysis without repeating constant connection information in every row. The log file is created below <font name='Courier'>/data/home/nodered/cerbo-balancer-csv/</font>. Filenames must be a simple basename consisting of letters, digits, dot, underscore, or hyphen, and end in .csv."),
        p("The nominal data cadence is one valid telemetry sample per eight-second service cycle. Each data row uses the Cerbo timezone, format HH:MM:SS, plus a monotonically increasing sample_number. The combination preserves ordering even when a session exceeds 24 hours."),
        p("7.1 Constant metadata block", "h2"),
        table([
            ["Metadata line", "Purpose"],
            ["# schema_version=11", "CSV layout version."],
            ["# serial_port / # baud / # poll_interval_seconds", "Constant serial and timing context."],
            ["# timestamp_format=HH:MM:SS <Cerbo timezone>", "Clock source and display format."],
            ["# virtual_bms_service / # virtual_bms_device_instance / # dvcc_output_mode", "Virtual BMS identity and recording-mode context."],
            ["# status1_bits through # status5_bits", "CID2 0x44 bit explanations for hexadecimal Status1-5 columns."],
            ["# initial_addresses=&lt;addresses&gt;", "The fixed battery inventory captured at recording start."],
        ], [67 * mm, 111 * mm]),
        p("Status1-Status5 metadata legends", "h2"),
        table([
            ["Metadata key", "Exact recorded bit legend"],
            ["# status1_bits", "bit7 pack under-voltage protection; bit6 charge-temperature protection; bit5 discharge-temperature protection; bit4 discharge over-current protection; bit3 reserved; bit2 charge over-current protection; bit1 cell under-voltage protection; bit0 over-voltage protection."],
            ["# status2_bits", "bit7-bit4 reserved; bit3 module power active; bit2 discharge MOSFET ON; bit1 charge MOSFET ON; bit0 precharge MOSFET ON."],
            ["# status3_bits", "bit7 effective charging; bit6 effective discharging; bit5 heater active; bit4-bit2 reserved; bit3 fully charged; bit2-bit1 reserved; bit0 buzzer active."],
            ["# status4_bits", "bit7-bit0 cell voltage-check faults for cells 8-1 respectively."],
            ["# status5_bits", "bit7-bit0 cell voltage-check faults for cells 16-9 respectively."],
        ], [42 * mm, 136 * mm]),
        p("7.2 Header order and per-row groups", "h2"),
        table([
            ["Order", "Columns"],
            ["1 - System summary", "timestamp, sample_number, system_voltage_v, soc_percent, bms_temperature_c, vmin_v, vmax_v, spread_mv, battery_current_a"],
            ["2 - Per battery", "One complete battery_&lt;AA&gt;_* block for every address in initial_addresses, in ascending address order."],
            ["3 - Raw BMS limits", "ccl_a, dcl_a, charge_enabled, discharge_enabled"],
            ["4 - Controller request", "controller_requested_voltage_v, controller_requested_current_a, controller_charge_enabled, controller_command_reason, controller_command_fresh, controller_command_age_s"],
            ["5 - Final virtual BMS", "virtual_bms_effective_cvl_v, virtual_bms_effective_ccl_a, virtual_bms_effective_dcl_a, virtual_bms_charge_enabled, virtual_bms_discharge_enabled, virtual_bms_allow_to_charge, virtual_bms_allow_to_discharge, virtual_bms_thermal_factor, virtual_bms_charge_blocked_by_status, virtual_bms_discharge_blocked_by_status, virtual_bms_charge_blocked_by_controller, virtual_bms_output_valid, virtual_bms_arbitration_reason"],
        ], [33 * mm, 145 * mm]),
    ]

    story += section("7.3 Per-battery CSV block")
    battery_fields = [
        ("battery_&lt;AA&gt;_present", "Battery address was present in the snapshot."),
        ("battery_&lt;AA&gt;_valid", "Validated per-battery telemetry state."),
        ("battery_&lt;AA&gt;_voltage_v / current_a / soc_percent", "CID2 0x42 battery voltage/current and addressed CID2 0x61 integer SOC."),
        ("battery_&lt;AA&gt;_vmin_v / vmin_location", "Addressed CID2 0x61 minimum cell voltage and decoded location."),
        ("battery_&lt;AA&gt;_vmax_v / vmax_location", "Addressed CID2 0x61 maximum cell voltage and decoded location."),
        ("battery_&lt;AA&gt;_spread_mv", "Addressed CID2 0x61 Vmax minus Vmin."),
        ("battery_&lt;AA&gt;_status1 through status5", "CID2 0x44 values in fixed hexadecimal format, 0x00 through 0xFF."),
        ("battery_&lt;AA&gt;_cell_01_v through cell_16_v", "Logical cell vector from CID2 0x42; cell 16 may be calculated from that same pack voltage."),
        ("battery_&lt;AA&gt;_temp_01_c through temp_05_c", "First five CID2 0x42 battery temperature sensors."),
    ]
    story += [table([["Exact field template", "Meaning"]] + field_rows(battery_fields), [72 * mm, 106 * mm])]
    story += [
        p("Formatting and unavailable values", "h2"),
        table([
            ["Value type", "CSV representation"],
            ["Cell voltage, Vmin, Vmax", "Volts, exactly three decimal places."],
            ["Other pack voltages, currents, temperatures", "Decimal values, normally two decimal places."],
            ["Spread", "Whole millivolts without a decimal fraction."],
            ["Status1-Status5", "Fixed uppercase hexadecimal: 0x00 through 0xFF."],
            ["Unavailable/invalid field", "Empty CSV field; it is never replaced by a plausible synthetic value."],
        ], [63 * mm, 115 * mm]),
        p("Requested versus effective", "h2"),
        p("Controller-requested fields show the Node-RED proposal. virtual_bms_effective_* fields show the final output after Cerbo UI limits, Dyness CVL/CCL/DCL, permissions, thermal derating, command freshness, and safety rules. Effective values are the only values that would become DVCC authority if the virtual BMS is manually selected."),
        UmlDiagram("csv", 205),
        p("Figure 7 - CSV session lifecycle.", "small"),
        p("Session invariants", "h2"),
        bullet("The initial active inventory fixes the header and number of per-battery blocks. Batteries added later are ignored for that session."),
        bullet("If a battery from initial_addresses disappears, recording stops rather than producing misleading partial rows."),
        bullet("After a service restart, the logger reads the existing metadata/header and continues the same file and sample number only when the schema matches exactly."),
    ]

    story += [PageBreak()] + section("8. Troubleshooting and operational interpretation")
    story += [
        table([
            ["Observed status", "Meaning and operator response"],
            ["BALANCING_DUPLICATE_SAMPLE", "The controller was evaluated again before a new eight-second RS485 timestamp arrived. It intentionally holds the prior request and does not integrate PI twice. This is not a fault."],
            ["SOLAR_LIMITED_PAUSE", "Cloud/available solar current is insufficient under the defined four-sample test. Selection and state remain BALANCING while control updates are frozen."],
            ["BMS_LIMITED / BMS_CCL_ZERO", "Dyness BMS advertised a lower CCL or zero CCL. The controller does not counteract it; inspect BMS permission/status and temperatures."],
            ["SAFETY_STOP_TELEMETRY", "Expected pack inventory or fresh addressed CID2 0x61 telemetry is missing/invalid. Verify RS485 ownership, cable, address inventory, and service health."],
            ["VIRTUAL_BMS_NOT_SELECTED", "ACTIVE cannot use the virtual output until the user manually chooses Dyness RS485 virtual BMS in Cerbo battery-monitor settings."],
            ["FULL_SOC_COMPLETE", "All expected batteries report integer SOC 100. Latch remains until master charge permission cycles OFF then ON for five seconds."],
            ["BATTERY_CHARGE_PATH_INTERRUPTED", "Selected pack charge MOSFET went OFF or local protection became active. Only that pack is released; other packs continue under normal BMS/DVCC rules."],
        ], [55 * mm, 123 * mm]),
        p("Commissioning reminder", "h2"),
        p("Selecting the virtual BMS in Cerbo is manual and reversible. Validate standard virtual-BMS D-Bus readback and managed-charger behavior in TEST before changing mode to ACTIVE. No part of this documentation authorizes an automatic handover, a charger-specific write path, disabling DVCC, or transmitting any undocumented BMS control command."),
        p("Reference implementation", "h2"),
        p("The controller implementation is src/controller.js. The RS485 service, virtual BMS, arbitration, inventory, telemetry retention, and CSV logger are implemented in scripts/dyness_rs485_service.py. This PDF is generated by docs/pdf/generate_dyness_balancer_pdf.py.", "small"),
    ]

    doc.multiBuild(story, onFirstPage=header_footer, onLaterPages=header_footer)
    return OUTPUT


if __name__ == "__main__":
    print(build())
