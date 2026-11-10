"""Routes each file to the right checker and adds the shared extras."""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from .common import CHANGE, MANUAL, EXPORT, INFO, Issue, FileResult, short, youtube_id
from .checks.docx_check import check_docx
from .checks.pdf_check import check_pdf
from .checks.html_check import check_html
from .checks.pptx_check import check_pptx
from .checks.xlsx_check import check_xlsx
from .checks import colorblind
from .checks.links import check_urls

SUPPORTED = {
    ".docx": "word", ".docm": "word",
    ".pdf": "pdf",
    ".html": "html", ".htm": "html",
    ".pptx": "powerpoint", ".pptm": "powerpoint",
    ".xlsx": "excel", ".xlsm": "excel",
    ".zip": "zip",
}
OLD_FORMATS = {".doc": "Word", ".ppt": "PowerPoint", ".xls": "Excel", ".rtf": "Word"}

MAX_ZIP_FILES = 2000
MAX_ZIP_BYTES = 2 * 1024 ** 3


def is_supported(path: str) -> bool:
    ext = Path(path).suffix.lower()
    name = Path(path).name
    return (ext in SUPPORTED or ext in OLD_FORMATS) and not name.startswith("~$")


def check_file(path: str, display_name: str | None = None, options: dict | None = None,
               sibling_sources: dict | None = None, progress=None) -> FileResult:
    options = options or {}
    ext = Path(path).suffix.lower()
    name = display_name or Path(path).name
    if ext in OLD_FORMATS:
        res = FileResult(path=path, name=name, kind=OLD_FORMATS[ext].lower())
        res.error = (f"This is an older {OLD_FORMATS[ext]} format ({ext}) that cannot be checked. "
                     f"Open it in {OLD_FORMATS[ext]}, choose File, Save As, and save as the modern format "
                     f"({'.docx' if OLD_FORMATS[ext] == 'Word' else '.pptx' if OLD_FORMATS[ext] == 'PowerPoint' else '.xlsx'}), then check that copy.")
        return res
    kind = SUPPORTED.get(ext)
    try:
        if kind == "word":
            res = check_docx(path, name)
        elif kind == "pdf":
            src = None
            if sibling_sources:
                src = sibling_sources.get(Path(path).stem.lower())
            res = check_pdf(path, name, source_hint=src)
            if src:
                res.add(Issue("pdf.word-source", INFO, "Fix this PDF in its Word file",
                              detail=f"A Word file with the same name was found: {src}. Make the changes there and export the PDF again; editing the PDF directly is harder and gets lost on the next export.",
                              severity="low"))
        elif kind == "html":
            res = check_html(path, name)
        elif kind == "powerpoint":
            res = check_pptx(path, name)
        elif kind == "excel":
            res = check_xlsx(path, name)
        elif kind == "zip":
            return check_zip(path, name, options, progress)
        else:
            res = FileResult(path=path, name=name, kind="other")
            res.error = "This file type is not supported."
            return res
    except Exception as e:  # noqa
        res = FileResult(path=path, name=name, kind=kind or "other")
        res.error = f"The checker hit a problem reading this file: {short(str(e), 200)}"
        return res

    _extras(res, path, kind, options)
    return res


def _extras(res: FileResult, path: str, kind: str, options: dict):
    # color blindness previews for graphs and pictures
    if options.get("colorblind", True) and not res.error:
        media = res.stats.get("_media") or []
        if kind in ("word", "powerpoint", "excel") and media:
            res.previews = colorblind.previews_from_office(path, media)
        elif kind == "pdf":
            res.previews = colorblind.previews_from_pdf(path)
    res.stats.pop("_media", None)
    res.stats.pop("_images", None)

    # broken links
    if options.get("check_links") and res.links:
        urls = [l["url"] for l in res.links if not youtube_id(l["url"])]
        results = check_urls(urls)
        broken = [f"{short(u, 80)}: {note}" + (f" ({next((l['location'] for l in res.links if l['url'] == u and l['location']), '')})" if any(l['url'] == u and l['location'] for l in res.links) else "")
                  for u, (state, note) in results.items() if state == "broken"]
        warn = [f"{short(u, 80)}: {note}" for u, (state, note) in results.items() if state in ("warn", "error")]
        res.stats["links checked online"] = len(results)
        if broken:
            res.add(Issue("links.broken", CHANGE, f"{len(broken)} broken link(s)",
                          detail="These links lead to a page that no longer exists or a site that could not be reached.",
                          fix="Update or remove each link.", items=broken, wcag=["2.4.4"], severity="medium"))
        elif results:
            res.ok(f"All {len(results)} web link(s) work")
        if warn:
            res.add(Issue("links.warn", MANUAL, f"{len(warn)} link(s) could not be confirmed",
                          detail="The site was slow, refused automated checks, or had an error. They may still work.",
                          fix="Click each one to confirm it opens.", items=warn, wcag=["2.4.4"], severity="low"))


