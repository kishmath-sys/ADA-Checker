"""Optional AI review of "Check manually" items, using Claude Code on this computer.

Claude Code runs signed in with the user's Claude subscription, so no API key is needed.
For each file, the parts a reviewer needs (text in reading order, each picture with its
alt text, page images for PDFs) are copied to a private temporary folder. Claude Code is
started in that folder with only its Read tool allowed, looks at the evidence and returns
a verdict for every manual item: pass, fix or unsure. The original files are never
changed and Claude Code cannot write files or run commands.
"""
from __future__ import annotations

import base64
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .common import MANUAL, short

AI_PREFIX = "AI review (Claude): "

# Items an AI cannot settle from the file alone. These stay for a person.
SKIP_CHECKS = ("video-captions", "links.warn", "pptx.check", "xlsx.check", "html.audio", "pptx.media",
               "docx.video-context")

MAX_IMAGES = 40
MAX_PDF_PAGES = 40
MAX_TEXT = 400_000
TIMEOUT = 900  # seconds per file

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fingerprint": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fix", "unsure"]},
                    "note": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["fingerprint", "verdict", "note"],
            },
        }
    },
    "required": ["items"],
}

GUIDE = """
How to judge each kind of item:
- Alt text (alt-review, alt-long): open each picture and compare it with its alt text. For a graph or chart, the alt text, or text right next to the picture, must give what a student would learn by looking: the type of graph, what the axes show, the shape or trend, and key values. Generic or inaccurate alt text is fix. Put improved alt text in suggestion.
- Pictures of equations (math-images): if the picture is an equation and its alt text reads the whole equation accurately in words or linear math, pass. Otherwise fix, with suggested alt text that reads the equation.
- Math in a PDF (pdf.math): read the page text files, which show what a screen reader gets. If exponents, subscripts, fractions, radicals or symbols are lost or garbled so the meaning changes (for example x² shown as x2), fix and name the pages and examples. If the math reads correctly, pass.
- Reading order (reading-order, floating, textboxes, table-layout): compare the reading-order text with the page images. If content would be read in an order that changes the meaning, fix. If every affected page is a single column with no side by side content, or the order in the text matches the visual order, pass.
- Merged cells and tables (table-merged, merged, no-table): pass if the table still reads sensibly row by row with clear headers. Otherwise fix.
- Color (color-meaning, xlsx.color): pass if color is never the only way to tell something apart, for example the meaning is also given by words, bold, labels or symbols. Fix if instructions or answers depend on color alone.
- Contrast (pdf.contrast and similar): look at the page image. If the text sits on a background where it is clearly readable at 4.5:1 (3:1 for large text), pass. If it is light text on white or similar, fix.
- Section or title mismatch: decide from the content which section the file is really for. If the mismatch is a real error, fix and say what should change. If it is intentional, such as a reference to an earlier section, pass.
- Web page items flagged by axe as needing review (html.review.*, html.contrast-manual): judge from the HTML, the CSS and the screenshots. Use unsure if it depends on something you cannot see.
- Zoom (html.zoom): use browser_tests.txt and screen_400_zoom.png. Pass if at 320 px there is no sideways scrolling for text (tables, code, math and images may scroll), nothing is cut off or overlapping, and the viewport tag does not block zooming. Otherwise fix.
- Keyboard (html.keyboard): use the Tab walk in browser_tests.txt. Pass if every interactive control in the HTML is reached in a sensible order, each one shows a visible focus indicator (an outline style other than none, or a box shadow), and none is covered. Native buttons, links, inputs, details and summary work with Enter or Space by default; custom controls made from div or span with click handlers need a role and key handling in the script, otherwise fix.
- Anything else: decide from the evidence. Use unsure whenever the files here are not enough to be confident.
"""


# ---------------------------------------------------------------- finding Claude Code

def find_claude() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    home = Path.home()
    cands = [home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude",
             Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
             home / ".claude" / "local" / "claude.exe", home / ".claude" / "local" / "claude"]
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def _env():
    env = dict(os.environ)
    # Make sure the subscription login is used, never a pay per use API key
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return env


def _flags():
    return {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}  # no console window


def claude_status() -> dict:
    exe = find_claude()
    if not exe:
        return {"found": False, "message": "Claude Code is not installed on this computer."}
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30, env=_env(), **_flags())
        ver = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else ""
        return {"found": True, "path": exe, "version": ver, "message": f"Claude Code found, version {ver.split()[0] if ver else 'unknown'}."}
    except Exception as e:  # noqa
        return {"found": False, "path": exe, "message": f"Claude Code was found but would not start: {short(str(e), 120)}"}


