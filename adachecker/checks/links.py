"""Online link checker: finds links that are broken (page not found or site gone)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 ADA-Checker"}


def _check(url: str):
    try:
        r = requests.head(url, headers=HEADERS, timeout=8, allow_redirects=True)
        if r.status_code in (405, 403, 400, 501) or r.status_code >= 500:
            r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True, stream=True)
            r.close()
        code = r.status_code
        if code in (404, 410):
            return url, "broken", f"page not found ({code})"
        if code >= 500:
            return url, "error", f"site error ({code})"
        return url, "ok", str(code)
    except requests.exceptions.SSLError:
        return url, "warn", "security certificate problem"
    except requests.exceptions.ConnectionError:
        return url, "broken", "site could not be reached"
    except requests.exceptions.Timeout:
        return url, "warn", "took too long to respond"
    except Exception as e:  # noqa
        return url, "warn", str(e)[:80]


def check_urls(urls, workers: int = 8):
    unique = list(dict.fromkeys(u for u in urls if u and u.startswith(("http://", "https://"))))
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return {u: (state, note) for u, state, note in ex.map(_check, unique)}
