"""PowerPoint (.pptx) checks. Read only."""
from __future__ import annotations

import os
import re
import zipfile

from lxml import etree

from ..common import (CHANGE, MANUAL, EXPORT, Issue, FileResult, hex_to_rgb, contrast_ratio, is_large_text,
                      GENERIC_ALT, GENERIC_LINK_TEXT, looks_like_filename, short, youtube_id)

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "dc": "http://purl.org/dc/elements/1.1/",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
P = "{%s}" % NS["p"]
A = "{%s}" % NS["a"]
PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)


def _xml(z, name):
    try:
        return etree.fromstring(z.read(name), PARSER)
    except KeyError:
        return None


def _rels(z, part):
    folder, fname = os.path.split(part)
    root = _xml(z, f"{folder}/_rels/{fname}.rels")
    out = {}
    if root is not None:
        for r in root.findall("rel:Relationship", NS):
            out[r.get("Id")] = (r.get("Target"), r.get("TargetMode"), r.get("Type", ""))
    return out


def _resolve(base_part, target):
    if target.startswith("/"):
        return target.lstrip("/")
    parts = os.path.dirname(base_part).split("/")
    for seg in target.split("/"):
        if seg == "..":
            parts.pop()
        elif seg and seg != ".":
            parts.append(seg)
    return "/".join(parts)


def _theme_colors(z):
    colors = {}
    for n in z.namelist():
        if re.match(r"ppt/theme/theme\d+\.xml", n):
            root = _xml(z, n)
            scheme = root.find(".//a:clrScheme", NS) if root is not None else None
            if scheme is None:
                continue
            for child in scheme:
                nm = child.tag.split("}")[-1]
                srgb = child.find("a:srgbClr", NS)
                sysc = child.find("a:sysClr", NS)
                val = srgb.get("val") if srgb is not None else (sysc.get("lastClr") if sysc is not None else None)
                if val and hex_to_rgb(val):
                    colors[nm] = hex_to_rgb(val)
            break
    for a, b in {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}.items():
        if b in colors:
            colors[a] = colors[b]
    return colors


def _fill_color(el, theme):
    if el is None:
        return None
    s = el.find("a:solidFill/a:srgbClr", NS)
    if s is not None:
        return hex_to_rgb(s.get("val"))
    s = el.find("a:solidFill/a:schemeClr", NS)
    if s is not None:
        return theme.get(s.get("val"))
    return None