# ---------------------------------------------------------------- evidence

def _save_image(data: bytes, dest: Path) -> str | None:
    """Save as PNG, scaled down. Returns file name or None if it is not a picture Pillow can open."""
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGBA")
        im.thumbnail((1600, 1600))
        im.save(dest, "PNG")
        return dest.name
    except Exception:  # noqa
        return None


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _parse(z, name):
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    return etree.fromstring(z.read(name), parser)


def _rels(z, part):
    folder, fname = part.rsplit("/", 1)
    rp = f"{folder}/_rels/{fname}.rels"
    out = {}
    if rp in z.namelist():
        for r in _parse(z, rp):
            tgt = r.get("Target", "")
            if r.get("TargetMode") == "External":
                continue
            parts = (folder + "/" + tgt).split("/") if not tgt.startswith("/") else tgt.lstrip("/").split("/")
            norm = []
            for s in parts:
                if s == "..":
                    if norm:
                        norm.pop()
                elif s and s != ".":
                    norm.append(s)
            out[r.get("Id")] = "/".join(norm)
    return out


class _Pics:
    def __init__(self, z, out: Path):
        self.z, self.out, self.n, self.seen = z, out, 0, {}

    def add(self, media_part: str) -> str:
        if media_part in self.seen:
            return self.seen[media_part]
        name = ""
        if self.n < MAX_IMAGES and media_part in self.z.namelist():
            self.n += 1
            name = _save_image(self.z.read(media_part), self.out / f"picture{self.n:02d}.png") or ""
        self.seen[media_part] = name
        return name


def _math_text(el) -> str:
    return "".join(t.text or "" for t in el.iter(M + "t"))


def _docx_evidence(path: str, out: Path) -> list[str]:
    lines = []
    with zipfile.ZipFile(path) as z:
        doc = _parse(z, "word/document.xml")
        rels = _rels(z, "word/document.xml")
        pics = _Pics(z, out)
        body = doc.find(W + "body")
        para_no = 0

        def para_line(p, prefix=""):
            nonlocal para_no
            para_no += 1
            bits = []
            style = p.find(f"{W}pPr/{W}pStyle")
            sname = style.get(W + "val") if style is not None else ""
            for el in p.iter():
                if el.tag == W + "t":
                    bits.append(el.text or "")
                elif el.tag == M + "oMath" and el.getparent().tag != M + "oMathPara":
                    bits.append(f" [EQUATION: {_math_text(el)}] ")
                elif el.tag == M + "oMathPara":
                    bits.append(f" [EQUATION: {_math_text(el)}] ")
                elif el.tag in (WP + "inline", WP + "anchor"):
                    pr = el.find(WP + "docPr")
                    alt = (pr.get("descr") or pr.get("title") or "").strip() if pr is not None else ""
                    blip = el.find(f".//{A}blip")
                    fname = ""
                    if blip is not None and blip.get(R + "embed") in rels:
                        fname = pics.add(rels[blip.get(R + "embed")])
                    kind = "floating picture" if el.tag == WP + "anchor" else "picture"
                    if el.find(f".//{A}graphicData") is not None and "chart" in (el.find(f".//{A}graphicData").get("uri") or ""):
                        kind = "chart (no picture file)"
                    bits.append(f" [{kind.upper()}{': file ' + fname if fname else ''} | alt text: \"{alt}\"] ")
            text = re.sub(r"[ \t]+", " ", "".join(bits)).strip()
            if text:
                lines.append(f"{prefix}{'[' + sname + '] ' if sname and sname.lower().startswith(('heading', 'title')) else ''}{text}")

        for child in body:
            if child.tag == W + "p":
                para_line(child)
            elif child.tag == W + "tbl":
                lines.append("[TABLE START]")
                for ri, tr in enumerate(child.iter(W + "tr"), 1):
                    cells = []
                    for tc in tr.findall(W + "tc"):
                        span = tc.find(f"{W}tcPr/{W}gridSpan")
                        vm = tc.find(f"{W}tcPr/{W}vMerge")
                        t = " ".join("".join(x.text or "" for x in p.iter(W + "t")) for p in tc.iter(W + "p")).strip()
                        tag = (f" (spans {span.get(W + 'val')} columns)" if span is not None else "") + (" (merged vertically)" if vm is not None else "")
                        cells.append(t + tag)
                    hdr = " (header row)" if tr.find(f"{W}trPr/{W}tblHeader") is not None else ""
                    lines.append(f"  Row {ri}{hdr}: " + " | ".join(cells))
                lines.append("[TABLE END]")
        # text boxes
        for tb in doc.iter(W + "txbxContent"):
            t = " ".join("".join(x.text or "" for x in p.iter(W + "t")) for p in tb.iter(W + "p")).strip()
            if t:
                lines.append(f"[TEXT BOX CONTENT]: {t}")
    (out / "document.txt").write_text("\n".join(lines)[:MAX_TEXT], encoding="utf-8")
    files = ["document.txt"] + [f for f in pics.seen.values() if f]
    return files


