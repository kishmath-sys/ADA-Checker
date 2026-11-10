"""HTML checks: axe-core in the Edge browser already on Windows (light and dark mode),
plus a few checks of our own. Falls back to a built-in static check if no browser can start."""
from __future__ import annotations

import os
import re
from pathlib import Path

from lxml import html as lhtml

from ..common import (CHANGE, MANUAL, INFO, Issue, FileResult, GENERIC_LINK_TEXT, GENERIC_ALT,
                      looks_like_filename, short, youtube_id)

AXE_PATH = Path(__file__).resolve().parent.parent / "vendor" / "axe.min.js"

FIX_HINTS = {
    "color-contrast": "Darken the text color or lighten the background until the ratio reaches 4.5:1 (3:1 for large text). In your page generator, change the color in the template once.",
    "image-alt": "Add an alt attribute that describes the image, or alt=\"\" if it is decorative.",
    "html-has-lang": "Add lang=\"en\" to the <html> tag.",
    "document-title": "Add a <title> inside <head>.",
    "label": "Give each form field a visible <label for=…> or an aria-label.",
    "link-name": "Give each link text that says where it goes.",
    "button-name": "Give each button visible text or an aria-label.",
    "frame-title": "Add a title attribute to each <iframe>, for example title=\"Video: Classroom Example 1.4.1\".",
    "heading-order": "Use heading levels in order without skipping (h2 then h3, not h2 then h4).",
    "target-size": "Make clickable targets at least 24 by 24 pixels or add space around them.",
    "list": "Only put <li> elements directly inside <ul> or <ol>.",
    "region": "Put page content inside landmarks such as <main>, <nav>, <header> and <footer>.",
    "landmark-one-main": "Wrap the main content in a single <main> element.",
    "page-has-heading-one": "Add one <h1> that names the page.",
    "meta-viewport": "Remove user-scalable=no and maximum-scale limits so students can zoom.",
}

# JavaScript run in the page to re-check contrast warnings axe could not decide
VERIFY_CONTRAST_JS = r"""
(targets) => {
  function parse(c){const m=c.match(/rgba?\(([^)]+)\)/); if(!m) return null; const p=m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1};}
  function lum(c){const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)};return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);}
  function blend(top,bot){const a=top.a;return {r:top.r*a+bot.r*(1-a),g:top.g*a+bot.g*(1-a),b:top.b*a+bot.b*(1-a),a:1};}
  const out=[];
  for (const sel of targets){
    let el; try{el=document.querySelector(sel);}catch(e){out.push({sel,state:'unknown'});continue;}
    if(!el){out.push({sel,state:'unknown'});continue;}
    const cs=getComputedStyle(el);
    if(cs.visibility==='hidden'||cs.display==='none'||el.getClientRects().length===0){out.push({sel,state:'hidden'});continue;}
    let fg=parse(cs.color); let layers=[]; let n=el; let img=false;
    while(n && n.nodeType===1){const s=getComputedStyle(n); if(s.backgroundImage && s.backgroundImage!=='none'){img=true;break;}
      const bg=parse(s.backgroundColor); if(bg && bg.a>0){layers.push(bg); if(bg.a>=1) break;} n=n.parentElement;}
    if(img){out.push({sel,state:'image'});continue;}
    let bg={r:255,g:255,b:255,a:1};
    if(!layers.length || layers[layers.length-1].a<1){const root=parse(getComputedStyle(document.documentElement).backgroundColor); if(root&&root.a>0) bg=blend(root,bg);}
    for(let i=layers.length-1;i>=0;i--) bg=blend(layers[i],bg);
    if(fg.a<1) fg=blend(fg,bg);
    const l1=lum(fg),l2=lum(bg); const ratio=(Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
    const size=parseFloat(cs.fontSize); const bold=parseInt(cs.fontWeight)>=700;
    const need=(size>=24||(bold&&size>=18.66))?3:4.5;
    out.push({sel,state: ratio>=need?'pass':'fail', ratio:Math.round(ratio*100)/100, need});
  }
  return out;
}
"""

