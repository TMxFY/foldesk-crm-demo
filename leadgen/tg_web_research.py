#!/usr/bin/env python3
"""Web-research top leads: fetch project site + X profile, write a precise
1-line 'what they do' per lead. Only for leads with a project or url."""
import argparse
import json
import re
import sys

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 10


def norm(url):
    if not url:
        return ""
    if url.startswith("www."):
        return "https://" + url
    if not url.startswith("http"):
        return "https://" + url
    return url


def fetch(url):
    url = norm(url)
    if not url:
        return "", ""
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if "text/html" not in r.headers.get("Content-Type", ""):
            return "", ""
        soup = BeautifulSoup(r.text[:300000], "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        desc = ""
        for key in ({"name": "description"}, {"property": "og:description"},
                    {"name": "twitter:description"}):
            m = soup.find("meta", attrs=key)
            if m and m.get("content"):
                desc = m["content"].strip()
                break
        # visible text fallback
        if not desc:
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            txt = " ".join(soup.get_text(" ").split())
            desc = txt[:300]
        return title[:120], desc[:240]
    except Exception:
        return "", ""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--top", type=int, default=33)
    args = p.parse_args()

    rows = [json.loads(l) for l in open(args.input) if l.strip()]
    top = rows[:args.top]

    for i, r in enumerate(top, 1):
        url = r.get("project_url") or ""
        title, desc = fetch(url) if url else ("", "")
        # clean junk descriptions
        junk = ("just a moment", "403 forbidden", "404", "attention required",
                "access denied", "enable javascript")
        if desc and any(j in desc.lower() for j in junk):
            desc = ""
        if desc and (not r.get("what_they_do") or len(r["what_they_do"]) < 25):
            r["what_they_do"] = desc
        if title and not r.get("project"):
            r["project"] = title
        r["web_title"] = title
        r["web_desc"] = desc
        print(f"[{i}/{len(top)}] {r.get('name')} | {r.get('project') or '-'} | {(desc or '(no site)')[:70]}", file=sys.stderr)

    # write back full list with enriched top
    enriched = {r["id"]: r for r in top}
    out = []
    for r in rows:
        out.append(enriched.get(r["id"], r))
    with open(args.output, "w") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Web-researched top {len(top)} -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