def _pptx_evidence(path: str, out: Path) -> list[str]:
    lines = []
    with zipfile.ZipFile(path) as z:
        pics = _Pics(z, out)
        pres = _parse(z, "ppt/presentation.xml")
        prels = _rels(z, "ppt/presentation.xml")
        order = [prels.get(s.get(R + "id")) for s in pres.iter(P + "sldId")]
        for n, part in enumerate([o for o in order if o], 1):
            lines.append(f"=== Slide {n} (objects in reading order) ===")
            sl = _parse(z, part)
            rels = _rels(z, part)
            tree = sl.find(f"{P}cSld/{P}spTree")
            for el in tree.iter(P + "sp", P + "pic", P + "graphicFrame"):
                pr = el.find(f".//{P}cNvPr")
                name = pr.get("name", "") if pr is not None else ""
                alt = (pr.get("descr") or "").strip() if pr is not None else ""
                if el.tag == P + "pic":
                    blip = el.find(f".//{A}blip")
                    fname = pics.add(rels[blip.get(R + "embed")]) if blip is not None and blip.get(R + "embed") in rels else ""
                    lines.append(f"[PICTURE {name}{': file ' + fname if fname else ''} | alt text: \"{alt}\"]")
                else:
                    t = " ".join("".join(x.text or "" for x in p.iter(A + "t")) for p in el.iter(A + "p")).strip()
                    if t:
                        lines.append(f"{name}: {t}")
    (out / "document.txt").write_text("\n".join(lines)[:MAX_TEXT], encoding="utf-8")
    return ["document.txt"] + [f for f in pics.seen.values() if f]


def _xlsx_evidence(path: str, out: Path) -> list[str]:
    lines = []
    with zipfile.ZipFile(path) as z:
        pics = _Pics(z, out)
        for name in sorted(n for n in z.namelist() if n.startswith("xl/drawings/") and n.endswith(".xml") and "/_rels/" not in n):
            d = _parse(z, name)
            rels = _rels(z, name)
            for pr in d.iter("{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}cNvPr"):
                anchor = pr.getparent().getparent()
                blip = anchor.find(f".//{A}blip")
                fname = pics.add(rels[blip.get(R + "embed")]) if blip is not None and blip.get(R + "embed") in rels else ""
                lines.append(f"[{name}: {pr.get('name', '')}{': file ' + fname if fname else ''} | alt text: \"{(pr.get('descr') or '').strip()}\"]")
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            lines.append(f"=== Sheet {ws.title} (first 60 rows) ===")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 60:
                    break
                if any(v is not None for v in row):
                    lines.append(" | ".join("" if v is None else str(v) for v in row[:30]))
    except Exception:  # noqa
        pass
    (out / "document.txt").write_text("\n".join(lines)[:MAX_TEXT], encoding="utf-8")
    return ["document.txt"] + [f for f in pics.seen.values() if f]


