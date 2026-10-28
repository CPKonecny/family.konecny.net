#!/usr/bin/env python3
"""
gramps_export.py — public-safe export of the Konecny tree for family.konecny.net

Reads the tree through the Gramps Web API (loopback, through the SSH tunnel on
the server), applies the publication rules, and writes ONE JSON bundle plus the
public media files. Nothing else ever touches the Gramps server.

Rules (see the bundle schema document for the full statement):
  1. Living people: name, gender and relationships only. "Living" is Gramps'
     own probably-alive verdict (GET /api/living/{handle}), overridable per
     person in overrides.json (living / deceased / exclude).
  2. Anything marked private in Gramps stays out, at every level (objects,
     names, event refs, attributes, citations, notes, media refs).
  3. Media: a file is published only if the media object carries the public tag
     (default "public"), is not private, is not attached to a living person and
     does not look like an Ancestry/FamilySearch record image. Everything else
     is listed as metadata with access="login" (the file stays on the private
     site). Citations, transcriptions and reasoning notes are always public.
  4. Review state is exported honestly from the tags on each object:
     reviewed -> human, ai-validated -> ai, everything else -> unreviewed
     (needs-human and agent-proposed are kept as flags).

Usage
  python3 gramps_export.py --dry-run                # report only, writes nothing
  python3 gramps_export.py --out ~/family-site      # writes data/bundle.json,
                                                    #   data/report.md, media/
Credentials: --user or GRAMPS_USER, password from the prompt, GRAMPS_PASSWORD or
--password-file (chmod 600). Nothing is ever written to disk by this script
other than the bundle, the report, the media files and the optional raw cache.

Requires only the Python 3 standard library (3.8+).
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import getpass
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.3.0"
SCHEMA_VERSION = "0.3"

TABLES = [
    "people", "families", "events", "places", "citations",
    "sources", "repositories", "notes", "media", "tags",
]
# Gramps 6.0 death / birth fallbacks (gramps.gen.lib.eventtype)
DEATH_TYPES = {"Death", "Burial", "Cremation", "Cause Of Death", "Probate", "Stillbirth"}
BIRTH_TYPES = {"Birth", "Baptism", "Christening", "Stillbirth"}
REVIEW_TAGS = {
    "reviewed": "human",
    "ai-validated": "ai",
    "needs-human": "unreviewed",
    "agent-proposed": "unreviewed",
    "unreviewed": "unreviewed",
}
REVIEW_FLAGS = {"needs-human", "agent-proposed", "sync-to-ancestry"}
PROVENANCE_PREFIXES = ("import-", "agent-", "chris")
TIERS = ["record", "index", "compiled", "tree", "oral", "ai-draft"]
CITATION_ATTRS = {
    "Tier": "tier", "Source": "source_type", "Information": "information",
    "Evidence": "evidence", "URL": "url", "Ancestry record ID": "ancestry_record_id",
    "Proposal file": "proposal_file",
}
PERSON_XREF_ATTRS = {"Ancestry ID": "ancestry", "RM RIN": "rm_rin"}
RECORD_IMAGE_HINTS = ("ancestry", "familysearch", "family tree_media", "records/")
MOD = {0: None, 1: "before", 2: "after", 3: "about", 4: "between", 5: "from-to",
       6: "text", 7: "from", 8: "to"}
QUAL = {0: None, 1: "estimated", 2: "calculated"}
CAL = {0: "gregorian", 1: "julian", 2: "hebrew", 3: "french", 4: "persian",
       5: "islamic", 6: "swedish"}
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
GENDER = {0: "F", 1: "M", 2: "U", 3: "X"}
ID_PREFIX = {"people": "I", "families": "F", "events": "E", "places": "P",
             "citations": "C", "sources": "S", "repositories": "R",
             "notes": "N", "media": "O"}


# --------------------------------------------------------------------------- #
# HTTP client (stdlib only)
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    pass


class Api:
    def __init__(self, base: str, timeout: int = 60, verbose: bool = False):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token = None
        self.user = None
        self.password = None
        self.verbose = verbose
        self.calls = 0

    def login(self, user: str, password: str) -> None:
        self.user, self.password = user, password
        body = json.dumps({"username": user, "password": password}).encode()
        req = urllib.request.Request(
            f"{self.base}/token/", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise ApiError(f"login failed: HTTP {e.code} {e.read()[:200]!r}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"cannot reach {self.base}: {e.reason}") from None
        self.token = data["access_token"]

    BUSY = (500, 502, 503, 504)      # Gramps Web under write load (SQLite lock, worker busy): wait and try again
    WAITS = (3, 10, 30)

    def _request(self, path: str, params: dict | None = None, raw: bool = False,
                 retry: bool = True, attempt: int = 0):
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "*/*" if raw else "application/json"})
        self.calls += 1
        if self.verbose:
            print(f"  GET {url}", file=sys.stderr)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                headers = dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry and self.password:
                self.login(self.user, self.password)
                return self._request(path, params, raw, retry=False)
            if e.code in self.BUSY and attempt < len(self.WAITS):
                time.sleep(self.WAITS[attempt])
                return self._request(path, params, raw, retry, attempt + 1)
            raise ApiError(f"GET {url}: HTTP {e.code} {e.read()[:300]!r}") from None
        if raw:
            return body, headers
        return json.loads(body.decode()), headers

    def get(self, path: str, params: dict | None = None):
        return self._request(path, params)[0]

    def get_bytes(self, path: str, params: dict | None = None) -> bytes:
        return self._request(path, params, raw=True)[0]

    def send(self, method: str, path: str, payload=None, retry: bool = True, attempt: int = 0):
        """PUT/POST/DELETE with a JSON body — used by the validator and the geocoder, never by the export."""
        url = f"{self.base}/{path.lstrip('/')}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "Accept": "application/json"})
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry and self.password:
                self.login(self.user, self.password)
                return self.send(method, path, payload, retry=False)
            if e.code in self.BUSY and attempt < len(self.WAITS) and method != "POST":
                time.sleep(self.WAITS[attempt])
                return self.send(method, path, payload, retry, attempt + 1)
            raise ApiError(f"{method} {url}: HTTP {e.code} {e.read()[:300]!r}") from None
        return json.loads(body.decode()) if body else None

    def paged(self, endpoint: str, pagesize: int = 500):
        """Yield every object of a collection endpoint, page by page."""
        page, seen, total = 1, 0, None
        while True:
            batch, headers = self._request(f"{endpoint}/", {"page": page, "pagesize": pagesize})
            if total is None:
                hdr = {k.lower(): v for k, v in headers.items()}
                try:
                    total = int(hdr.get("x-total-count", "-1"))
                except ValueError:
                    total = -1
            if not batch:
                return
            for obj in batch:
                yield obj
            seen += len(batch)
            if len(batch) < pagesize or (total >= 0 and seen >= total):
                return
            page += 1


# --------------------------------------------------------------------------- #
# Fetch (API or raw cache)
# --------------------------------------------------------------------------- #
def fetch_all(api: Api, raw_dir: str | None, living_api: bool, progress) -> dict:
    data = {}
    try:
        data["metadata"] = api.get("metadata/")
    except ApiError as e:
        progress(f"metadata unavailable ({e}); continuing")
        data["metadata"] = {}
    for table in TABLES:
        progress(f"fetching {table} …")
        data[table] = list(api.paged(table))
        progress(f"  {len(data[table])} {table}")
    data["living"] = {}
    data["living_dates"] = {}
    if living_api:
        progress("asking Gramps for its probably-alive verdict on every person …")
        for i, p in enumerate(data["people"], 1):
            h = p["handle"]
            try:
                data["living"][h] = bool(api.get(f"living/{h}")["living"])
            except ApiError as e:
                progress(f"  living/{h}: {e} — falling back to local logic for this person")
            if i % 50 == 0:
                progress(f"  {i}/{len(data['people'])}")
        # explanations only for the ones that end up name-only (cheap, and useful in the report)
        for p in data["people"]:
            h = p["handle"]
            if data["living"].get(h):
                try:
                    d = api.get(f"living/{h}/dates")
                    other = d.get("other") or {}
                    data["living_dates"][h] = {
                        "birth": d.get("birth"), "death": d.get("death"), "explain": d.get("explain"),
                        "other": other.get("gramps_id") if isinstance(other, dict) else None}
                except ApiError:
                    pass
    if raw_dir:
        os.makedirs(raw_dir, exist_ok=True)
        for key in TABLES + ["metadata", "living", "living_dates"]:
            with open(os.path.join(raw_dir, f"{key}.json"), "w", encoding="utf-8") as f:
                json.dump(data[key], f, ensure_ascii=False, indent=1)
        progress(f"raw API responses cached in {raw_dir}")
    return data


def load_raw(raw_dir: str) -> dict:
    data = {}
    for key in TABLES + ["metadata", "living", "living_dates"]:
        path = os.path.join(raw_dir, f"{key}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data[key] = json.load(f)
        elif key in TABLES:
            raise ApiError(f"raw cache incomplete: {path} is missing")
        else:
            data[key] = {}
    return data


# --------------------------------------------------------------------------- #
# Small helpers over the raw Gramps JSON
# --------------------------------------------------------------------------- #
def typestr(v) -> str:
    """Gramps Web API emits GrampsType values as plain strings; tolerate dicts too."""
    if isinstance(v, dict):
        return str(v.get("string", ""))
    return "" if v is None else str(v)


def date_year(d: dict | None) -> int | None:
    if not d or not isinstance(d, dict):
        return None
    if d.get("modifier") == 6:
        return None
    dv = d.get("dateval") or []
    if len(dv) >= 3 and dv[2]:
        return int(dv[2])
    return None


def date_obj(d: dict | None) -> dict | None:
    """Gramps Date -> compact public form (None if empty)."""
    if not d or not isinstance(d, dict):
        return None
    dv = list(d.get("dateval") or [])
    mod = d.get("modifier", 0)
    text = d.get("text") or ""
    if mod == 6:
        return {"text": text, "year": None, "sort": None, "modifier": "text",
                "quality": None} if text else None
    if len(dv) < 3 or not any(dv[:3]):
        return {"text": text, "year": None, "sort": None, "modifier": "text",
                "quality": None} if text else None
    out = {
        "year": int(dv[2]) if dv[2] else None,
        "month": int(dv[1]) if len(dv) > 1 and dv[1] else None,
        "day": int(dv[0]) if dv[0] else None,
        "modifier": MOD.get(mod),
        "quality": QUAL.get(d.get("quality", 0)),
        "calendar": CAL.get(d.get("calendar", 0), "gregorian"),
        "sort": d.get("sortval") or None,
    }
    if mod in (4, 5) and len(dv) >= 7:
        out["end"] = {
            "year": int(dv[6]) if dv[6] else None,
            "month": int(dv[5]) if dv[5] else None,
            "day": int(dv[4]) if dv[4] else None,
        }
    out["iso"] = iso_date(out["year"], out["month"], out["day"])
    out["text"] = format_date(out)
    if out["calendar"] == "gregorian":
        del out["calendar"]
    return out


def iso_date(y, m, d) -> str | None:
    if not y:
        return None
    if m and d:
        return f"{y:04d}-{m:02d}-{d:02d}"
    if m:
        return f"{y:04d}-{m:02d}"
    return f"{y:04d}"


def _fmt_ymd(y, m, d) -> str:
    if not y:
        return ""
    if m and d:
        return f"{d} {MONTHS[m]} {y}"
    if m:
        return f"{MONTHS[m]} {y}"
    return str(y)


def format_date(o: dict) -> str:
    base = _fmt_ymd(o.get("year"), o.get("month"), o.get("day"))
    mod = o.get("modifier")
    if mod == "between" and o.get("end"):
        e = o["end"]
        base = f"between {base} and {_fmt_ymd(e.get('year'), e.get('month'), e.get('day'))}"
    elif mod == "from-to" and o.get("end"):
        e = o["end"]
        base = f"from {base} to {_fmt_ymd(e.get('year'), e.get('month'), e.get('day'))}"
    elif mod in ("before", "after", "about", "from", "to"):
        base = f"{mod} {base}"
    q = o.get("quality")
    if q:
        base = f"{q} {base}"
    if o.get("calendar") and o["calendar"] != "gregorian":
        base = f"{base} ({o['calendar']})"
    return base


def tag_names(obj: dict, tags_by_handle: dict) -> list[str]:
    return [tags_by_handle[h] for h in obj.get("tag_list", []) if h in tags_by_handle]


def review_of(names: list[str]) -> dict:
    state = "unreviewed"
    for n in names:
        if REVIEW_TAGS.get(n) == "human":
            state = "human"
            break
        if REVIEW_TAGS.get(n) == "ai":
            state = "ai"
    flags = sorted(n for n in names if n in REVIEW_FLAGS)
    prov = sorted(n for n in names if n.startswith(PROVENANCE_PREFIXES))
    return {"review": state, "flags": flags, "provenance": prov}


def styled_to_html(text: dict | str) -> str:
    """StyledText -> minimal HTML (bold/italic/underline/link/superscript); escapes everything else."""
    if isinstance(text, str):
        return "<p>" + html.escape(text).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    s = text.get("string", "") or ""
    n = len(s)
    spans = []  # (start, end, open_tag, close_tag)
    for tag in text.get("tags", []) or []:
        name = typestr(tag.get("name")).lower()
        value = tag.get("value")
        for rng in tag.get("ranges", []) or []:
            if not rng or len(rng) < 2:
                continue
            a, b = max(0, int(rng[0])), min(n, int(rng[1]))
            if b <= a:
                continue
            if name == "bold":
                o, c = "<b>", "</b>"
            elif name == "italic":
                o, c = "<i>", "</i>"
            elif name == "underline":
                o, c = "<u>", "</u>"
            elif name == "superscript":
                o, c = "<sup>", "</sup>"
            elif name == "strikethrough":
                o, c = "<s>", "</s>"
            elif name == "link" and value:
                href = html.escape(str(value), quote=True)
                if not re.match(r"^(https?:|mailto:)", href):
                    continue  # gramps:// internal links are not resolvable on the site
                o, c = f'<a href="{href}" rel="noopener">', "</a>"
            else:
                continue
            spans.append((a, b, o, c))
    spans.sort(key=lambda x: (x[0], -x[1]))
    # walk the string; keep a stack of open spans so paragraph breaks close and reopen them
    out = ["<p>"]
    stack = []
    i = 0
    while i < n:
        # close spans ending here (innermost first)
        while stack and stack[-1][1] <= i:
            out.append(stack.pop()[3])
        for sp in spans:
            if sp[0] == i:
                out.append(sp[2])
                stack.append(sp)
        if s.startswith("\n\n", i) or (s[i] == "\n" and s[i + 1:].lstrip(" \t") .startswith("\n")):
            # paragraph break: close everything, reopen after
            j = i
            while j < n and s[j] in "\n \t":
                j += 1
            for sp in reversed(stack):
                out.append(sp[3])
            out.append("</p><p>")
            for sp in stack:
                out.append(sp[2])
            i = j
            continue
        if s[i] == "\n":
            out.append("<br>")
        else:
            out.append(html.escape(s[i]))
        i += 1
    while stack:
        out.append(stack.pop()[3])
    out.append("</p>")
    body = "".join(out)
    body = body.replace("<p></p>", "")
    return body


def plain_text(text) -> str:
    if isinstance(text, dict):
        return text.get("string", "") or ""
    return text or ""


def looks_like_record_image(path: str, tags: list[str]) -> bool:
    p = (path or "").replace("\\", "/").lower()
    if any(h in p for h in RECORD_IMAGE_HINTS):
        return True
    return any(t.startswith("import-ancestry") for t in tags)


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
class Overrides:
    KEYS = ("living", "deceased", "exclude", "public_media", "withhold")

    def __init__(self, path: str | None):
        self.path = path
        self.raw = {}
        self.sets = {k: set() for k in self.KEYS}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.raw = json.load(f)
            for k in self.KEYS:
                v = self.raw.get(k, [])
                if isinstance(v, dict):   # {"I0001": "reason", ...}
                    v = list(v.keys())
                self.sets[k] = set(str(x).strip() for x in v if str(x).strip())
        self.unmatched = {k: set(v) for k, v in self.sets.items()}

    def has(self, key: str, obj: dict) -> bool:
        ids = {obj.get("gramps_id"), obj.get("handle")}
        hit = ids & self.sets[key]
        if hit:
            self.unmatched[key] -= hit
            return True
        return False

    def note(self, key: str, obj: dict) -> str:
        v = self.raw.get(key)
        if isinstance(v, dict):
            return v.get(obj.get("gramps_id")) or v.get(obj.get("handle")) or ""
        return ""


# --------------------------------------------------------------------------- #
# Local probably-alive fallback (simplified port of gramps.gen.utils.alive)
# --------------------------------------------------------------------------- #
class LocalAlive:
    """Used when the API's /living endpoint is unavailable, and to explain verdicts."""

    def __init__(self, idx, today: dt.date, max_age: int, gen_gap: int = 20, sib_gap: int = 20):
        self.idx = idx
        self.today = today
        self.max_age = max_age
        self.gen_gap = gen_gap
        self.sib_gap = sib_gap

    def own_dates(self, p: dict):
        """(birth_year, death_year, has_death_event, birth_src, death_src)"""
        birth = death = None
        has_death = False
        bsrc = dsrc = ""
        for ref in p.get("event_ref_list", []) or []:
            ev = self.idx.events.get(ref.get("ref"))
            if not ev or typestr(ref.get("role")) != "Primary":
                continue
            t = typestr(ev.get("type"))
            y = date_year(ev.get("date"))
            if t in DEATH_TYPES:
                has_death = True
                if y and (death is None or t == "Death"):
                    death, dsrc = y, t
            if t in BIRTH_TYPES and y and (birth is None or t == "Birth"):
                birth, bsrc = y, t
        return birth, death, has_death, bsrc, dsrc

    def estimate_birth(self, p: dict, depth: int = 0):
        """Rough birth-year estimate from relatives, in Gramps' order: children, parents, siblings, spouse."""
        ests = []
        for fh in p.get("family_list", []) or []:
            fam = self.idx.families.get(fh)
            if not fam:
                continue
            for cr in fam.get("child_ref_list", []) or []:
                c = self.idx.people.get(cr.get("ref"))
                if c:
                    b = self.own_dates(c)[0]
                    if b:
                        ests.append((b - self.gen_gap, f"child born {b}"))
        for fh in p.get("parent_family_list", []) or []:
            fam = self.idx.families.get(fh)
            if not fam:
                continue
            for ph in (fam.get("father_handle"), fam.get("mother_handle")):
                par = self.idx.people.get(ph) if ph else None
                if par:
                    b = self.own_dates(par)[0]
                    if b:
                        ests.append((b + self.gen_gap, f"parent born {b}"))
            for cr in fam.get("child_ref_list", []) or []:
                s = self.idx.people.get(cr.get("ref"))
                if s and s is not p:
                    b = self.own_dates(s)[0]
                    if b:
                        ests.append((b, f"sibling born {b}"))
        if not ests and depth == 0:
            for fh in p.get("family_list", []) or []:
                fam = self.idx.families.get(fh)
                if not fam:
                    continue
                for ph in (fam.get("father_handle"), fam.get("mother_handle")):
                    sp = self.idx.people.get(ph) if ph and ph != p["handle"] else None
                    if sp:
                        b = self.own_dates(sp)[0]
                        if b:
                            ests.append((b, f"spouse born {b}"))
                for ref in fam.get("event_ref_list", []) or []:
                    ev = self.idx.events.get(ref.get("ref"))
                    y = date_year(ev.get("date")) if ev else None
                    if y:
                        ests.append((y - self.gen_gap, f"family event {y}"))
        return ests

    def verdict(self, p: dict):
        """-> (alive: bool, explanation: str)"""
        birth, death, has_death, bsrc, dsrc = self.own_dates(p)
        if has_death:
            return False, (f"{dsrc} {death}" if death else f"{dsrc} event, no date")
        cutoff = self.today.year - self.max_age
        if birth:
            return (birth > cutoff), f"born {birth} ({bsrc}), no death event"
        ests = self.estimate_birth(p)
        if ests:
            y, why = ests[0]
            return (y > cutoff), f"no own dates; estimated born ~{y} ({why})"
        return True, "no evidence at all — treated as living"


