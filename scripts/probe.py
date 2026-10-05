#!/usr/bin/env python3
"""Hilfsskript: lädt Seiten roh herunter, damit neue Quellen offline entwickelt werden können."""
import json, sys, urllib.request, urllib.error, hashlib, re, http.cookiejar
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
report = []
for line in Path(sys.argv[1]).read_text().splitlines():
    if not line.strip() or line.startswith("#"):
        continue
    name, url = line.split(" ", 1)
    method, body = "GET", None
    if url.startswith("POST "):
        _, url, body = url.split(" ", 2)
        method = "POST"
    req = urllib.request.Request(url, data=body.encode() if body else None, method=method,
                                 headers={"User-Agent": UA, "Accept": "*/*", "Accept-Language": "de,en;q=0.8",
                                          **({"Content-Type": "application/json"} if body else {})})
    entry = {"name": name, "url": url}
    try:
        with opener.open(req, timeout=40) as r:
            data = r.read()
            entry.update(status=r.status, final_url=r.geturl(), ctype=r.headers.get("Content-Type"), size=len(data))
    except urllib.error.HTTPError as e:
        data = e.read()
        entry.update(status=e.code, ctype=e.headers.get("Content-Type"), size=len(data))
    except Exception as e:
        data = b""
        entry.update(error=repr(e))
    ext = "zip" if data[:2] == b"PK" else "txt"
    (out / f"{name}.{ext}").write_bytes(data[:5_000_000])
    report.append(entry)
(out / "report.json").write_text(json.dumps(report, indent=1))
