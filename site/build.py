#!/usr/bin/env python3
"""
build.py — static site generator for family.konecny.net

Reads data/bundle.json (written by export/gramps_export.py), site/config.json and
the markdown in content/, and writes a complete static site to _site/.
No network, no database: everything the site shows is in the bundle.

    python3 site/build.py            # -> _site/
    python3 site/build.py --out /tmp/x --bundle path/to/bundle.json
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import html
import json
import os
import re
import shutil
import sys

from jinja2 import Environment, FileSystemLoader, select_autoescape

try:
    import markdown as _markdown
except ImportError:  # pragma: no cover
    _markdown = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

TIER_RANK = {"record": 0, "index": 1, "compiled": 2, "tree": 3, "oral": 4, "ai-draft": 5}
TIER_LABEL = {"record": "record", "index": "index", "compiled": "compiled", "tree": "tree", "oral": "oral", "ai-draft": "AI draft"}
CONF_LABEL = ["very low", "low", "normal", "high", "very high"]
EVENT_LABEL = {
    "Birth": "Born", "Baptism": "Baptized", "Christening": "Christened", "Adult Christening": "Christened as an adult",
    "Death": "Died", "Burial": "Buried", "Cremation": "Cremated", "Cause Of Death": "Cause of death",
    "Marriage": "Married", "Marriage Banns": "Banns", "Marriage License": "Marriage license", "Engagement": "Engaged",
    "Divorce": "Divorced", "Annulment": "Annulled",
    "Immigration": "Immigrated", "Emigration": "Emigrated", "Naturalization": "Naturalized",
    "Census": "Census", "Residence": "Lived at", "Occupation": "Occupation", "Education": "Education",
    "Military Service": "Military service", "Probate": "Probate", "Will": "Will", "Religion": "Religion",
    "Property": "Property", "Retirement": "Retired", "Graduation": "Graduated", "Ordination": "Ordained",
    "Confirmation": "Confirmed", "First Communion": "First communion", "Nobility Title": "Title",
    "Number of Marriages": "Marriages", "Medical Information": "Medical note", "Alternate Parentage": "Alternate parentage",
}
BIRTH_LIKE = ("Birth", "Baptism", "Christening")
DEATH_LIKE = ("Death", "Burial", "Cremation")
CROSSING_TYPES = ("Immigration", "Emigration", "Arrival", "Departure")
# GEDCOM/Ancestry artefacts imported as events: never shown as life events
HIDDEN_EVENT_TYPES = {"_AMTID", "_MTTAG", "_ORIG"}
NOTE_EVENT_TYPES = {"research note", "spouse order research note"}   # shown as research notes
NAME_EVENT_TYPES = {"aka"}                                             # shown with the names


TIER_ALIASES = {"published": "compiled", "hint": "tree", "newspaper": "record", "member-tree": "tree", "ai": "ai-draft"}
_REC_KEYS = ("census", "passenger", "crew list", "draft registration", "naturalization", "church record", "parish",
             "register", "acta publica", "porta fontium", "drouin", "directories", "lutheran", "certificate",
             "herald", "tribune", "intelligencer", "gazette", "reporter", "times", "newspapers.com)", "obituary,")
_COMPILED_KEYS = ("family histories", "genealogy", "lineage", "mayflower", "marriage records, 1560", "great migration",
                  "torrey", "abstract", "find a grave")
_TREE_KEYS = ("family tree", "member tree", "thrulines", "reunion", "wikitree", "geni.com")


def clean_title(t: str) -> str:
    """RootsMagic/Ancestry source titles -> readable: drop <I> tags, leading commas, 'N.p.' and publisher tails."""
    s = re.sub(r"</?i>", "", t or "", flags=re.I).strip().lstrip(",").strip()
    s = re.sub(r"\s*\(N\.p\.:?\s*n\.p\.,?\s*n\.d\.\)\.?\s*$", "", s)
    s = re.sub(r"\s*\((Provo|Lehi|Salt Lake City|Orem)[^()]*\)\.?\s*$", "", s)
    s = re.sub(r"^Ancestry\.com(?: and The Church of Jesus Christ of Latter-day Saints)?,\s*", "", s)
    return s.rstrip(" .") or (t or "")


def estimate_tier(title: str, has_image: bool):
    """-> (tier, estimated?) from the source type, used only when the citation carries no Tier attribute"""
    t = (title or "").lower()
    if any(k in t for k in _TREE_KEYS):
        return "tree"
    if any(k in t for k in _COMPILED_KEYS):
        return "compiled"
    if "index" in t:
        return "index"
    if any(k in t for k in _REC_KEYS):
        if "parish" in t or "acta publica" in t or "porta fontium" in t or "drouin" in t:
            return "record"
        return "record" if has_image else "index"
    return None


# ----------------------------------------------------------------------------- #
# helpers exposed to templates
# ----------------------------------------------------------------------------- #
def typo(s: str) -> str:
    """Straight quotes -> typographic, in plain text only (never on HTML)."""
    if not s:
        return s
    s = re.sub(r'(^|[\s(\[{])"', "\\1\u201c", s)
    s = s.replace('"', "\u201d")
    s = re.sub(r"(^|[\s(\[{])'", "\\1\u2018", s)
    s = s.replace("'", "\u2019")
    return s


def year_text(date: dict | None, unknown: str = "?") -> str:
    if not date or not date.get("year"):
        return unknown
    y = str(date["year"])
    mod = date.get("modifier")
    if mod == "about":
        return f"c.\u00a0{y}"
    if mod == "before":
        return f"before {y}"
    if mod == "after":
        return f"after {y}"
    if mod in ("between", "from-to") and date.get("end", {}).get("year"):
        return f"{y}\u2013{date['end']['year']}"
    return y


def lifespan(p: dict) -> str:
    if p.get("status") != "full":
        return "living"
    b = year_text(p.get("birth", {}).get("date") if p.get("birth") else None, "?")
    d = year_text(p.get("death", {}).get("date") if p.get("death") else None, "?")
    if b == "?" and d == "?":
        return "dates unknown"
    return f"{b}\u2013{d}"


def conf_dots(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(4, n))
    return "\u25cf" * n + "\u25cb" * (4 - n)


def review_label(obj: dict) -> tuple[str, str]:
    """-> (css class, label)"""
    flags = obj.get("flags") or []
    rv = obj.get("review") or "unreviewed"
    if rv == "human":
        return "human", "reviewed"
    if "needs-human" in flags:
        return "needs", "needs a look"
    if rv == "ai":
        return "ai", "AI-checked"
    return "un", "unreviewed"


def md(text: str) -> str:
    if not text:
        return ""
    if _markdown:
        return _markdown.markdown(text, extensions=["tables", "sane_lists"])
    # minimal fallback: paragraphs, headings, lists, emphasis, links
    out, buf, in_list = [], [], False

    def flush():
        nonlocal buf
        if buf:
            out.append("<p>" + " ".join(buf) + "</p>")
            buf = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s:
            flush()
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
        s = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            flush()
            lvl = len(m.group(1)) + 1
            out.append(f"<h{lvl}>{m.group(2)}</h{lvl}>")
            continue
        if s.startswith(("- ", "* ")):
            flush()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{s[2:]}</li>")
            continue
        buf.append(s)
    flush()
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# ----------------------------------------------------------------------------- #
# model
# ----------------------------------------------------------------------------- #
class Site:
    def __init__(self, bundle: dict, config: dict):
        self.B = bundle
        self.cfg = config
        self.people = bundle["people"]
        self.families = bundle["families"]
        self.events = bundle["events"]
        self.places = bundle["places"]
        self.citations = bundle["citations"]
        self.sources = bundle["sources"]
        for src in self.sources.values():
            src["title_raw"] = src.get("title", "")
            src["title"] = clean_title(src.get("title", ""))
        self.repositories = bundle["repositories"]
        self.notes = bundle["notes"]
        self.media = bundle["media"]
        self.meta = bundle["meta"]
        self.warnings: list[str] = []
        self.surname_line = {}
        for line in config.get("lines", []):
            for s in line.get("surnames", []):
                self.surname_line.setdefault(s.lower(), line["color"])
        # reverse: events -> people/family refs already in bundle; places -> events
        self.place_events = collections.defaultdict(list)
        for e in self.events.values():
            if e.get("place"):
                self.place_events[e["place"]].append(e["id"])
        # citations -> which facts, with person names (for citation pages)
        self.ancestry_index = {p["xref"]["ancestry"]: p["id"] for p in self.people.values()
                               if p.get("xref", {}).get("ancestry")}
        self.name_index = collections.defaultdict(list)
        for p in self.people.values():
            self.name_index[p["name"]["display"].lower()].append(p["id"])
        self.living_names = {p["name"]["display"].lower() for p in self.people.values() if p["status"] == "living"
                             and p["name"].get("first") and p["name"].get("surname")}

    # ---- lookups ---------------------------------------------------------- #
    def resolve(self, ref: str) -> str | None:
        """config references: I0123 | ancestry:2127... | name:First Last"""
        if not ref:
            return None
        if ref.startswith("ancestry:"):
            return self.ancestry_index.get(ref[9:])
        if ref.startswith("name:"):
            ids = self.name_index.get(ref[5:].lower(), [])
            if len(ids) > 1:
                self.warnings.append(f"name {ref[5:]!r} matches {len(ids)} people; using {ids[0]}")
            return ids[0] if ids else None
        return ref if ref in self.people else None

    def person(self, pid): return self.people.get(pid)
    def family(self, fid): return self.families.get(fid)
    def event(self, eid): return self.events.get(eid)
    def place(self, pid): return self.places.get(pid)
    def citation(self, cid): return self.citations.get(cid)
    def source(self, sid): return self.sources.get(sid)
    def note(self, nid): return self.notes.get(nid)
    def medium(self, mid): return self.media.get(mid)

    def line_of(self, p: dict) -> str:
        return self.surname_line.get((p.get("name", {}).get("surname") or "").lower(), "neutral")

    def place_name(self, pid, short=False) -> str:
        pl = self.places.get(pid)
        if not pl:
            return ""
        return pl["name"] if short else (pl.get("display") or pl["name"])

    def town(self, pid) -> str:
        """first part of a place name: 'Madison, Dane County, Wisconsin, USA' -> 'Madison'"""
        name = self.place_name(pid, short=True)
        return name.split(",")[0].strip() if name else ""

    def parents_of(self, p: dict) -> list[dict]:
        out = []
        for fid in p.get("parents", []):
            f = self.families.get(fid)
            if not f:
                continue
            for key in ("father", "mother"):
                if f.get(key) and self.people.get(f[key]):
                    out.append(self.people[f[key]])
        return out

    def siblings_of(self, p: dict) -> list[dict]:
        out, seen = [], {p["id"]}
        for fid in p.get("parents", []):
            f = self.families.get(fid)
            for c in (f or {}).get("children", []):
                if c["ref"] not in seen and c["ref"] in self.people:
                    seen.add(c["ref"])
                    out.append(self.people[c["ref"]])
        return out

    def spouse_in(self, f: dict, pid: str) -> dict | None:
        other = f.get("mother") if f.get("father") == pid else f.get("father")
        return self.people.get(other) if other else None

    # ---- facts ------------------------------------------------------------ #
    def sort_key(self, e: dict):
        d = e.get("date") or {}
        return (0, d["sort"]) if d.get("sort") else (1, 0)

    @staticmethod
    def is_bare(e: dict) -> bool:
        """an Ancestry 'deceased'-style marker: no date, place, text, citation or note"""
        return not (e.get("date") or e.get("place") or e.get("description") or e.get("citations") or e.get("notes"))

    def kind(self, e: dict) -> str:
        t = e["type"].lower()
        if e["type"] in HIDDEN_EVENT_TYPES:
            return "hidden"
        if t in NOTE_EVENT_TYPES:
            return "note"
        if t in NAME_EVENT_TYPES:
            return "name"
        if self.is_bare(e):
            return "hidden"
        return "fact"

    def extras_of(self, p: dict) -> dict:
        """research-note and AKA pseudo-events from the Ancestry import, kept out of the fact list"""
        notes, names = [], []
        for ref in p.get("events", []):
            e = self.events.get(ref["ref"])
            if not e:
                continue
            k = self.kind(e)
            if k == "note" and e.get("description"):
                notes.append({"event": e, "text": e["description"], "citations": self.ladder(e.get("citations", []))})
            elif k == "name" and e.get("description"):
                names.append({"event": e, "text": e["description"], "citations": self.ladder(e.get("citations", []))})
        return {"notes": notes, "names": names}

    def facts_of(self, p: dict) -> list[dict]:
        """person's events (own + family events), sorted, with labels and evidence"""
        out = []
        for ref in p.get("events", []):
            e = self.events.get(ref["ref"])
            if not e or self.kind(e) != "fact":
                continue
            out.append(self._fact(e, role=ref.get("role"), ref_citations=ref.get("citations", []),
                                  ref_notes=ref.get("notes", []), person=p))
        for fid in p.get("families", []):
            f = self.families.get(fid)
            if not f or f.get("status") != "full":
                continue
            spouse = self.spouse_in(f, p["id"])
            for ref in f.get("events", []):
                e = self.events.get(ref["ref"])
                if not e or self.kind(e) != "fact":
                    continue
                out.append(self._fact(e, role=ref.get("role"), ref_citations=ref.get("citations", []),
                                      ref_notes=ref.get("notes", []), person=p, spouse=spouse, family=f))
        out.sort(key=lambda x: self.sort_key(x["event"]))
        return out

    def _fact(self, e: dict, role=None, ref_citations=(), ref_notes=(), person=None, spouse=None, family=None) -> dict:
        label = EVENT_LABEL.get(e["type"], e["type"])
        if spouse is not None and e["type"] in ("Marriage", "Engagement", "Divorce", "Marriage Banns", "Marriage License"):
            label = f"{label} {spouse['name']['display']}"
        if role and role not in ("Primary", "Family"):
            label = f"{label} ({role.lower()})"
        cits = list(dict.fromkeys(list(e.get("citations", [])) + list(ref_citations)))
        notes = list(dict.fromkeys(list(e.get("notes", [])) + list(ref_notes)))
        return {
            "event": e, "label": label, "date": e.get("date"), "place": self.places.get(e.get("place")) if e.get("place") else None,
            "description": e.get("description") or "", "citations": self.ladder(cits), "notes": [self.notes[n] for n in notes if n in self.notes],
            "media": [self.media[m["ref"]] for m in e.get("media", []) if m["ref"] in self.media],
            "review": review_label(e), "spouse": spouse, "family": family, "role": role,
            "crossing": e["type"] in CROSSING_TYPES,
        }

    def ladder(self, cit_ids) -> list[dict]:
        rows = []
        for cid in cit_ids:
            c = self.citations.get(cid)
            if not c:
                continue
            src = self.sources.get(c.get("source")) if c.get("source") else None
            tier = TIER_ALIASES.get(c.get("tier"), c.get("tier"))
            est = False
            if tier not in TIER_RANK:
                tier = estimate_tier((src or {}).get("title", ""), bool(c.get("media")))
                est = tier is not None
            label = TIER_LABEL.get(tier, "unclassified") + (" · est." if est else "")
            rows.append({
                "citation": c, "source": src, "tier": tier, "tier_label": label, "tier_est": est,
                "rank": TIER_RANK.get(tier, 9), "confidence": c.get("confidence", 2),
                "conf_label": CONF_LABEL[max(0, min(4, int(c.get("confidence", 2) or 0)))],
                "review": review_label(c),
                "has_image": bool(c.get("media")), "public_image": any(self.media.get(m["ref"], {}).get("access") == "public" for m in c.get("media", [])),
                "notes": [self.notes[n] for n in c.get("notes", []) if n in self.notes],
            })
        rows.sort(key=lambda r: (r["rank"], -int(r["confidence"] or 0)))
        return rows

    def review_summary(self, facts: list[dict]) -> dict:
        c = collections.Counter(f["review"][0] for f in facts)
        return {"total": len(facts), "human": c.get("human", 0), "ai": c.get("ai", 0),
                "un": c.get("un", 0) + c.get("needs", 0), "needs": c.get("needs", 0)}

    # ---- lines (spines) --------------------------------------------------- #
    def ancestor_path(self, start: str, goal: str) -> list[tuple[str, str | None]] | None:
        """BFS up the parent links: [(person_id, family_id_below)] from start to goal."""
        prev = {start: None}
        queue = collections.deque([start])
        while queue:
            pid = queue.popleft()
            if pid == goal:
                break
            p = self.people.get(pid)
            if not p:
                continue
            for fid in p.get("parents", []):
                f = self.families.get(fid)
                if not f:
                    continue
                for key in ("father", "mother"):
                    par = f.get(key)
                    if par and par not in prev and par in self.people:
                        prev[par] = (pid, fid)
                        queue.append(par)
        if goal not in prev:
            return None
        path, cur, via = [], goal, None
        while cur is not None:
            path.append((cur, via))
            link = prev[cur]
            if link is None:
                break
            cur, via = link
        path.reverse()  # start ... goal, each with the family that links DOWN to the previous entry
        # rebuild: entry i = (person_i, family through which person_i is a parent of person_{i-1})
        out = []
        for i, (pid, _) in enumerate(path):
            fam_below = None
            if i > 0:
                child = path[i - 1][0]
                for fid in self.people[child].get("parents", []):
                    f = self.families.get(fid)
                    if f and pid in (f.get("father"), f.get("mother")):
                        fam_below = fid
                        break
            out.append((pid, fam_below))
        return out

    def ancestors_by_depth(self, pid: str) -> dict:
        depth, queue = {pid: 0}, collections.deque([pid])
        while queue:
            cur = queue.popleft()
            for fid in self.people.get(cur, {}).get("parents", []):
                f = self.families.get(fid) or {}
                for key in ("father", "mother"):
                    par = f.get(key)
                    if par and par in self.people and par not in depth:
                        depth[par] = depth[cur] + 1
                        queue.append(par)
        return depth

    def deepest(self, start: str | None, line: dict) -> str | None:
        """'to': 'auto' or 'auto:I0073' -> the furthest-back ancestor whose surname belongs to the line"""
        if not start:
            return None
        via = line["to"].split(":", 1)[1] if ":" in line["to"] else None
        base = self.resolve(via) if via else start
        if not base:
            return None
        names = {n.lower() for n in line.get("surnames", [])}
        depth = self.ancestors_by_depth(base)
        hits = [(d, pid) for pid, d in depth.items()
                 if (self.people[pid]["name"].get("surname") or "").lower() in names and self.people[pid]["status"] == "full"]
        if not hits:
            return base
        return max(hits, key=lambda x: (x[0], -((self.people[x[1]].get("birth") or {}).get("year") or 9999)))[1]

    def build_line(self, line: dict) -> dict | None:
        start = self.resolve(line["from"])
        goal = self.deepest(start, line) if str(line.get("to", "")).startswith("auto") else self.resolve(line["to"])
        if not start or not goal:
            self.warnings.append(f"line {line['slug']}: cannot resolve {'from' if not start else 'to'} ({line['from'] if not start else line['to']})")
            return None
        path = self.ancestor_path(start, goal)
        if not path:
            self.warnings.append(f"line {line['slug']}: no ancestor path from {start} to {goal}")
            return None
        stations = []
        for gen, (pid, fam_below) in enumerate(path):
            p = self.people[pid]
            fam = self.families.get(fam_below) if fam_below else None
            spouse = self.spouse_in(fam, pid) if fam else None
            siblings = max(0, len((fam or {}).get("children", [])) - 1) if fam else 0
            birth_pl = self.town(p["birth"]["place"]) if p.get("birth") and p["birth"].get("place") else ""
            death_pl = self.town(p["death"]["place"]) if p.get("death") and p["death"].get("place") else ""
            crossing = None
            for f in self.facts_of(p) if p["status"] == "full" else []:
                if f["crossing"]:
                    crossing = f
                    break
            marriage = fam.get("marriage") if fam and fam.get("status") == "full" else None
            stations.append({
                "gen": gen + 1, "person": p, "spouse": spouse, "family": fam, "siblings": siblings,
                "lifespan": lifespan(p), "birth_place": birth_pl, "death_place": death_pl,
                "crossing": crossing, "marriage": marriage,
                "facts": self.review_summary(self.facts_of(p)) if p["status"] == "full" else None,
            })
        if not any(st["person"]["status"] == "full" for st in stations):
            self.warnings.append(f"line {line['slug']}: only living people so far — not shown yet")
            return None
        return {**line, "stations": stations, "generations": len(stations)}