# --------------------------------------------------------------------------- #
# Index and classification
# --------------------------------------------------------------------------- #
class Index:
    def __init__(self, data: dict):
        self.data = data
        self.people = {o["handle"]: o for o in data["people"]}
        self.families = {o["handle"]: o for o in data["families"]}
        self.events = {o["handle"]: o for o in data["events"]}
        self.places = {o["handle"]: o for o in data["places"]}
        self.citations = {o["handle"]: o for o in data["citations"]}
        self.sources = {o["handle"]: o for o in data["sources"]}
        self.repositories = {o["handle"]: o for o in data["repositories"]}
        self.notes = {o["handle"]: o for o in data["notes"]}
        self.media = {o["handle"]: o for o in data["media"]}
        self.tags = {o["handle"]: o["name"] for o in data["tags"]}
        self.by_table = {
            "people": self.people, "families": self.families, "events": self.events,
            "places": self.places, "citations": self.citations, "sources": self.sources,
            "repositories": self.repositories, "notes": self.notes, "media": self.media,
        }

    def tags_of(self, obj: dict) -> list[str]:
        return tag_names(obj, self.tags)


class Report:
    def __init__(self):
        self.sections = collections.OrderedDict()
        self.warnings = []

    def add(self, section: str, line: str):
        self.sections.setdefault(section, []).append(line)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def render(self, header: str) -> str:
        out = [header, ""]
        for sec, lines in self.sections.items():
            out.append(f"## {sec}")
            out.append("")
            out.extend(lines)
            out.append("")
        out.append("## Warnings")
        out.append("")
        if self.warnings:
            out.extend(f"- {w}" for w in self.warnings)
        else:
            out.append("- none")
        out.append("")
        return "\n".join(out)