def _pdf_evidence(path: str, out: Path) -> list[str]:
    try:
        import pymupdf as fitz
    except ImportError:  # older PyMuPDF
        import fitz
    files = []
    doc = fitz.open(path)
    try:
        text_lines = []
        for i, page in enumerate(doc):
            if i >= MAX_PDF_PAGES:
                text_lines.append(f"(stopped after {MAX_PDF_PAGES} pages)")
                break
            text_lines.append(f"=== Page {i + 1} ===")
            text_lines.append(page.get_text("text"))
            pix = page.get_pixmap(dpi=100)
            fn = f"page{i + 1:02d}.png"
            pix.save(str(out / fn))
            files.append(fn)
        (out / "page_text.txt").write_text("\n".join(text_lines)[:MAX_TEXT], encoding="utf-8")
    finally:
        doc.close()
    # full alt text of every tagged figure and formula, in tag order
    figs = []
    try:
        import pikepdf
        with pikepdf.open(path) as pdf:
            pages = {p.objgen: n for n, p in enumerate(pdf.pages, 1)}
            root = pdf.Root.get("/StructTreeRoot")
            seen = 0

            def walk(node, page=None):
                nonlocal seen
                seen += 1
                if seen > 20000 or not isinstance(node, pikepdf.Dictionary):
                    return
                pg = node.get("/Pg")
                if pg is not None:
                    page = pages.get(pg.objgen, page)
                s = str(node.get("/S", ""))
                if s in ("/Figure", "/Formula"):
                    figs.append(f"Page {page or '?'}: {s[1:]} alt text: \"{str(node.get('/Alt', '') or '')}\"")
                k = node.get("/K")
                kids = k if isinstance(k, pikepdf.Array) else [k] if k is not None else []
                for c in kids:
                    if isinstance(c, pikepdf.Dictionary):
                        walk(c, page)
            if root is not None:
                walk(root)
    except Exception:  # noqa
        pass
    if figs:
        (out / "figures_alt_text.txt").write_text("\n".join(figs), encoding="utf-8")
        files.append("figures_alt_text.txt")
    return ["page_text.txt"] + files


def _html_evidence(path: str, out: Path) -> list[str]:
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    files = []
    n = 0

    def data_uri(m):
        nonlocal n
        if n >= MAX_IMAGES:
            return m.group(1) + "about:blank" + m.group(3)
        try:
            data = base64.b64decode(m.group(2).split(",", 1)[1])
        except Exception:  # noqa
            return m.group(0)
        n += 1
        fn = _save_image(data, out / f"picture{n:02d}.png")
        if fn:
            files.append(fn)
            return m.group(1) + fn + m.group(3)
        return m.group(1) + "about:blank" + m.group(3)

    src = re.sub(r'(src\s*=\s*["\'])(data:image/[^"\']+)(["\'])', data_uri, src, flags=re.I)

    def local(m):
        nonlocal n
        ref = htmllib.unescape(m.group(2))
        if re.match(r"^[a-z]+:", ref, re.I) or n >= MAX_IMAGES:
            return m.group(0)
        f = (Path(path).parent / ref.split("?")[0]).resolve()
        if f.is_file():
            n += 1
            fn = _save_image(f.read_bytes(), out / f"picture{n:02d}.png")
            if fn:
                files.append(fn)
                return m.group(1) + fn + m.group(3)
        return m.group(0)

    src = re.sub(r'(<img\b[^>]*?\bsrc\s*=\s*["\'])([^"\']+)(["\'])', local, src, flags=re.I)
    (out / "page.html").write_text(src[:MAX_TEXT], encoding="utf-8")
    try:
        files += _html_browser_evidence(path, out)
    except Exception as e:  # noqa
        (out / "browser_note.txt").write_text(f"The browser tests could not run: {short(str(e), 200)}", encoding="utf-8")
        files.append("browser_note.txt")
    return ["page.html"] + files


KEYBOARD_JS = r"""
() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const cx = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), cy = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
  const top = document.elementFromPoint(cx, cy);
  return {tag: el.tagName.toLowerCase(), id: el.id || "", text: (el.innerText || el.value || el.getAttribute("aria-label") || el.title || "").trim().slice(0, 60),
          outline: cs.outlineStyle + " " + cs.outlineWidth + " " + cs.outlineColor, shadow: cs.boxShadow,
          in_view: r.bottom > 0 && r.top < innerHeight && r.width > 0 && r.height > 0,
          covered: !!top && top !== el && !el.contains(top) && !top.contains(el)};
}
"""

REFLOW_JS = r"""
() => {
  const vw = document.documentElement.clientWidth;
  const wide = [...document.querySelectorAll("body *")].filter(e => {
    const r = e.getBoundingClientRect(); return r.width > 0 && r.right > vw + 2 && !e.closest("table, pre, code, math, svg, img, canvas, video, iframe");
  }).slice(0, 15).map(e => e.tagName.toLowerCase() + (e.className && typeof e.className === "string" ? "." + e.className.split(" ")[0] : "") + ": " + (e.innerText || "").trim().slice(0, 50));
  const focusables = document.querySelectorAll('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"]), details summary').length;
  return {viewport: vw, page_width: document.documentElement.scrollWidth, wide, focusables,
          meta_viewport: (document.querySelector('meta[name=viewport]') || {}).content || ""};
}
"""