def _safe_extract(zf: zipfile.ZipFile, dest: str):
    total = 0
    count = 0
    base = os.path.realpath(dest)
    for info in zf.infolist():
        if info.is_dir():
            continue
        count += 1
        total += info.file_size
        if count > MAX_ZIP_FILES or total > MAX_ZIP_BYTES:
            raise ValueError("The zip file is too large to check (over 2,000 files or 2 GB).")
        target = os.path.realpath(os.path.join(dest, info.filename))
        if not target.startswith(base + os.sep):
            continue  # skip unsafe paths
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def check_zip(path: str, name: str, options: dict, progress=None) -> FileResult:
    res = FileResult(path=path, name=name, kind="zip")
    tmp = tempfile.mkdtemp(prefix="adacheck_")
    try:
        with zipfile.ZipFile(path) as zf:
            is_course = "imsmanifest.xml" in zf.namelist()
            _safe_extract(zf, tmp)
        res.stats["type"] = "Brightspace course export" if is_course else "zip file"
        files = [os.path.join(dp, f) for dp, _, fs in os.walk(tmp) for f in fs if is_supported(f) and not f.lower().endswith(".zip")]
        files.sort()
        sources = {Path(f).stem.lower(): os.path.relpath(f, tmp) for f in files if Path(f).suffix.lower() in (".docx", ".docm")}
        res.stats["files checked"] = len(files)
        for i, f in enumerate(files, 1):
            rel = os.path.relpath(f, tmp).replace(os.sep, "/")
            if progress:
                progress(f"{name} ▸ {rel}", i, len(files))
            child = check_file(f, rel, options, sources)
            child.path = f"{path}::{rel}"
            res.children.append(child)
        if not files:
            res.error = "No Word, PDF, HTML, PowerPoint or Excel files were found inside this zip."
    except zipfile.BadZipFile:
        res.error = "This zip file is damaged and could not be opened."
    except Exception as e:  # noqa
        res.error = short(str(e), 200)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def status_of(result: dict, marks) -> str:
    """'error', 'needs work', 'needs review' or 'pass'. marks(path) returns (ignored, reviewed) fingerprint sets."""
    if result.get("error"):
        return "error"
    if result.get("kind") == "zip":
        sts = [status_of(c, marks) for c in result.get("children", [])]
        for s in ("needs work", "error", "needs review"):
            if s in sts:
                return s
        return "pass"
    c = counts_of(result, marks)
    if c["change"]:
        return "needs work"
    if c["manual"]:
        return "needs review"
    return "pass"


def counts_of(result: dict, marks) -> dict:
    if result.get("kind") == "zip":
        tot = {"change": 0, "manual": 0, "export": 0}
        for c in result.get("children", []):
            for k, v in counts_of(c, marks).items():
                tot[k] += v
        return tot
    ignored, reviewed = marks(result["path"])
    iss = result.get("issues", [])
    return {
        "change": sum(1 for i in iss if i["group"] == CHANGE and i["fingerprint"] not in ignored),
        "manual": sum(1 for i in iss if i["group"] == MANUAL and i["fingerprint"] not in ignored and i["fingerprint"] not in reviewed),
        "export": sum(1 for i in iss if i["group"] == EXPORT and i["fingerprint"] not in reviewed and i["fingerprint"] not in ignored),
    }