ASTERISK_NAMES = set()   # gramps ids whose name carried a leading asterisk (Ancestry artefact; research log A6)


def _clean(s: str | None, gid: str | None = None) -> str:
    s = (s or "").strip()
    if s.startswith("*"):
        if gid:
            ASTERISK_NAMES.add(gid)
        s = s.lstrip("* ").strip()
    return s


def name_of(p: dict) -> dict:
    n = p.get("primary_name") or {}
    gid = p.get("gramps_id")
    surnames = n.get("surname_list") or []
    prim = next((s for s in surnames if s.get("primary")), surnames[0] if surnames else {})
    first = _clean(n.get("first_name"), gid)
    bare_surname = _clean(prim.get("surname"), gid)
    surname = " ".join(filter(None, [prim.get("prefix"), bare_surname])).strip()
    parts = [n.get("title"), first, surname, n.get("suffix")]
    display = " ".join(x for x in parts if x).strip() or "(unnamed)"
    sort = ", ".join(x for x in [surname, first] if x) or display
    return {
        "first": first,
        "surname": bare_surname,
        "prefix": prim.get("prefix") or "",
        "suffix": n.get("suffix") or "",
        "title": n.get("title") or "",
        "call": _clean(n.get("call")),
        "nick": _clean(n.get("nick")),
        "type": typestr(n.get("type")) or "Birth Name",
        "display": display,
        "sort": sort,
    }


def alt_names_of(p: dict) -> list[dict]:
    out = []
    for n in p.get("alternate_names", []) or []:
        if n.get("private"):
            continue
        fake = {"primary_name": n}
        d = name_of(fake)
        d["date"] = date_obj(n.get("date"))
        out.append(d)
    return out