# ----------------------------------------------------------------------------- #
# build
# ----------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=os.path.join(ROOT, "data", "bundle.json"))
    ap.add_argument("--media", default=os.path.join(ROOT, "media"))
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--content", default=os.path.join(ROOT, "content"))
    ap.add_argument("--out", default=os.path.join(ROOT, "_site"))
    args = ap.parse_args(argv)

    if os.path.exists(args.bundle):
        with open(args.bundle, encoding="utf-8") as f:
            bundle = json.load(f)
    else:
        print(f"warning: {args.bundle} not found — building the placeholder site (run the export first)")
        bundle = {k: {} for k in ("people", "families", "events", "places", "citations", "sources", "repositories", "notes", "media")}
        bundle["meta"] = {"generated": "not yet", "gramps_version": "", "counts_people": {"full": 0, "living": 0},
                          "living_rule": {"max_age": 110}, "counts": {}}
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    site = Site(bundle, cfg)

    env = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                      autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
    env.filters["typo"] = typo
    env.filters["year"] = year_text
    env.filters["lifespan"] = lifespan
    env.filters["dots"] = conf_dots
    env.filters["md"] = md
    env.globals.update({
        "site": site, "cfg": cfg, "meta": bundle["meta"], "review_label": review_label,
        "now": dt.datetime.now(dt.timezone.utc).strftime("%-d %B %Y"),
        "CONF_LABEL": CONF_LABEL, "TIER_LABEL": TIER_LABEL,
    })

    def content(name: str) -> str:
        path = os.path.join(args.content, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        return ""

    out = args.out
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)

    def write(path: str, template: str, **ctx):
        full = os.path.join(out, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tpl = env.get_template(template)
        with open(full, "w", encoding="utf-8") as f:
            f.write(tpl.render(**ctx))

    # lines
    lines = [ln for ln in (site.build_line(l) for l in cfg.get("lines", [])) if ln]
    for ln in lines:
        write(f"line/{ln['slug']}/index.html", "line.html", line=ln, title=ln["title"])

    # people
    people_sorted = sorted(site.people.values(), key=lambda p: (p["name"]["sort"].lower(), p["id"]))
    for p in people_sorted:
        facts = site.facts_of(p) if p["status"] == "full" else []
        fams = []
        for fid in p.get("families", []):
            f = site.families.get(fid)
            if not f:
                continue
            fams.append({"family": f, "spouse": site.spouse_in(f, p["id"]),
                         "children": [site.people[c["ref"]] for c in f.get("children", []) if c["ref"] in site.people]})
        parent_fams = [site.families[fid] for fid in p.get("parents", []) if fid in site.families]
        write(f"person/{p['id']}/index.html", "person.html", p=p, facts=facts, extras=site.extras_of(p) if p["status"] == "full" else {"notes": [], "names": []}, families=fams, parent_families=parent_fams,
              parents=site.parents_of(p), siblings=site.siblings_of(p), summary=site.review_summary(facts),
              line_color=site.line_of(p), title=p["name"]["display"],
              notes=[site.notes[n] for n in p.get("notes", []) if n in site.notes],
              media=[site.media[m["ref"]] for m in p.get("media", []) if m["ref"] in site.media],
              name_citations=site.ladder(p.get("name", {}).get("citations", [])))

    # families
    for f in site.families.values():
        father = site.people.get(f.get("father")) if f.get("father") else None
        mother = site.people.get(f.get("mother")) if f.get("mother") else None
        facts = []
        if f.get("status") == "full":
            for ref in f.get("events", []):
                e = site.events.get(ref["ref"])
                if e and site.kind(e) == "fact":
                    facts.append(site._fact(e, role=ref.get("role"), ref_citations=ref.get("citations", []), ref_notes=ref.get("notes", [])))
            facts.sort(key=lambda x: site.sort_key(x["event"]))
        children = [site.people[c["ref"]] for c in f.get("children", []) if c["ref"] in site.people]
        title = " & ".join(x["name"]["display"] for x in (father, mother) if x) or "Family"
        write(f"family/{f['id']}/index.html", "family.html", f=f, father=father, mother=mother, facts=facts,
              children=children, title=title, notes=[site.notes[n] for n in f.get("notes", []) if n in site.notes],
              media=[site.media[m["ref"]] for m in f.get("media", []) if m["ref"] in site.media])

    # citations
    for c in site.citations.values():
        src = site.sources.get(c.get("source")) if c.get("source") else None
        used = []
        for o in c.get("cited_by", []):
            if o["type"] == "event" and o["id"] in site.events:
                e = site.events[o["id"]]
                who = [site.people[x["person"]] for x in e.get("participants", []) if x["person"] in site.people]
                fams = [site.families[x["family"]] for x in e.get("families", []) if x["family"] in site.families]
                used.append({"type": "event", "event": e, "label": EVENT_LABEL.get(e["type"], e["type"]), "people": who, "families": fams})
            elif o["type"] == "person" and o["id"] in site.people:
                used.append({"type": "person", "person": site.people[o["id"]]})
            elif o["type"] == "family" and o["id"] in site.families:
                used.append({"type": "family", "family": site.families[o["id"]]})
            elif o["type"] == "place" and o["id"] in site.places:
                used.append({"type": "place", "place": site.places[o["id"]]})
            elif o["type"] == "media" and o["id"] in site.media:
                used.append({"type": "media", "media": site.media[o["id"]]})
        row = site.ladder([c["id"]])[0]
        write(f"citation/{c['id']}/index.html", "citation.html", c=c, src=src, used=used, row=row,
              media=[site.media[m["ref"]] for m in c.get("media", []) if m["ref"] in site.media],
              notes=[site.notes[n] for n in c.get("notes", []) if n in site.notes],
              title=(src["title"] if src else "Citation") + (f" — {c['page']}" if c.get("page") else ""))

    # sources
    sources_sorted = sorted(site.sources.values(), key=lambda s: s["title"].lower())
    for s in sources_sorted:
        cits = [site.citations[cid] for cid in s.get("citations", []) if cid in site.citations]
        cits.sort(key=lambda c: (c.get("page") or ""))
        repos = [(site.repositories.get(r["ref"]), r) for r in s.get("repositories", [])]
        write(f"source/{s['id']}/index.html", "source.html", s=s, citations=cits, repos=[x for x in repos if x[0]],
              notes=[site.notes[n] for n in s.get("notes", []) if n in site.notes], title=s["title"])

    # places
    places_sorted = sorted(site.places.values(), key=lambda p: (p.get("display") or p["name"]).lower())
    for pl in places_sorted:
        evs = [site.events[e] for e in site.place_events.get(pl["id"], [])]
        evs.sort(key=site.sort_key)
        rows = []
        for e in evs:
            who = [site.people[x["person"]] for x in e.get("participants", []) if x["person"] in site.people]
            fams = [site.families[x["family"]] for x in e.get("families", []) if x["family"] in site.families]
            rows.append({"event": e, "label": EVENT_LABEL.get(e["type"], e["type"]), "people": who, "families": fams})
        chain = [site.places[x] for x in pl.get("hierarchy", []) if x in site.places]
        within = [q for q in site.places.values() if q.get("enclosed_by") and q["enclosed_by"][0]["ref"] == pl["id"]]
        write(f"place/{pl['id']}/index.html", "place.html", pl=pl, rows=rows, chain=chain, within=sorted(within, key=lambda q: q["name"]),
              title=pl.get("display") or pl["name"])

    # indexes
    by_letter = collections.OrderedDict()
    for p in people_sorted:
        letter = (p["name"]["surname"] or p["name"]["display"] or "?")[:1].upper()
        by_letter.setdefault(letter, []).append(p)
    write("people/index.html", "people.html", by_letter=by_letter, title="People", total=len(people_sorted))
    place_rows = [{"place": pl, "n": len(site.place_events.get(pl["id"], []))} for pl in places_sorted]
    write("places/index.html", "places.html", rows=place_rows, title="Places")
    src_rows = [{"source": s, "n": len(s.get("citations", []))} for s in sources_sorted]
    write("sources/index.html", "sources.html", rows=src_rows, title="Sources")

    # open questions + about + home
    oq = content("open-questions.md")
    if oq and site.living_names:
        kept = []
        for para in oq.split("\n\n"):
            low = para.lower()
            if any(n in low for n in site.living_names):
                site.warnings.append("open-questions.md: dropped a paragraph that names a living person")
                continue
            kept.append(para)
        oq = "\n\n".join(kept)
    write("questions/index.html", "questions.html", body=oq, title="Open questions")
    facts_all = []
    stats = collections.Counter()
    for p in site.people.values():
        if p["status"] == "full":
            fs = site.facts_of(p)
            facts_all.extend(fs)
    rs = site.review_summary(facts_all)
    cit_rev = collections.Counter(review_label(c)[0] for c in site.citations.values())
    stats.update({"people_full": site.meta["counts_people"]["full"], "people_living": site.meta["counts_people"]["living"],
                  "events": len(site.events), "citations": len(site.citations), "sources": len(site.sources),
                  "places": len(site.places), "media_public": sum(1 for m in site.media.values() if m["access"] == "public"),
                  "media_login": sum(1 for m in site.media.values() if m["access"] == "login")})
    write("about/index.html", "about.html", body=content("about.md"), stats=stats, rs=rs, cit_rev=cit_rev, title="About this site")
    recent = sorted([p for p in site.people.values() if p["status"] == "full" and p.get("change")], key=lambda p: -p["change"])[:8]
    sides = []
    for sd in cfg.get("sides") or [{"key": None, "title": "", "subtitle": ""}]:
        ls = [l for l in lines if l.get("side") == sd["key"] or sd["key"] is None]
        if ls:
            sides.append({**sd, "lines": ls})
    write("index.html", "home.html", sides=sides, lines=lines, stats=stats, rs=rs, recent=recent, body=content("home.md"), title=None)
    write("404.html", "404.html", title="Not found")

    # static, data, media, CNAME
    shutil.copytree(os.path.join(HERE, "static"), os.path.join(out, "static"))
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    with open(os.path.join(out, "data", "bundle.json"), "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
    if os.path.isdir(args.media):
        shutil.copytree(args.media, os.path.join(out, "media"), dirs_exist_ok=True)
    cname = os.path.join(ROOT, "CNAME")
    if os.path.exists(cname):
        shutil.copy(cname, os.path.join(out, "CNAME"))
    with open(os.path.join(out, ".nojekyll"), "w") as f:
        f.write("")

    n_pages = sum(len(files) for _, _, files in os.walk(out) if True)
    print(f"built {out}: {len(site.people)} people, {len(site.families)} families, {len(site.citations)} citations, "
          f"{len(site.places)} places, {len(lines)} lines; {n_pages} files")
    for w in site.warnings:
        print("warning:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
