"""PDF accessibility checks based on WCAG 2.x and PDF/UA. Read only."""
from __future__ import annotations

import os
import re

import pikepdf

try:
    import pymupdf
except ImportError:  # older installs
    import fitz as pymupdf

from ..common import (CHANGE, MANUAL, EXPORT, INFO, Issue, FileResult, contrast_ratio, is_large_text,
                      GENERIC_ALT, GENERIC_LINK_TEXT, looks_like_filename, short, section_number, youtube_id)

HEADING_TAGS = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
MATH_CHARS = re.compile(r"[\U0001D400-\U0001D7FF√∛∜≠≤≥±∓∞∑∏∫]")


def _kids(node):
    k = node.get("/K")
    if k is None:
        return []
    if isinstance(k, pikepdf.Array):
        return [x for x in k if isinstance(x, pikepdf.Dictionary)]
    return [k] if isinstance(k, pikepdf.Dictionary) else []


def _role(node, rolemap):
    s = node.get("/S")
    if s is None:
        return None
    s = str(s)
    seen = set()
    while rolemap and s in rolemap and s not in seen:
        seen.add(s)
        s = str(rolemap[s])
    return s


def check_pdf(path: str, display_name: str | None = None, source_hint: str | None = None) -> FileResult:
    name = display_name or os.path.basename(path)
    res = FileResult(path=path, name=name, kind="pdf")
    try:
        pdf = pikepdf.open(path)
    except pikepdf.PasswordError:
        res.error = "This PDF is password protected, so it cannot be checked. Students using screen readers may not be able to open it either."
        return res
    except Exception as e:  # noqa
        res.error = f"Could not open this PDF ({e})."
        return res
    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa
        res.error = f"Could not read this PDF ({e})."
        return res

    with pdf:
        root = pdf.Root
        npages = len(pdf.pages)
        res.stats["pages"] = npages
        producer = str(pdf.docinfo.get("/Producer", "")) if pdf.docinfo else ""
        creator = str(pdf.docinfo.get("/Creator", "")) if pdf.docinfo else ""
        res.stats["made with"] = short(creator or producer or "unknown", 60)
        from_word = "word" in (creator + producer).lower()
        fix_where = "in the Word file, then export the PDF again" if from_word else "in the original file and export again, or in Adobe Acrobat"

        if source_hint:
            res.stats["Word source"] = source_hint

        # ---------------------------------------------------------- scanned pages
        scanned = []
        text_total = 0
        for i, page in enumerate(doc):
            txt = page.get_text("text").strip()
            text_total += len(txt)
            if len(txt) < 25 and page.get_images():
                scanned.append(i + 1)
        if scanned:
            all_scanned = len(scanned) == npages
            res.add(Issue("pdf.scanned", CHANGE,
                          "This PDF is a scan with no readable text" if all_scanned else f"{len(scanned)} page(s) are scanned images with no readable text",
                          detail="Pages that are only pictures cannot be read by screen readers, searched, or zoomed cleanly.",
                          fix="Go back to the original typed document if you have one. Otherwise run text recognition (Acrobat: Scan & OCR, Recognize Text), then fix the tags. Handwritten notes need a typed version.",
                          items=[f"Page {p}" for p in scanned[:30]], wcag=["1.1.1", "1.4.5"], severity="high"))

        # ---------------------------------------------------------- tagging
        tagged = "/StructTreeRoot" in root
        marked = bool(root.get("/MarkInfo", {}).get("/Marked", False)) if root.get("/MarkInfo") is not None else False
        if not tagged:
            res.add(Issue("pdf.untagged", CHANGE, "The PDF has no accessibility tags",
                          detail="Without tags a screen reader has no headings, tables, lists or alt text, and may read the page in the wrong order.",
                          fix=("Export from Word with File, Save As, PDF, Options, “Document structure tags for accessibility” checked. Do not use Print to PDF."
                               if from_word else "Export again from the original program with tagging turned on, or use Acrobat's Accessibility, Autotag Document and then review the tags."),
                          wcag=["1.3.1", "1.3.2"], severity="high"))
        else:
            res.ok("The PDF is tagged for accessibility")
            if not marked:
                res.add(Issue("pdf.marked", CHANGE, "The PDF is not flagged as tagged",
                              fix="Re-export with tags on. In Acrobat, Accessibility, Full Check fixes this automatically.",
                              wcag=["PDF/UA 7.1"], severity="low"))

        # ---------------------------------------------------------- title and language
        title = ""
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                title = str(meta.get("dc:title") or "")
        except Exception:  # noqa
            pass
        if not title and pdf.docinfo is not None:
            title = str(pdf.docinfo.get("/Title", "") or "")
        title = title.strip()
        vp = root.get("/ViewerPreferences")
        display_title = bool(vp is not None and vp.get("/DisplayDocTitle", False))
        if not title:
            res.add(Issue("pdf.no-title", CHANGE, "The PDF has no title",
                          detail="The title bar and screen readers show the file name instead.",
                          fix=("In Word use File, Info, Properties, Title, then export again." if from_word
                               else "Set the title in the original file, or in Acrobat: File, Properties, Description, Title."),
                          wcag=["2.4.2"], severity="medium"))
        else:
            res.ok(f"Title is set: “{short(title, 60)}”")
            if not display_title:
                res.add(Issue("pdf.display-title", CHANGE, "The title bar shows the file name instead of the title",
                              fix="In Acrobat: File, Properties, Initial View, Show: Document Title. Word exports set this for you.",
                              wcag=["2.4.2", "PDF/UA 7.1"], severity="low"))
            _, tno = section_number(title)
            _, fno = section_number(os.path.splitext(name)[0].replace("_", " "))
            if tno and fno and tno != fno:
                res.add(Issue("pdf.title-mismatch", MANUAL, "Title does not match the file name",
                              detail=f"Title “{short(title, 60)}” but the file name says {fno}.",
                              fix="Confirm which one is right and update the other.", wcag=["2.4.2"], severity="medium"))

        lang = str(root.get("/Lang", "") or "").strip()
        if lang:
            res.ok(f"Language is set ({lang})")
        else:
            res.add(Issue("pdf.no-lang", CHANGE, "The PDF language is not set",
                          fix=("Set the proofing language in Word (Review, Language) and export again." if from_word
                               else "Acrobat: File, Properties, Advanced, Reading Options, Language."),
                          wcag=["3.1.1"], severity="medium"))

        # ---------------------------------------------------------- permissions
        try:
            if pdf.is_encrypted and not pdf.allow.accessibility:
                res.add(Issue("pdf.security", CHANGE, "Security settings block screen readers",
                              fix="Remove the restriction or allow “Content copying for accessibility” in the security settings.",
                              wcag=["PDF/UA 7.16"], severity="high"))
        except Exception:  # noqa
            pass

        # ---------------------------------------------------------- structure tree
        counts = {}
        headings = []
        figures = []
        formulas = []
        tables = []
        links_tagged = 0
        if tagged:
            st = root.StructTreeRoot
            rolemap = st.get("/RoleMap") or {}
            page_index = {p.objgen: i + 1 for i, p in enumerate(pdf.pages)}
            stack = [(st, None)]
            visited = 0
            while stack and visited < 200000:
                node, pg = stack.pop()
                visited += 1
                p = node.get("/Pg")
                if p is not None:
                    pg = page_index.get(p.objgen, pg)
                role = _role(node, rolemap)
                if role:
                    counts[role] = counts.get(role, 0) + 1
                    if role in HEADING_TAGS:
                        headings.append((role, pg, _actual_text(node)))
                    elif role == "/Figure":
                        figures.append((pg, str(node.get("/Alt", "") or "").strip(), node.get("/ActualText") is not None))
                    elif role == "/Formula":
                        formulas.append((pg, str(node.get("/Alt", "") or "").strip()))
                    elif role == "/Table":
                        tables.append((pg or _first_page(node, page_index), node))
                    elif role == "/Link":
                        links_tagged += 1
                for c in reversed(_kids(node)):
                    stack.append((c, pg))
            res.stats["tags"] = sum(counts.values())

            # headings
            res.stats["headings"] = len(headings)
            if not headings and npages >= 1 and text_total > 400:
                res.add(Issue("pdf.no-headings", CHANGE, "No headings are tagged",
                              detail="Screen reader users jump between sections using headings. Bold or large text alone does not count.",
                              fix=f"Apply heading styles {fix_where}." if from_word else f"Add heading tags {fix_where}.",
                              wcag=["1.3.1", "2.4.6"], severity="high"))
            elif headings:
                res.ok(f"{len(headings)} heading(s) are tagged")
                levels = [int(r[2:]) if len(r) > 2 else 1 for r, _, _ in headings]
                skips = [f"Page {headings[i][1]}: H{levels[i]} after H{levels[i-1]}" for i in range(1, len(levels)) if levels[i] > levels[i - 1] + 1]
                if skips:
                    res.add(Issue("pdf.heading-skip", CHANGE, "Heading levels skip a level", items=skips[:20],
                                  fix=f"Fix the heading levels {fix_where}.", wcag=["1.3.1"], severity="low"))

            # figures
            missing = [f"Page {p}" for p, alt, at in figures if not alt and not at]
            generic = [f"Page {p}: “{short(alt, 40)}”" for p, alt, at in figures if alt and (alt.lower().strip(' .') in GENERIC_ALT or looks_like_filename(alt))]
            if missing:
                res.add(Issue("pdf.alt-missing", CHANGE, f"{len(missing)} image(s) have no alt text",
                              fix=f"Add alt text {fix_where}. In Word: right click the image, View Alt Text.",
                              items=missing, wcag=["1.1.1"], severity="high"))
            if generic:
                res.add(Issue("pdf.alt-generic", CHANGE, f"{len(generic)} image(s) have placeholder alt text",
                              fix=f"Replace with a real description {fix_where}.", items=generic, wcag=["1.1.1"], severity="high"))
            good = [f"Page {p}: “{short(alt, 90)}”" for p, alt, at in figures if alt and not (alt.lower().strip(' .') in GENERIC_ALT or looks_like_filename(alt))]
            if figures and not missing and not generic:
                res.ok(f"All {len(figures)} image(s) have alt text")
            if good:
                res.add(Issue("pdf.alt-review", MANUAL, f"Confirm alt text describes each of {len(good)} image(s)",
                              fix="Read each description and confirm it tells a student what they need from the image.",
                              items=good, wcag=["1.1.1"], severity="medium"))
            fmiss = [f"Page {p}" for p, alt in formulas if not alt]
            if fmiss:
                res.add(Issue("pdf.formula-alt", CHANGE, f"{len(fmiss)} formula tag(s) have no alt text",
                              fix="Add alt text that reads the formula aloud, for example “x squared plus 1”.",
                              items=fmiss, wcag=["1.1.1", "PDF/UA 7.7"], severity="medium"))

            # tables
            first_col, no_th, layout = [], [], []
            for i, (pg, t) in enumerate(tables, 1):
                rows = _table_rows(t, rolemap)
                if not rows:
                    continue
                where = f"Table {i} (page {pg})"
                th_total = sum(r.count("/TH") for r in rows)
                if len(rows) <= 1 or max(len(r) for r in rows) <= 1:
                    layout.append(f"{where}: {len(rows)} row(s)")
                    continue
                if th_total == 0:
                    no_th.append(where)
                elif len(rows) >= 3 and all(r and r[0] == "/TH" for r in rows[1:]) and not all(c == "/TH" for r in rows[1:] for c in r):
                    first_col.append(where)
            if first_col:
                res.add(Issue("pdf.table-first-column", CHANGE, f"{len(first_col)} table(s) tag every first column cell as a header",
                              detail="A screen reader announces each first column cell as a label for its row. That only makes sense when the first column holds row labels.",
                              fix=("In Word, click in each table, open Table Design and uncheck First Column, then export again." if from_word
                                   else "Change those cells from TH to TD in the tags, or in the source file turn off first column headers."),
                              items=first_col, wcag=["1.3.1"], severity="medium"))
            if no_th:
                res.add(Issue("pdf.table-no-header", CHANGE, f"{len(no_th)} table(s) have no header cells",
                              fix=("In Word, turn on Header Row and Repeat Header Rows for the table, then export again." if from_word
                                   else "Tag the header row cells as TH."),
                              items=no_th, wcag=["1.3.1"], severity="medium"))
            if layout:
                res.add(Issue("pdf.table-layout", MANUAL, f"{len(layout)} table(s) may be used only for layout",
                              fix="If it only positions content, consider plain paragraphs instead, and confirm it reads in a sensible order.",
                              items=layout, wcag=["1.3.1", "1.3.2"], severity="low"))
            if tables and not (first_col or no_th):
                res.ok(f"{len(tables)} table(s) have header cells")

            # lists
            if counts.get("/L"):
                res.ok(f"{counts['/L']} list(s) are tagged as lists")

        # ---------------------------------------------------------- tab order
        bad_tabs = [i + 1 for i, p in enumerate(pdf.pages) if p.get("/Annots") and str(p.get("/Tabs", "")) != "/S"]
        if bad_tabs:
            res.add(Issue("pdf.tab-order", CHANGE, f"{len(bad_tabs)} page(s) do not use document structure for tab order",
                          fix="Acrobat: Organize Pages, select all pages, Page Properties, Tab Order, Use Document Structure. Word exports set this for you.",
                          items=[f"Page {p}" for p in bad_tabs[:20]], wcag=["2.4.3", "PDF/UA 7.18.3"], severity="low"))

        # ---------------------------------------------------------- links
        links = []
        bad_link_alt = []
        for i, page in enumerate(pdf.pages):
            for a in page.get("/Annots", []) or []:
                try:
                    if a.get("/Subtype") != "/Link":
                        continue
                    act = a.get("/A")
                    uri = str(act.get("/URI", "")) if act is not None and act.get("/URI") is not None else ""
                    contents = str(a.get("/Contents", "") or "")
                    if uri:
                        links.append({"url": uri, "text": contents, "location": f"page {i + 1}"})
                    if tagged and not contents and uri:
                        bad_link_alt.append(f"Page {i + 1}: {short(uri, 60)}")
                except Exception:  # noqa
                    continue
        # link text from the page
        vague = []
        for i, page in enumerate(doc):
            for l in page.get_links():
                if l.get("kind") == pymupdf.LINK_URI:
                    txt = page.get_textbox(l["from"]).strip().lower().strip(".:")
                    if txt in GENERIC_LINK_TEXT:
                        vague.append(f"Page {i + 1}: “{txt}” → {short(l.get('uri', ''), 50)}")
        res.links = links
        res.stats["links"] = len(links)
        if vague:
            res.add(Issue("pdf.link-text", CHANGE, f"{len(vague)} link(s) use vague text like “click here”",
                          fix=f"Change the link text so it says where it goes {fix_where}.", items=vague, wcag=["2.4.4"], severity="medium"))
        if len(bad_link_alt) > 0 and len(bad_link_alt) == len(links):
            res.add(Issue("pdf.link-alt", CHANGE, f"{len(bad_link_alt)} link(s) have no description",
                          fix="Re-export from the source with tags on, or add link descriptions in Acrobat's Tags panel.",
                          items=bad_link_alt[:20], wcag=["2.4.4", "PDF/UA 7.18.5"], severity="low"))

        # videos
        third_words = ("not your instructor", "not my video", "not the instructor")
        full_text = " ".join(p.get_text("text") for p in doc).lower()
        seen = set()
        for l in links:
            vid = youtube_id(l["url"])
            if vid and vid not in seen:
                seen.add(vid)
                res.videos.append({"url": l["url"], "title": "", "alt": l["text"], "page": l["location"],
                                   "context": "", "third_party_hint": False})
        if res.videos:
            if any(w in full_text for w in third_words):
                res.stats["note"] = "Some videos are marked as not your own"
            res.add(Issue("pdf.video-captions", MANUAL, f"Confirm captions on {len(res.videos)} linked video(s)",
                          fix="Play each video with captions on (CC) and confirm the captions match what is said, including math terms. For your own videos, fix any errors in YouTube Studio, Subtitles.",
                          items=[f"{v['page']}: {short(v['alt'] or v['url'], 70)}" for v in res.videos],
                          wcag=["1.2.2", "1.2.5"], severity="medium"))

        # ---------------------------------------------------------- fonts
        not_embedded = {}
        for i, page in enumerate(doc):
            for f in page.get_fonts(full=True):
                # (xref, ext, type, basefont, name, encoding, referencer)
                ext, ftype, basefont = f[1], f[2], f[3]
                if ext in ("n/a", "") and ftype != "Type3":
                    not_embedded.setdefault(basefont.split("+")[-1], set()).add(i + 1)
        if not_embedded:
            res.add(Issue("pdf.fonts", CHANGE, f"{len(not_embedded)} font(s) are not embedded",
                          detail="Fonts that are not embedded can display or read incorrectly on other computers. PDF/UA requires every font to be embedded.",
                          fix=("In Word, open File, Options, Save, check “Embed fonts in the file” and uncheck “Do not embed common system fonts”, then export the PDF again. "
                               "If the font is in an inserted object such as a video, a quick fix is Acrobat's Preflight “Embed missing fonts.”" if from_word
                               else "Export again with font embedding on, or use Acrobat Preflight, Embed missing fonts."),
                          items=[f"{f} (page {', '.join(map(str, sorted(p)))})" for f, p in sorted(not_embedded.items())],
                          wcag=["PDF/UA 7.21.4"], severity="low"))
        else:
            res.ok("All fonts are embedded")

        # ---------------------------------------------------------- math
        math_font = any("math" in f[3].lower() for p in doc for f in p.get_fonts())
        if math_font or MATH_CHARS.search(full_text):
            res.add(Issue("pdf.math", MANUAL, "Check that the math reads correctly",
                          detail="The math in this PDF is plain text. Exponents and radicals can lose their structure when exported, so i² may be read as “i 2” and a square root may be read as just a symbol.",
                          fix="Listen to a few equations with Read Out Loud (Acrobat: View, Read Out Loud) or a screen reader. If they read wrong, link an accessible version such as an HTML page or the Word file.",
                          wcag=["1.3.1", "1.1.1"], severity="medium"))

        # ---------------------------------------------------------- bookmarks
        if npages >= 6 and not doc.get_toc():
            res.add(Issue("pdf.bookmarks", CHANGE, "No bookmarks in a longer document",
                          fix=("In Word export Options, check “Create bookmarks using Headings” (requires heading styles)." if from_word
                               else "Add bookmarks for each section."),
                          wcag=["2.4.5"], severity="low"))

        # ---------------------------------------------------------- contrast (text on white, best effort)
        low = []
        for i, page in enumerate(doc):
            if len(low) >= 25:
                break
            d = page.get_text("dict")
            for block in d.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        t = span.get("text", "").strip()
                        if not t:
                            continue
                        c = span.get("color", 0)
                        rgb = ((c >> 16) & 255, (c >> 8) & 255, c & 255)
                        bold = "bold" in span.get("font", "").lower()
                        need = 3.0 if is_large_text(span.get("size"), bold) else 4.5
                        r = contrast_ratio(rgb, (255, 255, 255))
                        if r < need:
                            low.append(f"Page {i + 1}: “{short(t, 40)}” is {r:.1f}:1 on white")
        if low:
            res.add(Issue("pdf.contrast", MANUAL, f"{len(low)} text run(s) may have low contrast",
                          detail="These colors are below 4.5:1 against a white page. If they sit on a dark background they may be fine.",
                          fix=f"Darken the text color {fix_where}.", items=low[:25], wcag=["1.4.3"], severity="medium"))

        # ---------------------------------------------------------- reading order note
        if tagged:
            res.add(Issue("pdf.reading-order", MANUAL, "Spot check the reading order",
                          detail="Tags decide the order a screen reader reads the page. Columns, side by side tables and text boxes are where it usually goes wrong.",
                          fix="In Acrobat open Accessibility, Reading Order (or use Read Out Loud) on pages with side by side content and confirm it reads in a sensible order.",
                          wcag=["1.3.2"], severity="low"))

        # PDF/UA identifier
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                ua = meta.get("pdfuaid:part")
            if ua:
                res.ok(f"Declares PDF/UA-{ua} conformance")
        except Exception:  # noqa
            pass

    res.stats["_images"] = True
    doc.close()
    return res


def _first_page(node, page_index, depth=0):
    if depth > 6:
        return None
    p = node.get("/Pg")
    if p is not None:
        return page_index.get(p.objgen)
    for c in _kids(node):
        r = _first_page(c, page_index, depth + 1)
        if r:
            return r
    return None


def _actual_text(node) -> str:
    at = node.get("/ActualText") or node.get("/Alt") or node.get("/T")
    return str(at) if at is not None else ""


def _table_rows(table, rolemap):
    rows = []
    stack = [table]
    while stack:
        n = stack.pop(0)
        for c in _kids(n):
            r = _role(c, rolemap)
            if r == "/TR":
                rows.append([_role(x, rolemap) for x in _kids(c) if _role(x, rolemap) in ("/TH", "/TD")])
            elif r in ("/THead", "/TBody", "/TFoot"):
                stack.append(c)
    return rows