class Exporter:
    def __init__(self, data: dict, overrides: Overrides, args, report: Report):
        self.data = data
        self.idx = Index(data)
        self.ov = overrides
        self.args = args
        self.report = report
        self.today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        self.local = LocalAlive(self.idx, self.today, args.max_age)
        self.status = {}        # person handle -> "full" | "living" | "excluded"
        self.reason = {}        # person handle -> reason
        self.fam_status = {}    # family handle -> "full" | "living" | "excluded"
        self.living_set = set()
        self.excluded_set = set()
        self.included = {t: set() for t in TABLES if t != "tags"}
        self.media_access = {}  # media handle -> ("public"|"login"|"excluded", reason)
        self.ids = {}           # (table, handle) -> gramps_id
        self.counts = collections.OrderedDict()

    # ---- people ---------------------------------------------------------- #
    def classify_people(self):
        api_living = self.data.get("living") or {}
        api_dates = self.data.get("living_dates") or {}
        reasons = collections.Counter()
        disagreements = []
        check_list = []
        for h, p in self.idx.people.items():
            gid = p.get("gramps_id", h)
            name = name_of(p)["display"]
            local_alive, why = self.local.verdict(p)
            if self.ov.has("exclude", p):
                st, rs = "excluded", "override: exclude"
            elif p.get("private"):
                st, rs = "excluded", "private"
            elif self.ov.has("deceased", p):
                st, rs = "full", "override: deceased"
            elif self.ov.has("living", p):
                st, rs = "living", "override: living"
            elif h in api_living:
                alive = api_living[h]
                st = "living" if alive else "full"
                expl = (api_dates.get(h) or {}).get("explain") or ""
                rs = ("Gramps: alive" if alive else "Gramps: deceased") + f" — {why}"
                if expl and alive:
                    rs += f" [{expl}]"
                if alive != local_alive:
                    disagreements.append(f"{gid} {name}: Gramps says {'alive' if alive else 'deceased'}, "
                                         f"local rule says {'alive' if local_alive else 'deceased'} ({why})")
            else:
                st = "living" if local_alive else "full"
                rs = f"local rule — {why}"
            self.status[h] = st
            self.reason[h] = rs
            if st == "living":
                self.living_set.add(h)
                if "no own dates" in why or "no evidence" in why:
                    check_list.append(f"{gid} {name}: {rs}")
            elif st == "excluded":
                self.excluded_set.add(h)
            if rs.startswith("override"):
                key = rs
            elif st == "excluded":
                key = "private"
            elif "no evidence" in why:
                key = "no evidence at all (treated as living)"
            elif "no own dates" in why:
                key = "no own dates, estimated from relatives"
            elif st == "living" and not local_alive:
                key = "Gramps' verdict (local rule disagrees — see below)"
            else:
                key = f"born within {self.args.max_age} years, no death event"
            reasons[(st, key)] += 1
        full = sum(1 for s in self.status.values() if s == "full")
        living = len(self.living_set)
        excluded = len(self.excluded_set)
        self.counts["people"] = (len(self.status), full, living, excluded)
        r = self.report
        r.add("People", f"| status | n |\n|---|---:|\n| total in Gramps | {len(self.status)} |\n"
                        f"| **full** (deceased, everything published) | {full} |\n"
                        f"| **name-only** (living) | {living} |\n| **excluded** | {excluded} |")
        r.add("People", "")
        r.add("People", "Why name-only / excluded:")
        r.add("People", "")
        for (st, key), n in sorted(reasons.items()):
            if st != "full":
                r.add("People", f"- {st}: {key} — {n}")
        if not any(st != "full" for (st, _k) in reasons):
            r.add("People", "- (none)")
        mode = "Gramps' own probably-alive (API) + overrides" if api_living else "local rule only (API living endpoint not used)"
        n_private = sum(1 for objs in self.idx.by_table.values() for o in objs.values() if o.get("private"))
        r.add("People", "")
        r.add("People", f"Living logic: {mode}; max age {self.args.max_age}; today {self.today.isoformat()}.  \n"
                        f"Private objects visible to this account: {n_private} "
                        f"(a Guest-role account never sees them — the API strips them before this script runs; "
                        f"an Owner account sees them and this script drops them).")
        if disagreements:
            r.add("People", "")
            r.add("People", f"Gramps and the local rule disagree on {len(disagreements)} people "
                            f"(Gramps' verdict was used; add an override to settle):")
            r.add("People", "")
            r.add("People", "\n".join(f"- {d}" for d in disagreements))
        if check_list:
            r.add("People", "")
            r.add("People", f"Name-only without dates of their own (Gramps inferred the verdict from relatives, or had nothing to go on). "
                            f"Confirm each: living is fine as is; a deceased person goes in overrides.deceased ({len(check_list)}):")
            r.add("People", "")
            r.add("People", "\n".join(f"- {c}" for c in check_list))
        # the full list of name-only people, so Chris can scan it in one go
        living_lines = []
        for h in sorted(self.living_set, key=lambda x: name_of(self.idx.people[x])["sort"]):
            p = self.idx.people[h]
            living_lines.append(f"- {p.get('gramps_id')} {name_of(p)['display']} — {self.reason[h]}")
        if living_lines:
            r.add("People", "")
            r.add("People", f"<details><summary>All {len(living_lines)} name-only people</summary>\n\n"
                            + "\n".join(living_lines) + "\n\n</details>")
        excl_lines = [f"- {self.idx.people[h].get('gramps_id')} {name_of(self.idx.people[h])['display']} — {self.reason[h]}"
                      for h in sorted(self.excluded_set)]
        if excl_lines:
            r.add("People", "")
            r.add("People", "Excluded:\n\n" + "\n".join(excl_lines))

    # ---- families -------------------------------------------------------- #
    def classify_families(self):
        c = collections.Counter()
        for h, f in self.idx.families.items():
            if f.get("private"):
                st = "excluded"
            else:
                partners = [x for x in (f.get("father_handle"), f.get("mother_handle")) if x]
                kids = [cr.get("ref") for cr in f.get("child_ref_list", []) or []]
                pub_partners = [x for x in partners if self.status.get(x) in ("full", "living")]
                pub_kids = [x for x in kids if self.status.get(x) in ("full", "living")]
                if not pub_partners and not pub_kids:
                    st = "excluded"
                elif any(self.status.get(x) == "living" for x in partners):
                    st = "living"
                else:
                    st = "full"
            self.fam_status[h] = st
            c[st] += 1
        self.counts["families"] = (len(self.fam_status), c["full"], c["living"], c["excluded"])
        self.report.add("Families", f"| status | n |\n|---|---:|\n| total | {len(self.fam_status)} |\n"
                                    f"| full (events and dates published) | {c['full']} |\n"
                                    f"| structure only (a partner is living) | {c['living']} |\n"
                                    f"| excluded (private, or nobody publishable) | {c['excluded']} |")

    # ---- reachability ---------------------------------------------------- #
    def _refs_of(self, obj: dict, key: str):
        return [x for x in (obj.get(key) or []) if x]

    def _media_refs(self, obj: dict):
        return [m.get("ref") for m in (obj.get("media_list") or []) if m.get("ref") and not m.get("private")]

    def _citation_refs(self, obj: dict):
        return [c for c in (obj.get("citation_list") or []) if c]

    def _note_refs(self, obj: dict):
        return [n for n in (obj.get("note_list") or []) if n]

    def withheld(self, table: str, h: str) -> bool:
        """overrides.withhold: any Gramps ID or handle listed there is kept off the site"""
        obj = self.idx.by_table[table].get(h)
        return bool(obj) and self.ov.has("withhold", obj)

    def compute_inclusion(self):
        inc = self.included
        withheld_events = {}   # event handle -> reason
        # people and families
        for h, st in self.status.items():
            if st in ("full", "living"):
                inc["people"].add(h)
        for h, st in self.fam_status.items():
            if st in ("full", "living"):
                inc["families"].add(h)
        # events referenced by a living person or living family are withheld entirely
        living_events = set()
        for h in self.living_set | self.excluded_set:
            for ref in self.idx.people[h].get("event_ref_list", []) or []:
                living_events.add(ref.get("ref"))
        for h, st in self.fam_status.items():
            if st in ("living", "excluded"):
                for ref in self.idx.families[h].get("event_ref_list", []) or []:
                    living_events.add(ref.get("ref"))
        self.living_events = living_events
        # secondary objects reachable from full people / full families
        cit_refs, note_refs, media_refs, place_refs = set(), set(), set(), set()
        self.event_participants = collections.defaultdict(list)
        self.event_families = collections.defaultdict(list)

        def take_sub(obj: dict, owner_full: bool):
            """collect citations / notes / media referenced by an object and its sub-objects"""
            if not owner_full:
                return
            cit_refs.update(self._citation_refs(obj))
            note_refs.update(self._note_refs(obj))
            media_refs.update(self._media_refs(obj))
            for a in obj.get("attribute_list", []) or []:
                if not a.get("private"):
                    cit_refs.update(self._citation_refs(a))
                    note_refs.update(self._note_refs(a))

        for h in inc["people"]:
            p = self.idx.people[h]
            full = self.status[h] == "full"
            if not full:
                continue
            take_sub(p, True)
            pn = p.get("primary_name") or {}
            if not pn.get("private"):
                cit_refs.update(self._citation_refs(pn))
                note_refs.update(self._note_refs(pn))
            for n in p.get("alternate_names", []) or []:
                if not n.get("private"):
                    cit_refs.update(self._citation_refs(n))
                    note_refs.update(self._note_refs(n))
            for ref in p.get("event_ref_list", []) or []:
                eh = ref.get("ref")
                ev = self.idx.events.get(eh)
                if not ev or ref.get("private") or ev.get("private"):
                    if ev and ev.get("private"):
                        withheld_events[eh] = "private"
                    continue
                if eh in living_events:
                    withheld_events[eh] = "shared with a living or excluded person"
                    continue
                if self.withheld("events", eh):
                    withheld_events[eh] = "override: withhold"
                    continue
                inc["events"].add(eh)
                self.event_participants[eh].append((h, typestr(ref.get("role")) or "Primary"))
                cit_refs.update(self._citation_refs(ref))
                note_refs.update(self._note_refs(ref))
        for h in inc["families"]:
            f = self.idx.families[h]
            full = self.fam_status[h] == "full"
            take_sub(f, full)
            for cr in f.get("child_ref_list", []) or []:
                if full and not cr.get("private"):
                    cit_refs.update(self._citation_refs(cr))
                    note_refs.update(self._note_refs(cr))
            if not full:
                continue
            for ref in f.get("event_ref_list", []) or []:
                eh = ref.get("ref")
                ev = self.idx.events.get(eh)
                if not ev or ref.get("private") or ev.get("private"):
                    if ev and ev.get("private"):
                        withheld_events[eh] = "private"
                    continue
                if eh in living_events:
                    withheld_events[eh] = "shared with a living or excluded person"
                    continue
                if self.withheld("events", eh):
                    withheld_events[eh] = "override: withhold"
                    continue
                inc["events"].add(eh)
                self.event_families[eh].append((h, typestr(ref.get("role")) or "Family"))
                cit_refs.update(self._citation_refs(ref))
                note_refs.update(self._note_refs(ref))
        for eh in inc["events"]:
            ev = self.idx.events[eh]
            take_sub(ev, True)
            if ev.get("place"):
                place_refs.add(ev["place"])
        # places: the referenced ones plus everything that encloses them
        stack = list(place_refs)
        while stack:
            ph = stack.pop()
            pl = self.idx.places.get(ph)
            if not pl or ph in inc["places"]:
                continue
            if pl.get("private"):
                continue
            inc["places"].add(ph)
            take_sub(pl, True)
            for pr in pl.get("placeref_list", []) or []:
                if pr.get("ref"):
                    stack.append(pr["ref"])
        # citations -> sources -> repositories; citations also carry notes and media
        for ch in cit_refs:
            cit = self.idx.citations.get(ch)
            if not cit or cit.get("private") or self.withheld("citations", ch):
                continue
            inc["citations"].add(ch)
            note_refs.update(self._note_refs(cit))
            media_refs.update(self._media_refs(cit))
            for a in cit.get("attribute_list", []) or []:
                if not a.get("private"):
                    note_refs.update(self._note_refs(a))
            sh = cit.get("source_handle")
            src = self.idx.sources.get(sh) if sh else None
            if src and not src.get("private"):
                inc["sources"].add(sh)
        for sh in list(inc["sources"]):
            src = self.idx.sources[sh]
            note_refs.update(self._note_refs(src))
            media_refs.update(self._media_refs(src))
            for rr in src.get("reporef_list", []) or []:
                rh = rr.get("ref")
                repo = self.idx.repositories.get(rh) if rh else None
                if repo and not repo.get("private") and not rr.get("private"):
                    inc["repositories"].add(rh)
                    note_refs.update(self._note_refs(rr))
        for rh in inc["repositories"]:
            note_refs.update(self._note_refs(self.idx.repositories[rh]))
        # media: decide access per object
        media_by_living = set()
        for h in self.living_set | self.excluded_set:
            media_by_living.update(m.get("ref") for m in self.idx.people[h].get("media_list") or [])
        for h, st in self.fam_status.items():
            if st in ("living", "excluded"):
                media_by_living.update(m.get("ref") for m in self.idx.families[h].get("media_list") or [])
        for mh in media_refs:
            m = self.idx.media.get(mh)
            if not m:
                continue
            tags = self.idx.tags_of(m)
            if m.get("private"):
                self.media_access[mh] = ("excluded", "private")
                continue
            if self.withheld("media", mh):
                self.media_access[mh] = ("excluded", "override: withhold")
                continue
            if mh in media_by_living:
                self.media_access[mh] = ("login", "attached to a living or excluded person")
            elif self.ov.has("public_media", m):
                self.media_access[mh] = ("public", "override: public_media")
            elif self.args.public_media_tag in tags:
                if looks_like_record_image(m.get("path", ""), tags):
                    self.media_access[mh] = ("login", f"tagged {self.args.public_media_tag} but looks like an Ancestry/FamilySearch record image "
                                                      "(path or import tag) — add to overrides.public_media to force")
                else:
                    self.media_access[mh] = ("public", f"tagged {self.args.public_media_tag}")
            else:
                self.media_access[mh] = ("login", "not tagged public (record image stays behind the login)")
            if self.media_access[mh][0] != "excluded":
                inc["media"].add(mh)
                note_refs.update(self._note_refs(m))
                cit_refs2 = self._citation_refs(m)
                for ch in cit_refs2:
                    cit = self.idx.citations.get(ch)
                    if cit and not cit.get("private"):
                        inc["citations"].add(ch)
                        sh = cit.get("source_handle")
                        if sh and sh in self.idx.sources and not self.idx.sources[sh].get("private"):
                            inc["sources"].add(sh)
        # notes
        for nh in note_refs:
            n = self.idx.notes.get(nh)
            if n and not n.get("private") and not self.withheld("notes", nh):
                inc["notes"].add(nh)
        self.ev_withheld = collections.Counter(withheld_events.values())
        self.media_by_living = media_by_living

    # ---- id maps --------------------------------------------------------- #
    def assign_ids(self):
        seen = collections.defaultdict(dict)
        for table, objs in self.idx.by_table.items():
            for h, o in objs.items():
                gid = o.get("gramps_id") or ""
                if not gid:
                    gid = f"{ID_PREFIX[table]}_{h[:8]}"
                    self.report.warn(f"{table} {h} has no Gramps ID; using {gid}")
                if gid in seen[table]:
                    self.report.warn(f"duplicate Gramps ID {gid} in {table} ({h} and {seen[table][gid]})")
                    gid = f"{gid}_{h[:6]}"
                seen[table][gid] = h
                self.ids[(table, h)] = gid

    def gid(self, table: str, h: str | None) -> str | None:
        if not h:
            return None
        return self.ids.get((table, h))

    def pub(self, table: str, h: str | None) -> str | None:
        """gramps id if the object is published, else None"""
        if h and h in self.included.get(table, ()):
            return self.gid(table, h)
        return None

    # ---- bundle ---------------------------------------------------------- #
    def build_bundle(self) -> dict:
        idx, inc = self.idx, self.included
        B = {"meta": {}, "people": {}, "families": {}, "events": {}, "places": {},
             "citations": {}, "sources": {}, "repositories": {}, "notes": {}, "media": {}}
        cited_by = collections.defaultdict(list)
        note_used_by = collections.defaultdict(list)
        media_used_by = collections.defaultdict(list)

        def cites(obj: dict, owner: tuple):
            out = []
            for ch in self._citation_refs(obj):
                g = self.pub("citations", ch)
                if g:
                    out.append(g)
                    cited_by[g].append(owner)
            return out

        def notes(obj: dict, owner: tuple):
            out = []
            for nh in self._note_refs(obj):
                g = self.pub("notes", nh)
                if g:
                    out.append(g)
                    note_used_by[g].append(owner)
            return out

        def media(obj: dict, owner: tuple):
            out = []
            for m in obj.get("media_list") or []:
                if m.get("private"):
                    continue
                g = self.pub("media", m.get("ref"))
                if g:
                    rect = m.get("rect") or None
                    out.append({"ref": g, "rect": rect} if rect else {"ref": g})
                    media_used_by[g].append(owner)
            return out

        def attrs(obj: dict, owner: tuple, skip=()):
            out = []
            for a in obj.get("attribute_list") or []:
                if a.get("private"):
                    continue
                t = typestr(a.get("type"))
                if t in skip:
                    continue
                d = {"type": t, "value": a.get("value", "")}
                c = cites(a, owner)
                if c:
                    d["citations"] = c
                n = notes(a, owner)
                if n:
                    d["notes"] = n
                out.append(d)
            return out

        # events
        for h in inc["events"]:
            ev = idx.events[h]
            g = self.gid("events", h)
            owner = ("event", g)
            rec = {
                "id": g, "handle": h, "type": typestr(ev.get("type")) or "Unknown",
                "date": date_obj(ev.get("date")),
                "place": self.pub("places", ev.get("place") or None),
                "description": ev.get("description") or "",
                "participants": [{"person": self.gid("people", ph), "role": role}
                                 for ph, role in self.event_participants.get(h, [])],
                "families": [{"family": self.gid("families", fh), "role": role}
                             for fh, role in self.event_families.get(h, [])],
            }
            rec.update(review_of(idx.tags_of(ev)))
            rec["tags"] = idx.tags_of(ev)
            rec["attributes"] = attrs(ev, owner)
            rec["citations"] = cites(ev, owner)
            rec["notes"] = notes(ev, owner)
            rec["media"] = media(ev, owner)
            rec["change"] = ev.get("change")
            B["events"][g] = rec

        # places
        for h in inc["places"]:
            pl = idx.places[h]
            g = self.gid("places", h)
            owner = ("place", g)
            name = (pl.get("name") or {}).get("value") or pl.get("title") or ""
            encl = []
            for pr in pl.get("placeref_list") or []:
                pg = self.pub("places", pr.get("ref"))
                if pg:
                    d = {"ref": pg}
                    dd = date_obj(pr.get("date"))
                    if dd:
                        d["date"] = dd
                    encl.append(d)
            rec = {
                "id": g, "handle": h, "name": name,
                "type": typestr(pl.get("place_type")) or "Unknown",
                "alt_names": [{"value": a.get("value"), "lang": a.get("lang") or "", "date": date_obj(a.get("date"))}
                              for a in (pl.get("alt_names") or []) if a.get("value")],
                "lat": _num(pl.get("lat")), "long": _num(pl.get("long")),
                "code": pl.get("code") or "",
                "enclosed_by": encl,
            }
            rec.update(review_of(idx.tags_of(pl)))
            rec["tags"] = idx.tags_of(pl)
            rec["citations"] = cites(pl, owner)
            rec["notes"] = notes(pl, owner)
            rec["media"] = media(pl, owner)
            rec["urls"] = [{"url": u.get("path"), "desc": u.get("desc", "")}
                           for u in (pl.get("urls") or []) if u.get("path") and not u.get("private")]
            B["places"][g] = rec
        # place display names from the hierarchy (current names, first enclosing ref)
        def chain(g, depth=0):
            rec = B["places"].get(g)
            if not rec or depth > 12:
                return []
            up = rec["enclosed_by"][0]["ref"] if rec["enclosed_by"] else None
            return [rec["name"]] + (chain(up, depth + 1) if up else [])
        for g, rec in B["places"].items():
            parts = [p for p in chain(g) if p]
            rec["display"] = ", ".join(parts)
            rec["hierarchy"] = [x for x in _chain_ids(B["places"], g)]

        # people
        for h in inc["people"]:
            p = idx.people[h]
            g = self.gid("people", h)
            st = self.status[h]
            rec = {
                "id": g, "handle": h, "status": st,
                "name": name_of(p), "gender": GENDER.get(p.get("gender", 2), "U"),
                "parents": [self.pub("families", fh) for fh in p.get("parent_family_list") or []],
                "families": [self.pub("families", fh) for fh in p.get("family_list") or []],
            }
            rec["parents"] = [x for x in rec["parents"] if x]
            rec["families"] = [x for x in rec["families"] if x]
            if st == "living":
                rec["name"] = {k: rec["name"][k] for k in ("first", "surname", "prefix", "suffix", "title",
                                                             "call", "nick", "display", "sort")}
                B["people"][g] = rec
                continue
            owner = ("person", g)
            rec.update(review_of(idx.tags_of(p)))
            rec["tags"] = idx.tags_of(p)
            rec["alt_names"] = alt_names_of(p)
            evs = []
            for ref in p.get("event_ref_list") or []:
                eg = self.pub("events", ref.get("ref"))
                if not eg or ref.get("private"):
                    continue
                d = {"ref": eg, "role": typestr(ref.get("role")) or "Primary"}
                c = cites(ref, owner)
                if c:
                    d["citations"] = c
                n = notes(ref, owner)
                if n:
                    d["notes"] = n
                evs.append(d)
            rec["events"] = evs
            # birth / death convenience fields
            for kind, types in (("birth", ("Birth", "Baptism", "Christening")),
                                ("death", ("Death", "Burial", "Cremation"))):
                best = None
                for d in evs:
                    ev = B["events"].get(d["ref"])
                    if d["role"] == "Primary" and ev and ev["type"] in types:
                        rank = types.index(ev["type"])
                        if best is None or rank < best[0]:
                            best = (rank, ev)
                if best:
                    ev = best[1]
                    rec[kind] = {"event": ev["id"], "type": ev["type"], "date": ev.get("date"),
                                 "year": (ev.get("date") or {}).get("year"), "place": ev.get("place")}
            pn = p.get("primary_name") or {}
            nc = cites(pn, owner) if not pn.get("private") else []
            if nc:
                rec["name"]["citations"] = nc
            nn = notes(pn, owner) if not pn.get("private") else []
            if nn:
                rec["name"]["notes"] = nn
            xref = {}
            for a in p.get("attribute_list") or []:
                t = typestr(a.get("type"))
                if t in PERSON_XREF_ATTRS and not a.get("private"):
                    xref[PERSON_XREF_ATTRS[t]] = a.get("value", "")
            rec["xref"] = xref
            rec["attributes"] = attrs(p, owner, skip=tuple(PERSON_XREF_ATTRS))
            rec["citations"] = cites(p, owner)
            rec["notes"] = notes(p, owner)
            rec["media"] = media(p, owner)
            rec["urls"] = [{"url": u.get("path"), "desc": u.get("desc", ""), "type": typestr(u.get("type"))}
                           for u in (p.get("urls") or []) if u.get("path") and not u.get("private")]
            rec["change"] = p.get("change")
            B["people"][g] = rec

        # families
        for h in inc["families"]:
            f = idx.families[h]
            g = self.gid("families", h)
            st = self.fam_status[h]
            rec = {
                "id": g, "handle": h, "status": st,
                "father": self.pub("people", f.get("father_handle") or None),
                "mother": self.pub("people", f.get("mother_handle") or None),
                "children": [],
            }
            for cr in f.get("child_ref_list") or []:
                if cr.get("private"):
                    continue
                cg = self.pub("people", cr.get("ref"))
                if not cg:
                    continue
                ch = {"ref": cg}
                frel, mrel = typestr(cr.get("frel")), typestr(cr.get("mrel"))
                if st == "full":
                    if frel and frel != "Birth":
                        ch["frel"] = frel
                    if mrel and mrel != "Birth":
                        ch["mrel"] = mrel
                rec["children"].append(ch)
            if st == "living":
                B["families"][g] = rec
                continue
            owner = ("family", g)
            rec["type"] = typestr(f.get("type")) or "Married"
            rec.update(review_of(idx.tags_of(f)))
            rec["tags"] = idx.tags_of(f)
            evs = []
            for ref in f.get("event_ref_list") or []:
                eg = self.pub("events", ref.get("ref"))
                if not eg or ref.get("private"):
                    continue
                d = {"ref": eg, "role": typestr(ref.get("role")) or "Family"}
                c = cites(ref, owner)
                if c:
                    d["citations"] = c
                n = notes(ref, owner)
                if n:
                    d["notes"] = n
                evs.append(d)
            rec["events"] = evs
            for d in evs:
                ev = B["events"].get(d["ref"])
                if ev and ev["type"] == "Marriage":
                    rec["marriage"] = {"event": ev["id"], "date": ev.get("date"),
                                       "year": (ev.get("date") or {}).get("year"), "place": ev.get("place")}
                    break
            rec["attributes"] = attrs(f, owner)
            rec["citations"] = cites(f, owner)
            rec["notes"] = notes(f, owner)
            rec["media"] = media(f, owner)
            rec["change"] = f.get("change")
            B["families"][g] = rec

        # citations
        for h in inc["citations"]:
            cit = idx.citations[h]
            g = self.gid("citations", h)
            owner = ("citation", g)
            rec = {
                "id": g, "handle": h,
                "source": self.pub("sources", cit.get("source_handle") or None),
                "page": cit.get("page") or "",
                "date": date_obj(cit.get("date")),
                "confidence": int(cit.get("confidence", 2) or 0),
            }
            extra = {}
            for a in cit.get("attribute_list") or []:
                if a.get("private"):
                    continue
                t = typestr(a.get("type"))
                if t in CITATION_ATTRS:
                    rec[CITATION_ATTRS[t]] = a.get("value", "")
                else:
                    extra[t] = a.get("value", "")
            rec.setdefault("tier", None)
            if rec["tier"] and rec["tier"] not in TIERS:
                self.report.warn(f"citation {g}: unknown Tier value {rec['tier']!r}")
            if extra:
                rec["attributes"] = extra
            rec.update(review_of(idx.tags_of(cit)))
            rec["tags"] = idx.tags_of(cit)
            rec["notes"] = notes(cit, owner)
            rec["media"] = media(cit, owner)
            rec["change"] = cit.get("change")
            B["citations"][g] = rec

        # sources
        for h in inc["sources"]:
            src = idx.sources[h]
            g = self.gid("sources", h)
            owner = ("source", g)
            rec = {
                "id": g, "handle": h, "title": src.get("title") or "",
                "author": src.get("author") or "", "pubinfo": src.get("pubinfo") or "",
                "abbrev": src.get("abbrev") or "",
                "repositories": [],
            }
            for rr in src.get("reporef_list") or []:
                rg = self.pub("repositories", rr.get("ref"))
                if rg and not rr.get("private"):
                    rec["repositories"].append({"ref": rg, "call_number": rr.get("call_number") or "",
                                                "media_type": typestr(rr.get("media_type"))})
            rec["attributes"] = {typestr(a.get("type")): a.get("value", "")
                                 for a in (src.get("attribute_list") or []) if not a.get("private")}
            rec.update(review_of(idx.tags_of(src)))
            rec["tags"] = idx.tags_of(src)
            rec["notes"] = notes(src, owner)
            rec["media"] = media(src, owner)
            B["sources"][g] = rec

        # repositories
        for h in inc["repositories"]:
            repo = idx.repositories[h]
            g = self.gid("repositories", h)
            rec = {"id": g, "handle": h, "name": repo.get("name") or "",
                   "type": typestr(repo.get("type")) or "Unknown",
                   "urls": [{"url": u.get("path"), "desc": u.get("desc", "")}
                            for u in (repo.get("urls") or []) if u.get("path") and not u.get("private")],
                   "notes": notes(repo, ("repository", g))}
            B["repositories"][g] = rec

        # notes
        for h in inc["notes"]:
            n = idx.notes[h]
            g = self.gid("notes", h)
            text = n.get("text")
            rec = {"id": g, "handle": h, "type": typestr(n.get("type")) or "General",
                   "text": plain_text(text), "html": styled_to_html(text if isinstance(text, dict) else plain_text(text)),
                   "preformatted": bool(n.get("format")),
                   "change": n.get("change")}
            rec.update(review_of(idx.tags_of(n)))
            rec["tags"] = idx.tags_of(n)
            B["notes"][g] = rec

        # media
        for h in inc["media"]:
            m = idx.media[h]
            g = self.gid("media", h)
            access, why = self.media_access[h]
            owner = ("media", g)
            ext = os.path.splitext(m.get("path") or "")[1].lower() or _ext_from_mime(m.get("mime"))
            rec = {"id": g, "handle": h, "access": access, "desc": m.get("desc") or "",
                   "mime": m.get("mime") or "", "date": date_obj(m.get("date")),
                   "checksum": m.get("checksum") or ""}
            if access == "public":
                rec["file"] = f"media/{g}{ext}"
                rec["thumb"] = f"media/thumbs/{g}.jpg"
            else:
                rec["private_url"] = self.args.private_site.rstrip("/") + f"/media/{g}"
            rec.update(review_of(idx.tags_of(m)))
            rec["tags"] = idx.tags_of(m)
            rec["attributes"] = {typestr(a.get("type")): a.get("value", "")
                                 for a in (m.get("attribute_list") or []) if not a.get("private")}
            rec["citations"] = cites(m, owner)
            rec["notes"] = notes(m, owner)
            B["media"][g] = rec

        # reverse indexes
        for g, owners in cited_by.items():
            if g in B["citations"]:
                B["citations"][g]["cited_by"] = [{"type": t, "id": i} for t, i in sorted(set(owners))]
        for g in B["citations"]:
            B["citations"][g].setdefault("cited_by", [])
        for g, owners in note_used_by.items():
            if g in B["notes"]:
                B["notes"][g]["attached_to"] = [{"type": t, "id": i} for t, i in sorted(set(owners))]
        for g in B["notes"]:
            B["notes"][g].setdefault("attached_to", [])
        for g, owners in media_used_by.items():
            if g in B["media"]:
                B["media"][g]["attached_to"] = [{"type": t, "id": i} for t, i in sorted(set(owners))]
        for g in B["media"]:
            B["media"][g].setdefault("attached_to", [])
        # sources: which citations use them
        src_cits = collections.defaultdict(list)
        for g, c in B["citations"].items():
            if c.get("source"):
                src_cits[c["source"]].append(g)
        for g in B["sources"]:
            B["sources"][g]["citations"] = sorted(src_cits.get(g, []))
        # prune empty note/media objects nobody references after all (shouldn't happen, but be strict)
        B["notes"] = {g: n for g, n in B["notes"].items() if n["attached_to"]}
        B["media"] = {g: m for g, m in B["media"].items() if m["attached_to"]}

        meta = self.data.get("metadata") or {}
        B["meta"] = {
            "schema": SCHEMA_VERSION,
            "generator": f"gramps_export.py {VERSION}",
            "generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "today": self.today.isoformat(),
            "tree": (meta.get("database") or {}).get("name") or "",
            "gramps_version": (meta.get("gramps") or {}).get("version") or "",
            "gramps_webapi_version": (meta.get("gramps_webapi") or {}).get("version") or "",
            "living_rule": {"source": "gramps-api" if self.data.get("living") else "local",
                            "max_age": self.args.max_age,
                            "overrides_file": os.path.basename(self.ov.path) if self.ov.path else None},
            "private_site": self.args.private_site.rstrip("/"),
            "review_states": {"human": "reviewed by a person", "ai": "checked by the AI validator against its citation",
                              "unreviewed": "not yet checked"},
            "tiers": TIERS,
            "confidence": ["very low", "low", "normal", "high", "very high"],
            "counts": {k: len(v) for k, v in B.items() if k != "meta"},
            "counts_people": {"full": sum(1 for p in B["people"].values() if p["status"] == "full"),
                              "living": sum(1 for p in B["people"].values() if p["status"] == "living")},
        }
        self.bundle = B
        return B

    # ---- report sections for secondary objects --------------------------- #
    def report_secondary(self):
        idx, inc, r = self.idx, self.included, self.report
        # events
        n_all = len(idx.events)
        n_priv = sum(1 for e in idx.events.values() if e.get("private"))
        n_inc = len(inc["events"])
        shared = self.ev_withheld.get("shared with a living or excluded person", 0)
        forced = self.ev_withheld.get("override: withhold", 0)
        orphan = n_all - n_inc - shared - forced - n_priv
        r.add("Events", f"| | n |\n|---|---:|\n| total | {n_all} |\n| published | {n_inc} |\n"
                        f"| withheld — shared with a living or excluded person | {shared} |\n"
                        f"| withheld — private | {n_priv} |\n| withheld — overrides.withhold | {forced} |\n"
                        f"| only attached to living/excluded people (their own events) | {max(orphan, 0)} |")
        types = collections.Counter(typestr(idx.events[h].get("type")) for h in inc["events"])
        r.add("Events", "")
        r.add("Events", "Published by type: " + ", ".join(f"{t} {n}" for t, n in types.most_common()))
        undated = sum(1 for h in inc["events"] if not date_year(idx.events[h].get("date")))
        r.add("Events", f"\nPublished events without a usable date: {undated}")
        # places
        n_pl = len(inc["places"])
        with_coords = sum(1 for h in inc["places"] if _num(idx.places[h].get("lat")) and _num(idx.places[h].get("long")))
        r.add("Places", f"| | n |\n|---|---:|\n| in Gramps | {len(idx.places)} |\n| published (reachable from published events, with their enclosing places) | {n_pl} |\n"
                        f"| with coordinates | {with_coords} |\n| without coordinates (v1 geocoding backfill) | {n_pl - with_coords} |")
        # citations
        cits = [idx.citations[h] for h in inc["citations"]]
        with_img = sum(1 for c in cits if any(not m.get("private") for m in (c.get("media_list") or [])))
        tiers = collections.Counter()
        conf = collections.Counter()
        rev = collections.Counter()
        for c in cits:
            tier = next((a.get("value") for a in (c.get("attribute_list") or []) if typestr(a.get("type")) == "Tier"), None)
            tiers[tier or "(no Tier attribute)"] += 1
            conf[int(c.get("confidence", 2) or 0)] += 1
            rev[review_of(idx.tags_of(c))["review"]] += 1
        n_priv_c = sum(1 for c in idx.citations.values() if c.get("private"))
        r.add("Citations", f"| | n |\n|---|---:|\n| in Gramps | {len(idx.citations)} |\n| published | {len(cits)} |\n"
                           f"| with a record image (public or behind the login) | {with_img} |\n| without an image | {len(cits) - with_img} |\n"
                           f"| withheld — private | {n_priv_c} |\n| withheld — only used by living/excluded people | {len(idx.citations) - len(cits) - n_priv_c} |")
        r.add("Citations", "")
        r.add("Citations", "By tier: " + ", ".join(f"{t} {n}" for t, n in tiers.most_common()))
        r.add("Citations", "By confidence: " + ", ".join(f"{k} {conf[k]}" for k in sorted(conf)))
        r.add("Citations", "By review state: " + ", ".join(f"{k} {v}" for k, v in rev.most_common()))
        # sources / repos / notes
        notes_inc = [idx.notes[h] for h in inc["notes"]]
        ntypes = collections.Counter(typestr(n.get("type")) for n in notes_inc)
        r.add("Sources, repositories, notes",
              f"- sources: {len(inc['sources'])} of {len(idx.sources)} published\n"
              f"- repositories: {len(inc['repositories'])} of {len(idx.repositories)} published\n"
              f"- notes: {len(notes_inc)} of {len(idx.notes)} published "
              f"({sum(1 for n in idx.notes.values() if n.get('private'))} private); by type: "
              + ", ".join(f"{t} {n}" for t, n in ntypes.most_common()))
        # media
        acc = collections.Counter(a for a, _w in self.media_access.values())
        whys = collections.Counter(f"{a}: {w}" for a, w in self.media_access.values())
        unref = len(idx.media) - len(self.media_access)
        r.add("Media", f"| | n |\n|---|---:|\n| in Gramps | {len(idx.media)} |\n| **public** (file copied to media/) | {acc.get('public', 0)} |\n"
                       f"| **login-only** (metadata published, file stays on the private site) | {acc.get('login', 0)} |\n"
                       f"| excluded (private) | {acc.get('excluded', 0)} |\n| not reachable from any published object | {unref} |")
        r.add("Media", "")
        r.add("Media", "\n".join(f"- {k} — {n}" for k, n in whys.most_common()))
        pub = [h for h, (a, _w) in self.media_access.items() if a == "public"]
        if pub:
            r.add("Media", "")
            r.add("Media", "Public files:\n\n" + "\n".join(
                f"- {idx.media[h].get('gramps_id')} {idx.media[h].get('desc') or ''} ({idx.media[h].get('path')})" for h in sorted(pub)))
        # tags
        tag_counts = collections.defaultdict(collections.Counter)
        for table, objs in idx.by_table.items():
            for h, o in objs.items():
                for t in idx.tags_of(o):
                    tag_counts[t][table] += 1
        lines = ["| tag | " + " | ".join(idx.by_table) + " |", "|---|" + "---:|" * len(idx.by_table)]
        for t in sorted(tag_counts):
            lines.append(f"| {t} | " + " | ".join(str(tag_counts[t].get(tb, 0) or "") for tb in idx.by_table) + " |")
        r.add("Tags in Gramps (all objects, before filtering)", "\n".join(lines))
        unknown = sorted(t for t in tag_counts if t not in REVIEW_TAGS and t not in REVIEW_FLAGS
                         and not t.startswith(PROVENANCE_PREFIXES) and t != self.args.public_media_tag)
        if unknown:
            r.warn("tags not in the operating model (ignored for review state): " + ", ".join(unknown))
        no_review = collections.Counter()
        for table in ("people", "families", "events", "citations", "media"):
            for h in inc[table]:
                tags = idx.tags_of(idx.by_table[table][h])
                if not any(t in REVIEW_TAGS for t in tags):
                    no_review[table] += 1
        if no_review:
            r.warn("published objects with NO review tag at all (shown as unreviewed): "
                   + ", ".join(f"{t} {n}" for t, n in no_review.items()))
        applied = {k: sorted(self.ov.sets[k] - self.ov.unmatched[k]) for k in self.ov.KEYS}
        if any(applied.values()):
            r.add("Overrides applied", "\n".join(f"- {k}: {', '.join(v)}" for k, v in applied.items() if v))
        for k, s in self.ov.unmatched.items():
            if s:
                r.warn(f"overrides.{k}: ids not found in Gramps: {', '.join(sorted(s))}")
        if ASTERISK_NAMES:
            r.warn(f"leading asterisks stripped from {len(ASTERISK_NAMES)} names (Ancestry artefact — fix in Gramps, research log A6): "
                   + ", ".join(sorted(ASTERISK_NAMES)[:20]) + (" …" if len(ASTERISK_NAMES) > 20 else ""))
        # heuristic: published free text that names a living or excluded person in full
        names = {}
        for h in self.living_set | self.excluded_set:
            nm = name_of(idx.people[h])
            if nm["first"] and nm["surname"]:
                names[f"{nm['first']} {nm['surname']}".lower()] = idx.people[h].get("gramps_id")
        hits = []
        if names:
            texts = [("note", idx.notes[h].get("gramps_id"), plain_text(idx.notes[h].get("text"))) for h in inc["notes"]]
            texts += [("event", idx.events[h].get("gramps_id"), idx.events[h].get("description") or "") for h in inc["events"]]
            for kind, gid, text in texts:
                low = text.lower()
                for full, pid in names.items():
                    if full in low:
                        hits.append(f"{kind} {gid} mentions {pid}")
        if hits:
            r.warn("published text may name living/excluded people (review, then overrides.withhold if needed): "
                   + "; ".join(hits[:30]) + (" …" if len(hits) > 30 else ""))

    # ---- leak audit ------------------------------------------------------ #
    def audit(self, B: dict) -> list[str]:
        problems = []
        living_ids = {g for g, p in B["people"].items() if p["status"] == "living"}
        excluded_ids = {self.gid("people", h) for h in self.excluded_set}
        allowed_living = {"id", "handle", "status", "name", "gender", "parents", "families"}
        for g, p in B["people"].items():
            if p["status"] == "living":
                extra = set(p) - allowed_living
                if extra:
                    problems.append(f"living person {g} carries {sorted(extra)}")
                if set(p["name"]) & {"citations", "notes", "date"}:
                    problems.append(f"living person {g}: name carries citations/notes")
        for g, f in B["families"].items():
            if f["status"] == "living" and set(f) - {"id", "handle", "status", "father", "mother", "children"}:
                problems.append(f"living family {g} carries extra keys")
            for key in ("father", "mother"):
                if f.get(key) and f[key] not in B["people"]:
                    problems.append(f"family {g}.{key} -> unknown person {f[key]}")
            for c in f["children"]:
                if c["ref"] not in B["people"]:
                    problems.append(f"family {g} child -> unknown person {c['ref']}")
        for g, e in B["events"].items():
            for part in e["participants"]:
                if part["person"] in living_ids or part["person"] in excluded_ids:
                    problems.append(f"event {g} names living/excluded person {part['person']}")
                if part["person"] not in B["people"]:
                    problems.append(f"event {g} -> unknown person {part['person']}")
            if e.get("place") and e["place"] not in B["places"]:
                problems.append(f"event {g} -> unknown place {e['place']}")
        for g, c in B["citations"].items():
            if c.get("source") and c["source"] not in B["sources"]:
                problems.append(f"citation {g} -> unknown source {c['source']}")
            for o in c["cited_by"]:
                if o["type"] == "person" and (o["id"] in living_ids or o["id"] in excluded_ids):
                    problems.append(f"citation {g} cited by living/excluded person {o['id']}")
        for g, n in B["notes"].items():
            for o in n["attached_to"]:
                if o["type"] == "person" and (o["id"] in living_ids or o["id"] in excluded_ids):
                    problems.append(f"note {g} attached to living/excluded person {o['id']}")
        for g, m in B["media"].items():
            if m["access"] == "public" and self.idx.media and any(
                    h in self.media_by_living for h in [m["handle"]]):
                problems.append(f"public media {g} is attached to a living person")
            for o in m["attached_to"]:
                if o["type"] == "person" and (o["id"] in living_ids or o["id"] in excluded_ids):
                    problems.append(f"media {g} attached to living/excluded person {o['id']}")
        # private flags must never survive
        def walk(x, path):
            if isinstance(x, dict):
                if x.get("private") is True:
                    problems.append(f"private flag survived at {path}")
                for k, v in x.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(x, list):
                for i, v in enumerate(x):
                    walk(v, f"{path}[{i}]")
        walk(B, "bundle")
        # every private object in Gramps must be absent
        for table, objs in self.idx.by_table.items():
            for h, o in objs.items():
                if o.get("private") and h in self.included.get(table, ()):
                    problems.append(f"private {table} {h} was included")
        return problems


