"""Shared building blocks: the Issue model, WCAG reference list and color math."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict

CHECKER_VERSION = "1.1.0"

# Issue groups shown in the report
CHANGE = "change"      # something to fix in the file
MANUAL = "manual"      # needs a person to judge
EXPORT = "export"      # checklist for after exporting Word to PDF
INFO = "info"          # good to know, not a problem

# WCAG success criteria used by the checks. Value: (name, level, version added)
WCAG = {
    "1.1.1": ("Non-text Content", "A", "2.0"),
    "1.2.2": ("Captions (Prerecorded)", "A", "2.0"),
    "1.2.3": ("Audio Description or Media Alternative", "A", "2.0"),
    "1.2.5": ("Audio Description (Prerecorded)", "AA", "2.0"),
    "1.3.1": ("Info and Relationships", "A", "2.0"),
    "1.3.2": ("Meaningful Sequence", "A", "2.0"),
    "1.4.1": ("Use of Color", "A", "2.0"),
    "1.4.3": ("Contrast (Minimum)", "AA", "2.0"),
    "1.4.5": ("Images of Text", "AA", "2.0"),
    "1.4.11": ("Non-text Contrast", "AA", "2.1"),
    "1.4.4": ("Resize Text", "AA", "2.0"),
    "1.4.10": ("Reflow", "AA", "2.1"),
    "2.1.1": ("Keyboard", "A", "2.0"),
    "2.3.1": ("Three Flashes or Below Threshold", "A", "2.0"),
    "2.4.1": ("Bypass Blocks", "A", "2.0"),
    "2.4.2": ("Page Titled", "A", "2.0"),
    "2.4.3": ("Focus Order", "A", "2.0"),
    "2.4.4": ("Link Purpose (In Context)", "A", "2.0"),
    "2.4.5": ("Multiple Ways", "AA", "2.0"),
    "2.4.6": ("Headings and Labels", "AA", "2.0"),
    "2.4.7": ("Focus Visible", "AA", "2.0"),
    "2.4.11": ("Focus Not Obscured (Minimum)", "AA", "2.2"),
    "2.5.3": ("Label in Name", "A", "2.1"),
    "3.3.2": ("Labels or Instructions", "A", "2.0"),
    "3.3.7": ("Redundant Entry", "A", "2.2"),
    "3.3.8": ("Accessible Authentication (Minimum)", "AA", "2.2"),
    "2.5.8": ("Target Size (Minimum)", "AA", "2.2"),
    "3.1.1": ("Language of Page", "A", "2.0"),
    "4.1.2": ("Name, Role, Value", "A", "2.0"),
}


def wcag_label(code: str) -> str:
    """Readable label such as 'WCAG 1.3.1 Info and Relationships (A)'."""
    if code in WCAG:
        name, level, _ = WCAG[code]
        return f"WCAG {code} {name} ({level})"
    return code


def wcag_version(codes: list[str]) -> str:
    """Which standard an issue belongs to: 2.1 (legally required now), 2.2, or best practice."""
    versions = [WCAG[c][2] for c in codes if c in WCAG]
    if not versions:
        if any(c.startswith("PDF/UA") for c in codes):
            return "PDF/UA"
        return "Best practice"
    if all(v == "2.2" for v in versions):
        return "WCAG 2.2"
    return "WCAG 2.1"


@dataclass
class Issue:
    check: str                 # short id of the check, e.g. "docx.no-headings"
    group: str                 # CHANGE, MANUAL, EXPORT, INFO
    title: str                 # one line summary
    detail: str = ""           # what was found
    fix: str = ""              # how to fix, written for the app the file came from
    location: str = ""         # where in the file
    wcag: list[str] = field(default_factory=list)
    severity: str = "medium"   # high, medium, low
    items: list[str] = field(default_factory=list)  # individual places, for grouped issues
    key: str = ""              # extra text that makes the fingerprint unique
    standard: str = ""
    fingerprint: str = ""

    def finish(self, file_key: str = "") -> "Issue":
        self.standard = wcag_version(self.wcag)
        raw = "|".join([self.check, self.location, self.key or self.detail[:120]])
        self.fingerprint = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["wcag_labels"] = [wcag_label(c) for c in self.wcag]
        return d


@dataclass
class FileResult:
    path: str
    name: str
    kind: str                                  # word, pdf, html, powerpoint, excel, zip
    issues: list[Issue] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    videos: list[dict] = field(default_factory=list)   # {"url", "context"}
    links: list[dict] = field(default_factory=list)    # {"url", "text", "location"}
    previews: list[dict] = field(default_factory=list) # color blindness previews
    error: str = ""
    children: list["FileResult"] = field(default_factory=list)  # files inside a zip

    def add(self, issue: Issue):
        self.issues.append(issue.finish())

    def ok(self, text: str):
        self.passed.append(text)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "name": self.name, "kind": self.kind,
            "issues": [i.to_dict() for i in self.issues],
            "passed": self.passed, "stats": self.stats,
            "videos": self.videos, "links": self.links,
            "previews": self.previews, "error": self.error,
            "children": [c.to_dict() for c in self.children],
        }


# ---------------------------------------------------------------- color math

def hex_to_rgb(h: str):
    h = (h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return None
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def luminance(rgb) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(fg, bg) -> float:
    l1, l2 = luminance(fg), luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def is_large_text(size_pt: float | None, bold: bool) -> bool:
    if size_pt is None:
        return False
    return size_pt >= 18 or (bold and size_pt >= 14)


def tint(rgb, t: float):
    """Word theme tint (lighten toward white)."""
    return tuple(round(c + (255 - c) * t) for c in rgb)


def shade(rgb, s: float):
    """Word theme shade (darken toward black)."""
    return tuple(round(c * s) for c in rgb)


# ---------------------------------------------------------------- text helpers

GENERIC_LINK_TEXT = {
    "click here", "here", "click", "link", "this link", "more", "read more",
    "learn more", "this", "go", "website", "web site", "page", "click this link",
}

GENERIC_ALT = {
    "image", "picture", "photo", "graphic", "img", "figure", "chart",
    "screenshot", "screen shot", "logo", "icon", "diagram",
}

STOPWORDS = set("""a an the and or of to in on for with by at from is are was were be this that these those it its
as how what why when video videos link title titled called your our you we use using into about via not instructor's
instructors instructor youtube example examples review section""".split())


def content_words(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z']+", (text or "").lower())
    out = set()
    for w in words:
        w = w.strip("'")
        if w in STOPWORDS or len(w) < 3:
            continue
        # light stemming so factor/factoring/factors match
        for suf in ("ing", "ers", "er", "es", "s", "ed"):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def looks_like_filename(text: str) -> bool:
    return bool(re.search(r"\.(png|jpe?g|gif|bmp|tiff?|svg|webp|emf|wmf)\s*$", text or "", re.I)) or \
        bool(re.fullmatch(r"(img|image|dsc|pic|screenshot)[\s_-]*\d+.*", (text or "").strip(), re.I))


SECTION_RE = re.compile(r"\b(Section|Chapter|Module|Unit|Lesson)\s*(\d+(?:\.\d+)*)", re.I)


def section_number(text: str):
    m = SECTION_RE.search(text or "")
    if m:
        return m.group(1).title(), m.group(2)
    m = re.search(r"\b(\d+\.\d+)\b", text or "")
    return (None, m.group(1)) if m else (None, None)


def short(text: str, n: int = 70) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str):
    m = YOUTUBE_RE.search(url or "")
    return m.group(1) if m else None
