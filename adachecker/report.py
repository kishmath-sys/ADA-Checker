"""Standalone HTML reports, the folder summary spreadsheet and the compliance record."""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path

from .common import CHANGE, MANUAL, EXPORT, INFO, CHECKER_VERSION

STATUS_LABEL = {"needs work": "Changes needed", "needs review": "Check manually", "pass": "Ready", "error": "Could not check"}
GROUP_TITLE = {
    CHANGE: ("Fix in the file", "These were found automatically. Each one tells you where it is and how to fix it."),
    MANUAL: ("Check manually", "These need your judgment. Check each one, then mark it reviewed in the app."),
    EXPORT: ("After you export to PDF", "A short checklist for the PDF you make from this file."),
    INFO: ("Good to know", ""),
}

CSS = """
:root{--ink:#1b1f24;--muted:#57606a;--line:#d0d7de;--bg:#ffffff;--soft:#f6f8fa;--red:#a40e26;--amber:#7a4b00;--green:#116329;--blue:#0a4fa6}
*{box-sizing:border-box}body{font:16px/1.5 "Segoe UI",system-ui,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:24px;max-width:980px;margin:auto}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.25rem;margin:1.6em 0 .3em;border-bottom:2px solid var(--line);padding-bottom:.2em}h3{font-size:1.05rem;margin:0}
.meta{color:var(--muted)}.pill{display:inline-block;padding:.1em .7em;border-radius:99px;font-weight:600;font-size:.9rem}
.s-needs-work{background:#ffebe9;color:var(--red)}.s-needs-review{background:#fff8c5;color:var(--amber)}.s-pass{background:#dafbe1;color:var(--green)}.s-error{background:#eaeef2;color:var(--ink)}
.item{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:10px 0;background:#fff}
.item.done{opacity:.65}.tag{font-size:.8rem;color:var(--muted);margin-right:.6em}
.fix{background:var(--soft);border-left:4px solid var(--blue);padding:6px 10px;margin:8px 0 0}
ul.places{margin:.4em 0 0 1.1em;padding:0}ul.places li{margin:.1em 0}
.passed li{color:var(--green)}.grid{display:flex;gap:10px;flex-wrap:wrap}.grid figure{margin:0;text-align:center;font-size:.85rem}
.grid img{max-width:220px;border:1px solid var(--line)}table{border-collapse:collapse}td,th{padding:4px 10px;border-bottom:1px solid var(--line);text-align:left}
@media print{body{padding:0}.item{break-inside:avoid}}
"""


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _issue_html(i, marks):
    ignored, reviewed = marks
    if i["fingerprint"] in ignored:
        return ""
    done = i["fingerprint"] in reviewed
    places = "".join(f"<li>{e(p)}</li>" for p in i.get("items", [])[:60])
    tags = " ".join(f'<span class="tag">{e(w)}</span>' for w in i.get("wcag_labels", []))
    std = f'<span class="tag">{e(i.get("standard", ""))}</span>' if i.get("standard") else ""
    return (f'<div class="item{" done" if done else ""}"><h3>{"✔ Reviewed: " if done else ""}{e(i["title"])}</h3>'
            f'<div>{std}{tags}<span class="tag">Priority: {e(i.get("severity", ""))}</span></div>'
            + (f'<p>{e(i["detail"])}</p>' if i.get("detail") else "")
            + (f'<p class="meta">Where: {e(i["location"])}</p>' if i.get("location") else "")
            + (f'<ul class="places">{places}</ul>' if places else "")
            + (f'<p class="fix"><strong>How to fix:</strong> {e(i["fix"])}</p>' if i.get("fix") else "")
            + "</div>")


