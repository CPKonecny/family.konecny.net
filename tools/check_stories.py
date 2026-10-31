#!/usr/bin/env python3
"""Fail the site build if a hand-written story names a living person.

content/stories/ sits outside the Gramps export, so the export's living-person
rule never sees it. This check runs in GitHub Actions before site/build.py:
every name a living (name-only) person carries in data/bundle.json — first
name + surname, and first given name + surname — is searched for in all text
of content/stories/stories.json (and any .md under content/stories/).
A name a deceased person also carries (a namesake) is skipped and listed.

If naming someone is fine (name and relationship only, nothing else), list
their Gramps ID in content/stories/living-ok.txt, one per line, with a reason
after a '#'. Exit 0 = clean or nothing to check, 1 = living person named.
"""
import json
import os
import re
import sys
import unicodedata

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
BUNDLE = os.path.join(ROOT, "data", "bundle.json")
STORIES = os.path.join(ROOT, "content", "stories")
ALLOW = os.path.join(STORIES, "living-ok.txt")


def fold(t: str) -> str:
    """lower case, Czech diacritics stripped: 'Konečný' and 'Konecny' are the same name here"""
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).lower()


def strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


def main() -> int:
    if not os.path.isdir(STORIES):
        print("check_stories: no content/stories/ — nothing to check")
        return 0
    if not os.path.exists(BUNDLE):
        print("check_stories: no data/bundle.json — nothing to check against")
        return 0
    with open(BUNDLE, encoding="utf-8") as f:
        people = json.load(f).get("people", {})
    allow = set()
    if os.path.exists(ALLOW):
        with open(ALLOW, encoding="utf-8") as f:
            allow = {ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()}

    def keys(p):
        out = set()
        for n in [p.get("name") or {}] + list(p.get("alt_names") or []):
            first, sur = (n.get("first") or "").strip(), (n.get("surname") or "").strip()
            if len(first) >= 2 and len(sur) >= 2:
                out |= {fold(f"{g} {sur}") for g in (first, first.split()[0])}
        return out

    # a name that a deceased person also carries can't be told apart in text: skip it, but say so
    dead = set()
    for p in people.values():
        if p.get("status") == "full":
            dead |= keys(p)
    pats, namesakes = {}, []
    for pid, p in people.items():
        if p.get("status") != "living" or pid in allow:
            continue
        display = (p.get("name") or {}).get("display") or pid
        for k in keys(p):
            if k in dead:
                namesakes.append(f"{pid} '{k}'")
            else:
                pats[k] = (pid, display)
    rx = [(re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, k.split())) + r"(?!\w)"), v) for k, v in pats.items()]

    texts = []
    for dirpath, _dirs, files in os.walk(STORIES):
        for fn in sorted(files):
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            if fn.endswith(".json"):
                with open(path, encoding="utf-8") as f:
                    texts += [(rel, s) for s in strings(json.load(f))]
            elif fn.endswith(".md"):
                with open(path, encoding="utf-8") as f:
                    texts.append((rel, f.read()))

    hits = set()
    for rel, text in texts:
        low = fold(text)
        for r, (pid, display) in rx:
            if r.search(low):
                hits.add((rel, pid, display))
    if hits:
        print("check_stories: stories name living people — remove them, or list the ID in content/stories/living-ok.txt:")
        for rel, pid, display in sorted(hits):
            print(f"  {rel}: {pid} {display}")
        return 1
    if namesakes:
        print("check_stories: not checked (a deceased person has the same name): " + "; ".join(sorted(namesakes)))
    print(f"check_stories: ok ({len(texts)} text fields, {len(rx)} living-name patterns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
