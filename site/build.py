#!/usr/bin/env python3
"""
build.py — static site generator for family.konecny.net

Reads data/bundle.json (written by export/gramps_export.py), site/config.json,
the markdown in content/, the research board in content/research/ and the
hand-maintained pages in content/stories/, and writes a complete static site to _site/.
No network, no database: everything the site shows is in the bundle.

    python3 site/build.py            # -> _site/
    python3 site/build.py --out /tmp/x --bundle path/to/bundle.json
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

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
LINE_SHORT = {"konecny": "Konečný", "tauber": "Tauber", "wood": "Wood", "toepel": "Toepel", "hansen": "Hansen", "morris": "Morris", "all": ""}
NOTE_EVENT_TYPES = {"research note", "spouse order research note"}   # shown as research notes
NAME_EVENT_TYPES = {"aka"}                                             # shown with the names
# how a child is related to a parent, when it is not by birth (Gramps' child reference types)
REL_WORD = {"Adopted": "adopted", "Stepchild": "stepchild", "Foster": "foster child", "Sponsored": "sponsored child"}
GROUP_LABEL = {"adopted": "Adoptive parents", "stepchild": "Step-parents", "foster child": "Foster parents",
               "sponsored child": "Sponsors"}
PARENT_WORD = {("adopted", "father"): "adoptive father", ("adopted", "mother"): "adoptive mother",
               ("stepchild", "father"): "stepfather", ("stepchild", "mother"): "stepmother",
               ("foster child", "father"): "foster father", ("foster child", "mother"): "foster mother",
               ("sponsored child", "father"): "sponsor", ("sponsored child", "mother"): "sponsor"}


def _rel_side(word: str, parent: str) -> str:
    if not parent:
        return word
    return f"adopted by the {parent}" if word == "adopted" else f"{word} of the {parent}"


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
def last_change(path: str):
    """When a file in this repository last changed, from git (the Pages build checks out the whole history).
    None outside a git checkout, or for a file git doesn't track."""
    rel = os.path.relpath(os.path.abspath(path), ROOT)
    if rel.startswith(".."):
        return None
    try:
        r = subprocess.run(["git", "-C", ROOT, "log", "-1", "--format=%cI", "--", rel],
                           capture_output=True, text=True, timeout=30)
        return dt.datetime.fromisoformat(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def fold(s: str) -> str:
    """Diacritics folded, lower-cased: the key for sorting and grouping names so that
    Konečný files with Konecny and Šebák under S, not after Z."""
    return "".join(ch for ch in unicodedata.normalize("NFD", s or "") if not unicodedata.combining(ch)).lower()


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
# family stories (content/stories/) — hand-maintained; the export never writes there
# ----------------------------------------------------------------------------- #
# Hosts that appear as bare citations in the family-stories notes.
_STORY_URL = re.compile(
    r"(?:https?://)?(?:[A-Za-z0-9-]+\.)+(?:org|com|net|cz|info|eu)(?:/[^\s]*)?",
    re.IGNORECASE,
)
# Drafting note in the village caption. It names a local folder, not a source,
# so the public page keeps the Wikimedia credit and leaves the path off.
_STORY_LOCAL_PATH = ", saved on the box at /workspace/stories-photos"


def _story_href(display: str) -> str:
    raw = display if display.startswith(("http://", "https://")) else "https://" + display
    parts = urlsplit(raw)
    return urlunsplit((
        parts.scheme or "https",
        parts.netloc,
        quote(parts.path, safe="/%"),
        quote(parts.query, safe="=&%"),
        quote(parts.fragment, safe="/=&%"),
    ))


def story_html(text: str):
    """Escape story text, apply the site's quotation marks, and link citation URLs.

    The visible characters of each URL stay as written. A trailing period or
    semicolon is left outside the link.
    """
    if not text:
        return Markup("")
    pieces = []
    pos = 0
    for match in _STORY_URL.finditer(text):
        raw = match.group(0)
        trimmed = raw.rstrip(".,;:")
        if not trimmed or match.start() < pos:
            continue
        pieces.append(html.escape(typo(text[pos:match.start()])))
        href = _story_href(trimmed)
        pieces.append(f'<a href="{html.escape(href, quote=True)}">{html.escape(trimmed)}</a>')
        pos = match.start() + len(trimmed)
    pieces.append(html.escape(typo(text[pos:])))
    return Markup("".join(pieces))


def _jpeg_size(path: str):
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                marker = f.read(1)
                if not marker:
                    return None
                if marker != b"\xff":
                    continue
                while marker == b"\xff":
                    marker = f.read(1)
                if marker in (b"\xc0", b"\xc1", b"\xc2"):
                    f.read(3)
                    height, width = struct.unpack(">HH", f.read(4))
                    return width, height
                seg = f.read(2)
                if len(seg) < 2:
                    return None
                f.read(struct.unpack(">H", seg)[0] - 2)
    except OSError:
        return None


def section_lede(sec: dict) -> str:
    """A line for the index card, taken from the section's own first words."""
    kind = sec.get("kind")
    if kind == "entries" and sec.get("entries"):
        return sec["entries"][0]["title"]
    if kind == "table" and sec.get("rows"):
        return sec["rows"][0][1]
    if kind == "places" and sec.get("places"):
        return sec["places"][0]["heading"]
    if kind == "cousins":
        text = (sec.get("callout") or {}).get("text") or ""
        sentence = text.split(". ", 1)[0]
        if sentence and not sentence.endswith("."):
            sentence += "."
        return sentence
    return ""


def load_stories(content_dir: str):
    """Load every story in content/stories/.

    `status` is display-only (`confirming` shows a banner, `proven` shows none).
    A story is never skipped for being a draft or for still being confirmed.
    """
    path = os.path.join(content_dir, "stories", "stories.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    photo_dir = os.path.join(content_dir, "stories", "photos")
    for sec in data.get("sections", []):
        caption = sec.get("caption") or ""
        sec["caption_public"] = caption.replace(_STORY_LOCAL_PATH, "") if _STORY_LOCAL_PATH in caption else caption
        sec["lede"] = section_lede(sec)
        for place in sec.get("places") or []:
            photo = place.get("photo")
            if not photo:
                continue
            full = os.path.join(photo_dir, photo)
            size = _jpeg_size(full) if os.path.exists(full) else None
            if size:
                place["width"], place["height"] = size
            stem, ext = os.path.splitext(photo)
            small = stem + "-800" + ext
            if os.path.exists(os.path.join(photo_dir, small)):
                place["photo_sm"] = small
    return data


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
        self.living_names |= {f'{p["name"]["first"]} {m["surname"]}'.lower() for p in self.people.values()
                              if p["status"] == "living" and p["name"].get("first")
                              for m in self.married_names(p)}
        # parts of content/ that the export found naming a child under 18 (it knows their names; this build
        # doesn't): (path under content/, section or card id, or None for the whole file). They are left out.
        self.minor_text = set()
        for e in self.meta.get("minor_text") or []:
            rel = str(e.get("file") or "")
            rel = rel[len("content/"):] if rel.startswith("content/") else rel
            self.minor_text.add((rel, e.get("id")))

    # ---- married names ----------------------------------------------------- #
    @staticmethod
    def married_names(p: dict) -> list[dict]:
        """the person's Married Name alternates, in the order Gramps keeps them (the last is taken as the latest)"""
        return [a for a in p.get("alt_names") or [] if a.get("type") == "Married Name" and (a.get("surname") or "").strip()]

    def heading_married(self, p: dict) -> dict | None:
        """the married name the heading shows, or None: none filed, a child under 18, or a primary name that
        already carries a married surname (filed that way before the birth name was known; Karel splits those)"""
        if p.get("minor"):
            return None
        ms = self.married_names(p)
        birth = fold(p["name"].get("surname") or "").split()
        if not ms:
            return None
        if birth and any(fold(m["surname"]).split()[-1] in birth for m in ms):
            return None
        return ms[-1]

    def shown_name(self, p: dict) -> str:
        """'Lisa (Konecny) Youmans' for someone with a married name; otherwise the name as filed"""
        m = self.heading_married(p)
        n = p["name"]
        if not m:
            return n["display"]
        birth = " ".join(x for x in (n.get("prefix"), n.get("surname")) if x)
        married = " ".join(x for x in (m.get("prefix"), m.get("surname")) if x)
        # a birth surname that isn't known: the married one alone ("Lois Elizabeth Morris")
        return " ".join(x for x in (n.get("title"), n.get("first"), f"({birth})" if birth else "", married, n.get("suffix")) if x)

    def index_surname(self, p: dict) -> str:
        """the surname a person files under in the index: the birth surname, or the married one when that's unknown"""
        if p["name"].get("surname"):
            return p["name"]["surname"]
        m = self.heading_married(p)
        return m["surname"] if m else ""

    def also_names(self, p: dict) -> list[dict]:
        """the alternate names for the 'also' line: all of them except the married name the heading already shows"""
        m = self.heading_married(p)
        return [a for a in p.get("alt_names") or [] if a is not m]

    def names_child(self, rel: str, part_id=None) -> bool:
        return (rel, None) in self.minor_text or (part_id is not None and (rel, part_id) in self.minor_text)

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

    _HOUSE_PART = re.compile(r"^(house|čp\.?|č\.\s*p\.?|no\.?|nr\.?|#)?\s*\d+[a-z]?$", re.I)

    def town(self, pid) -> str:
        """first part of a place name that is a settlement: 'Madison, Dane County, Wisconsin, USA' -> 'Madison';
        'House 131, Svatý Štěpán, Moravia' -> 'Svatý Štěpán' (house numbers and bare numbers are skipped)"""
        name = self.place_name(pid, short=True)
        if not name:
            return ""
        parts = [x.strip() for x in name.split(",") if x.strip()]
        for part in parts:
            if not self._HOUSE_PART.match(part):
                return part
        return parts[0] if parts else ""

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

    # ---- adopted, step, foster ------------------------------------------- #
    @staticmethod
    def rel_word(v) -> str:
        """Gramps' child-to-parent relationship as a word; '' for a birth child or when it isn't known"""
        if not v or v in ("Birth", "None", "Unknown"):
            return ""
        return REL_WORD.get(v, str(v).lower())

    def child_rel(self, f: dict, ch: dict) -> str:
        """'adopted', 'stepchild of the father' …: how a child in this family is related, '' for a birth child"""
        fw = self.rel_word(ch.get("frel")) if f.get("father") else ""
        mw = self.rel_word(ch.get("mrel")) if f.get("mother") else ""
        if fw and fw == mw:
            return fw
        if fw and mw:
            return f"{_rel_side(fw, 'father')}, {_rel_side(mw, 'mother')}"
        if fw:
            return _rel_side(fw, "father" if f.get("mother") else "")
        if mw:
            return _rel_side(mw, "mother" if f.get("father") else "")
        return ""

    def children_in(self, f: dict) -> list[dict]:
        """the children of a family, each {"person", "rel"}"""
        return [{"person": self.people[c["ref"]], "rel": self.child_rel(f, c)}
                for c in f.get("children", []) if c["ref"] in self.people]

    def parent_groups(self, p: dict) -> list[dict]:
        """one row per family the person is a child in: {"label", "family", "parents": [{"person", "rel"}]}.
        'Adoptive parents' when both are; otherwise 'Parents', with 'stepfather' or the like by the one who is."""
        out = []
        for fid in p.get("parents", []):
            f = self.families.get(fid)
            if not f:
                continue
            ch = next((c for c in f.get("children", []) if c["ref"] == p["id"]), {})
            rows = []
            for key, rel in (("father", ch.get("frel")), ("mother", ch.get("mrel"))):
                q = self.people.get(f[key]) if f.get(key) else None
                if q:
                    rows.append({"person": q, "rel": self.rel_word(rel), "role": key})
            if not rows:
                continue
            words = {r["rel"] for r in rows}
            if len(words) == 1 and next(iter(words)):
                w = next(iter(words))
                label = GROUP_LABEL.get(w, f"Parents ({w})")
                for r in rows:
                    r["rel"] = ""
            else:
                label = "Parents"
                for r in rows:
                    r["rel"] = PARENT_WORD.get((r["rel"], r["role"]), r["rel"])
            if len(rows) == 1 and label.endswith("s"):
                label = label[:-1]
            out.append({"label": label, "family": f, "parents": rows})
        return out

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


def tree_data(site: "Site", lines: list, board: list) -> dict:
    """What the tree explorer (/tree/) draws, written to /data/tree.json: everyone's name and family links, and
    for people shown in full their years, birth and death towns, first crossing, the review state of their
    facts (the same counts as the person page's summary bar) and the open research cards that name them.
    A family carries its two partners and its children, and "w" when the two are on record as married and
    both are shown in full.
    A living person carries name, gender and family links, exactly as on their page; a child under 18 is the
    placeholder. Names are the ones the rest of the site shows: "n" is shown_name(), "m" the married surname
    in it; the picture itself labels people by first name and birth surname, as a pedigree does.
    Nothing is taken from anywhere but the published bundle."""
    cfg = site.cfg
    slug_of = {}
    for line in cfg.get("lines", []):
        for s in line.get("surnames", []):
            slug_of.setdefault(s.lower(), line["slug"])
    cards = collections.defaultdict(list)
    for it in board:
        if it.get("status") == "done":
            continue
        for p in it.get("people") or []:
            cards[p["id"]].append([it["id"], it["title"], it.get("status", "open")])
    people = {}
    for pid, p in site.people.items():
        nm = p["name"]
        o = {"n": site.shown_name(p), "f": nm.get("call") or nm.get("first") or "", "s": nm.get("surname") or "", "g": p.get("gender") or "U",
             "pf": [f for f in p.get("parents", []) if f in site.families], "fs": [f for f in p.get("families", []) if f in site.families]}
        if p.get("minor"):
            o["st"] = "m"
        elif p["status"] != "full":
            o["st"] = "l"
        else:
            o["st"] = "f"
            b, d = p.get("birth") or {}, p.get("death") or {}
            if b.get("year"):
                o["by"], o["bt"] = b["year"], year_text(b.get("date"), "") or str(b["year"])
            if d.get("year"):
                o["dy"], o["dt"] = d["year"], year_text(d.get("date"), "") or str(d["year"])
            if b.get("place"):
                o["bp"] = site.town(b["place"])
            if d.get("place"):
                o["dp"] = site.town(d["place"])
            facts = site.facts_of(p)
            rs = site.review_summary(facts)
            o["ev"] = [rs["total"], rs["human"], rs["ai"], rs["un"], rs["needs"]]
            cross = next((f for f in facts if f["crossing"] and (f["date"] or {}).get("year")), None)
            if cross:
                o["x"] = {"y": cross["date"]["year"], "t": cross["event"]["type"], "p": site.town(cross["place"]["id"]) if cross["place"] else ""}
            if pid in cards:
                o["rq"] = cards[pid]
        married = site.heading_married(p)
        if married:
            o["m"] = married["surname"]
        slug = slug_of.get((nm.get("surname") or "").lower())
        if slug:
            o["c"] = slug
        people[pid] = o
    families = {}
    for fid, f in site.families.items():
        kids = []
        for c in f.get("children", []):
            if c["ref"] not in site.people:
                continue
            fr, mr = c.get("frel") or "", c.get("mrel") or ""
            kids.append([c["ref"], fr, mr] if (fr or mr) else c["ref"])
        families[fid] = {"a": f.get("father") if f.get("father") in site.people else None,
                         "b": f.get("mother") if f.get("mother") in site.people else None, "k": kids}
        # "w": married, so the explorer may say husband and wife. Only of two people both shown in full: for the
        # living a couple is a family link and nothing more, so they are each other's partner
        if (f.get("type") == "Married" or f.get("marriage")) and all(people.get(families[fid][k], {}).get("st") == "f" for k in ("a", "b")):
            families[fid]["w"] = 1
    named = [pid for pid, o in people.items() if o["st"] != "m"]
    start = site.resolve(str(cfg.get("root_person") or ""))
    if start not in named:
        start = next((ln["stations"][0]["person"]["id"] for ln in lines), None)
    if start not in named:
        start = min(named, key=lambda pid: -len(people[pid]["pf"]) - len(people[pid]["fs"])) if named else None
    return {
        "meta": {"generated": str(site.meta.get("generated", ""))[:10], "people": len(people)},
        "root": start, "priv": cfg.get("private_site", ""), "events": cfg.get("world_events", []),
        "lines": [{"slug": ln["slug"], "title": ln["title"], "short": LINE_SHORT.get(ln["slug"]) or ln["title"], "color": ln["color"],
                   "path": [st["person"]["id"] for st in ln["stations"]]} for ln in lines],
        "P": people, "F": families,
    }


EVENT_CODE = {"Birth": "b", "Baptism": "p", "Christening": "p", "Marriage": "m", "Residence": "r", "Census": "r", "Occupation": "o",
              "Arrival": "a", "Immigration": "a", "Departure": "l", "Emigration": "l", "Naturalization": "n", "Death": "d", "Burial": "u"}


_STREET = re.compile(r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|ct|court)\.?$", re.I)


def map_town(name: str) -> str:
    """The town a place record is in, for a mark on the map: "Chicago Ward 7, Cook, Illinois, USA" is Chicago,
    "house 35 Svatý Štěpán, parish Brumov" is Svatý Štěpán, "722 14th St, Milwaukee, …" is Milwaukee."""
    parts = [x.strip() for x in (name or "").split(",") if x.strip()]
    first = parts[0] if parts else ""
    first = re.sub(r"^(house|čp\.?|č\.\s*p\.?)\s*\d+[a-z]?\s+", "", first, flags=re.I)
    first = re.sub(r"\s*\(.*\)$", "", first)
    first = re.sub(r"\s+(house|ward|precinct|district|čp\.?|č\.\s*p\.?)\s*\d+[a-z]?$", "", first, flags=re.I)
    first = re.sub(r"\s+\d+[a-z]?$", "", first)
    if (re.match(r"^\d", first) or _STREET.search(first) or re.match(r"^(house|čp\.?|č\.\s*p\.?)?$", first, re.I)) and len(parts) > 1:
        first = parts[1]
    return first


def lives_data(site: "Site", lines: list) -> dict:
    """What the map and the lifelines (/map/) draw, written to /data/lives.json: for each person shown in full the
    years of birth and death and their dated events at a place that has coordinates, as their own page shows
    them, with the towns those events name and the colour of the line each person belongs to. The living and
    children under 18 are not in it at all: the site shows no dates or places for them.
    A person's line is the lineage route they stand on, else their surname's, else that of their nearest
    relative by blood who has one (a husband or wife takes it from their children)."""
    cfg = site.cfg
    slug_of = {}
    for line in cfg.get("lines", []):
        for s in line.get("surnames", []):
            slug_of.setdefault(s.lower(), line["slug"])
    line_of = {}
    for ln in lines:
        for st in ln["stations"]:
            line_of.setdefault(st["person"]["id"], ln["slug"])
    for pid, p in site.people.items():
        slug = slug_of.get((p["name"].get("surname") or "").lower())
        if slug:
            line_of.setdefault(pid, slug)
    queue = collections.deque(line_of)
    while queue:
        pid = queue.popleft()
        p = site.people.get(pid) or {}
        near = []
        for fid in p.get("parents", []):
            f = site.families.get(fid) or {}
            near += [f.get("father"), f.get("mother")]
        for fid in p.get("families", []):
            near += [c["ref"] for c in (site.families.get(fid) or {}).get("children", [])]
        for q in near:
            if q in site.people and q not in line_of:
                line_of[q] = line_of[pid]
                queue.append(q)
    places, people = {}, []
    for pid, p in site.people.items():
        if p["status"] != "full" or p.get("minor"):
            continue
        b, d = p.get("birth") or {}, p.get("death") or {}
        evs = []
        for f in site.facts_of(p):
            year, pl = (f["date"] or {}).get("year"), f["place"]
            if not year or not pl or pl.get("lat") is None or pl.get("long") is None:
                continue
            places.setdefault(pl["id"], [map_town(pl.get("name")) or site.town(pl["id"]) or "", round(float(pl["lat"]), 3), round(float(pl["long"]), 3)])
            evs.append([year, pl["id"], EVENT_CODE.get(f["event"]["type"], "x")])
        o = {"id": pid, "n": site.shown_name(p), "c": line_of.get(pid, ""), "e": evs}
        if b.get("year"):
            o["b"] = b["year"]
        if d.get("year"):
            o["d"] = d["year"]
        if evs or "b" in o or "d" in o:
            people.append(o)
    return {
        "meta": {"generated": str(site.meta.get("generated", ""))[:10]},
        "lines": [{"slug": ln["slug"], "short": LINE_SHORT.get(ln["slug"]) or ln["title"], "color": ln["color"]} for ln in lines],
        "events": cfg.get("world_events", []), "places": places, "people": people,
    }


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
    env.filters["story"] = story_html
    env.filters["fold"] = fold
    env.filters["shown"] = site.shown_name
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    except Exception:          # no tz database: fall back to UTC
        tz = dt.timezone.utc
    try:
        exported = dt.datetime.fromisoformat(bundle["meta"]["generated"].replace("Z", "+00:00")).astimezone(tz)
    except (KeyError, ValueError):
        exported = dt.datetime.now(dt.timezone.utc).astimezone(tz)
    def asset(name: str) -> str:
        """The address of a file in static/ with a short hash of its content. When the file changes its address
        does too, so a page never runs with a script or stylesheet the browser kept from before the change."""
        with open(os.path.join(HERE, "static", name), "rb") as f:
            return f"/static/{name}?v={hashlib.sha256(f.read()).hexdigest()[:8]}"

    env.globals.update({
        "site": site, "cfg": cfg, "meta": bundle["meta"], "review_label": review_label, "asset": asset,
        "now": dt.datetime.now(dt.timezone.utc).strftime("%-d %B %Y"),
        "updated": exported.strftime("%-d %B %Y"), "updated_time": exported.strftime("%H:%M %Z"),
        "CONF_LABEL": CONF_LABEL, "TIER_LABEL": TIER_LABEL,
    })

    def content(name: str) -> str:
        path = os.path.join(args.content, name)
        if site.names_child(name):
            site.warnings.append(f"content/{name}: left out — it names a child under 18 (the export report says where)")
            return ""
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
    env.globals["lines"] = lines
    # which lineage routes a person stands on: person id -> [{line, gen}] (for the person page's side panel)
    line_members = collections.defaultdict(list)
    for ln in lines:
        for st in ln["stations"]:
            line_members[st["person"]["id"]].append({"line": ln, "gen": st["gen"]})
    for ln in lines:
        write(f"line/{ln['slug']}/index.html", "line.html", line=ln, title=ln["title"],
              others=[x for x in lines if x["slug"] != ln["slug"]], nav="lines")

    # stories — hand-maintained pages; the nightly export never writes content/stories/
    stories = load_stories(args.content)
    if stories and site.names_child("stories/stories.json"):
        site.warnings.append("stories: left out — stories.json names a child under 18 outside its sections "
                             "(the export report says where)")
        stories = None
    elif stories:
        kept = []
        for sec in stories.get("sections") or []:
            if site.names_child("stories/stories.json", sec.get("id")):
                site.warnings.append(f"story {sec.get('id')}: dropped — names a child under 18 (the export report says where)")
            else:
                kept.append(sec)
        stories["sections"] = kept
    if stories:
        sections = stories.get("sections") or []
        write("stories/index.html", "stories.html", stories=stories, section=None, title="Family stories", nav="stories")
        for i, sec in enumerate(sections):
            write(
                f"stories/{sec['id']}/index.html", "stories.html", stories=stories, section=sec,
                prev=sections[i - 1] if i else None, next=sections[i + 1] if i + 1 < len(sections) else None,
                title=sec["heading"], nav="stories",
            )
        photo_src = os.path.join(args.content, "stories", "photos")
        if os.path.isdir(photo_src):
            shutil.copytree(photo_src, os.path.join(out, "stories", "photos"), dirs_exist_ok=True)
    else:
        site.warnings.append("content/stories/stories.json not found — no /stories/ pages")

    # people — ordered with diacritics folded, so Konečný files with Konecny and Šebák under S
    # children under 18 are a placeholder in their families: no page, not in the index
    def index_key(p):
        sur = site.index_surname(p)
        s = f'{sur}, {p["name"].get("first") or ""}' if sur and not p["name"].get("surname") else p["name"]["sort"]
        return (fold(s), s, p["id"])
    people_sorted = sorted((p for p in site.people.values() if not p.get("minor")), key=index_key)
    for p in people_sorted:
        facts = site.facts_of(p) if p["status"] == "full" else []
        fams = []
        for fid in p.get("families", []):
            f = site.families.get(fid)
            if not f:
                continue
            fams.append({"family": f, "spouse": site.spouse_in(f, p["id"]), "children": site.children_in(f)})
        parent_fams = [site.families[fid] for fid in p.get("parents", []) if fid in site.families]
        siblings = site.siblings_of(p)
        kids_note = any(q.get("minor") for q in siblings) or any(c["person"].get("minor") for fm in fams for c in fm["children"])
        write(f"person/{p['id']}/index.html", "person.html", p=p, facts=facts, extras=site.extras_of(p) if p["status"] == "full" else {"notes": [], "names": []}, families=fams, parent_families=parent_fams,
              parents=site.parents_of(p), parent_groups=site.parent_groups(p), siblings=siblings, kids_note=kids_note,
              summary=site.review_summary(facts),
              line_color=site.line_of(p), title=site.shown_name(p), also=site.also_names(p), on_lines=line_members.get(p["id"], []),
              notes=[site.notes[n] for n in p.get("notes", []) if n in site.notes],
              media=[site.media[m["ref"]] for m in p.get("media", []) if m["ref"] in site.media],
              name_citations=site.ladder(p.get("name", {}).get("citations", [])), nav="people")

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
        children = site.children_in(f)
        title = " & ".join(site.shown_name(x) for x in (father, mother) if x) or "Family"
        write(f"family/{f['id']}/index.html", "family.html", f=f, father=father, mother=mother, facts=facts,
              children=children, kids_note=any(c["person"].get("minor") for c in children), title=title, notes=[site.notes[n] for n in f.get("notes", []) if n in site.notes],
              media=[site.media[m["ref"]] for m in f.get("media", []) if m["ref"] in site.media], nav="people")

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
              title=(src["title"] if src else "Citation") + (f" — {c['page']}" if c.get("page") else ""), nav="sources")

    # sources
    sources_sorted = sorted(site.sources.values(), key=lambda s: (fold(s["title"]), s["title"]))
    for s in sources_sorted:
        cits = [site.citations[cid] for cid in s.get("citations", []) if cid in site.citations]
        cits.sort(key=lambda c: (c.get("page") or ""))
        repos = [(site.repositories.get(r["ref"]), r) for r in s.get("repositories", [])]
        write(f"source/{s['id']}/index.html", "source.html", s=s, citations=cits, repos=[x for x in repos if x[0]],
              notes=[site.notes[n] for n in s.get("notes", []) if n in site.notes], title=s["title"], nav="sources")

    # places
    places_sorted = sorted(site.places.values(), key=lambda p: (fold(p.get("display") or p["name"]), p.get("display") or p["name"]))
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
        write(f"place/{pl['id']}/index.html", "place.html", pl=pl, rows=rows, chain=chain, within=sorted(within, key=lambda q: fold(q["name"])),
              title=pl.get("display") or pl["name"], nav="places")

    # indexes
    by_letter = collections.OrderedDict()
    for p in people_sorted:
        letter = fold(site.index_surname(p) or p["name"]["display"] or "?")[:1].upper()
        if not letter.isalpha():
            letter = "?"
        by_letter.setdefault(letter, []).append(p)
    write("people/index.html", "people.html", by_letter=by_letter, title="People", total=len(people_sorted),
          living=sum(1 for p in people_sorted if p["status"] == "living"), nav="people")
    place_rows = [{"place": pl, "n": len(site.place_events.get(pl["id"], []))} for pl in places_sorted]
    write("places/index.html", "places.html", rows=place_rows, title="Places", nav="places")
    src_rows = [{"source": s, "n": len(s.get("citations", []))} for s in sources_sorted]
    write("sources/index.html", "sources.html", rows=src_rows, title="Sources", nav="sources")

    # research board (content/research/board.json) — hand-maintained; living-name check as for the stories
    board_path = os.path.join(args.content, "research", "board.json")
    board = []
    board_changed = None      # when the cards last changed: the board is kept by hand, the export never touches it
    if os.path.exists(board_path):
        with open(board_path, encoding="utf-8") as f:
            board_raw = json.load(f)
        raw_items = board_raw.get("items", [])
        board_changed = last_change(board_path)
        if board_changed is None and board_raw.get("reviewed"):
            try:
                board_changed = dt.datetime.fromisoformat(str(board_raw["reviewed"])).replace(tzinfo=tz)
            except ValueError:
                board_changed = None
        line_title = {l["slug"]: l["title"] for l in cfg.get("lines", [])}
        for it in raw_items:
            text = " ".join(str(it.get(k) or "") for k in ("title", "summary", "detail", "ask")).lower()
            if any(n in text for n in site.living_names):
                site.warnings.append(f"board {it.get('id')}: dropped — names a living person")
                continue
            if site.names_child("research/board.json", it.get("id")):
                site.warnings.append(f"board {it.get('id')}: dropped — names a child under 18 (the export report says where)")
                continue
            it = dict(it)
            it["people"] = [site.people[i] for i in it.get("people") or [] if i in site.people and site.people[i]["status"] == "full"]
            it["line_title"] = line_title.get(it.get("line"), "")
            it["line_short"] = LINE_SHORT.get(it.get("line"), "")
            it["color"] = it.get("line") if it.get("line") in line_title else "neutral"
            for k in ("since", "done"):
                if it.get(k):
                    try:
                        it[k + "_txt"] = dt.date.fromisoformat(it[k]).strftime("%-d %b %Y")
                    except ValueError:
                        it[k + "_txt"] = it[k]
            board.append(it)
    by_status = collections.defaultdict(list)
    for it in board:
        by_status[it.get("status", "open")].append(it)
    by_status["done"].sort(key=lambda x: x.get("done") or "", reverse=True)
    asks = [it for it in board if it.get("ask") and it.get("status") != "done"]

    # facts the checker flagged, by person (needs-human on the citation or the fact)
    flagged = collections.defaultdict(list)
    for p in site.people.values():
        if p["status"] != "full":
            continue
        for f in site.facts_of(p):
            if f["review"][0] == "needs" or any(r["review"][0] == "needs" for r in f["citations"]):
                flagged[p["id"]].append(f["label"])
    flagged_rows = sorted(({"person": site.people[pid], "facts": sorted(set(v))} for pid, v in flagged.items()),
                          key=lambda r: (-len(r["facts"]), r["person"]["name"]["sort"]))
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
    write("about/index.html", "about.html", body=content("about.md"), stats=stats, rs=rs, cit_rev=cit_rev, title="About this site", nav="about")
    recent = sorted([p for p in site.people.values() if p["status"] == "full" and p.get("change")], key=lambda p: -p["change"])[:8]
    ref = exported.timestamp()
    windows = [("day", 1), ("week", 7), ("month", 30), ("year", 365)]

    def touched(objs):
        ts = [o.get("change") or 0 for o in objs]
        return [sum(1 for t in ts if t >= ref - days * 86400) for _label, days in windows]

    activity = [
        {"label": "People", "counts": touched([p for p in site.people.values() if p["status"] == "full"])},
        {"label": "Facts", "counts": touched([e for e in site.events.values() if site.kind(e) == "fact"])},
        {"label": "Citations", "counts": touched(site.citations.values())},
        {"label": "Notes", "counts": touched(site.notes.values())},
    ]
    activity_cols = [w[0] for w in windows]
    sides = []
    for sd in cfg.get("sides") or [{"key": None, "title": "", "subtitle": ""}]:
        ls = [l for l in lines if l.get("side") == sd["key"] or sd["key"] is None]
        if ls:
            sides.append({**sd, "lines": ls})
    board_age = None
    if board_changed:
        board_changed = board_changed.astimezone(tz)
        board_age = (dt.datetime.now(tz).date() - board_changed.date()).days
        if board_age > 2:
            site.warnings.append(f"research board: the cards last changed {board_age} days ago ({board_changed:%d %b %Y})")
    write("research/index.html", "research.html", board_changed=board_changed.strftime("%-d %B %Y") if board_changed else None,
          board_age=board_age, board=by_status, asks=asks, flagged=flagged_rows,
          flagged_total=sum(len(r["facts"]) for r in flagged_rows), activity=activity, activity_cols=activity_cols,
          recent=recent, stats=stats, rs=rs, title="Research", nav="research")
    write("questions/index.html", "redirect.html", target="/research/", title="Research")
    # the tree explorer: one page, and the small data file it draws from
    tree = tree_data(site, lines, board)
    if tree["root"]:
        write("tree/index.html", "tree.html", title="The family tree", nav="tree")
    # the map and the lifelines: one page, drawn from the dated, located events of people shown in full
    lives = lives_data(site, lines)
    on_map = sum(1 for p in lives["people"] if p["e"])
    if on_map:
        write("map/index.html", "map.html", title="Map and lifelines", nav="map")
    write("index.html", "home.html", sides=sides, lines=lines, stats=stats, rs=rs, recent=recent,
          stories=stories, asks=asks[:3], body=content("home.md"), title=None, nav="home",
          tree_people=len(tree["P"]) if tree["root"] else 0, map_people=on_map, map_places=len(lives["places"]))
    write("404.html", "404.html", title="Not found")

    # static, data, media, CNAME
    shutil.copytree(os.path.join(HERE, "static"), os.path.join(out, "static"))
    os.makedirs(os.path.join(out, "data"), exist_ok=True)
    with open(os.path.join(out, "data", "bundle.json"), "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out, "data", "tree.json"), "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(out, "data", "lives.json"), "w", encoding="utf-8") as f:
        json.dump(lives, f, ensure_ascii=False, separators=(",", ":"))
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