def _result_body(r, marks_for, status_for, level=2):
    marks = marks_for(r["path"])
    h = f"h{level}"
    out = []
    if r.get("error"):
        out.append(f'<p class="item"><strong>Could not check:</strong> {e(r["error"])}</p>')
    for group in (CHANGE, MANUAL, EXPORT, INFO):
        items = [i for i in r.get("issues", []) if i["group"] == group and i["fingerprint"] not in marks[0]]
        if not items:
            continue
        title, intro = GROUP_TITLE[group]
        out.append(f"<{h}>{e(title)} ({len(items)})</{h}>" + (f"<p class='meta'>{e(intro)}</p>" if intro else ""))
        out.extend(_issue_html(i, marks) for i in items)
    if r.get("passed"):
        out.append(f"<{h}>Passed</{h}><ul class='passed'>" + "".join(f"<li>✔ {e(p)}</li>" for p in r["passed"]) + "</ul>")
    if r.get("previews"):
        out.append(f"<{h}>Color blindness preview</{h}><p class='meta'>How the pictures in this file look to students with the most common types of color blindness. Check that nothing important depends on telling these colors apart.</p>")
        for p in r["previews"]:
            figs = "".join(f'<figure><img src="{v["src"]}" alt="{e(p["label"])}, {e(v["name"])}"><figcaption>{e(v["name"])}</figcaption></figure>' for v in p["views"])
            out.append(f"<p><strong>{e(p['label'])}</strong></p><div class='grid'>{figs}</div>")
    if r.get("stats"):
        rows = "".join(f"<tr><th scope='row'>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in r["stats"].items() if not str(k).startswith("_"))
        out.append(f"<{h}>Details</{h}><table>{rows}</table>")
    for c in r.get("children", []):
        st = status_for(c)
        out.append(f"<h2>{e(c['name'])} <span class='pill s-{st.replace(' ', '-')}'>{e(STATUS_LABEL.get(st, st))}</span></h2>")
        out.append(_result_body(c, marks_for, status_for, level=3))
    return "".join(out)


def report_html(r: dict, status: str, marks_for, status_for, scanned_at: str = "") -> str:
    when = scanned_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accessibility report: {e(r['name'])}</title><style>{CSS}</style></head><body><main>
<p class="meta">ADA Checker accessibility report</p>
<h1>{e(r['name'])}</h1>
<p><span class="pill s-{status.replace(' ', '-')}">{e(STATUS_LABEL.get(status, status))}</span></p>
<p class="meta">Checked {e(when)} against WCAG 2.1 AA (the ADA Title II standard) and WCAG 2.2 AA. Checker version {CHECKER_VERSION}.<br>File: {e(r['path'])}</p>
{_result_body(r, marks_for, status_for)}
<p class="meta" style="margin-top:2em">Automated checks find many problems but not all. Items under “Check manually” need a person to confirm.</p>
</main></body></html>"""


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return name[:120] or "report"


def top_issues(r: dict, marks_for, n=3) -> str:
    out = []
    def walk(x):
        ignored, _ = marks_for(x["path"])
        for i in x.get("issues", []):
            if i["group"] == CHANGE and i["fingerprint"] not in ignored:
                out.append(i["title"])
        for c in x.get("children", []):
            walk(c)
    walk(r)
    return "; ".join(out[:n]) + (f"; +{len(out) - n} more" if len(out) > n else "")


def write_summary_xlsx(path: Path, rows: list[dict], folder_label: str):
    """rows: name, folder, kind, status, change, manual, export, top, report (path or '')"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.worksheet.table import Table, TableStyleInfo
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    headers = ["File", "Folder", "Type", "Status", "Changes needed", "Manual checks", "After export", "Main issues", "Report"]
    ws.append(headers)
    fills = {"Changes needed": "FFEBE9", "Check manually": "FFF8C5", "Ready": "DAFBE1", "Could not check": "EAEEF2"}
    order = {"needs work": 0, "error": 1, "needs review": 2, "pass": 3}
    for r in sorted(rows, key=lambda x: (order.get(x["status"], 9), -x["change"], x["name"].lower())):
        label = STATUS_LABEL.get(r["status"], r["status"])
        ws.append([r["name"], r["folder"], r["kind"], label, r["change"], r["manual"], r["export"], r["top"], "Open report" if r["report"] else ""])
        row = ws.max_row
        ws.cell(row, 4).fill = PatternFill("solid", fgColor=fills.get(label, "FFFFFF"))
        if r["report"]:
            c = ws.cell(row, 9)
            c.hyperlink = r["report"]
            c.font = Font(color="0A4FA6", underline="single")
    widths = [42, 30, 12, 17, 10, 10, 10, 70, 14]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w
    for row in ws.iter_rows(min_row=2):
        row[7].alignment = Alignment(wrap_text=True, vertical="top")
    if ws.max_row > 1:
        t = Table(displayName="Summary", ref=f"A1:I{ws.max_row}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(t)
    ws.freeze_panes = "A2"
    info = wb.create_sheet("About this scan")
    info.append(["Folder checked", folder_label])
    info.append(["Date", datetime.now().strftime("%Y-%m-%d %H:%M")])
    info.append(["Standard", "WCAG 2.1 AA (ADA Title II) and WCAG 2.2 AA"])
    info.append(["Files", len(rows)])
    for st in ("needs work", "needs review", "pass", "error"):
        info.append([STATUS_LABEL[st], sum(1 for r in rows if r["status"] == st)])
    info.column_dimensions["A"].width = 22
    info.column_dimensions["B"].width = 80
    wb.properties.title = f"Accessibility summary for {folder_label}"
    wb.save(path)


def write_compliance_xlsx(path: Path, store):
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table, TableStyleInfo
    wb = Workbook()

    def sheet(ws, title, headers, rows, widths):
        ws.title = title
        ws.append(headers)
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[chr(64 + i)].width = w
        if ws.max_row > 1:
            t = Table(displayName=re.sub(r"\W", "", title), ref=f"A1:{chr(64 + len(headers))}{ws.max_row}")
            t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            ws.add_table(t)
        ws.freeze_panes = "A2"

    latest = store.latest_per_file()
    sheet(wb.active, "Current status", ["File", "Location", "Type", "Last checked", "Status", "Changes needed", "Manual checks open"],
          [[r["name"], r["file_key"], r["kind"], r["scanned_at"].replace("T", " "), STATUS_LABEL.get(r["status"], r["status"]), r["n_change"], r["n_manual"]] for r in latest],
          [40, 60, 12, 18, 16, 10, 10])
    hist = store.history()
    sheet(wb.create_sheet(), "Scan history", ["Date", "File", "Location", "Status", "Changes needed", "Manual checks open"],
          [[r["scanned_at"].replace("T", " "), r["name"], r["file_key"], STATUS_LABEL.get(r["status"], r["status"]), r["n_change"], r["n_manual"]] for r in hist],
          [18, 40, 60, 16, 10, 10])
    rev = store.all_marks("reviewed")
    sheet(wb.create_sheet(), "Reviewed items", ["Date reviewed", "File", "Item", "Note"],
          [[m["created_at"].replace("T", " "), m["file_key"], m["title"], m["note"]] for m in rev], [18, 60, 60, 40])
    ign = store.all_marks("ignored")
    sheet(wb.create_sheet(), "Accepted as is", ["Date", "File", "Item", "Reason"],
          [[m["created_at"].replace("T", " "), m["file_key"], m["title"], m["note"]] for m in ign], [18, 60, 60, 40])
    wb.properties.title = "Accessibility compliance record"
    wb.save(path)
