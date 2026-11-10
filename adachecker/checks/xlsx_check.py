"""Excel (.xlsx) checks. Read only."""
from __future__ import annotations

import os
import re
import zipfile

from lxml import etree

from ..common import (CHANGE, MANUAL, EXPORT, Issue, FileResult, hex_to_rgb, contrast_ratio,
                      GENERIC_ALT, GENERIC_LINK_TEXT, looks_like_filename, short)

PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
NS = {
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def check_xlsx(path: str, display_name: str | None = None) -> FileResult:
    name = display_name or os.path.basename(path)
    res = FileResult(path=path, name=name, kind="excel")
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:  # noqa
        res.error = f"Could not open this Excel file ({e})."
        return res

    sheets = wb.worksheets
    res.stats["sheets"] = len(sheets)
    default_names, blank, merged, no_table, low_contrast, colored, raw_links, vague, links = [], [], [], [], [], [], [], [], []
    for ws in sheets:
        if re.fullmatch(r"Sheet\d*", ws.title):
            default_names.append(ws.title)
        if ws.max_row <= 1 and ws.max_column <= 1 and ws.cell(1, 1).value is None:
            blank.append(ws.title)
            continue
        if ws.merged_cells.ranges:
            merged.append(f"{ws.title}: {', '.join(str(r) for r in list(ws.merged_cells.ranges)[:6])}")
        if not ws.tables and ws.max_row >= 4 and ws.max_column >= 2:
            no_table.append(ws.title)
        fills = 0
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 400), max_col=min(ws.max_column, 40)):
            for c in row:
                if c.value is None:
                    continue
                if c.hyperlink and c.hyperlink.target:
                    t = str(c.value).strip()
                    links.append({"url": c.hyperlink.target, "text": t, "location": f"{ws.title}!{c.coordinate}"})
                    if t.lower() in GENERIC_LINK_TEXT:
                        vague.append(f"{ws.title}!{c.coordinate}: “{t}”")
                    elif t.startswith(("http", "www.")):
                        raw_links.append(f"{ws.title}!{c.coordinate}")
                fg = None
                if c.font is not None and c.font.color is not None and c.font.color.type == "rgb" and isinstance(c.font.color.rgb, str):
                    fg = hex_to_rgb(c.font.color.rgb[-6:])
                bg = (255, 255, 255)
                if c.fill is not None and c.fill.fill_type == "solid" and c.fill.fgColor is not None and c.fill.fgColor.type == "rgb" and isinstance(c.fill.fgColor.rgb, str):
                    bg = hex_to_rgb(c.fill.fgColor.rgb[-6:]) or bg
                    if bg != (255, 255, 255):
                        fills += 1
                if fg:
                    size = c.font.sz or 11
                    need = 3.0 if (size >= 18 or (c.font.b and size >= 14)) else 4.5
                    r = contrast_ratio(fg, bg)
                    if r < need:
                        low_contrast.append(f"{ws.title}!{c.coordinate}: “{short(str(c.value), 25)}” is {r:.1f}:1")
        if fills >= 5 or ws.conditional_formatting:
            colored.append(ws.title)

    # alt text on images and charts (read straight from the drawing parts)
    missing_alt, generic_alt, review_alt = [], [], []
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if re.match(r"xl/drawings/drawing\d+\.xml$", n):
                    root = etree.fromstring(z.read(n), PARSER)
                    for nv in root.iter("{%s}cNvPr" % NS["xdr"]):
                        parent = nv.getparent().getparent()
                        kind = parent.tag.split("}")[-1]
                        if kind not in ("pic", "graphicFrame", "sp"):
                            continue
                        text = "".join(t.text or "" for t in parent.iter("{%s}t" % NS["a"])).strip()
                        if kind == "sp" and text:
                            continue
                        decorative = any(e.tag.endswith("decorative") and e.get("val") in ("1", "true") for e in nv.iter())
                        if decorative:
                            continue
                        descr = (nv.get("descr") or "").strip()
                        label = f"{'chart' if kind == 'graphicFrame' else 'picture' if kind == 'pic' else 'shape'} “{nv.get('name', '')}”"
                        if not descr:
                            missing_alt.append(label)
                        elif descr.lower() in GENERIC_ALT or looks_like_filename(descr):
                            generic_alt.append(f"{label}: “{descr}”")
                        else:
                            review_alt.append(f"{label}: “{short(descr, 80)}”")
            core = z.read("docProps/core.xml") if "docProps/core.xml" in z.namelist() else None
            title = ""
            if core:
                t = etree.fromstring(core, PARSER).find("dc:title", NS)
                title = (t.text or "").strip() if t is not None else ""
    except Exception:  # noqa
        title = ""

    res.links = links
    if not title:
        res.add(Issue("xlsx.no-title", CHANGE, "Workbook title property is empty",
                      fix="File, Info, Properties, Title.", wcag=["2.4.2"], severity="low"))
    if default_names:
        res.add(Issue("xlsx.sheet-names", CHANGE, f"{len(default_names)} sheet(s) still have default names",
                      detail="Screen readers announce sheet names. “Sheet1” says nothing about what is on it.",
                      fix="Double click each sheet tab and give it a descriptive name.", items=default_names, wcag=["2.4.6"], severity="medium"))
    else:
        res.ok("Sheet names are descriptive")
    if blank:
        res.add(Issue("xlsx.blank-sheets", CHANGE, f"{len(blank)} blank sheet(s)",
                      fix="Delete empty sheets so students do not land on a blank page.", items=blank, severity="low"))
    if merged:
        res.add(Issue("xlsx.merged", MANUAL, f"Merged cells on {len(merged)} sheet(s)",
                      detail="Merged cells can make screen readers announce the wrong cell positions.",
                      fix="Unmerge cells and use Center Across Selection (Format Cells, Alignment) for the same look.",
                      items=merged, wcag=["1.3.1"], severity="low"))
    if no_table:
        res.add(Issue("xlsx.no-table", MANUAL, f"{len(no_table)} sheet(s) with data are not formatted as a table",
                      detail="Formatting a range as a table marks the header row so screen readers can announce column names.",
                      fix="Select the data, press Ctrl+T, and check “My table has headers.”",
                      items=no_table, wcag=["1.3.1"], severity="low"))
    if missing_alt:
        res.add(Issue("xlsx.alt-missing", CHANGE, f"{len(missing_alt)} chart(s) or picture(s) have no alt text",
                      fix="Right click, View Alt Text. For charts describe the trend or key values.", items=missing_alt, wcag=["1.1.1"], severity="high"))
    if generic_alt:
        res.add(Issue("xlsx.alt-generic", CHANGE, f"{len(generic_alt)} picture(s) have placeholder alt text",
                      fix="Rewrite the description.", items=generic_alt, wcag=["1.1.1"], severity="medium"))
    if review_alt:
        res.add(Issue("xlsx.alt-review", MANUAL, f"Confirm alt text on {len(review_alt)} chart(s) or picture(s)",
                      fix="Make sure each description gives the trend or values a student needs.", items=review_alt, wcag=["1.1.1"], severity="medium"))
    if low_contrast:
        res.add(Issue("xlsx.contrast", CHANGE, f"{len(low_contrast)} cell(s) have low color contrast",
                      fix="Choose a darker font color or a lighter fill.", items=low_contrast[:25], wcag=["1.4.3"], severity="high"))
    if colored:
        res.add(Issue("xlsx.color", MANUAL, f"Cell colors are used on {len(colored)} sheet(s)",
                      detail="Fill colors or conditional formatting are fine as long as color is not the only way to tell values apart.",
                      fix="If a color means something, add a text label or symbol too.", items=colored, wcag=["1.4.1"], severity="low"))
    if vague:
        res.add(Issue("xlsx.link-text", CHANGE, f"{len(vague)} link(s) use vague text", fix="Change the cell text to say where the link goes.",
                      items=vague, wcag=["2.4.4"], severity="medium"))
    if raw_links:
        res.add(Issue("xlsx.link-raw", CHANGE, f"{len(raw_links)} link(s) show the raw web address",
                      fix="Use Insert, Link and set Text to display to a short description.", items=raw_links[:20], wcag=["2.4.4"], severity="low"))
    res.add(Issue("xlsx.check", MANUAL, "Run Excel's own Accessibility Checker too",
                  fix="Review, Check Accessibility.", severity="low"))
    return res