def _html_browser_evidence(path: str, out: Path) -> list[str]:
    """Screenshots at desktop width and at 320 px (the same as 400% zoom), plus a keyboard Tab walk."""
    from playwright.sync_api import sync_playwright
    from .checks.html_check import _launch
    url = Path(path).resolve().as_uri()
    notes = []
    files = []
    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            for width, label, fn in ((1280, "desktop width 1280 px", "screen_desktop.png"), (320, "320 px wide, the same as 400% zoom on a laptop", "screen_400_zoom.png")):
                page = browser.new_page(viewport={"width": width, "height": 800}, device_scale_factor=1)
                page.goto(url, wait_until="load", timeout=30000)
                page.wait_for_timeout(500)
                info = page.evaluate(REFLOW_JS)
                h = min(page.evaluate("document.documentElement.scrollHeight"), 6000)
                page.set_viewport_size({"width": width, "height": max(h, 400)})
                page.screenshot(path=str(out / fn), full_page=False)
                page.set_viewport_size({"width": width, "height": 800})
                files.append(fn)
                notes.append(f"At {label}: page width {info['page_width']} px in a {info['viewport']} px window "
                             f"({'needs sideways scrolling' if info['page_width'] > info['viewport'] + 2 else 'no sideways scrolling'}). Screenshot: {fn}")
                if info["wide"]:
                    notes.append("  Elements wider than the window (tables, code, math and images are allowed to be): " + "; ".join(info["wide"]))
                if width == 1280:
                    notes.append(f"Viewport meta tag: {info['meta_viewport'] or '(none)'}")
                    # keyboard walk
                    stops, seen = [], set()
                    for _ in range(min(info["focusables"] + 3, 80)):
                        page.keyboard.press("Tab")
                        st = page.evaluate(KEYBOARD_JS)
                        if not st:
                            continue
                        key = (st["tag"], st["id"], st["text"])
                        if key in seen:
                            break
                        seen.add(key)
                        stops.append(st)
                    notes.append(f"Keyboard: the page has {info['focusables']} focusable element(s); pressing Tab reached {len(stops)} of them in this order:")
                    for n, st in enumerate(stops, 1):
                        notes.append(f"  {n}. <{st['tag']}> \"{st['text']}\" focus outline: {st['outline']}; box shadow: {st['shadow']}"
                                     + ("" if st["in_view"] else "; NOT scrolled into view") + ("; COVERED by another element" if st["covered"] else ""))
                page.close()
        finally:
            browser.close()
    (out / "browser_tests.txt").write_text("\n".join(notes), encoding="utf-8")
    return ["browser_tests.txt"] + files


def build_evidence(kind: str, path: str, out: Path) -> list[str]:
    if kind == "word":
        return _docx_evidence(path, out)
    if kind == "pdf":
        return _pdf_evidence(path, out)
    if kind == "html":
        return _html_evidence(path, out)
    if kind == "powerpoint":
        return _pptx_evidence(path, out)
    if kind == "excel":
        return _xlsx_evidence(path, out)
    return []


# ---------------------------------------------------------------- review

def reviewable(issue: dict) -> bool:
    return issue.get("group") == MANUAL and not any(s in issue.get("check", "") for s in SKIP_CHECKS)