def _chain_ids(places: dict, g: str, depth: int = 0) -> list[str]:
    rec = places.get(g)
    if not rec or depth > 12:
        return []
    up = rec["enclosed_by"][0]["ref"] if rec["enclosed_by"] else None
    return [g] + (_chain_ids(places, up, depth + 1) if up else [])


def _num(v):
    try:
        if v in (None, "", 0, "0"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _ext_from_mime(mime: str | None) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
            "image/tiff": ".tif", "application/pdf": ".pdf"}.get(mime or "", "")


# --------------------------------------------------------------------------- #
# Media files
# --------------------------------------------------------------------------- #
def write_media(api: Api | None, exp: Exporter, out_dir: str, raw_dir: str | None, thumb: int, progress) -> tuple[int, int]:
    media_dir = os.path.join(out_dir, "media")
    thumb_dir = os.path.join(media_dir, "thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    keep = set()
    copied = failed = 0
    for g, m in exp.bundle["media"].items():
        if m["access"] != "public":
            continue
        dest = os.path.join(out_dir, m["file"])
        tdest = os.path.join(out_dir, m["thumb"])
        keep.update([os.path.abspath(dest), os.path.abspath(tdest)])
        h = m["handle"]
        try:
            if api is not None:
                if not os.path.exists(dest) or _md5(dest) != (m.get("checksum") or "x"):
                    data = api.get_bytes(f"media/{h}/file")
                    with open(dest, "wb") as f:
                        f.write(data)
                if not os.path.exists(tdest) or os.path.getmtime(tdest) < os.path.getmtime(dest):
                    tdata = api.get_bytes(f"media/{h}/thumbnail/{thumb}", {"square": "0"})
                    with open(tdest, "wb") as f:
                        f.write(tdata)
                copied += 1
            elif raw_dir and os.path.exists(os.path.join(raw_dir, "files", h)):
                shutil.copyfile(os.path.join(raw_dir, "files", h), dest)
                shutil.copyfile(os.path.join(raw_dir, "files", h), tdest)
                copied += 1
            else:
                progress(f"  no source for public media {g} (offline run); skipped")
                failed += 1
        except (ApiError, OSError) as e:
            failed += 1
            exp.report.warn(f"media {g}: {e}")
    # remove files of media that are no longer public
    for root in (media_dir, thumb_dir):
        for name in os.listdir(root):
            path = os.path.abspath(os.path.join(root, name))
            if os.path.isfile(path) and path not in keep:
                os.remove(path)
                progress(f"  removed stale {os.path.relpath(path, out_dir)}")
    return copied, failed


def _md5(path: str) -> str:
    """Gramps stores an MD5 of the file as the media checksum."""
    hsh = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hsh.update(chunk)
    return hsh.hexdigest()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing (except --raw-dir cache)")
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"),
                    help="Gramps Web API base URL (default http://127.0.0.1:5055/api)")
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"), help="Gramps Web user (or GRAMPS_USER)")
    ap.add_argument("--password-file", help="file holding the password (chmod 600); else GRAMPS_PASSWORD or a prompt")
    ap.add_argument("--out", default=os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")),
                    help="site repo root; writes data/bundle.json, data/report.md, media/ (default: parent of this script)")
    ap.add_argument("--overrides", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "overrides.json"),
                    help="per-person overrides JSON (default export/overrides.json)")
    ap.add_argument("--raw-dir", help="cache the raw API responses here (debugging / offline re-runs)")
    ap.add_argument("--from-raw", help="build from a raw cache instead of the API (no login; media copied from <dir>/files/<handle>)")
    ap.add_argument("--max-age", type=int, default=110, help="years after birth a person is assumed alive (default 110)")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD) for the living rule")
    ap.add_argument("--public-media-tag", default="public", help="Gramps tag that marks a media object public (default: public)")
    ap.add_argument("--private-site", default="https://familytree.konecny.net",
                    help="URL of the logged-in Gramps Web site, for links to login-only media")
    ap.add_argument("--no-living-api", action="store_true", help="do not ask the API's /living endpoint; use the local rule")
    ap.add_argument("--thumb", type=int, default=600, help="thumbnail size for public media (default 600)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()

    def progress(msg):
        print(msg, file=sys.stderr, flush=True)

    report = Report()
    api = None
    if args.from_raw:
        try:
            data = load_raw(args.from_raw)
        except ApiError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        progress(f"loaded raw cache from {args.from_raw}")
    else:
        user = args.user or input("Gramps user: ")
        if args.password_file:
            with open(args.password_file, encoding="utf-8") as f:
                password = f.read().strip()
        else:
            password = os.environ.get("GRAMPS_PASSWORD") or getpass.getpass("Gramps password: ")
        api = Api(args.api, verbose=args.verbose)
        try:
            api.login(user, password)
        except ApiError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        progress(f"logged in to {args.api} as {user}")
        try:
            data = fetch_all(api, args.raw_dir, not args.no_living_api, progress)
        except ApiError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    overrides = Overrides(args.overrides)
    if overrides.path and not os.path.exists(overrides.path):
        report.warn(f"overrides file not found: {overrides.path} (no overrides applied)")
    exp = Exporter(data, overrides, args, report)
    exp.assign_ids()
    exp.classify_people()
    exp.classify_families()
    exp.compute_inclusion()
    exp.report_secondary()
    bundle = exp.build_bundle()
    problems = exp.audit(bundle)
    if problems:
        report.add("LEAK AUDIT — FAILED", "\n".join(f"- {p}" for p in problems))
    else:
        report.add("Leak audit", "- passed: no living/excluded person appears outside name-and-relationship fields; "
                                 "no private object or flag survived; all references resolve")

    meta = data.get("metadata") or {}
    mode = "DRY RUN — nothing written" if args.dry_run else f"export written to {args.out}"
    header = (f"# family.konecny.net export report\n\n"
              f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · {mode} · gramps_export.py {VERSION} · schema {SCHEMA_VERSION}  \n"
              f"Tree: {(meta.get('database') or {}).get('name') or '?'} · Gramps {(meta.get('gramps') or {}).get('version') or '?'} · "
              f"Web API {(meta.get('gramps_webapi') or {}).get('version') or '?'} · "
              f"source: {'raw cache ' + args.from_raw if args.from_raw else args.api}  \n"
              f"Overrides: {overrides.path if overrides.path and os.path.exists(overrides.path) else 'none'}")
    summary = ("| object | in Gramps | published |\n|---|---:|---:|\n"
               + "\n".join(f"| {t} | {len(exp.idx.by_table[t])} | {len(exp.included[t])} |" for t in exp.idx.by_table))
    report.sections = collections.OrderedDict([("Summary", [summary])] + list(report.sections.items()))
    text = report.render(header)
    print(text)

    if args.dry_run:
        progress(f"dry run done in {time.time() - t0:.1f}s ({api.calls if api else 0} API calls); nothing written")
        return 1 if problems else 0

    if problems:
        print("ERROR: leak audit failed; bundle NOT written", file=sys.stderr)
        return 3
    data_dir = os.path.join(args.out, "data")
    os.makedirs(data_dir, exist_ok=True)
    copied, failed = write_media(api, exp, args.out, args.from_raw, args.thumb, progress)
    bundle_path = os.path.join(data_dir, "bundle.json")
    tmp = bundle_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, bundle_path)
    with open(os.path.expanduser("~/gramps-tools/export-report.md"), "w", encoding="utf-8") as f:
        f.write(text)
    size = os.path.getsize(bundle_path) / 1024
    progress(f"wrote {bundle_path} ({size:.0f} KB), ~/gramps-tools/export-report.md (private), {copied} public media files"
             + (f", {failed} failed" if failed else "") + f" in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
