"""Tiny HTTP helpers on the standard library."""
from __future__ import annotations

import gzip
import http.client
import json
import time
import urllib.error
import urllib.request
import zlib
from typing import Any

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, url: str, detail: str = ""):
        super().__init__(f"HTTP {status or 'error'} for {url} {detail}".strip())
        self.status = status
        self.url = url


def request(url: str, *, data: Any = None, headers: dict | None = None,
            timeout: float = 30, retries: int = 2) -> tuple[int, bytes]:
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip", **(headers or {})}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_STATUS and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise HttpError(exc.code, url) from None
        except (OSError, http.client.HTTPException, EOFError, zlib.error) as exc:
            # no connection, a timeout, a bad certificate, a reply cut short, or a body that isn't the gzip it claims to be
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise HttpError(0, url, str(exc)) from None
    raise HttpError(0, url)


def get_json(url: str, **kw) -> Any:
    return json.loads(request(url, **kw)[1])


def post_json(url: str, data: Any, **kw) -> Any:
    return json.loads(request(url, data=data, **kw)[1])


def get_text(url: str, **kw) -> str:
    return request(url, **kw)[1].decode("utf-8", "replace")
