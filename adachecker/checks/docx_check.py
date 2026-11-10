"""Word (.docx) accessibility checks. Read only: the file is never modified."""
from __future__ import annotations

import os
import re
import zipfile
from collections import Counter

from lxml import etree

from ..common import (CHANGE, MANUAL, EXPORT, INFO, Issue, FileResult, hex_to_rgb, contrast_ratio,
                      is_large_text, tint, shade, GENERIC_LINK_TEXT, GENERIC_ALT, content_words,
                      looks_like_filename, section_number, short, youtube_id)

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "dc": "http://purl.org/dc/elements/1.1/",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "v": "urn:schemas-microsoft-com:vml",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
W = "{%s}" % NS["w"]
M = "{%s}" % NS["m"]

SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)


def _xml(z: zipfile.ZipFile, name: str):
    try:
        return etree.fromstring(z.read(name), SAFE_PARSER)
    except KeyError:
        return None


def _on(el) -> bool:
    """True when a toggle property like <w:b/> is switched on."""
    if el is None:
        return False
    v = el.get(W + "val")
    return v not in ("0", "false", "off", "none")


def _local(tag) -> str:
    return tag.split("}")[-1] if isinstance(tag, str) else ""


class Styles:
    """Minimal style resolver for size, bold, underline, color and heading level."""

    def __init__(self, root, theme_colors):
        self.theme = theme_colors
        self.styles = {}
        self.default_size = 22  # half points
        self.default_lang = None
        self.default_para = "Normal"
        if root is None:
            return
        rpr = root.find("w:docDefaults/w:rPrDefault/w:rPr", NS)
        if rpr is not None:
            sz = rpr.find("w:sz", NS)
            if sz is not None:
                self.default_size = int(sz.get(W + "val", "22"))
            lang = rpr.find("w:lang", NS)
            if lang is not None:
                self.default_lang = lang.get(W + "val")
        for st in root.findall("w:style", NS):
            sid = st.get(W + "styleId")
            name_el = st.find("w:name", NS)
            name = name_el.get(W + "val") if name_el is not None else sid
            based = st.find("w:basedOn", NS)
            ppr = st.find("w:pPr", NS)
            outline = None
            if ppr is not None and ppr.find("w:outlineLvl", NS) is not None:
                outline = int(ppr.find("w:outlineLvl", NS).get(W + "val", "9"))
            self.styles[sid] = {
                "name": name or sid, "type": st.get(W + "type"),
                "based": based.get(W + "val") if based is not None else None,
                "rpr": st.find("w:rPr", NS), "outline": outline,
            }
            if st.get(W + "default") in ("1", "true") and st.get(W + "type") == "paragraph":
                self.default_para = sid
            if st.get(W + "type") == "paragraph" and sid == self.default_para:
                r = st.find("w:rPr", NS)
                if r is not None and r.find("w:lang", NS) is not None and not self.default_lang:
                    self.default_lang = r.find("w:lang", NS).get(W + "val")

    def chain(self, sid):
        seen = set()
        while sid and sid in self.styles and sid not in seen:
            seen.add(sid)
            yield self.styles[sid]
            sid = self.styles[sid]["based"]

    def prop(self, sid, tag):
        for st in self.chain(sid):
            if st["rpr"] is not None:
                el = st["rpr"].find("w:" + tag, NS)
                if el is not None:
                    return el
        return None

    def heading_level(self, sid):
        if not sid:
            return None
        for st in self.chain(sid):
            name = (st["name"] or "").lower()
            m = re.fullmatch(r"heading (\d)", name)
            if m:
                return int(m.group(1))
            if name == "title":
                return 0
            if st["outline"] is not None and st["outline"] < 9:
                return st["outline"] + 1
        return None

    def name(self, sid):
        return self.styles.get(sid, {}).get("name", sid or "Normal")

    def color(self, el):
        """Resolve a <w:color> element to RGB, or None for automatic."""
        if el is None:
            return None
        tc = el.get(W + "themeColor")
        if tc and tc in self.theme:
            rgb = self.theme[tc]
            t = el.get(W + "themeTint")
            s = el.get(W + "themeShade")
            if t:
                rgb = tint(rgb, 1 - int(t, 16) / 255)
            if s:
                rgb = shade(rgb, int(s, 16) / 255)
            return rgb
        val = el.get(W + "val")
        if not val or val.lower() == "auto":
            return None
        return hex_to_rgb(val)


def _theme_colors(z):
    root = _xml(z, "word/theme/theme1.xml")
    colors = {}
    if root is None:
        return colors
    scheme = root.find(".//a:clrScheme", NS)
    if scheme is None:
        return colors
    for child in scheme:
        name = _local(child.tag)
        srgb = child.find("a:srgbClr", NS)
        sysc = child.find("a:sysClr", NS)
        val = srgb.get("val") if srgb is not None else (sysc.get("lastClr") if sysc is not None else None)
        rgb = hex_to_rgb(val) if val else None
        if rgb:
            colors[name] = rgb
    # Word theme color names map to scheme names
    alias = {"text1": "dk1", "background1": "lt1", "text2": "dk2", "background2": "lt2",
             "dark1": "dk1", "light1": "lt1", "dark2": "dk2", "light2": "lt2"}
    for a, b in alias.items():
        if b in colors:
            colors[a] = colors[b]
    return colors


def _rels(z, part):
    folder, fname = os.path.split(part)
    root = _xml(z, f"{folder}/_rels/{fname}.rels")
    out = {}
    if root is None:
        return out
    for r in root.findall("rel:Relationship", NS):
        out[r.get("Id")] = (r.get("Target"), r.get("TargetMode"))
    return out