EXTRA_JS = r"""
() => {
  const vids=[...document.querySelectorAll('iframe[src], a[href], video')].map(e=>({tag:e.tagName, url:e.src||e.href||'', title:e.title||e.innerText||e.getAttribute('aria-label')||''}));
  const videos=[...document.querySelectorAll('video')].map(v=>({src:v.currentSrc||v.src, captions:[...v.querySelectorAll('track')].some(t=>['captions','subtitles'].includes((t.kind||'').toLowerCase()))}));
  const audio=document.querySelectorAll('audio').length;
  const math=document.querySelectorAll('math').length;
  const mathImgs=[...document.images].filter(i=>/latex|codecogs|equation|mathtex|formula/i.test(i.src+' '+i.alt)).map(i=>i.alt||i.src.slice(-40));
  const interactive=document.querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"]), details').length;
  const links=[...document.querySelectorAll('a[href]')].filter(a=>/^https?:/i.test(a.href)).map(a=>({url:a.href, text:(a.innerText||a.getAttribute('aria-label')||'').trim()}));
  const sticky=[...document.querySelectorAll('body *')].filter(e=>{const p=getComputedStyle(e).position; return p==='fixed'||p==='sticky';}).length;
  const headings=document.querySelectorAll('h1,h2,h3,h4,h5,h6').length;
  return {vids, videos, audio, math, mathImgs, interactive, links, sticky, headings, title:document.title, lang:document.documentElement.lang};
}
"""