def _task(name: str, kind: str, files: list[str], issues: list[dict]) -> str:
    items = [{"fingerprint": i["fingerprint"], "check": i["check"], "title": i["title"], "detail": i.get("detail", ""),
              "where": i.get("location", ""), "places": i.get("items", [])[:60], "what_a_person_would_do": i.get("fix", "")}
             for i in issues]
    return f"""You are reviewing a college statistics course file for accessibility under WCAG 2.1 AA (the ADA Title II standard).
An automated checker already ran. It could not decide the items below and flagged them for a person. Your job is to make that judgment.

File: {name} ({kind})
Evidence in this folder (use the Read tool to open each one you need, including every picture you are judging):
{chr(10).join('- ' + f for f in files)}

Notes on the evidence:
- document.txt or page_text.txt is the text in the order a screen reader reads it. Pictures appear as [PICTURE: file pictureNN.png | alt text: "..."].
- pageNN.png files are images of PDF pages. For web pages, browser_tests.txt has zoom and keyboard test results measured in a real browser, with screenshots. figures_alt_text.txt has the full alt text of every tagged figure in a PDF.
- The places listed for each item may shorten alt text with an ellipsis. Use the full alt text from the evidence.

Items to judge:
{json.dumps(items, indent=1, ensure_ascii=False)}
{GUIDE}
Rules:
- Give exactly one result for every fingerprint above.
- verdict pass: you looked at the actual content and are confident it meets WCAG 2.1 AA. A person will not look at it again, so be strict.
- verdict fix: there is a real problem. In note, name the exact place (page, heading or picture) and what is wrong.
- verdict unsure: the evidence here is not enough to be confident.
- note: one or two plain sentences a teacher can act on. No em dashes.
- suggestion: for fix, the replacement text to use when that helps (such as improved alt text). Otherwise an empty string.
Do not try to edit files or run commands. Only read.
"""


def _parse_result(stdout: str) -> tuple[list[dict], str]:
    """Returns (items, error message)."""
    try:
        data = json.loads(stdout)
    except Exception:  # noqa
        # stream or text output; take the last JSON object
        m = re.findall(r"\{.*\}", stdout, re.S)
        if not m:
            return [], short(stdout.strip() or "No answer from Claude Code.", 300)
        try:
            data = json.loads(m[-1])
        except Exception:  # noqa
            return [], "Claude Code's answer could not be read."
    if isinstance(data, list):  # some versions print a list of messages
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), {})
    if data.get("is_error"):
        return [], short(str(data.get("result") or data.get("error") or "Claude Code reported an error."), 300)
    so = data.get("structured_output")
    if not so:
        txt = data.get("result") or ""
        m = re.search(r"\{.*\}", txt, re.S)
        try:
            so = json.loads(m.group(0)) if m else None
        except Exception:  # noqa
            so = None
    if not so or not isinstance(so.get("items"), list):
        return [], short("Claude Code did not return a review. " + str(data.get("result") or ""), 300)
    return so["items"], ""


def review_file(kind: str, path: str, name: str, issues: list[dict], model: str = "sonnet", exe: str | None = None) -> dict:
    """Returns {"items": {fingerprint: {verdict, note, suggestion}}, "error": str, "cost": float}."""
    exe = exe or find_claude()
    if not exe:
        return {"items": {}, "error": "Claude Code is not installed.", "cost": 0}
    work = Path(tempfile.mkdtemp(prefix="adachecker_ai_"))
    try:
        try:
            files = build_evidence(kind, path, work)
        except Exception as e:  # noqa
            return {"items": {}, "error": f"Could not prepare the file for AI review: {short(str(e), 200)}", "cost": 0}
        (work / "task.md").write_text(_task(name, kind, files, issues), encoding="utf-8")
        cmd = [exe, "-p", "Read task.md in the current folder and do what it says. Return the result in the required JSON format.",
               "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
               "--allowedTools", "Read", "--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent,Task",
               "--permission-mode", "dontAsk", "--max-budget-usd", "3"]
        if model:
            cmd += ["--model", model]
        try:
            p = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=TIMEOUT, env=_env(), stdin=subprocess.DEVNULL, **_flags())
        except subprocess.TimeoutExpired:
            return {"items": {}, "error": "Claude Code took too long on this file.", "cost": 0}
        items, err = _parse_result(p.stdout or "")
        if err and p.returncode and p.stderr.strip():
            err = short(p.stderr.strip(), 300)
        cost = 0.0
        try:
            cost = float(json.loads(p.stdout).get("total_cost_usd") or 0)
        except Exception:  # noqa
            pass
        wanted = {i["fingerprint"] for i in issues}
        out = {}
        for it in items:
            fp = str(it.get("fingerprint", ""))
            if fp in wanted and it.get("verdict") in ("pass", "fix", "unsure"):
                out[fp] = {"verdict": it["verdict"], "note": str(it.get("note", ""))[:1500],
                           "suggestion": str(it.get("suggestion", "") or "")[:2000]}
        if not out and not err:
            err = "Claude Code returned no usable results."
        return {"items": out, "error": err, "cost": cost}
    finally:
        shutil.rmtree(work, ignore_errors=True)