def _text(el) -> str:
    parts = []
    for node in el.iter():
        t = _local(node.tag)
        if node.tag in (W + "t", M + "t") and node.text:
            parts.append(node.text)
        elif t == "tab" and node.tag == W + "tab":
            parts.append(" ")
    return "".join(parts)


HIGHLIGHT = {
    "yellow": (255, 255, 0), "green": (0, 255, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255),
    "blue": (0, 0, 255), "red": (255, 0, 0), "darkBlue": (0, 0, 139), "darkCyan": (0, 139, 139),
    "darkGreen": (0, 100, 0), "darkMagenta": (128, 0, 128), "darkRed": (139, 0, 0),
    "darkYellow": (128, 128, 0), "darkGray": (169, 169, 169), "lightGray": (211, 211, 211),
    "black": (0, 0, 0), "white": (255, 255, 255),
}


def _shd(el):
    if el is None:
        return None
    s = el.find("w:shd", NS)
    if s is None:
        return None
    fill = s.get(W + "fill")
    if fill and fill.lower() != "auto":
        return hex_to_rgb(fill)
    return None


class _Walker:
    """Walks the document body in reading order, tracking page numbers and context."""

    def __init__(self, styles):
        self.styles = styles
        self.page = 1
        self.items = []   # sequence of ("p", el, info) and ("tbl", el, info)

    def walk(self, body, in_table=0, cell_bg=None, table_no=None):
        for child in body:
            tag = child.tag
            if tag == W + "p":
                info = {"page": self.page, "in_table": in_table, "cell_bg": cell_bg, "table_no": table_no}
                self.items.append(("p", child, info))
                for br in child.iter(W + "lastRenderedPageBreak", W + "br"):
                    if br.tag == W + "lastRenderedPageBreak" or br.get(W + "type") == "page":
                        self.page += 1
            elif tag == W + "tbl":
                info = {"page": self.page, "in_table": in_table}
                self.items.append(("tbl", child, info))
                no = len([1 for k, _, _ in self.items if k == "tbl"])
                for tr in child.findall("w:tr", NS):
                    for tc in tr.findall("w:tc", NS):
                        bg = _shd(tc.find("w:tcPr", NS)) or cell_bg
                        self.walk(tc, in_table + 1, bg, no)
            elif tag == W + "sdt":
                content = child.find("w:sdtContent", NS)
                if content is not None:
                    self.walk(content, in_table, cell_bg, table_no)
            elif tag in (W + "customXml", W + "smartTag"):
                self.walk(child, in_table, cell_bg, table_no)


def check_docx(path: str, display_name: str | None = None) -> FileResult:
    name = display_name or os.path.basename(path)
    res = FileResult(path=path, name=name, kind="word")
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        res.error = f"Could not open this Word file ({e}). It may be damaged, password protected, or an older .doc file saved with a .docx name."
        return res
    with z:
        doc = _xml(z, "word/document.xml")
        if doc is None:
            res.error = "This file does not contain a Word document."
            return res
        theme = _theme_colors(z)
        styles = Styles(_xml(z, "word/styles.xml"), theme)
        core = _xml(z, "docProps/core.xml")
        rels = _rels(z, "word/document.xml")
        body = doc.find("w:body", NS)
        walker = _Walker(styles)
        walker.walk(body)
        page_bg = _shd_from_background(doc)
        _run_checks(z, res, doc, body, walker, styles, core, rels, page_bg, name)
    return res


def _shd_from_background(doc):
    bg = doc.find("w:background", NS)
    if bg is not None and bg.get(W + "color") and bg.get(W + "color").lower() != "auto":
        return hex_to_rgb(bg.get(W + "color"))
    return (255, 255, 255)


def _para_style(p):
    ps = p.find("w:pPr/w:pStyle", NS)
    return ps.get(W + "val") if ps is not None else None


def _run_info(run, p, styles, para_style, cell_bg, page_bg):
    rpr = run.find("w:rPr", NS)

    def get(tag):
        el = rpr.find("w:" + tag, NS) if rpr is not None else None
        if el is None:
            cs = rpr.find("w:rStyle", NS) if rpr is not None else None
            if cs is not None:
                el = styles.prop(cs.get(W + "val"), tag)
        if el is None:
            el = styles.prop(para_style or styles.default_para, tag)
        return el

    sz_el = get("sz")
    size = int(sz_el.get(W + "val")) / 2 if sz_el is not None and sz_el.get(W + "val", "").isdigit() else styles.default_size / 2
    bold = _on(get("b"))
    u = get("u")
    underline = u is not None and u.get(W + "val", "single") != "none"
    color = styles.color(get("color"))
    bg = None
    if rpr is not None:
        hl = rpr.find("w:highlight", NS)
        if hl is not None and hl.get(W + "val") in HIGHLIGHT:
            bg = HIGHLIGHT[hl.get(W + "val")]
        bg = bg or _shd(rpr)
    bg = bg or _shd(p.find("w:pPr", NS)) or cell_bg or page_bg
    va = get("vertAlign")
    vert = va.get(W + "val") if va is not None else None
    return {"size": size, "bold": bold, "underline": underline, "color": color, "bg": bg, "vert": vert}


