"""Color blindness previews for images found in documents (graphs, diagrams)."""
from __future__ import annotations

import base64
import io
import zipfile

from PIL import Image

# Viénot, Brettel & Mollon (1999) simulation matrices in linear RGB
MATRICES = {
    "Red-blind (protanopia)": ((0.11238, 0.88762, 0.0), (0.11238, 0.88762, 0.0), (0.00401, -0.00401, 1.0)),
    "Green-blind (deuteranopia)": ((0.29275, 0.70725, 0.0), (0.29275, 0.70725, 0.0), (-0.02234, 0.02234, 1.0)),
    "Blue-blind (tritanopia)": ((1.0, 0.14461, -0.14461), (0.0, 0.85924, 0.14076), (0.0, 0.85924, 0.14076)),
}

_TO_LIN = [((i / 255) / 12.92) if (i / 255) <= 0.04045 else (((i / 255) + 0.055) / 1.055) ** 2.4 for i in range(256)]


def _to_srgb(v: float) -> int:
    v = min(max(v, 0.0), 1.0)
    v = 12.92 * v if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055
    return int(round(v * 255))


def _colorful(img: Image.Image) -> bool:
    small = img.convert("RGB").resize((48, 48))
    sat = 0
    for r, g, b in small.getdata():
        if max(r, g, b) - min(r, g, b) > 40:
            sat += 1
    return sat > 48 * 48 * 0.03


def _simulate(img: Image.Image, m) -> Image.Image:
    img = img.convert("RGB")
    out = Image.new("RGB", img.size)
    px = img.load()
    po = out.load()
    cache = {}
    for y in range(img.size[1]):
        for x in range(img.size[0]):
            c = px[x, y]
            v = cache.get(c)
            if v is None:
                r, g, b = _TO_LIN[c[0]], _TO_LIN[c[1]], _TO_LIN[c[2]]
                v = tuple(_to_srgb(row[0] * r + row[1] * g + row[2] * b) for row in m)
                cache[c] = v
            po[x, y] = v
    return out


def _b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def preview(img_bytes: bytes, label: str):
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img.load()
    except Exception:  # noqa
        return None
    if img.width < 40 or img.height < 40:
        return None
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    if not _colorful(img):
        return None
    img.thumbnail((260, 260))
    views = [{"name": "Original", "src": _b64(img)}]
    for name, m in MATRICES.items():
        views.append({"name": name, "src": _b64(_simulate(img, m))})
    return {"label": label, "views": views}


def previews_from_office(path: str, media_paths: list[str], limit: int = 6):
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for mp in media_paths:
                if mp in names and len(out) < limit:
                    p = preview(z.read(mp), mp.split("/")[-1])
                    if p:
                        out.append(p)
    except Exception:  # noqa
        pass
    return out


def previews_from_pdf(path: str, limit: int = 6):
    out = []
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        doc = pymupdf.open(path)
        seen = set()
        for pno, page in enumerate(doc, 1):
            # skip thumbnails that are links to videos
            video_rects = [l["from"] for l in page.get_links()
                           if any(k in (l.get("uri") or "") for k in ("youtube.com", "youtu.be", "vimeo.com"))]
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen or len(out) >= limit:
                    continue
                seen.add(xref)
                try:
                    rects = page.get_image_rects(xref)
                except Exception:  # noqa
                    rects = []
                if any(r.intersects(v) for r in rects for v in video_rects):
                    continue
                data = doc.extract_image(xref)
                if data and data.get("width", 0) >= 80:
                    p = preview(data["image"], f"Image on page {pno}")
                    if p:
                        out.append(p)
        doc.close()
    except Exception:  # noqa
        pass
    return out