def check_pptx(path: str, display_name: str | None = None) -> FileResult:
    name = display_name or os.path.basename(path)
    res = FileResult(path=path, name=name, kind="powerpoint")
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        res.error = f"Could not open this PowerPoint file ({e})."
        return res
    with z:
        pres = _xml(z, "ppt/presentation.xml")
        if pres is None:
            res.error = "This file does not contain a PowerPoint presentation."
            return res
        prels = _rels(z, "ppt/presentation.xml")
        slide_parts = []
        for sid in pres.findall("p:sldIdLst/p:sldId", NS):
            rid = sid.get("{%s}id" % NS["r"])
            if rid in prels:
                slide_parts.append(_resolve("ppt/presentation.xml", prels[rid][0]))
        theme = _theme_colors(z)
        res.stats["slides"] = len(slide_parts)

        core = _xml(z, "docProps/core.xml")
        title = ""
        if core is not None and core.find("dc:title", NS) is not None:
            title = (core.find("dc:title", NS).text or "").strip()
        if not title:
            res.add(Issue("pptx.no-title", CHANGE, "Presentation title property is empty",
                          fix="File, Info, Properties, Title. It becomes the PDF title when you export.",
                          wcag=["2.4.2"], severity="low"))
        else:
            res.ok(f"Title is set: “{short(title, 60)}”")

        no_title, dup_titles, missing_alt, generic_alt, review_alt = [], [], [], [], []
        order_issues, tables_nohdr, media, low_contrast, vague_links, empty_boxes = [], [], [], [], [], []
        links, videos, media_parts = [], [], []
        titles_seen = {}
        lang_set = False

        for n, part in enumerate(slide_parts, 1):
            s = _xml(z, part)
            if s is None:
                continue
            srels = _rels(z, part)
            if s.find(".//a:rPr[@lang]", NS) is not None or s.find(".//a:endParaRPr[@lang]", NS) is not None:
                lang_set = True
            hidden = s.get("show") == "0"
            tree = s.find("p:cSld/p:spTree", NS)
            if tree is None:
                continue
            slide_bg = _fill_color(s.find("p:cSld/p:bg/p:bgPr", NS), theme) or theme.get("bg1") or (255, 255, 255)

            shapes = [el for el in tree if el.tag in (P + "sp", P + "pic", P + "graphicFrame", P + "grpSp", P + "cxnSp")]
            # title
            title_text = None
            for sp in tree.iter(P + "sp"):
                ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
                if ph is not None and ph.get("type") in ("title", "ctrTitle"):
                    title_text = "".join(t.text or "" for t in sp.iter(A + "t")).strip()
            label = f"Slide {n}" + (f" “{short(title_text, 40)}”" if title_text else "") + (" (hidden)" if hidden else "")
            if not title_text:
                no_title.append(f"Slide {n}")
            else:
                titles_seen.setdefault(title_text.lower(), [title_text, []])[1].append(n)

            # reading order: compare tree order to visual top to bottom, left to right
            positioned = []
            for el in shapes:
                off = el.find(".//a:off", NS)
                nv = el.find(".//p:cNvPr", NS)
                if off is None or nv is None:
                    continue
                positioned.append((int(off.get("y", 0)), int(off.get("x", 0)), nv.get("name", "")))
            if len(positioned) >= 3:
                visual = sorted(positioned, key=lambda t: (round(t[0] / 300000), t[1]))
                if [p[2] for p in visual] != [p[2] for p in positioned]:
                    order_issues.append(label)

            # pictures, charts, groups, shapes without text
            for el in tree.iter(P + "pic", P + "graphicFrame", P + "sp", P + "grpSp"):
                nv = el.find("./*/p:cNvPr", NS)
                if nv is None:
                    continue
                decorative = any(e.tag.endswith("decorative") and e.get("val") in ("1", "true") for e in nv.iter())
                descr = (nv.get("descr") or "").strip()
                kind = el.tag.split("}")[-1]
                text = "".join(t.text or "" for t in el.iter(A + "t")).strip()
                if kind == "sp":
                    if el.find("p:nvSpPr/p:nvPr/p:ph", NS) is not None:
                        if not text and el.find("p:txBody", NS) is not None and not hidden:
                            empty_boxes.append(label)
                        continue
                    if text or decorative:
                        continue
                    if el.find("p:spPr/a:prstGeom", NS) is not None and el.find("p:spPr/a:prstGeom", NS).get("prst") in ("line", "rect") and not descr:
                        continue  # simple lines and boxes are usually decorative layout
                if kind == "grpSp":
                    continue
                if kind == "graphicFrame":
                    gd = el.find(".//a:graphicData", NS)
                    uri = gd.get("uri", "") if gd is not None else ""
                    if uri.endswith("/table"):
                        tbl = gd.find("a:tbl", NS)
                        tpr = tbl.find("a:tblPr", NS) if tbl is not None else None
                        if tpr is None or tpr.get("firstRow") not in ("1", "true"):
                            tables_nohdr.append(label)
                        continue
                    what = "chart" if "chart" in uri else ("SmartArt" if "diagram" in uri else "object")
                else:
                    what = "picture" if kind == "pic" else "shape"
                # media (video/audio)
                is_media = el.find(".//a:videoFile", NS) is not None or el.find(".//a:audioFile", NS) is not None or \
                    any(x.tag.endswith("}media") for x in el.iter())
                if is_media:
                    vf = el.find(".//a:videoFile", NS)
                    tgt = ""
                    if vf is not None:
                        rid = vf.get("{%s}link" % NS["r"])
                        tgt = srels.get(rid, ("", "", ""))[0] if rid else ""
                    media.append(f"{label}: {short(tgt or descr or nv.get('name', ''), 60)}")
                    if tgt and youtube_id(tgt):
                        videos.append({"url": tgt, "title": descr, "alt": descr, "page": f"slide {n}", "context": title_text or "", "third_party_hint": False})
                blip = el.find(".//a:blip", NS)
                if blip is not None and not decorative:
                    rid = blip.get("{%s}embed" % NS["r"])
                    if rid in srels:
                        media_parts.append(_resolve(part, srels[rid][0]))
                if decorative:
                    continue
                if not descr:
                    missing_alt.append(f"{label}: {what} “{nv.get('name', '')}”")
                elif descr.lower().strip(" .") in GENERIC_ALT or looks_like_filename(descr) or re.match(r"^(a )?(picture|image|graphic) (containing|of)\b.*", descr, re.I) and len(descr) < 40:
                    generic_alt.append(f"{label}: “{short(descr, 60)}”")
                else:
                    review_alt.append(f"{label}: “{short(descr, 80)}”")

            # hyperlinks
            for h in s.iter(A + "hlinkClick"):
                rid = h.get("{%s}id" % NS["r"])
                tgt = srels.get(rid, ("", "", ""))[0] if rid else ""
                if not tgt or not tgt.startswith("http"):
                    continue
                run = h.getparent().getparent()
                txt = "".join(t.text or "" for t in run.iter(A + "t")).strip() if run is not None else ""
                links.append({"url": tgt, "text": txt, "location": f"slide {n}"})
                if txt.lower().strip(".:") in GENERIC_LINK_TEXT:
                    vague_links.append(f"{label}: “{txt}”")
                if youtube_id(tgt):
                    videos.append({"url": tgt, "title": txt, "alt": "", "page": f"slide {n}", "context": title_text or "", "third_party_hint": False})

            # contrast of runs with explicit colors
            for sp in tree.iter(P + "sp"):
                box_bg = _fill_color(sp.find("p:spPr", NS), theme) or slide_bg
                for r in sp.iter(A + "r"):
                    rpr = r.find("a:rPr", NS)
                    t = (r.findtext("a:t", namespaces=NS) or "").strip()
                    if rpr is None or not t:
                        continue
                    fg = _fill_color(rpr, theme)
                    if fg is None:
                        continue
                    size = int(rpr.get("sz")) / 100 if rpr.get("sz") else 18
                    bold = rpr.get("b") == "1"
                    ratio = contrast_ratio(fg, box_bg)
                    need = 3.0 if is_large_text(size, bold) else 4.5
                    if ratio < need:
                        low_contrast.append(f"{label}: “{short(t, 30)}” is {ratio:.1f}:1, needs {need}:1")

        dups = [f"“{short(t, 40)}” on slides {', '.join(map(str, ns))}" for t, ns in titles_seen.values() if len(ns) > 1]
        res.links = links
        res.videos = videos

        if no_title:
            res.add(Issue("pptx.no-slide-title", CHANGE, f"{len(no_title)} slide(s) have no title",
                          detail="Screen reader users move between slides by their titles.",
                          fix="Use a layout with a title placeholder (Home, Layout) and type a title. If you do not want it visible, drag the title just off the edge of the slide. Screen readers still read it.",
                          items=no_title, wcag=["2.4.6", "1.3.1"], severity="high"))
        else:
            res.ok("Every slide has a title")
        if dups:
            res.add(Issue("pptx.dup-titles", CHANGE, f"{len(dups)} slide title(s) are repeated",
                          fix="Make each title unique, for example add “(continued)” or “Part 2”.", items=dups, wcag=["2.4.6"], severity="low"))
        if missing_alt:
            res.add(Issue("pptx.alt-missing", CHANGE, f"{len(missing_alt)} picture(s) or object(s) have no alt text",
                          fix="Right click, View Alt Text, write a description or check Mark as decorative.", items=missing_alt, wcag=["1.1.1"], severity="high"))
        if generic_alt:
            res.add(Issue("pptx.alt-generic", CHANGE, f"{len(generic_alt)} picture(s) have placeholder or auto-generated alt text",
                          detail="Automatic descriptions like “A picture containing…” rarely say what matters for the lesson.",
                          fix="Right click, View Alt Text, and rewrite the description.", items=generic_alt, wcag=["1.1.1"], severity="medium"))
        if review_alt:
            res.add(Issue("pptx.alt-review", MANUAL, f"Confirm alt text describes each of {len(review_alt)} picture(s)",
                          fix="Read each description and confirm it gives what a student needs from the picture.", items=review_alt, wcag=["1.1.1"], severity="medium"))
        if order_issues:
            res.add(Issue("pptx.reading-order", MANUAL, f"{len(order_issues)} slide(s) may read in the wrong order",
                          detail="The order objects are read differs from top to bottom, left to right on screen.",
                          fix="Open Review, Check Accessibility, Reading Order Pane and drag items into the order they should be read, title first.",
                          items=order_issues, wcag=["1.3.2"], severity="medium"))
        if tables_nohdr:
            res.add(Issue("pptx.table-header", CHANGE, f"{len(tables_nohdr)} table(s) have no header row",
                          fix="Click the table, open Table Design and check Header Row.", items=tables_nohdr, wcag=["1.3.1"], severity="medium"))
        if media:
            res.add(Issue("pptx.media", MANUAL, f"{len(media)} video or audio clip(s) in the slides",
                          fix="Make sure each video has captions (Playback, Insert Captions for embedded video) and audio has a transcript.",
                          items=media, wcag=["1.2.2", "1.2.5"], severity="medium"))
        if low_contrast:
            res.add(Issue("pptx.contrast", CHANGE, f"{len(low_contrast)} text run(s) have low contrast",
                          fix="Choose a darker text color or a lighter background.", items=low_contrast[:25], wcag=["1.4.3"], severity="high"))
        if vague_links:
            res.add(Issue("pptx.link-text", CHANGE, f"{len(vague_links)} link(s) use vague text",
                          fix="Change the link text to say where it goes.", items=vague_links, wcag=["2.4.4"], severity="medium"))
        if empty_boxes:
            res.add(Issue("pptx.empty-placeholder", CHANGE, f"{len(empty_boxes)} empty text placeholder(s)",
                          detail="Empty placeholders are announced as blank boxes.", fix="Delete unused placeholders.",
                          items=sorted(set(empty_boxes)), wcag=[], severity="low"))
        if not lang_set:
            res.add(Issue("pptx.lang", CHANGE, "Language may not be set",
                          fix="Select all text on each slide, Review, Language, choose English.", wcag=["3.1.1"], severity="low"))
        res.add(Issue("pptx.check", MANUAL, "Run PowerPoint's own Accessibility Checker too",
                      fix="Review, Check Accessibility. It catches a few things only PowerPoint can see, such as animations and slide transitions.",
                      severity="low"))
        if videos:
            res.add(Issue("pptx.video-captions", MANUAL, f"Confirm captions on {len(videos)} linked video(s)",
                          fix="Play each video with captions on (CC) and confirm the captions match what is said, including math terms. For your own videos, fix any errors in YouTube Studio, Subtitles.",
                          items=[f"{v['page']}: {short(v['title'] or v['url'], 60)}" for v in videos], wcag=["1.2.2", "1.2.5"], severity="medium"))
        res.add(Issue("pptx.export", EXPORT, "Export to PDF with tags on",
                      fix="File, Export, Create PDF, Options, check “Document structure tags for accessibility.” Then run the PDF through this checker.",
                      severity="medium"))
        res.stats["_media"] = list(dict.fromkeys(media_parts))[:12]
    return res