def _run_checks(z, res, doc, body, walker, styles, core, rels, page_bg, name):
    items = walker.items
    body_size = styles.default_size / 2
    normal_sz = styles.prop(styles.default_para, "sz")
    if normal_sz is not None and normal_sz.get(W + "val", "").isdigit():
        body_size = int(normal_sz.get(W + "val")) / 2

    pages = walker.page
    res.stats["pages (approx.)"] = pages

    # ------------------------------------------------------------ title and language
    title = ""
    if core is not None:
        t = core.find("dc:title", NS)
        title = (t.text or "").strip() if t is not None else ""
    if not title:
        res.add(Issue("docx.no-title", CHANGE, "Document title property is empty",
                      detail="Without a title, the PDF shows the file name in the title bar and screen readers announce the file name instead of a real title.",
                      fix="In Word choose File, then Info. On the right under Properties, click Title and type the document title (for example the section name). Word carries it into the PDF.",
                      wcag=["2.4.2"], severity="medium"))
    else:
        res.ok(f"Document title is set: “{short(title, 60)}”")

    if styles.default_lang:
        res.ok(f"Document language is set ({styles.default_lang})")
    else:
        res.add(Issue("docx.no-lang", CHANGE, "Document language is not set",
                      detail="Screen readers use the language setting to pick the right pronunciation.",
                      fix="Select all text (Ctrl+A), choose Review, Language, Set Proofing Language, pick English (United States) and click Set As Default.",
                      wcag=["3.1.1"], severity="medium"))

    # ------------------------------------------------------------ walk paragraphs
    heading_levels = []          # (level, text, page)
    candidates = []              # text formatted like a heading
    empty_runs = []              # (count, page)
    empty_total = 0
    current_empty = 0
    current_empty_page = 1
    typed_lists = []
    underlined = []
    superscripts = []
    low_contrast = []
    colored = []
    small_text = []
    links = []
    images = []
    videos = []
    equations = 0
    eq_sup = 0
    eq_rad = 0
    eq_frac = 0
    last_label = None            # nearest heading or heading-like line
    recent_text = []             # last few paragraph texts, for video context
    section_text = []            # text since the last heading, for video context
    first_heading = None

    hyperlink_runs = set()
    for hl in body.iter(W + "hyperlink"):
        for r in hl.iter(W + "r"):
            hyperlink_runs.add(r)
        rid = hl.get("{%s}id" % NS["r"])
        target = rels.get(rid, (None, None))[0] if rid else None
        anchor = hl.get(W + "anchor")
        links.append({"el": hl, "url": target or (f"#{anchor}" if anchor else ""), "text": _text(hl).strip()})
    # field-code hyperlinks: HYPERLINK "url"
    for instr in body.iter(W + "instrText"):
        m = re.search(r'HYPERLINK\s+"([^"]+)"', instr.text or "")
        if m:
            links.append({"el": instr, "url": m.group(1), "text": ""})
    link_by_el = {id(l["el"]): l for l in links}

    for kind, el, info in items:
        if kind == "tbl":
            continue
        p = el
        page = info["page"]
        text = _text(p)
        stripped = text.strip()
        has_math = p.find(".//m:oMath", NS) is not None
        has_drawing = p.find(".//w:drawing", NS) is not None or p.find(".//w:pict", NS) is not None or p.find(".//w:object", NS) is not None
        pstyle = _para_style(p)
        level = styles.heading_level(pstyle)

        # math
        if has_math:
            eq = len(p.findall(".//m:oMath", NS))
            equations += eq
            eq_sup += len(p.findall(".//m:sSup", NS)) + len(p.findall(".//m:sSubSup", NS))
            eq_rad += len(p.findall(".//m:rad", NS))
            eq_frac += len(p.findall(".//m:f", NS))

        # blank paragraphs used for spacing
        is_empty = not stripped and not has_math and not has_drawing and p.find(".//w:br[@w:type='page']", NS) is None
        if is_empty and not info["in_table"]:
            if current_empty == 0:
                current_empty_page = page
            current_empty += 1
            empty_total += 1
        else:
            if current_empty >= 2:
                empty_runs.append((current_empty, current_empty_page))
            current_empty = 0

        # headings
        if level is not None and stripped:
            heading_levels.append((level, stripped, page))
            last_label = stripped
            section_text = []
            if first_heading is None:
                first_heading = stripped
        elif stripped and not info["in_table"] and not has_math and not has_drawing:
            # does it look like a heading?
            runs = [r for r in p.iter(W + "r") if r.find("w:t", NS) is not None]
            total = sum(len(_text(r)) for r in runs) or 1
            fmt = [(len(_text(r)), _run_info(r, p, styles, pstyle, info["cell_bg"], page_bg)) for r in runs]
            bold_frac = sum(n for n, f in fmt if f["bold"]) / total
            u_frac = sum(n for n, f in fmt if f["underline"] and not any(r in hyperlink_runs for r in runs)) / total
            big_frac = sum(n for n, f in fmt if f["size"] >= body_size + 2) / total
            words = len(stripped.split())
            numbered = p.find("w:pPr/w:numPr", NS) is not None
            looks = (len(stripped) <= 90 and words <= 12 and not stripped.endswith((".", ","))
                     and not numbered and (bold_frac >= 0.8 or u_frac >= 0.8 or big_frac >= 0.8)
                     and (stripped.endswith(":") or bold_frac >= 0.8 and big_frac >= 0.8 or u_frac >= 0.8
                          or big_frac >= 0.8 or stripped.istitle()))
            if looks:
                candidates.append((stripped, page, "underlined" if u_frac >= 0.8 else ("bold" if bold_frac >= 0.8 else "larger text")))
                last_label = stripped
                section_text = []
                if first_heading is None:
                    first_heading = stripped
            elif u_frac > 0:
                pass

        # typed bullets or numbers instead of real lists
        if stripped and p.find("w:pPr/w:numPr", NS) is None and not (pstyle and "list" in styles.name(pstyle).lower()):
            if re.match(r"^([•▪●◦‣∙·\-\*]|\(?\d{1,2}[\.\)]|\(?[a-z][\.\)])\s+\S", stripped) and not has_math:
                if not re.match(r"^\d{1,2}\.\d", stripped):
                    typed_lists.append((stripped, page))

        # run level checks
        for r in p.iter(W + "r"):
            rt = _text(r)
            if not rt.strip():
                continue
            fi = _run_info(r, p, styles, pstyle, info["cell_bg"], page_bg)
            in_link = r in hyperlink_runs
            if fi["underline"] and not in_link and level is None:
                underlined.append((short(stripped, 60), page))
            if fi["vert"] == "superscript" and re.fullmatch(r"[\dnxyzabcijk\-+]{1,4}", rt.strip()) and level is None:
                if not re.fullmatch(r"(st|nd|rd|th)", rt.strip()):
                    superscripts.append((short(stripped, 60), page, rt.strip()))
            fg = fi["color"]
            if fg is not None:
                ratio = contrast_ratio(fg, fi["bg"])
                need = 3.0 if is_large_text(fi["size"], fi["bold"]) else 4.5
                if ratio < need:
                    low_contrast.append((short(rt, 40), page, ratio, need, "%02X%02X%02X" % fg))
                if not in_link and level is None and max(fg) - min(fg) > 60:
                    colored.append((short(rt, 40), page, "%02X%02X%02X" % fg))
            if fi["size"] < 9 and not info["in_table"]:
                small_text.append((short(rt, 40), page, fi["size"]))

        # links seen in this paragraph
        # images and videos
        for dr in list(p.iter("{%s}inline" % NS["wp"])) + list(p.iter("{%s}anchor" % NS["wp"])):
            docpr = dr.find("wp:docPr", NS)
            if docpr is None:
                continue
            descr = (docpr.get("descr") or "").strip()
            title_attr = (docpr.get("title") or "").strip()
            decorative = any(_local(e.tag) == "decorative" and e.get("val") in ("1", "true") for e in dr.iter())
            video = None
            for e in dr.iter():
                if _local(e.tag) == "webVideoPr":
                    html = e.get("embeddedHtml") or ""
                    m = re.search(r'src="([^"]+)"', html)
                    tm = re.search(r'title="([^"]+)"', html)
                    video = {"url": m.group(1) if m else "", "title": tm.group(1) if tm else ""}
            gd = dr.find(".//a:graphicData", NS)
            uri = gd.get("uri") if gd is not None else ""
            kind_name = "picture"
            if video:
                kind_name = "video"
            elif "chart" in uri:
                kind_name = "chart"
            elif "diagram" in uri:
                kind_name = "SmartArt"
            elif "wordprocessingShape" in uri:
                kind_name = "shape"
            shape_text = "".join(t.text or "" for t in dr.iter(W + "t")).strip()
            entry = {"alt": descr or title_attr, "decorative": decorative, "page": page, "kind": kind_name,
                     "label": last_label, "floating": _local(dr.tag) == "anchor", "video": video,
                     "shape_text": shape_text, "near": " ".join(recent_text[-3:]), "name": docpr.get("name", ""),
                     "section": " ".join(section_text)}
            # picture file name for color blindness preview
            blip = dr.find(".//a:blip", NS)
            if blip is not None:
                rid = blip.get("{%s}embed" % NS["r"])
                if rid and rid in rels:
                    entry["media"] = "word/" + rels[rid][0].lstrip("/").replace("word/", "")
            images.append(entry)
        for vs in p.iter("{%s}shape" % NS["v"]):
            if vs.find("v:imagedata", NS) is not None:
                images.append({"alt": (vs.get("alt") or "").strip(), "decorative": False, "page": page,
                               "kind": "picture", "label": last_label, "floating": False, "video": None,
                               "shape_text": "", "near": "", "name": vs.get("id", "")})

        if stripped:
            recent_text.append(stripped)
            recent_text = recent_text[-4:]
            if stripped != last_label:
                section_text.append(stripped)

    if current_empty >= 2:
        empty_runs.append((current_empty, current_empty_page))

    # ------------------------------------------------------------ headings
    res.stats["headings"] = len(heading_levels)
    if not heading_levels:
        cand_items = [f"Page {pg}: “{short(t, 70)}” ({how})" for t, pg, how in candidates[:40]]
        res.add(Issue("docx.no-headings", CHANGE, "No heading styles are used",
                      detail=("Section labels are formatted to look like headings but are regular text, so screen reader users cannot see the outline or jump between sections. "
                              f"{len(candidates)} line(s) look like they should be headings." if candidates else
                              "Screen reader users navigate long documents by headings. This document has none."),
                      fix="Click in each section label and pick a style from Home, Styles: Heading 1 for the main title, Heading 2 for sections, Heading 3 for parts inside a section. "
                          "To keep your current look, format one label the way you like it, right click Heading 2 in the Styles gallery and choose Update Heading 2 to Match Selection, then apply it to the rest. Headings also make the underlining on these labels unnecessary.",
                      items=cand_items, wcag=["1.3.1", "2.4.6"], severity="high"))
    else:
        res.ok(f"{len(heading_levels)} heading(s) use Word heading styles")
        if candidates:
            res.add(Issue("docx.fake-headings", CHANGE, f"{len(candidates)} line(s) look like headings but use regular text",
                          detail="These lines are bold, underlined, or enlarged like headings but are not tagged as headings.",
                          fix="Apply the matching Heading style (Home, Styles) to each line listed.",
                          items=[f"Page {pg}: “{short(t, 70)}” ({how})" for t, pg, how in candidates[:40]],
                          wcag=["1.3.1", "2.4.6"], severity="medium"))
        prev = None
        skips = []
        for lvl, t, pg in heading_levels:
            if lvl == 0:
                lvl = 1
            if prev is not None and lvl > prev + 1:
                skips.append(f"Page {pg}: Heading {lvl} “{short(t, 60)}” follows Heading {prev}")
            prev = lvl
        if skips:
            res.add(Issue("docx.heading-skip", CHANGE, "Heading levels skip a level",
                          detail="Going from Heading 1 straight to Heading 3 makes the outline look like something is missing.",
                          fix="Change the listed headings so each level is at most one deeper than the one before it.",
                          items=skips, wcag=["1.3.1"], severity="low"))
        else:
            res.ok("Heading levels are in order")
        if heading_levels[0][0] not in (0, 1):
            res.add(Issue("docx.no-h1", CHANGE, "The document does not start with a Heading 1 or Title",
                          fix="Put the document title at the top of the page as text and apply Heading 1 or the Title style.",
                          wcag=["1.3.1", "2.4.6"], severity="low"))

    # header text that is the only title
    header_texts = []
    for part in sorted(n for n in z.namelist() if re.match(r"word/header\d*\.xml", n)):
        hx = _xml(z, part)
        if hx is not None:
            t = _text(hx).strip()
            if t:
                header_texts.append(t)
    if header_texts:
        res.stats["header text"] = short(" | ".join(header_texts), 80)
        if not any(l in (0, 1) for l, _, _ in heading_levels):
            res.add(Issue("docx.title-in-header", CHANGE, "The document title only appears in the page header",
                          detail=f"Header text: “{short(header_texts[0], 70)}”. Headers are skipped by screen readers in an exported PDF, so the page has no visible title for them.",
                          fix="Add the title as the first line of the document body with the Title or Heading 1 style. You can keep it in the header too.",
                          wcag=["1.3.1", "2.4.6"], severity="medium"))

    # section number consistency between file name, header, and first heading
    _, file_no = section_number(os.path.splitext(name)[0].replace("_", " "))
    mismatches = []
    for t in header_texts:
        lab, no = section_number(t)
        if no and file_no and no != file_no:
            mismatches.append(f"Page header says “{short(t, 60)}” but the file name says {file_no}")
    if first_heading:
        lab, no = section_number(first_heading)
        if no and file_no and no != file_no:
            mismatches.append(f"First heading “{short(first_heading, 60)}” does not match the file name ({file_no})")
    if mismatches:
        res.add(Issue("docx.section-mismatch", MANUAL, "Section number does not match the file name",
                      detail="The section numbers inside the document disagree with the file name. This can confuse every student and is often a sign that part of another section was copied in.",
                      fix="Confirm which section this file is for and update the header, title, or file name to match.",
                      items=mismatches, wcag=["2.4.2", "2.4.6"], severity="high"))

    # ------------------------------------------------------------ blank paragraphs
    if empty_runs:
        longest = max(empty_runs, key=lambda x: x[0])
        res.add(Issue("docx.blank-paragraphs", CHANGE,
                      f"{sum(c for c, _ in empty_runs)} blank lines used for spacing",
                      detail=f"There are {len(empty_runs)} places where two or more empty paragraphs (pressing Enter repeatedly) create space. "
                             f"The longest run is {longest[0]} in a row on about page {longest[1]}. Screen readers announce each one as “blank.”",
                      fix="Delete the extra empty lines and add space with Layout, Spacing Before or After instead, or insert a page break (Ctrl+Enter) where you want a new page. Turning on Show/Hide ¶ on the Home tab makes them easy to see.",
                      items=[f"About page {pg}: {c} blank lines" for c, pg in sorted(empty_runs, key=lambda x: -x[0])[:15]],
                      wcag=["1.3.1"], severity="low"))
    else:
        res.ok("No stacks of blank lines used for spacing")

    if typed_lists:
        res.add(Issue("docx.typed-lists", CHANGE, f"{len(typed_lists)} list item(s) typed by hand",
                      detail="Bullets or numbers typed as characters are not recognized as a list, so screen readers do not announce how many items there are.",
                      fix="Select the items and click Bullets or Numbering on the Home tab, then delete the typed symbols.",
                      items=[f"Page {pg}: “{short(t, 60)}”" for t, pg in typed_lists[:20]], wcag=["1.3.1"], severity="low"))

    # ------------------------------------------------------------ tables
    tables = [(el, info) for kind, el, info in items if kind == "tbl"]
    res.stats["tables"] = len(tables)
    first_col, no_header, no_repeat, merged, nested, layout = [], [], [], [], [], []
    for i, (tbl, info) in enumerate(tables, 1):
        rows = tbl.findall("w:tr", NS)
        ncols = max((len(r.findall("w:tc", NS)) for r in rows), default=0)
        first_text = ""
        for t in tbl.iter(W + "t"):
            if t.text and t.text.strip():
                first_text = t.text.strip()
                break
        where = f"Table {i} (about page {info['page']}, starts “{short(first_text, 30)}”)"
        look = tbl.find("w:tblPr/w:tblLook", NS)
        frow, fcol = True, False
        if look is not None:
            val = int(look.get(W + "val", "0"), 16) if look.get(W + "val") else 0
            frow = look.get(W + "firstRow") == "1" if look.get(W + "firstRow") is not None else bool(val & 0x20)
            fcol = look.get(W + "firstColumn") == "1" if look.get(W + "firstColumn") is not None else bool(val & 0x80)
        repeat = rows and rows[0].find("w:trPr/w:tblHeader", NS) is not None and _on(rows[0].find("w:trPr/w:tblHeader", NS))
        if info["in_table"]:
            nested.append(where)
            continue
        if len(rows) <= 1 or ncols <= 1:
            layout.append(f"{where}: {len(rows)} row(s) × {ncols} column(s)")
            continue
        if fcol:
            first_col.append(where)
        if not frow and not repeat:
            no_header.append(where)
        if len(rows) >= 8 and not repeat:
            no_repeat.append(f"{where}: {len(rows)} rows")
        if tbl.find(".//w:gridSpan", NS) is not None or tbl.find(".//w:vMerge", NS) is not None:
            merged.append(where)
    if first_col:
        res.add(Issue("docx.table-first-column", CHANGE, f"{len(first_col)} table(s) mark the first column as headers",
                      detail="Word's First Column option is on, so in the PDF every cell in the first column becomes a header. "
                             "A screen reader then announces each first column cell as a label for the rest of its row. That is only right when the first column holds row labels.",
                      fix="Click in the table, open Table Design and uncheck First Column in Table Style Options. Leave Header Row checked. "
                          "This can remove bold from the first column, so reapply bold with Ctrl+B if you want that look.",
                      items=first_col, wcag=["1.3.1"], severity="medium"))
    if no_header:
        res.add(Issue("docx.table-no-header", CHANGE, f"{len(no_header)} table(s) have no header row",
                      fix="Click in the table, open Table Design and check Header Row. Then select the first row, open Layout and click Repeat Header Rows.",
                      items=no_header, wcag=["1.3.1"], severity="medium"))
    if no_repeat:
        res.add(Issue("docx.table-repeat-header", CHANGE, f"{len(no_repeat)} longer table(s) do not repeat the header row",
                      detail="If these tables break across pages, the column labels do not carry over, and the header row is not tagged as a header in the PDF.",
                      fix="Select the first row of the table, open the Layout tab (Table Layout) and click Repeat Header Rows.",
                      items=no_repeat, wcag=["1.3.1"], severity="low"))
    if merged:
        res.add(Issue("docx.table-merged", MANUAL, f"{len(merged)} table(s) have merged or split cells",
                      detail="Merged cells can make screen readers lose track of which header goes with which cell.",
                      fix="Where you can, unmerge cells so the table is a simple grid. If the merge is only for looks, keep it simple and check the table reads sensibly row by row.",
                      items=merged, wcag=["1.3.1"], severity="low"))
    if nested:
        res.add(Issue("docx.table-nested", CHANGE, f"{len(nested)} table(s) placed inside another table",
                      fix="Move nested tables out so each table stands alone.", items=nested, wcag=["1.3.1"], severity="medium"))
    if layout:
        res.add(Issue("docx.table-layout", MANUAL, f"{len(layout)} table(s) may be used only for layout",
                      detail="Tables with a single row or a single column are usually there to position things on the page. Screen readers still announce them as tables.",
                      fix="If it is only for layout, consider replacing it with regular paragraphs. If you keep it, make sure the content reads in the right order left to right, top to bottom.",
                      items=layout, wcag=["1.3.1", "1.3.2"], severity="low"))
    if tables and not (first_col or no_header or nested):
        res.ok(f"{len(tables)} table(s) have proper header rows")

    # ------------------------------------------------------------ images and videos
    pics = [i for i in images if not i["video"]]
    vids = [i for i in images if i["video"]]
    res.stats["images"] = len(pics)
    res.stats["videos"] = len(vids)
    missing, badalt, longalt, review, floating, mathy = [], [], [], [], [], []
    for im in images:
        where = f"Page {im['page']}" + (f", under “{short(im['label'], 40)}”" if im['label'] else "")
        if im["decorative"]:
            continue
        if im["kind"] == "shape" and im["shape_text"]:
            continue
        alt = im["alt"]
        if not alt:
            missing.append(f"{where}: {im['kind']} “{im['name']}”")
        elif alt.lower().strip(" .") in GENERIC_ALT or looks_like_filename(alt):
            badalt.append(f"{where}: alt text is “{short(alt, 50)}”")
        elif len(alt) > 250:
            longalt.append(f"{where}: {len(alt)} characters")
        elif not im["video"]:
            review.append(f"{where}: “{short(alt, 90)}”")
        if im["floating"] and not im["video"]:
            floating.append(f"{where}: {im['kind']}")
        if alt and re.search(r"(=|equation|formula|\bx\^|\bsolve\b|\bsimplify\b)", alt, re.I) and im["kind"] == "picture":
            mathy.append(f"{where}: “{short(alt, 60)}”")
    if missing:
        res.add(Issue("docx.alt-missing", CHANGE, f"{len(missing)} image(s) or object(s) have no alt text",
                      detail="Screen readers cannot describe these at all.",
                      fix="Right click the object and choose View Alt Text. Describe what a student needs to know from it, or check Mark as decorative if it adds no information.",
                      items=missing, wcag=["1.1.1"], severity="high"))
    if badalt:
        res.add(Issue("docx.alt-generic", CHANGE, f"{len(badalt)} image(s) have placeholder alt text",
                      detail="Alt text like a file name or the word “image” tells a screen reader user nothing.",
                      fix="Right click the image, choose View Alt Text, and replace it with a real description.",
                      items=badalt, wcag=["1.1.1"], severity="high"))
    if longalt:
        res.add(Issue("docx.alt-long", MANUAL, f"{len(longalt)} image(s) have very long alt text",
                      fix="Keep alt text to a sentence or two. Put longer explanations, like a full description of a graph, in the document text near the image.",
                      items=longalt, wcag=["1.1.1"], severity="low"))
    if review:
        res.add(Issue("docx.alt-review", MANUAL, f"Confirm alt text describes each of {len(review)} image(s)",
                      detail="The checker can see that alt text exists but not whether it is accurate. For graphs, the description should give what a student would learn from looking, such as intercepts or shape.",
                      fix="Read each description below and update any that are vague or out of date (right click, View Alt Text).",
                      items=review, wcag=["1.1.1"], severity="medium"))
    if mathy:
        res.add(Issue("docx.math-images", MANUAL, f"{len(mathy)} image(s) may be pictures of equations",
                      detail="Pictures of math cannot be read or zoomed by assistive technology the way equation editor math can.",
                      fix="Recreate the math with Insert, Equation, then delete the picture.",
                      items=mathy, wcag=["1.1.1", "1.4.5"], severity="medium"))
    if floating:
        res.add(Issue("docx.floating", MANUAL, f"{len(floating)} object(s) float over the text",
                      detail="Floating objects can be read out of order in the PDF.",
                      fix="Right click the object, choose Wrap Text, In Line with Text, then check the reading order makes sense.",
                      items=floating, wcag=["1.3.2"], severity="low"))
    if pics and not (missing or badalt):
        res.ok(f"All {len(pics)} image(s) have alt text or are marked decorative")

    # videos: alt text, context match, third party
    third_party_words = ("not your instructor", "not my video", "not the instructor", "third party", "third-party")
    mismatch = []
    for v in vids:
        url = v["video"]["url"]
        third = any(w in (v["near"] or "").lower() for w in third_party_words)
        res.videos.append({"url": url, "title": v["video"]["title"], "alt": v["alt"], "page": v["page"],
                           "context": v["label"] or "", "third_party_hint": third})
        if v["label"] and (v["alt"] or v["video"]["title"]):
            around = content_words(v["alt"] + " " + v["video"]["title"] + " " + (v.get("section") or ""))
            label_words = content_words(v["label"])
            if len(around) >= 2 and label_words and not (around & label_words):
                mismatch.append(f"Page {v['page']}: video “{short(v['video']['title'] or v['alt'], 55)}” sits under “{short(v['label'], 45)}”")
    if vids:
        if all(v["alt"] for v in vids):
            res.ok(f"All {len(vids)} inserted video(s) have alt text")
        res.add(Issue("docx.video-captions", MANUAL, f"Confirm captions on {len(vids)} linked video(s)",
                      detail="Every video needs accurate captions, and visual information that is not spoken needs a description. "
                             "Videos marked as not yours are labeled third party because you cannot edit their captions.",
                      fix="Play each video with captions on (CC) and confirm the captions match what is said, including math terms. For your own videos, fix any errors in YouTube Studio, Subtitles.",
                      items=[f"Page {v['page']}: {short(v['video']['title'] or v['alt'], 60)}" + ("  (third party)" if any(w in (v['near'] or '').lower() for w in third_party_words) else "") for v in vids],
                      wcag=["1.2.2", "1.2.5"], severity="medium"))
    if mismatch:
        res.add(Issue("docx.video-context", MANUAL, f"{len(mismatch)} video(s) may not match the section they are in",
                      detail="The heading above each of these videos shares no key words with the video, its alt text, or any text in that section. The heading or the videos may have been carried over from another section.",
                      fix="Confirm each video belongs in its section, and update the heading, the video, or its alt text.",
                      items=mismatch, wcag=["1.1.1", "2.4.6"], severity="medium"))

    # ------------------------------------------------------------ links
    bad_text, raw_url, link_list = [], [], []
    for l in links:
        url = l["url"] or ""
        if url.startswith("http"):
            link_list.append({"url": url, "text": l["text"], "location": ""})
        t = l["text"].strip().lower().strip(".:")
        if not l["text"]:
            continue
        if t in GENERIC_LINK_TEXT:
            bad_text.append(f"“{l['text']}” → {short(url, 60)}")
        elif re.match(r"^(https?://|www\.)", t):
            raw_url.append(f"{short(l['text'], 70)}")
    for v in vids:
        if v["video"]["url"]:
            link_list.append({"url": v["video"]["url"], "text": v["alt"], "location": f"page {v['page']}"})
    res.links = link_list
    res.stats["links"] = len(links)
    if bad_text:
        res.add(Issue("docx.link-text", CHANGE, f"{len(bad_text)} link(s) use vague text like “click here”",
                      detail="Screen reader users often pull up a list of links. “Click here” out of context tells them nothing.",
                      fix="Change the link text to say where it goes, for example “Khan Academy video on complex numbers.” Right click the link and choose Edit Hyperlink.",
                      items=bad_text, wcag=["2.4.4"], severity="medium"))
    if raw_url:
        res.add(Issue("docx.link-raw", CHANGE, f"{len(raw_url)} link(s) show the full web address as the text",
                      detail="Screen readers read web addresses character by character.",
                      fix="Replace the address with descriptive text (Edit Hyperlink, Text to display). Keep the address only if students need it on paper.",
                      items=raw_url[:20], wcag=["2.4.4"], severity="low"))
    if links and not bad_text:
        res.ok(f"{len(links)} link(s) have descriptive text")

    # ------------------------------------------------------------ color and text
    if low_contrast:
        res.add(Issue("docx.contrast", CHANGE, f"{len(low_contrast)} text run(s) have low color contrast",
                      detail="Text must have a contrast ratio of at least 4.5:1 (3:1 for large text) against its background.",
                      fix="Select the text and choose a darker font color, or a lighter background.",
                      items=[f"Page {pg}: “{t}” color #{c} is {r:.1f}:1, needs {n}:1" for t, pg, r, n, c in low_contrast[:25]],
                      wcag=["1.4.3"], severity="high"))
    else:
        res.ok("Text color contrast passes where colors are set")
    if colored:
        uniq = Counter(c for _, _, c in colored)
        res.add(Issue("docx.color-meaning", MANUAL, f"Colored text is used in {len(colored)} place(s)",
                      detail=f"Colors used: {', '.join('#' + c for c in list(uniq)[:6])}. That is fine as long as color is not the only way to tell something apart, such as “the answers in red.”",
                      fix="If color carries meaning, add a second cue such as bold, a label, or a symbol.",
                      items=[f"Page {pg}: “{t}”" for t, pg, _ in colored[:15]], wcag=["1.4.1"], severity="low"))
    cand_texts = {short(t, 60) for t, _, _ in candidates}
    uniq_u = []
    for t, pg in underlined:
        if t not in cand_texts and (t, pg) not in uniq_u:  # headings are covered by the heading fix
            uniq_u.append((t, pg))
    if uniq_u:
        res.add(Issue("docx.underline", CHANGE, f"Underlined text that is not a link ({len(uniq_u)} place(s))",
                      detail="Underlining looks like a link to many readers.",
                      fix="Use bold or a Heading style instead of underline for labels and emphasis.",
                      items=[f"Page {pg}: “{t}”" for t, pg in uniq_u[:20]], wcag=[], severity="low"))
    if superscripts:
        res.add(Issue("docx.superscript-math", CHANGE, f"{len(superscripts)} exponent(s) typed as superscript text",
                      detail="Superscript formatting is not announced, so x² typed this way is read as “x 2.”",
                      fix="Rebuild these with Insert, Equation so the exponent is real math.",
                      items=[f"Page {pg}: “{t}” (superscript {s})" for t, pg, s in superscripts[:20]], wcag=["1.3.1"], severity="medium"))
    if small_text:
        res.add(Issue("docx.small-text", CHANGE, f"{len(small_text)} text run(s) smaller than 9 point",
                      fix="Use at least 10 to 11 point for body text so it is readable when printed and zoomed.",
                      items=[f"Page {pg}: “{t}” at {s:g} pt" for t, pg, s in small_text[:15]], wcag=[], severity="low"))

    # text boxes
    tb = len(list(body.iter(W + "txbxContent")))
    if tb:
        res.add(Issue("docx.textboxes", MANUAL, f"{tb} text box(es) found",
                      detail="Text boxes float over the page and are often read out of order or skipped in the PDF.",
                      fix="Move text box content into regular paragraphs, or a one cell table if you want a border.",
                      wcag=["1.3.2"], severity="medium"))

    # comments and tracked changes
    if "word/comments.xml" in z.namelist() or body.find(".//w:ins", NS) is not None or body.find(".//w:del", NS) is not None:
        res.add(Issue("docx.revisions", INFO, "Comments or tracked changes are still in the file",
                      fix="Review, then accept or delete them before exporting (Review tab).", severity="low"))

    # ------------------------------------------------------------ equations
    res.stats["equations"] = equations
    if equations:
        res.ok(f"{equations} equation(s) are built with Word's equation editor, which screen readers can read in Word")

    # ------------------------------------------------------------ after export to PDF
    res.add(Issue("docx.export-tags", EXPORT, "Export with accessibility tags on",
                  fix="Use File, Save As (or Export), choose PDF, click Options and check “Document structure tags for accessibility” and “Create bookmarks using Headings.” Do not use Print to PDF, which removes all tags.",
                  wcag=["1.3.1"], severity="high"))
    if equations:
        res.add(Issue("docx.export-math", EXPORT, "Check that exponents and radicals read correctly in the PDF",
                      detail=f"This file has {equations} equations, including {eq_sup} with exponents, {eq_rad} with radicals and {eq_frac} fractions. "
                             "Word's PDF export turns equations into plain text, so the structure can be lost (i² may be read as “i 2”).",
                      fix="Open the PDF and listen to a few equations with Read Out Loud (Acrobat: View, Read Out Loud) or a screen reader. "
                          "If they read wrong, offer an accessible version alongside the PDF (an HTML page or the Word file itself), or add alt text to key equations.",
                      wcag=["1.3.1", "1.1.1"], severity="medium"))
    res.add(Issue("docx.export-recheck", EXPORT, "Run this checker on the exported PDF",
                  fix="Drop the new PDF into ADA Checker. It confirms tags, title, language, headings and that all fonts are embedded.",
                  severity="medium"))

    # keep media paths for color blindness previews
    res.stats["_media"] = [im["media"] for im in pics if im.get("media") and not im["decorative"]][:12]