def _criteria(tags):
    out = []
    for t in tags:
        m = re.fullmatch(r"wcag(\d)(\d)(\d+)", t)
        if m:
            out.append(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
    return out


def _launch(pw):
    """Use Microsoft Edge (always on Windows 11), then Chrome, then a bundled Chromium."""
    errors = []
    for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
        try:
            return pw.chromium.launch(headless=True, **kw)
        except Exception as e:  # noqa
            errors.append(str(e).splitlines()[0])
    raise RuntimeError("; ".join(errors))


def check_html(path: str, display_name: str | None = None) -> FileResult:
    name = display_name or os.path.basename(path)
    res = FileResult(path=path, name=name, kind="html")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                _browser_checks(browser, path, res)
            finally:
                browser.close()
        return res
    except Exception as e:  # noqa
        res.issues.clear()
        res.passed.clear()
        res.stats["engine"] = "built-in checks (browser not available)"
        res.stats["browser note"] = short(str(e), 120)
        _static_checks(path, res)
        return res


def _browser_checks(browser, path, res: FileResult):
    axe = AXE_PATH.read_text(encoding="utf-8")
    url = Path(path).resolve().as_uri()
    res.stats["engine"] = "axe-core in Edge, light and dark mode"
    combined = {}       # rule id -> dict
    incomplete = {}
    extra = None
    for scheme in ("light", "dark"):
        ctx = browser.new_context(color_scheme=scheme, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="load", timeout=45000)
        except Exception:  # noqa
            pass
        page.wait_for_timeout(1200)
        page.add_script_tag(content=axe)
        result = page.evaluate("""axe.run(document, {runOnly:{type:'tag', values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22a','wcag22aa','best-practice']}, resultTypes:['violations','incomplete']})""")
        # verify contrast warnings axe could not decide
        cc = [v for v in result["incomplete"] if v["id"] == "color-contrast"]
        verified_fail = []
        if cc:
            targets = [n["target"][0] for n in cc[0]["nodes"] if n.get("target") and isinstance(n["target"][0], str)]
            checks = page.evaluate(VERIFY_CONTRAST_JS, targets)
            fail_sel = {c["sel"]: c for c in checks if c["state"] == "fail"}
            unknown_sel = {c["sel"] for c in checks if c["state"] in ("image", "unknown")}
            keep_nodes = [n for n in cc[0]["nodes"] if n["target"][0] in unknown_sel]
            fail_nodes = [dict(n, failureSummary=f"Measured {fail_sel[n['target'][0]]['ratio']}:1, needs {fail_sel[n['target'][0]]['need']}:1") for n in cc[0]["nodes"] if n["target"][0] in fail_sel]
            res.stats.setdefault("contrast warnings cleared", 0)
            res.stats["contrast warnings cleared"] += len(cc[0]["nodes"]) - len(keep_nodes) - len(fail_nodes)
            cc[0]["nodes"] = keep_nodes
            if fail_nodes:
                verified_fail.append(dict(cc[0], nodes=fail_nodes))
            if not keep_nodes:
                result["incomplete"] = [v for v in result["incomplete"] if v["id"] != "color-contrast"]
        for v in result["violations"] + verified_fail:
            key = v["id"]
            entry = combined.setdefault(key, {"v": v, "nodes": [], "schemes": set()})
            entry["schemes"].add(scheme)
            for n in v["nodes"]:
                if n["html"] not in [x["html"] for x in entry["nodes"]]:
                    entry["nodes"].append(n)
        for v in result["incomplete"]:
            entry = incomplete.setdefault(v["id"], {"v": v, "nodes": [], "schemes": set()})
            entry["schemes"].add(scheme)
            for n in v["nodes"]:
                if n["html"] not in [x["html"] for x in entry["nodes"]]:
                    entry["nodes"].append(n)
        if extra is None:
            extra = page.evaluate(EXTRA_JS)
        ctx.close()

    for key, e in combined.items():
        v = e["v"]
        crit = _criteria(v["tags"])
        mode = "" if len(e["schemes"]) == 2 else f" (in {next(iter(e['schemes']))} mode only)"
        def reason(n):
            lines = [l.strip() for l in (n.get("failureSummary") or "").splitlines() if l.strip()]
            lines = [l for l in lines if not l.lower().startswith(("fix any", "fix all"))]
            return f"  ({short(lines[0], 90)})" if lines else ""
        items = [f"{short(n['html'], 110)}{reason(n)}" for n in e["nodes"][:20]]
        sev = {"critical": "high", "serious": "high", "moderate": "medium", "minor": "low"}.get(v.get("impact") or "", "medium")
        res.add(Issue(f"html.{key}", CHANGE, f"{v['help']}{mode}",
                      detail=f"{len(e['nodes'])} element(s). {v.get('description', '')}",
                      fix=FIX_HINTS.get(key, f"See {v.get('helpUrl', '')}"), items=items,
                      wcag=crit, severity=sev, key=key))
    for key, e in incomplete.items():
        if key in ("frame-tested", "video-caption", "audio-caption"):
            continue  # covered by our own video checks, or just engine notes
        v = e["v"]
        crit = _criteria(v["tags"])
        items = [short(n["html"], 110) for n in e["nodes"][:15]]
        res.add(Issue(f"html.review.{key}", MANUAL, f"Review: {v['help']}",
                      detail=f"The checker could not decide for {len(e['nodes'])} element(s). {v.get('description', '')}",
                      fix=FIX_HINTS.get(key, f"See {v.get('helpUrl', '')}"), items=items, wcag=crit, severity="low", key=key))
    if not combined:
        res.ok("Passed every automated WCAG 2.2 AA check in light and dark mode")

    # our own checks
    x = extra or {}
    if x.get("title"):
        res.ok(f"Page title: “{short(x['title'], 60)}”")
    if x.get("lang"):
        res.ok(f"Language is set ({x['lang']})")
    res.stats["headings"] = x.get("headings", 0)
    if x.get("math"):
        res.ok(f"{x['math']} equation(s) use MathML")
        res.stats["MathML equations"] = x["math"]
    if x.get("mathImgs"):
        res.add(Issue("html.math-images", MANUAL, f"{len(x['mathImgs'])} image(s) look like pictures of equations",
                      fix="Use MathML or text based math instead of images, or make sure the alt text reads the equation aloud.",
                      items=[short(a, 80) for a in x["mathImgs"][:15]], wcag=["1.1.1", "1.4.5"], severity="medium"))
    for v in x.get("videos", []):
        if not v["captions"]:
            res.add(Issue("html.video-no-track", CHANGE, "A video has no captions track",
                          fix="Add <track kind=\"captions\" src=\"….vtt\" srclang=\"en\" label=\"English\"> inside the <video> element.",
                          items=[short(v["src"], 80)], wcag=["1.2.2"], severity="high", key=v["src"]))
    if x.get("audio"):
        res.add(Issue("html.audio", MANUAL, f"{x['audio']} audio player(s) found", fix="Provide a transcript next to each audio clip.",
                      wcag=["1.2.3"], severity="medium"))
    seen = set()
    for v in x.get("vids", []):
        vid = youtube_id(v["url"])
        if vid and vid not in seen:
            seen.add(vid)
            res.videos.append({"url": v["url"], "title": short(v["title"], 80), "alt": "", "page": "", "context": "", "third_party_hint": False})
    if res.videos:
        res.add(Issue("html.video-captions", MANUAL, f"Confirm captions on {len(res.videos)} linked video(s)",
                      fix="Play each video with captions on (CC) and confirm the captions match what is said, including math terms. For your own videos, fix any errors in YouTube Studio, Subtitles.",
                      items=[short(v["title"] or v["url"], 80) for v in res.videos], wcag=["1.2.2", "1.2.5"], severity="medium"))
    res.links = [{"url": l["url"], "text": l["text"], "location": ""} for l in x.get("links", [])]
    vague = [f"“{l['text']}” → {short(l['url'], 60)}" for l in x.get("links", []) if l["text"].lower().strip(".:") in GENERIC_LINK_TEXT]
    if vague:
        res.add(Issue("html.vague-links", CHANGE, f"{len(vague)} link(s) use vague text like “click here”", fix=FIX_HINTS["link-name"],
                      items=vague[:20], wcag=["2.4.4"], severity="medium"))
    if x.get("interactive"):
        res.add(Issue("html.keyboard", MANUAL, "Keyboard test the interactive parts",
                      detail=f"This page has {x['interactive']} interactive control(s). Automated checks cannot confirm everything works without a mouse."
                             + (" It also has fixed or sticky elements that could cover the focused item." if x.get("sticky") else ""),
                      fix="Press Tab through the whole page. Every button and field should be reachable, show a visible focus outline, never be hidden behind a sticky bar, and work with Enter or Space.",
                      wcag=["2.1.1", "2.4.7", "2.4.11"], severity="medium"))
    res.add(Issue("html.zoom", MANUAL, "Zoom to 200% and 400%",
                  fix="Press Ctrl and + until the browser shows 200%, then 400%. Text should get larger without being cut off, and you should not need to scroll sideways to read a line.",
                  wcag=["1.4.4", "1.4.10"], severity="low"))


def _static_checks(path, res: FileResult):
    raw = Path(path).read_bytes()
    try:
        doc = lhtml.fromstring(raw)
    except Exception as e:  # noqa
        res.error = f"Could not read this HTML file ({e})."
        return
    htmlel = doc if doc.tag == "html" else doc.getroottree().getroot()
    if not (htmlel.get("lang") or "").strip():
        res.add(Issue("html.html-has-lang", CHANGE, "The page language is not set", fix=FIX_HINTS["html-has-lang"], wcag=["3.1.1"], severity="medium"))
    else:
        res.ok(f"Language is set ({htmlel.get('lang')})")
    title = doc.findtext(".//title")
    if not (title or "").strip():
        res.add(Issue("html.document-title", CHANGE, "The page has no title", fix=FIX_HINTS["document-title"], wcag=["2.4.2"], severity="medium"))
    else:
        res.ok(f"Page title: “{short(title, 60)}”")
    noalt = [short(lhtml.tostring(i, encoding="unicode"), 100) for i in doc.iter("img") if i.get("alt") is None and i.get("role") not in ("presentation", "none")]
    if noalt:
        res.add(Issue("html.image-alt", CHANGE, f"{len(noalt)} image(s) have no alt attribute", fix=FIX_HINTS["image-alt"], items=noalt[:20], wcag=["1.1.1"], severity="high"))
    badalt = [i.get("alt") for i in doc.iter("img") if i.get("alt") and (i.get("alt").lower().strip() in GENERIC_ALT or looks_like_filename(i.get("alt")))]
    if badalt:
        res.add(Issue("html.alt-generic", CHANGE, f"{len(badalt)} image(s) have placeholder alt text", fix="Replace with a real description.", items=badalt[:20], wcag=["1.1.1"], severity="high"))
    heads = [int(h.tag[1]) for h in doc.iter("h1", "h2", "h3", "h4", "h5", "h6")]
    res.stats["headings"] = len(heads)
    if not heads:
        res.add(Issue("html.no-headings", CHANGE, "No headings on the page", fix="Use <h1> for the page title and <h2>/<h3> for sections.", wcag=["1.3.1", "2.4.6"], severity="high"))
    else:
        skips = [f"h{heads[i]} after h{heads[i-1]}" for i in range(1, len(heads)) if heads[i] > heads[i - 1] + 1]
        if skips:
            res.add(Issue("html.heading-order", CHANGE, "Heading levels skip a level", fix=FIX_HINTS["heading-order"], items=skips[:20], wcag=["1.3.1"], severity="low"))
    frames = [short(f.get("src", ""), 80) for f in doc.iter("iframe") if not (f.get("title") or "").strip()]
    if frames:
        res.add(Issue("html.frame-title", CHANGE, f"{len(frames)} embedded frame(s) have no title", fix=FIX_HINTS["frame-title"], items=frames, wcag=["4.1.2"], severity="medium"))
    vague = [f"“{a.text_content().strip()}” → {short(a.get('href'), 60)}" for a in doc.iter("a") if a.text_content().strip().lower() in GENERIC_LINK_TEXT]
    if vague:
        res.add(Issue("html.link-name", CHANGE, f"{len(vague)} link(s) use vague text", fix=FIX_HINTS["link-name"], items=vague[:20], wcag=["2.4.4"], severity="medium"))
    labels = {l.get("for") for l in doc.iter("label")}
    unl = []
    for inp in doc.iter("input", "select", "textarea"):
        if inp.get("type") in ("hidden", "submit", "button", "reset", "image"):
            continue
        if inp.get("id") in labels or inp.get("aria-label") or inp.get("aria-labelledby") or inp.getparent().tag == "label" or inp.get("title"):
            continue
        unl.append(short(lhtml.tostring(inp, encoding="unicode"), 100))
    if unl:
        res.add(Issue("html.label", CHANGE, f"{len(unl)} form field(s) have no label", fix=FIX_HINTS["label"], items=unl[:20], wcag=["1.3.1", "4.1.2"], severity="high"))
    vp = doc.find(".//meta[@name='viewport']")
    if vp is not None and re.search(r"user-scalable\s*=\s*no|maximum-scale\s*=\s*1(\.0)?\b", vp.get("content", "")):
        res.add(Issue("html.meta-viewport", CHANGE, "The page blocks zooming", fix=FIX_HINTS["meta-viewport"], wcag=["1.4.4"], severity="high"))
    for t in doc.iter("table"):
        if t.find(".//th") is None and len(t.findall(".//tr")) > 1:
            res.add(Issue("html.table-th", CHANGE, "A data table has no header cells", fix="Use <th scope=\"col\"> for the header row.",
                          items=[short(t.text_content(), 60)], wcag=["1.3.1"], severity="medium", key=short(t.text_content(), 40)))
    res.stats["math"] = len(list(doc.iter("{http://www.w3.org/1998/Math/MathML}math"))) + len(list(doc.iter("math")))
    seen = set()
    for el in list(doc.iter("iframe")) + list(doc.iter("a")):
        u = el.get("src") or el.get("href") or ""
        vid = youtube_id(u)
        if vid and vid not in seen:
            seen.add(vid)
            res.videos.append({"url": u, "title": el.get("title") or el.text_content().strip(), "alt": "", "page": "", "context": "", "third_party_hint": False})
    if res.videos:
        res.add(Issue("html.video-captions", MANUAL, f"Confirm captions on {len(res.videos)} linked video(s)",
                      fix="Play each video with captions on (CC) and confirm the captions match what is said, including math terms. For your own videos, fix any errors in YouTube Studio, Subtitles.",
                      items=[short(v["title"] or v["url"], 80) for v in res.videos], wcag=["1.2.2", "1.2.5"], severity="medium"))
    res.links = [{"url": a.get("href"), "text": a.text_content().strip(), "location": ""} for a in doc.iter("a") if (a.get("href") or "").startswith("http")]
    res.add(Issue("html.contrast-manual", MANUAL, "Check color contrast",
                  detail="Edge could not be started, so color contrast and dark mode were not measured.",
                  fix="Install or repair Microsoft Edge, then scan again for a full check.", wcag=["1.4.3"], severity="medium"))
