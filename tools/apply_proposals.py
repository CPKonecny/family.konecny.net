#!/usr/bin/env python3
"""apply_proposals.py  v0.3.2  (2026-11-22)

Promotes Karel's proposal files into the Gramps master, and applies validator verdicts.

  python3 apply_proposals.py                 # apply what may be applied; hold the rest for Chris
  python3 apply_proposals.py --dry-run       # validate, print the plan, write nothing
  python3 apply_proposals.py --held          # what is waiting for Chris, in plain words
  python3 apply_proposals.py --approve all   # approve what --held last listed, then apply it
  python3 apply_proposals.py --approve NAME [NAME ...]
  python3 apply_proposals.py --reject NAME [NAME ...] [--reason "why"]
  python3 apply_proposals.py --undo NAME     # put back what one applied change proposal altered
  python3 apply_proposals.py --one           # apply only the first proposal, then stop
  python3 apply_proposals.py --resubmit      # move rejected/*.json back to inbox first, then run
  python3 apply_proposals.py --verdicts      # apply validation/*.json (tag swap + confidence)
  python3 apply_proposals.py --repair-links  # drop family_list/parent_family_list entries that aren't families

Login: GRAMPS_USER / GRAMPS_PASS from the environment (cron), else from .applier.env beside this file,
else prompted (never stored here). The proposal format is described in tools/PROPOSALS.md.

Additions: add_person, add_family (with the marriage, if wanted), add_event (person or family events),
add_citation, add_note, add_media.
Changes to what is already in the tree: update_person, update_family, update_event, merge_family, merge_event,
set_private.

What is never applied without Chris's approval (it goes to held/ and waits):
  * every change action;
  * anything about a private person, or a person who is living or was born in the last 100 years;
  * anything that depends on a person whose own proposal is waiting.
An approval is tied to the exact content of the file and is kept outside the proposals folder, so a
proposal that is edited after it was approved has to be approved again.

Order within a run: add_person and add_family first, then everything else, so a new person's events
in the same batch find their subject. A new person may name a spouse or parents who are also new in
the same batch: parents are created first (multi-pass), a spouse is linked when the second of the
pair is created. A new person must be linked to someone (parents, a spouse, or a family proposal in
the same batch). add_person: 'surname' is the birth surname (may be empty if unknown); optional
'married_surname' becomes an alternate name of type Married Name. An event of the same type and date
that the person or family already has is reused (the citation is added to it) rather than duplicated;
for birth, death, burial and baptism an empty placeholder or a year-only event of the same year is
filled in instead of adding a second one.

A family is private when one of its two partners is private, or when its add_family says "private": true.
A family made earlier in the same run (when the second of a couple was added) counts as new for that. An
add_family that asks for "private": true on a family that was already in the tree before the run is
rejected rather than half done: that change is set_private's, and waits for Chris.

Every change action records what the objects looked like before, in undo/<name>.undo.json.
"""
import copy
import fcntl
import getpass
import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import requests

VERSION = "0.3.2"
BASE = os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api")
HERE = Path(__file__).resolve().parent
APPROVALS = Path(os.environ.get("APPLIER_APPROVALS", os.path.expanduser("~/.config/family-site/approved-proposals.txt")))
LOCK = Path(os.environ.get("APPLIER_LOCK", os.path.expanduser("~/.cache/family-site/applier.lock")))
DRY = ONE = False
RECENT_YEARS = 100          # born this recently (or with nothing to date them): Chris approves first

AGENT_TAGS = {"karel": ["agent-karel", "agent-proposed"], "validator": ["agent-validator"]}
TAG_COLORS = {"agent-karel": "#3f51b5", "agent-proposed": "#ff9800", "agent-validator": "#009688",
              "ai-validated": "#8bc34a", "needs-human": "#f44336", "reviewed": "#4caf50",
              "sync-to-ancestry": "#9c27b0", "unreviewed": "#ff9800", "needs-place": "#795548"}
ADDS = {"add_person", "add_family", "add_event", "add_citation", "add_note", "add_media"}
CHANGES = {"update_person", "update_family", "update_event", "merge_family", "merge_event", "set_private"}
SUPPORTED = ADDS | CHANGES
# names an agent might reach for, and what the thing is called here (used only to make a rejection helpful)
OTHER_NAMES = {
    "link_child": "update_family with add_children", "add_child": "update_family with add_children",
    "rename_person": "update_person (set.first_name, set.surname, set.suffix, set.married_surname)",
    "set_gender": "update_person (set.gender)", "correct_fact": "update_person, update_family or update_event",
    "set_family_parents": "update_family (set.father, set.mother)", "merge_families": "merge_family",
    "merge_events": "merge_event", "delete_event": "merge_event, which folds a duplicate into the event that is kept",
    "remove_event": "merge_event, which folds a duplicate into the event that is kept",
    "edit_event": "update_event", "change_event": "update_event", "replace_event": "update_event",
    "add_marriage": "add_event with type Marriage, or family.marriage in add_family",
}
FIRST = {"add_person": 0, "add_family": 1}
FAMILY_EVENTS = {"Marriage", "Marriage Banns", "Marriage License", "Marriage Contract", "Marriage Settlement",
                 "Engagement", "Divorce", "Divorce Filing", "Annulment"}
ONCE_ONLY = {"Birth", "Death", "Burial", "Cremation", "Baptism", "Christening"}
MODIFIER = {"exact": 0, "before": 1, "after": 2, "about": 3}
QUALITY = {"estimated": 1, "calculated": 2}
DATE_QUALITIES = set(MODIFIER) | set(QUALITY)
MONTH_DAYS = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
TIER_MAX_CONF = {"record": 4, "compiled": 3, "index": 2, "tree": 1, "oral": 2, "ai-draft": 0}
GENDER = {"F": 0, "M": 1, "U": 2}
GENDER_WORD = {0: "female", 1: "male", 2: "sex unknown"}
CHILD_REL = {"Birth", "Adopted", "Stepchild", "Sponsored", "Foster", "Unknown"}
FAMILY_TYPES = {"Married", "Unmarried", "Civil Union", "Unknown"}
PERSON_SET = {"first_name", "surname", "suffix", "call_name", "nickname", "married_surname", "also_known_as",
              "gender", "private", "prefer_event"}
FAMILY_SET = {"father", "mother", "private", "type"}
EVENT_SET = {"date", "date_quality", "place", "description", "private"}
SHARED = {"sources", "repositories", "places", "tags"}      # found by name and used again by later proposals
RECORD_KINDS = {"I": ("people", "person"), "F": ("families", "family"), "E": ("events", "event"), "N": ("notes", "note")}
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def set_root(root):
    global ROOT, INBOX, APPLIED, REJECTED, VALID, HELD, UNDO
    ROOT = Path(root)
    INBOX, APPLIED, REJECTED, VALID = ROOT / "inbox", ROOT / "applied", ROOT / "rejected", ROOT / "validation"
    HELD, UNDO = ROOT / "held", ROOT / "undo"


set_root(os.environ.get("PROPOSALS_DIR", "/mnt/bulk8/Archive/Backup/family-history/proposals"))


def credentials():
    u, p = os.environ.get("GRAMPS_USER"), os.environ.get("GRAMPS_PASS")
    if not (u and p):
        for env in (HERE / ".applier.env", Path.home() / "gramps-tools" / ".applier.env"):
            if not env.exists():
                continue
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                if k.strip() == "GRAMPS_USER":
                    u = u or v
                if k.strip() == "GRAMPS_PASS":
                    p = p or v
            break
    if not (u and p) and not sys.stdin.isatty():
        sys.exit("no login: set GRAMPS_USER and GRAMPS_PASS, or put them in .applier.env beside this file")
    return u or input("Gramps user: "), p or getpass.getpass("Password: ")


class Refused(Exception):
    """The proposal cannot be applied as the tree now stands, and trying again will not change that."""


class ApiError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def new_handle():
    """A handle in the form Gramps makes them. Chosen here so that a reply lost on the way back cannot leave
    behind an object nobody knows about."""
    return "%08x%08x" % (int(time.time() * 10000), random.randint(0, sys.maxsize))


def add_tags(obj, tags):
    have = obj.get("tag_list") or []
    obj["tag_list"] = have + [t for t in tags if t not in have]


class Api:
    def __init__(self):
        self.user, self.password = credentials()
        self.h = {}
        self.login()
        self.writes = 0
        self.journal = []       # what the current proposal did: (kind, endpoint, handle, object before or None)
        self.tree = None

    def login(self):
        r = None
        for wait in (0, 2, 5):                      # the token endpoint allows one login a second
            time.sleep(wait)
            r = requests.post(f"{BASE}/token/", json={"username": self.user, "password": self.password})
            if r.status_code != 429:
                break
        if r.status_code != 200:
            sys.exit(f"login failed: {r.status_code} {r.text[:200]}")
        self.h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    def _send(self, method, path, **kw):
        """One request. Logs in again if the token ran out; waits and retries while SQLite is busy."""
        extra = kw.pop("headers", {})
        waits, relogged = [2, 5, 10], False
        while True:
            r = requests.request(method, f"{BASE}/{path}", headers={**self.h, **extra}, **kw)
            if r.status_code == 401 and not relogged:
                relogged = True
                self.login()
                continue
            if r.status_code == 500 and "locked" in r.text.lower() and waits:
                time.sleep(waits.pop(0))
                continue
            return r

    def get(self, path, **params):
        r = self._send("GET", path, params=params)
        if r.status_code >= 300:
            raise ApiError(f"GET {path}: {r.status_code} {r.text[:200]}", r.status_code)
        return r.json()

    def exists(self, path):
        return self._send("GET", path).status_code == 200

    def all(self, endpoint):
        page, out = 1, []
        while True:
            batch = self.get(f"{endpoint}/", page=page, pagesize=500)
            if not batch:
                return out
            out += batch
            page += 1

    def post(self, path, obj, raw=None, mime=None):
        """POST a JSON object (or raw bytes for media). Returns the new object's handle."""
        endpoint = path.strip("/")
        if DRY:
            self.writes += 1
            return f"DRY-{uuid.uuid4().hex[:8]}"
        if raw is not None:
            r = self._send("POST", path, headers={"Content-Type": mime}, data=raw)
            if r.status_code >= 300:
                raise ApiError(f"POST {path}: {r.status_code} {r.text[:300]}", r.status_code)
            res = r.json()
            if isinstance(res, list):
                adds = [e for e in res if e.get("type") == "add" and e.get("_class") == "Media"]
                entry = adds[0] if adds else res[0]
                res = entry.get("new") or entry
            handle = res["handle"]
            self.journal.append(("create", endpoint, handle, None))
        else:
            handle = obj.get("handle") or new_handle()
            self.journal.append(("create", endpoint, handle, None))     # before sending: see new_handle()
            r = self._send("POST", path, json={**obj, "handle": handle})
            if r.status_code >= 300:
                raise ApiError(f"POST {path}: {r.status_code} {r.text[:300]}", r.status_code)
        self.writes += 1
        return handle

    def put(self, path, obj):
        if DRY:
            self.writes += 1
            return
        r = self._send("PUT", path, json=obj)
        if r.status_code >= 300:
            raise ApiError(f"PUT {path}: {r.status_code} {r.text[:300]}", r.status_code)
        self.writes += 1

    def modify(self, endpoint, handle, fn):
        """GET an object, let fn change it, PUT it back if it changed. Returns the object as it now is."""
        if str(handle).startswith("DRY-"):
            obj = self.tree.cached(endpoint, handle) if self.tree else None
            if obj is not None:
                fn(obj)
            self.writes += 1
            return obj
        obj = self.get(f"{endpoint}/{handle}")
        before = copy.deepcopy(obj)
        fn(obj)
        if obj != before:
            if DRY:
                self.writes += 1
            else:
                self.journal.append(("update", endpoint, handle, before))
                self.put(f"{endpoint}/{handle}", obj)
        if self.tree:
            self.tree.remember(endpoint, obj)
        return obj

    def add_ref(self, endpoint, handle, field, value, tags=()):
        """Append value to one of an object's list fields (if missing) and add tags."""
        def fn(obj):
            lst = obj.setdefault(field, [])
            key = value["ref"] if isinstance(value, dict) and "ref" in value else value
            have = [v["ref"] if isinstance(v, dict) and "ref" in v else v for v in lst]
            if key not in have:
                lst.append(value)
            add_tags(obj, tags)
        return self.modify(endpoint, handle, fn)

    def rollback(self):
        """Undo what the current proposal did. Returns True if every step of that worked."""
        ok = restore(self, self.journal, say=lambda s: print("  rollback " + s))
        self.journal = []
        return ok


def restore(api, journal, say=print):
    """Put things back as the journal says they were: re-create what was deleted, restore what was changed
    (families first and people last, because the API rewrites people's family lists when a family changes),
    then delete what was created, newest first. Returns True if every step worked."""
    ok = True

    def req(method, path, obj=None):
        r = api._send(method, path, **({"json": obj} if obj is not None else {}))
        say(f"{method} {path}: {r.status_code}")
        return r.status_code < 300 or (method == "DELETE" and r.status_code == 404)

    back = [tuple(j) for j in reversed(journal)]
    for kind, endpoint, handle, before in back:
        if kind == "delete":
            ok &= req("POST", f"{endpoint}/", before)
    rank = {"families": 0, "people": 2}
    for kind, endpoint, handle, before in sorted((j for j in back if j[0] == "update"), key=lambda j: rank.get(j[1], 1)):
        ok &= req("PUT", f"{endpoint}/{handle}", before)
    for kind, endpoint, handle, before in back:
        if kind == "create":
            ok &= req("DELETE", f"{endpoint}/{handle}")
    return ok


class Tree:
    def __init__(self, api):
        self.api = api
        api.tree = self
        self.tags = {t["name"]: t["handle"] for t in api.all("tags")}
        self.people = api.all("people")
        self.people_h, self.by_anc, self.by_gid = {}, {}, {}
        for p in self.people:
            self.people_h[p["handle"]] = p
            self.index_person(p)
        self.sources = {s["title"].strip().lower(): s["handle"] for s in api.all("sources")}
        self.repos = {r["name"].strip().lower(): r["handle"] for r in api.all("repositories")}
        self.places, self.place_names = {}, {}
        for pl in api.all("places"):
            names = [pl.get("title", "")] + [pl.get("name", {}).get("value", "")]
            self.place_names[pl["handle"]] = next((n for n in reversed(names) if n), "")
            for n in names:
                if n:
                    self.places[n.strip().lower()] = pl["handle"]
        evs = api.all("events")
        self.events = {e["gramps_id"]: e for e in evs}
        self.events_h = {e["handle"]: e for e in evs}
        self.families = api.all("families")
        self.fam_h = {f["handle"]: f for f in self.families}
        self.notes = {}             # Gramps ID -> note, read from Gramps when a proposal first names it
        self.pending = set()        # Ancestry IDs of people whose own add_person is waiting for approval
        self.deferred = {}          # spouse's Ancestry ID -> people already added who named that spouse
        self.batch_names = {}       # Ancestry ID -> name, for the new people of this batch (to describe proposals)
        self.made = set()           # handles of what this run created: a family one proposal made is new to the next

    # ----- what we remember of the tree, kept in step with what we write -----
    def index_person(self, p):
        self.by_gid[p["gramps_id"]] = p
        for a in p.get("attribute_list", []):
            if tname(a) == "Ancestry ID":
                self.by_anc[str(a["value"])] = p

    def cached(self, endpoint, handle):
        return {"people": self.people_h, "families": self.fam_h, "events": self.events_h}.get(endpoint, {}).get(handle)

    def remember(self, endpoint, obj):
        """Keep our copy of a person, family or event as it now is (the same dict object, so earlier references see it)."""
        store = {"people": (self.people_h, self.people), "families": (self.fam_h, self.families),
                 "events": (self.events_h, None)}.get(endpoint)
        if store is None or not obj:
            return obj
        index, listing = store
        old = index.get(obj["handle"])
        if old is None:
            index[obj["handle"]] = old = dict(obj)
            if listing is not None:
                listing.append(old)
        elif old is not obj:
            old.clear()
            old.update(obj)
        if endpoint == "people":
            self.index_person(old)
        elif endpoint == "events":
            self.events[old.get("gramps_id")] = old
        return old

    def forget(self, endpoint, handle):
        if endpoint == "families":
            gone = self.fam_h.pop(handle, None)
            self.families[:] = [f for f in self.families if f is not gone]
        elif endpoint == "events":
            gone = self.events_h.pop(handle, None)
            if gone is not None:
                self.events.pop(gone.get("gramps_id"), None)

    def refresh(self, endpoint, handle):
        if DRY or str(handle).startswith("DRY-"):
            return self.cached(endpoint, handle)
        return self.remember(endpoint, self.api.get(f"{endpoint}/{handle}"))

    def create(self, endpoint, obj):
        """POST a new person, family or event and remember it."""
        h = self.api.post(f"{endpoint}/", obj)
        self.made.add(h)
        made = {**obj, "handle": h, "gramps_id": "(new)"} if DRY else self.api.get(f"{endpoint}/{h}")
        return self.remember(endpoint, made)

    # ----- finding things -----
    def find(self, ref):
        """ref = {'ancestry_id': ..} or {'gramps_id': ..} or a bare ancestry id string."""
        if isinstance(ref, str):
            return self.by_anc.get(ref) if ref else None
        if not ref:
            return None
        return self.by_anc.get(str(ref.get("ancestry_id", ""))) or self.by_gid.get(ref.get("gramps_id", ""))

    def find_family(self, ref):
        """ref = 'F0183', {'gramps_id': 'F0183'}, or {'father': person ref, 'mother': person ref} (either may be left
        out, meaning that the family has no such parent). Returns (family, None) or (None, why not)."""
        gid = ref if isinstance(ref, str) else (ref or {}).get("gramps_id")
        if gid:
            hits = [f for f in self.families if f.get("gramps_id") == gid]
            return (hits[0], None) if hits else (None, f"family {gid} not found")
        if not isinstance(ref, dict) or not (ref.get("father") or ref.get("mother")):
            return None, "family must be given as a Gramps ID (F0123) or by its parents"
        fa, mo = self.find(ref.get("father")), self.find(ref.get("mother"))
        if (ref.get("father") and not fa) or (ref.get("mother") and not mo):
            return None, "a parent named for the family is not in Gramps"
        hits = self.family_with(fa, mo)
        if len(hits) != 1:
            return None, f"{len(hits)} families have exactly these parents; give the family's Gramps ID"
        return hits[0], None

    def event(self, ref):
        gid = ref if isinstance(ref, str) else (ref or {}).get("gramps_id")
        return self.events.get(gid) if gid else None

    def note(self, gid):
        """A note by Gramps ID, with what refers to it ('backlinks'). Notes are not read when the tree is loaded."""
        if gid not in self.notes:
            r = self.api._send("GET", "notes/", params={"gramps_id": gid, "backlinks": 1})
            hits = r.json() if r.status_code == 200 else []
            self.notes[gid] = hits[0] if isinstance(hits, list) and hits else None
        return self.notes[gid]

    def record(self, ref):
        """A person, family, event or note named by Gramps ID ('I0123', 'F0045', 'E0678', 'N0012'), or a person
        named by {'ancestry_id': ..}. Returns (endpoint, object, None) or (None, None, why not)."""
        if isinstance(ref, dict) and not ref.get("gramps_id"):
            person = self.find(ref)
            return ("people", person, None) if person else (None, None, f"person not found in Gramps ({ref_text(ref)})")
        gid = str((ref.get("gramps_id") if isinstance(ref, dict) else ref) or "").strip()
        endpoint = RECORD_KINDS.get(gid[:1].upper(), (None,))[0] if isinstance(ref, (str, dict)) else None
        if not endpoint:
            return None, None, f"{gid or ref!r}: name a person (I…), family (F…), event (E…) or note (N…) by Gramps ID"
        obj = {"people": lambda: self.by_gid.get(gid),
               "families": lambda: next((f for f in self.families if f.get("gramps_id") == gid), None),
               "events": lambda: self.events.get(gid), "notes": lambda: self.note(gid)}[endpoint]()
        return (endpoint, obj, None) if obj else (None, None, f"{gid} not found in Gramps")

    def owners_of(self, event):
        """The people and families that have this event, as text."""
        h = event["handle"]
        return [person_label(x) for x in self.people if any(r["ref"] == h for r in x.get("event_ref_list", []))] + \
               [family_label(f, self) for f in self.families if any(r["ref"] == h for r in f.get("event_ref_list", []))]

    def tag(self, name):
        if name not in self.tags:
            if DRY:
                self.tags[name] = f"DRY-tag-{name}"
            else:
                self.api.post("tags/", {"name": name, "color": TAG_COLORS.get(name, "#9e9e9e"), "priority": 0})
                self.tags = {t["name"]: t["handle"] for t in self.api.all("tags")}
        return self.tags[name]

    def family_of(self, person, spouse=None):
        fams = [f for f in self.families if person["handle"] in (f.get("father_handle"), f.get("mother_handle"))]
        if spouse:
            fams = [f for f in fams if spouse["handle"] in (f.get("father_handle"), f.get("mother_handle"))]
        return fams

    def family_with(self, father, mother):
        """Family with exactly these parents (either may be None). The API gives an empty parent as "", not null."""
        fh, mh = (father or {}).get("handle") or None, (mother or {}).get("handle") or None
        return [f for f in self.families
                if (f.get("father_handle") or None) == fh and (f.get("mother_handle") or None) == mh]

    def matching_event(self, owner, etype, dateval):
        """An event the owner already has that a new one of this type and date belongs with: the same (y, m, d);
        any event of that type in that year if the proposal gives only a year; and, for things that happen once
        in a life, an empty placeholder left by the import or a year-only event that the new date sharpens."""
        mine = [ev for ev in (self.events_h.get(ref["ref"]) for ref in owner.get("event_ref_list", []))
                if ev and ev.get("type") == etype]
        for ev in mine:
            have = event_ymd(ev)
            if dateval is None or have == dateval or (dateval[1:] == (0, 0) and have[0] == dateval[0]):
                return ev
        if etype in ONCE_ONLY and dateval:
            for ev in mine:
                if is_placeholder(ev) or (sharper(dateval, event_ymd(ev)) and date_modifier(ev) in (0, 3)):
                    return ev
        return None

    # ----- who needs Chris's eye -----
    def years(self, person, kinds):
        out = []
        for ref in person.get("event_ref_list", []):
            ev = self.events_h.get(ref["ref"])
            if ev and ev.get("type") in kinds and event_ymd(ev)[0]:
                out.append(event_ymd(ev)[0])
        return out

    def latest_birth(self, person, also=()):
        """The latest year this person can have been born in, from their own dates or their nearest relatives';
        None if there is nothing to go on. also: further limits that the proposal in hand implies."""
        born = self.years(person, {"Birth", "Baptism", "Christening"})
        if born:
            return min(born)
        limits = list(also) + self.years(person, {"Death", "Burial", "Cremation", "Probate"})
        for fh in person.get("family_list", []):
            fam = self.fam_h.get(fh) or {}
            limits += [y - 12 for y in self.years(fam, {"Marriage", "Marriage Banns", "Marriage License", "Engagement", "Divorce"})]
            for c in fam.get("child_ref_list", []):
                kid = self.people_h.get(c["ref"]) or {}
                limits += [y - 12 for y in self.years(kid, {"Birth", "Baptism", "Christening"})]
            for h in (fam.get("father_handle"), fam.get("mother_handle")):
                if h and h != person["handle"]:
                    limits += [y + 40 for y in self.years(self.people_h.get(h) or {}, {"Birth", "Baptism", "Christening"})]
        for fh in person.get("parent_family_list", []):
            fam = self.fam_h.get(fh) or {}
            for h in (fam.get("father_handle"), fam.get("mother_handle")):
                par = self.people_h.get(h) or {}
                limits += [y + 70 for y in self.years(par, {"Birth", "Baptism", "Christening"})]
                limits += [y + 1 for y in self.years(par, {"Death", "Burial", "Cremation"})]
        return min(limits) if limits else None

    def recent(self, person, also=()):
        """True for someone Chris may know or know of: private, or possibly born in the last RECENT_YEARS years."""
        if person.get("private"):
            return True
        born = self.latest_birth(person, also)
        return born is None or born >= date.today().year - RECENT_YEARS

    def place_text(self, handle):
        return self.place_names.get(handle, "") if handle else ""

    def person_text(self, person):
        """A person as Chris should see them in a description: ID, name and years, so that namesakes can be told apart."""
        born = self.years(person, {"Birth", "Baptism", "Christening"})
        died = self.years(person, {"Death", "Burial", "Cremation"})
        span = f" ({min(born) if born else ''}–{min(died) if died else ''})" if born or died else " (no dates)"
        return person_label(person) + span + (" [private]" if person.get("private") else "")

    def family_text(self, fam):
        who = [self.person_text(self.people_h[h]) if h in self.people_h else "—" for h in (fam.get("father_handle"), fam.get("mother_handle"))]
        return f"{fam.get('gramps_id')} ({who[0]} × {who[1]})"


# ---------- helpers ----------
def tname(a):
    t = a.get("type")
    return t if isinstance(t, str) else (t or {}).get("string", "")

def attr(name, value, cls="Attribute"):
    return {"_class": cls, "type": name, "value": str(value), "private": False}

def jdn(y, m, d):
    a = (14 - m) // 12; y2 = y + 4800 - a; m2 = m + 12 * a - 3
    return d + (153 * m2 + 2) // 5 + 365 * y2 + y2 // 4 - y2 // 100 + y2 // 400 - 32045

def date_key(text):
    if not text: return None
    parts = [int(x) for x in text.split("-")]
    return tuple((parts + [0, 0])[:3])

def gramps_date(text, quality="exact"):
    if not text: return None
    y, m, d = date_key(text)
    return {"_class": "Date", "calendar": 0, "modifier": MODIFIER.get(quality, 0),
            "quality": QUALITY.get(quality, 0), "dateval": [d, m, y, False],
            "text": "", "sortval": jdn(y, m or 1, d or 1), "newyear": 0}

def event_ymd(ev):
    dv = ((ev or {}).get("date") or {}).get("dateval") or [0, 0, 0, False]
    return (dv[2], dv[1], dv[0])

def date_modifier(ev):
    return ((ev or {}).get("date") or {}).get("modifier") or 0

def date_problem(text):
    """Why a date in a proposal cannot be used, or None. Dates are written 1898, 1898-07 or 1898-07-04."""
    if not isinstance(text, str):
        return f"{text!r} must be text in quotes, like \"1898\" or \"1898-07-04\""
    if not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", text):
        return f"{text!r} is not a date: write YYYY, YYYY-MM or YYYY-MM-DD"
    y, m, d = date_key(text)
    if not y or (len(text) > 4 and not 1 <= m <= 12) or (len(text) > 7 and not 1 <= d <= MONTH_DAYS[m]):
        return f"{text} is not a date"
    today = date.today()
    if (y, m or 1, d or 1) > (today.year, today.month, today.day):
        return f"{text} is in the future"
    return None

def date_problems(x, where):
    """Every 'date' and 'date_quality' in one part of a proposal, checked. A date that cannot be read must never
    get as far as being applied: half a proposal would be written before it failed."""
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            here = f"{where}.{k}"
            if k == "date":
                if v not in (None, "") and date_problem(v): out.append(f"{here}: {date_problem(v)}")
            elif k == "date_quality":
                if v == "between": out.append(f"{here}: 'between' dates are not supported; log it as ASK")
                elif v not in (None, "") and v not in DATE_QUALITIES:
                    out.append(f"{here} must be one of {', '.join(sorted(DATE_QUALITIES))}")
            else:
                out += date_problems(v, here)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            out += date_problems(v, f"{where}[{i}]")
    return out

def sharper(new, have):
    """True if a (y, m, d) says more than the one we have without contradicting it."""
    if not new or not new[0]:
        return False
    if have == (0, 0, 0):
        return True
    return have[0] == new[0] and have[1:] == (0, 0) and new[1:] != (0, 0)

def is_placeholder(ev):
    """An event with nothing in it: the import made these for 'born' and 'died' with no details."""
    return (event_ymd(ev) == (0, 0, 0) and not ((ev.get("date") or {}).get("text") or "").strip()
            and not ev.get("place") and not (ev.get("description") or "").strip()
            and not ev.get("citation_list") and not ev.get("note_list") and not ev.get("media_list"))

def ymd_text(ymd, modifier=0, quality=0):
    y, m, d = ymd
    if not y:
        return "no date"
    core = " ".join(x for x in (str(d) if d else "", MONTHS[m] if m else "", str(y)) if x)
    lead = {1: "before ", 2: "after ", 3: "about "}.get(modifier, "") or {1: "estimated ", 2: "calculated "}.get(quality, "")
    return lead + core

def date_text(ev):
    d = (ev or {}).get("date") or {}
    if event_ymd(ev) == (0, 0, 0):
        return (d.get("text") or "").strip() or "no date"
    return ymd_text(event_ymd(ev), d.get("modifier") or 0, d.get("quality") or 0)

def own_events(owner, tree, etype=None):
    """The owner's own events, in the order the owner lists them: a person's as Primary, a family's as Family."""
    out = []
    for r in owner.get("event_ref_list", []):
        ev = tree.events_h.get(r["ref"])
        if ev and tname({"type": r.get("role")}) in ("Primary", "Family", "") and (etype is None or ev.get("type") == etype):
            out.append(ev)
    return out

def pick_event(sel, owner, tree):
    """The one event of owner that sel names: a Gramps ID ('E0240' or {'gramps_id': ..}), or {'type': .., 'date': ..}
    when exactly one of the owner's own events fits ('date' is the date it has now, as far as it is given).
    Returns (event, None) or (None, why not)."""
    gid = sel if isinstance(sel, str) else (sel or {}).get("gramps_id") if isinstance(sel, dict) else None
    if gid:
        ev = tree.events.get(gid)
        if not ev:
            return None, f"event {gid} not found"
        if ev["handle"] not in {r["ref"] for r in owner.get("event_ref_list", [])}:
            return None, f"{gid} is not an event of {owner['gramps_id']}"
        return ev, None
    if not isinstance(sel, dict) or not sel.get("type"):
        return None, "event: give its Gramps ID (\"E0123\") or {\"type\": \"Birth\"}, with \"date\" if there are several"
    hits = own_events(owner, tree, sel["type"])
    if sel.get("date"):
        want = date_key(sel["date"])
        hits = [ev for ev in hits if all(w == 0 or w == h for w, h in zip(want, event_ymd(ev)))]
    if len(hits) != 1:
        which = f"{sel['type']} event" + (f" dated {sel['date']}" if sel.get("date") else "")
        return None, (f"{owner['gramps_id']} has no {which}" if not hits else
                      f"{owner['gramps_id']} has {len(hits)} {which}s: give the Gramps ID, or the date it has now")
    return hits[0], None

def event_changes(ev, s, tree):
    """What the 'set' of an update_event would change on this event, as [(field, as it is, as it would be)]."""
    out = []
    d = ev.get("date") or {}
    if s.get("date"):
        q = s.get("date_quality") or "exact"
        want = (date_key(s["date"]), MODIFIER.get(q, 0), QUALITY.get(q, 0))
        if (event_ymd(ev), d.get("modifier") or 0, d.get("quality") or 0) != want:
            out.append(("date", date_text(ev), ymd_text(*want)))
    if s.get("place"):
        name = str(s["place"]).strip()
        h = tree.places.get(name.lower())
        if h is None or h != ev.get("place"):
            out.append(("place", tree.place_text(ev.get("place")) or "none", name))
    if "description" in s and (ev.get("description") or "") != s["description"]:
        out.append(("description", ev.get("description") or "none", s["description"] or "none"))
    if "private" in s and bool(ev.get("private")) != s["private"]:
        out.append(("private", "yes" if ev.get("private") else "no", "yes" if s["private"] else "no"))
    return out

def event_text(ev, tree):
    d = ev.get("date") or {}
    bits = [ymd_text(event_ymd(ev), d.get("modifier") or 0, d.get("quality") or 0)]
    if tree.place_text(ev.get("place")):
        bits.append(tree.place_text(ev.get("place")))
    return f"{ev.get('type')} {ev.get('gramps_id')} ({', '.join(bits)})"

def note(text, ntype="Research"):
    return {"_class": "Note", "text": {"_class": "StyledText", "string": text, "tags": []},
            "type": ntype, "format": 0, "private": False, "tag_list": []}

def surname_obj(surname):
    return {"_class": "Surname", "surname": surname, "prefix": "", "primary": True, "origintype": "", "connector": ""}

def name_obj(first, surname, ntype, suffix=""):
    n = {"_class": "Name", "type": ntype, "first_name": first, "surname_list": [surname_obj(surname)], "private": False}
    if suffix:
        n["suffix"] = suffix
    return n

def name_text(n):
    sur = " ".join(s.get("surname", "") for s in (n or {}).get("surname_list", []))
    return " ".join(x for x in ((n or {}).get("first_name", ""), sur, (n or {}).get("suffix", "")) if x)

def person_label(p):
    return f"{p['gramps_id']} {name_text(p.get('primary_name', {}))}".strip()

def family_label(f, tree):
    who = [person_label(tree.people_h[h]) if h in tree.people_h else "—" for h in (f.get("father_handle"), f.get("mother_handle"))]
    return f"{f.get('gramps_id')} ({who[0]} × {who[1]})"

def ref_text(ref):
    if isinstance(ref, dict):
        return ref.get("gramps_id") or f"Ancestry {ref.get('ancestry_id')}"
    return f"Ancestry {ref}"

def ref_anc(ref):
    """The Ancestry ID a person ref names, if it names one."""
    return str(ref) if isinstance(ref, str) else str((ref or {}).get("ancestry_id") or "")

def split_name(aka):
    """'Annie Wacek' or {'first_name': .., 'surname': ..} -> (first, surname)."""
    if isinstance(aka, dict):
        return str(aka.get("first_name") or "").strip(), str(aka.get("surname") or "").strip()
    first, _, sur = str(aka).strip().rpartition(" ")
    return (first, sur) if first else (sur, "")

def as_list(x):
    return [] if x in (None, "") else x if isinstance(x, list) else [x]

def child_rels(c):
    """(father's, mother's) relationship to a child, from 'rel' or 'father_rel' / 'mother_rel'; None where not given."""
    c = c if isinstance(c, dict) else {}
    return c.get("father_rel") or c.get("rel"), c.get("mother_rel") or c.get("rel")

def parents_after(cur_f, cur_m, want_f, want_m):
    """Who the father and mother of a family would be. An empty place can be filled and the two can change
    places; nobody who is a parent now may drop out."""
    new_f, new_m = want_f or cur_f, want_m or cur_m
    if new_f and new_f == new_m:
        raise ValueError("father and mother would be the same person (to swap them, give both)")
    if {h for h in (cur_f, cur_m) if h} - {new_f, new_m}:
        raise ValueError("that would replace a parent the family already has, which is not supported")
    return new_f, new_m

def birth_parent_clash(child, family, frel, mrel, tree, father, mother):
    """A person has one father and one mother by birth, and belongs once to each set of parents. Returns a
    sentence if linking child into family would break that, else None. father/mother: handles of the parents
    the family will have ("" for none); family: the family as it is now, or None if it does not exist yet."""
    for fh in child.get("parent_family_list", []):
        other = tree.fam_h.get(fh)
        if not other or (family and other["handle"] == family.get("handle")):
            continue
        ref = next((r for r in other.get("child_ref_list", []) if r["ref"] == child["handle"]), {})
        for side, here, rel in (("father", father, frel), ("mother", mother, mrel)):
            there = other.get(f"{side}_handle")
            if not (here and there):
                continue
            if here == there:
                return (f"{person_label(child)} is already a child in {other.get('gramps_id')}, which has the same {side}: "
                        "merge the two families (merge_family) instead of linking the child twice")
            if rel == "Birth" and tname({"type": ref.get(f"{side[0]}rel")}) == "Birth":
                return (f"{person_label(child)} already has a {side} by birth in {other.get('gramps_id')}: if this is an "
                        f"adoption or a step-family say so with {side}_rel, otherwise one of the two links is wrong")
    return None

def merge_family_problem(keep, drop, tree):
    """Why two families may not be merged, or None. Merging families whose parents differ would make Gramps merge
    the two people into one, so this is checked on fresh data immediately before the merge as well."""
    if keep["handle"] == drop["handle"]:
        return "keep and drop are the same family"
    shared = False
    for side in ("father", "mother"):
        k, d = keep.get(f"{side}_handle") or None, drop.get(f"{side}_handle") or None
        if k and d and k != d:
            return (f"the two families have different {side}s ({person_label(tree.people_h[k])} and "
                    f"{person_label(tree.people_h[d])}), so they are not duplicates")
        shared = shared or bool(k and d)
    if not shared:
        return "the two families share no parent"
    parents = {keep.get("father_handle"), keep.get("mother_handle"), drop.get("father_handle"), drop.get("mother_handle")}
    if any(c["ref"] in parents for c in keep.get("child_ref_list", []) + drop.get("child_ref_list", [])):
        return "a child of one family is a parent in the other"
    return None

def merge_event_problem(keep, drop, allow):
    """Why two events may not be merged without 'allow_different', or None."""
    if keep.get("type") != drop.get("type"):
        return f"the events are of different types ({keep.get('type')} and {drop.get('type')})"
    if allow:
        return None
    k, d = event_ymd(keep), event_ymd(drop)
    if d != (0, 0, 0) and d != k and not sharper(k, d):
        return (f"the dates differ or the one to drop is the fuller ({ymd_text(k)} kept, {ymd_text(d)} dropped): "
                "swap keep and drop, or add \"allow_different\": true if this is meant")
    if drop.get("place") and keep.get("place") and drop["place"] != keep["place"]:
        return "the two events have different place records: add \"allow_different\": true if they are the same place"
    return None


def merge_pair(p, owner, tree):
    """The two events a merge_event means, as (keep, drop, None), or (None, None, why not). They are named by Gramps ID
    ('keep' and 'drop'), or by 'type' alone when the owner has exactly two events of that type whose dates do not
    contradict each other: then the one that says more is kept (on a tie, the one the owner lists first)."""
    mine = {r["ref"] for r in owner.get("event_ref_list", [])}
    allow = p.get("allow_different") is True
    if p.get("keep") or p.get("drop") or not p.get("type"):
        keep, drop = tree.event(p.get("keep")), tree.event(p.get("drop"))
        for ev, what in ((keep, "keep"), (drop, "drop")):
            if not ev:
                return None, None, f"{what}: event {p.get(what)} not found"
        if keep["handle"] == drop["handle"]:
            return None, None, "keep and drop are the same event"
        for ev, what in ((keep, "keep"), (drop, "drop")):
            if ev["handle"] not in mine:
                return None, None, f"{what}: {ev['gramps_id']} is not an event of {owner['gramps_id']}"
    else:
        two = own_events(owner, tree, p["type"])
        if len(two) != 2:
            return None, None, (f"{owner['gramps_id']} has {len(two)} {p['type']} event(s), not two: "
                                "name the two with keep and drop (Gramps IDs)")
        full = lambda ev: (sum(1 for x in event_ymd(ev) if x), bool(ev.get("place")), len(ev.get("citation_list") or []),
                           len(ev.get("note_list") or []) + len(ev.get("media_list") or []),
                           bool((ev.get("description") or "").strip()))
        keep, drop = sorted(two, key=full, reverse=True)
        k, d = event_ymd(keep), event_ymd(drop)
        if d != (0, 0, 0) and d != k and not sharper(k, d):
            return None, None, (f"the two {p['type']} events have different dates ({ymd_text(k)} and {ymd_text(d)}): "
                                "that is a research decision, so say which to keep with keep and drop")
    problem = merge_event_problem(keep, drop, allow)
    return (None, None, problem) if problem else (keep, drop, None)


# ---------- validation ----------
def check_citation(c, errs):
    for k in ("source_title", "page", "confidence", "tier"):
        if k not in c: errs.append(f"citation.{k} missing")
    if c.get("tier") in TIER_MAX_CONF and int(c.get("confidence", 0)) > TIER_MAX_CONF[c["tier"]]:
        errs.append(f"confidence {c.get('confidence')} too high for tier {c['tier']}")
    if not c.get("image") and c.get("tier") == "record":
        errs.append("tier 'record' requires an image")
    if c.get("image") and not any((d / c["image"]).exists() for d in (INBOX, HELD, APPLIED)):
        errs.append(f"image file {c['image']} not in inbox (or held/, applied/)")

def note_from_proposal(p):
    """'note' may be a plain string or {text, type, tier, private} (Karel's format since 27 Oct 2026)."""
    n = p["note"] if isinstance(p.get("note"), dict) else {"text": p["note"]}
    text = str(n.get("text") or "").strip()
    ntype = str(n.get("type") or p.get("note_type") or "Research")
    if ntype.lower().startswith("research"):
        ntype = "Research"
    tier = n.get("tier") or p.get("tier")
    if tier and f"tier: {tier}".lower() not in text.lower():
        text += f"\n\nTier: {tier}"
    obj = note(f"{date.today()} ({p['agent']}): {text}", ntype)
    obj["private"] = bool(n.get("private"))
    return obj


def research_note_only(p):
    """AI-draft research note with no facts: lives only in Gramps, so no Ancestry check (31 Oct 2026)."""
    if p.get("action") != "add_note" or any(p.get(k) for k in ("citation", "event", "events", "media", "family")):
        return False
    n = p["note"] if isinstance(p.get("note"), dict) else {}
    return (p.get("tier") or n.get("tier")) == "ai-draft" and str(n.get("type") or "Research").lower().startswith("research")


def links_named(p):
    """Ancestry IDs of the people a proposal ties to somebody (used to see that a new person is not left alone)."""
    act, out = p.get("action"), []
    if act == "add_person":
        rel = p.get("relations") or {}
        out += [rel.get(k) for k in ("father_ancestry_id", "mother_ancestry_id", "spouse_ancestry_id")]
    elif act == "add_family":
        fam = p.get("family") or {}
        out += [fam.get("father_ancestry_id"), fam.get("mother_ancestry_id")] + list(fam.get("children_ancestry_ids") or [])
    elif act == "update_family":
        s = p.get("set") or {}
        out += [ref_anc(s.get("father")), ref_anc(s.get("mother"))] + [ref_anc(c) for c in p.get("add_children") or []]
    return {str(x) for x in out if x}


def validate(p, tree, batch_ids=None, batch_links=frozenset()):
    """Returns (errors, waits). Errors reject the proposal. Waits are the Ancestry IDs of people it needs whose
    own add_person is held for approval: such a proposal is held too, and applied once they are in.
    batch_ids: the new people of this batch, as {their Ancestry ID: the spouse's Ancestry ID they name, or ""}."""
    errs, waits = [], []
    batch_ids = batch_ids or {}

    def person(ref, what, required=True):
        if ref in (None, "", {}):
            if required:
                errs.append(f"{what} missing")
            return None
        found = tree.find(ref)
        if not found:
            if ref_anc(ref) and ref_anc(ref) in tree.pending:
                waits.append(ref_anc(ref))
            else:
                errs.append(f"{what} not found in Gramps ({ref_text(ref)})")
        return found

    if p.get("proposal_version") != 1: errs.append("proposal_version must be 1")
    if p.get("agent") not in AGENT_TAGS: errs.append("agent must be karel")
    act = p.get("action")
    if act not in SUPPORTED:
        errs.append(f"action {act} not supported" + (f": use {OTHER_NAMES[act]} (see tools/PROPOSALS.md)" if act in OTHER_NAMES else ""))
    if not p.get("reasoning"): errs.append("reasoning missing")
    if p.get("ancestry_attached") is not True and not research_note_only(p):
        errs.append("ancestry_attached must be true (attach on Ancestry first)")
    if p.get("citation") and act in {"add_person", "add_family", "add_event", "add_citation", "add_media",
                                     "update_person", "update_family", "update_event"}:
        check_citation(p["citation"], errs)

    if act == "add_person":
        pe = p.get("person") or {}
        for k in ("ancestry_id", "first_name", "gender"):
            if not pe.get(k): errs.append(f"person.{k} missing")
        if not (pe.get("surname") or pe.get("married_surname")): errs.append("person needs surname or married_surname")
        if pe.get("gender") not in GENDER: errs.append("person.gender must be M, F or U")
        if "private" in pe and not isinstance(pe["private"], bool): errs.append("person.private must be true or false")
        if tree.find(str(pe.get("ancestry_id", ""))): errs.append("person already exists in Gramps (use add_event/add_citation)")
        rel = p.get("relations") or {}
        for k in ("father_ancestry_id", "mother_ancestry_id", "spouse_ancestry_id"):
            rid = str(rel.get(k) or "")
            if not rid or tree.find(rid):
                continue
            if rid in tree.pending:
                waits.append(rid)
            elif k == "spouse_ancestry_id" and batch_ids.get(rid) == str(pe.get("ancestry_id", "")):
                pass                                    # the two name each other: the second one added links them
            else:
                errs.append(f"relations.{k} {rid} not in Gramps" +
                            (" (its own proposal in this batch was not applied)" if rid in batch_ids else
                             " (and not an add_person in this batch)"))
        for k in ("father_rel", "mother_rel"):
            if rel.get(k) and rel[k] not in CHILD_REL:
                errs.append(f"relations.{k} must be one of {', '.join(sorted(CHILD_REL))}")
        if not any(rel.get(k) for k in ("father_ancestry_id", "mother_ancestry_id", "spouse_ancestry_id")) \
                and str(pe.get("ancestry_id", "")) not in batch_links:
            errs.append("person is not linked to anyone: give relations (father, mother or spouse), or file an "
                        "add_family or update_family in the same batch that places this person")
        for which in ("birth", "death"):
            if (pe.get(which) or {}).get("date_quality") == "between": errs.append(f"'between' dates not supported ({which})")
        return errs, waits

    if act == "add_family":
        fam = p.get("family") or {}
        fa = person(str(fam.get("father_ancestry_id") or ""), "family.father_ancestry_id", required=False)
        mo = person(str(fam.get("mother_ancestry_id") or ""), "family.mother_ancestry_id", required=False)
        if not (fam.get("father_ancestry_id") or fam.get("mother_ancestry_id")):
            errs.append("family needs at least one parent in Gramps")
        if "private" in fam and not isinstance(fam["private"], bool): errs.append("family.private must be true or false")
        mar = fam.get("marriage")
        if mar is not None:
            if not (mar.get("date") or mar.get("place")): errs.append("family.marriage needs a date or a place")
            if not (fam.get("father_ancestry_id") and fam.get("mother_ancestry_id")): errs.append("family.marriage needs both partners")
            if not p.get("citation"): errs.append("citation missing (the marriage needs its source)")
            if "private" in mar and not isinstance(mar["private"], bool): errs.append("family.marriage.private must be true or false")
        target = (tree.family_with(fa, mo) or [None])[0] if not (errs or waits) else None
        for c in fam.get("children_ancestry_ids", []):
            child = person(str(c), f"child {c}")
            if child and not (errs or waits):
                if child["handle"] in ((fa or {}).get("handle"), (mo or {}).get("handle")):
                    errs.append(f"child {c} is also named as a parent of the family")
                clash = birth_parent_clash(child, target, "Birth", "Birth", tree,
                                           (fa or {}).get("handle") or "", (mo or {}).get("handle") or "")
                if clash: errs.append(clash)
        return errs, waits

    if act == "update_family":
        s = p.get("set") or {}
        if not isinstance(s, dict) or set(s) - FAMILY_SET:
            errs.append(f"set may contain only: {', '.join(sorted(FAMILY_SET))}")
            return errs, waits
        kids = p.get("add_children") or []
        if not s and not kids:
            errs.append("nothing to do: give set and/or add_children")
        fam, why = tree.find_family(p.get("family"))
        fa = person(s.get("father"), "set.father", required=False)
        mo = person(s.get("mother"), "set.mother", required=False)
        if "private" in s and not isinstance(s["private"], bool): errs.append("set.private must be true or false")
        if "type" in s and s["type"] not in FAMILY_TYPES: errs.append(f"set.type must be one of {', '.join(sorted(FAMILY_TYPES))}")
        children = []
        for c in kids:
            child = person(c, "add_children entry")
            for rel in child_rels(c):
                if rel and rel not in CHILD_REL: errs.append(f"child relationship must be one of {', '.join(sorted(CHILD_REL))}")
            children.append((child, c))
        if not fam:
            named = p.get("family") if isinstance(p.get("family"), dict) else {}
            anc = [a for a in (ref_anc(named.get("father")), ref_anc(named.get("mother"))) if a and a in tree.pending]
            if anc: waits += anc
            else: errs.append(why)
        if errs or waits:
            return errs, waits
        try:
            new_f, new_m = parents_after(fam.get("father_handle") or None, fam.get("mother_handle") or None,
                                         (fa or {}).get("handle"), (mo or {}).get("handle"))
        except ValueError as ex:
            errs.append(f"{fam['gramps_id']}: {ex}")
            return errs, waits
        if (new_f, new_m) != (fam.get("father_handle") or None, fam.get("mother_handle") or None):
            twins = [f for f in tree.families if f is not fam and (f.get("father_handle") or None) == new_f
                     and (f.get("mother_handle") or None) == new_m]
            if twins and new_f and new_m:
                errs.append(f"{twins[0]['gramps_id']} already has these two parents: use merge_family "
                            f"(keep {twins[0]['gramps_id']}, drop {fam['gramps_id']})")
        kid_handles = {r["ref"] for r in fam.get("child_ref_list", [])} | {c["handle"] for c, _ in children}
        if {new_f, new_m} & kid_handles:
            errs.append("a parent of the family cannot also be its child")
        for child, c in children:
            have = next((r for r in fam.get("child_ref_list", []) if r["ref"] == child["handle"]), None)
            frel, mrel = child_rels(c)
            clash = birth_parent_clash(child, fam, frel or tname({"type": (have or {}).get("frel")}) or "Birth",
                                       mrel or tname({"type": (have or {}).get("mrel")}) or "Birth", tree,
                                       new_f or "", new_m or "")
            if clash: errs.append(clash)
        return errs, waits

    if act == "merge_family":
        keep, why_k = tree.find_family(p.get("keep"))
        drop, why_d = tree.find_family(p.get("drop"))
        for fam, why, what in ((keep, why_k, "keep"), (drop, why_d, "drop")):
            if not fam: errs.append(f"{what}: {why}")
        if keep and drop:
            problem = merge_family_problem(keep, drop, tree)
            if problem: errs.append(problem)
        return errs, waits

    if act in {"merge_event", "update_event"}:
        owner = None
        if p.get("family"):
            owner, why = tree.find_family(p["family"])
            if not owner: errs.append(why)
        else:
            owner = person(p.get("subject"), "subject")
        if act == "merge_event":
            if not (p.get("keep") and p.get("drop")) and not p.get("type"):
                errs.append("give keep and drop (Gramps IDs of the two events), or type (when there are exactly two of that type)")
            elif owner and not errs:
                _, _, why = merge_pair(p, owner, tree)
                if why: errs.append(why)
            return errs, waits
        s = p.get("set")
        if not isinstance(s, dict) or not s or set(s) - EVENT_SET:
            errs.append(f"set must name what to change; it may contain only: {', '.join(sorted(EVENT_SET))}")
            return errs, waits
        for k in ("date", "place"):
            if k in s and not str(s[k] or "").strip(): errs.append(f"set.{k} may not be empty (a date or place cannot be taken away)")
        if "date_quality" in s and "date" not in s: errs.append("set.date_quality needs set.date")
        if "description" in s and not isinstance(s["description"], str): errs.append("set.description must be text")
        if "private" in s and not isinstance(s["private"], bool): errs.append("set.private must be true or false")
        if (s.get("date") or s.get("place")) and not p.get("citation"):
            errs.append("citation missing (a corrected date or place needs its source)")
        if owner and not errs:
            _, why = pick_event(p.get("event"), owner, tree)
            if why: errs.append(why)
        return errs, waits

    if act == "set_private":
        recs = p.get("records")
        if not isinstance(recs, list) or not recs:
            errs.append("records must be a list of Gramps IDs: people (I…), families (F…), events (E…), notes (N…)")
        if not isinstance(p.get("private"), bool): errs.append("private must be true or false")
        for ref in recs if isinstance(recs, list) else []:
            _, obj, why = tree.record(ref)
            if obj:
                continue
            if isinstance(ref, dict) and ref_anc(ref) in tree.pending: waits.append(ref_anc(ref))
            else: errs.append(why)
        return errs, waits

    who = person(p.get("subject"), "subject")

    if act == "update_person":
        s = p.get("set")
        if not isinstance(s, dict) or not s or set(s) - PERSON_SET:
            errs.append(f"set must name what to change; it may contain only: {', '.join(sorted(PERSON_SET))}")
            return errs, waits
        for k in ("first_name", "surname", "married_surname"):
            if k in s and not str(s[k] or "").strip(): errs.append(f"set.{k} may not be empty")
        for k in ("suffix", "call_name", "nickname"):
            if k in s and not isinstance(s[k], str): errs.append(f"set.{k} must be text")
        if "gender" in s and s["gender"] not in GENDER: errs.append("set.gender must be M, F or U")
        if "private" in s and not isinstance(s["private"], bool): errs.append("set.private must be true or false")
        for aka in as_list(s.get("also_known_as")):
            if not any(split_name(aka)): errs.append("set.also_known_as entry is empty")
        if "prefer_event" in s:
            ev = tree.event(s["prefer_event"])
            if not ev: errs.append(f"set.prefer_event: event {s['prefer_event']} not found")
            elif who and ev["handle"] not in {r["ref"] for r in who.get("event_ref_list", [])}:
                errs.append(f"set.prefer_event: {ev['gramps_id']} is not an event of {who['gramps_id']}")
        return errs, waits

    if act in {"add_event", "add_citation"} and not p.get("citation"):
        errs.append("citation missing")
    if act == "add_event":
        e = p.get("event") or {}
        if "type" not in e: errs.append("event.type missing")
        if e.get("date_quality") == "between": errs.append("'between' dates not supported; log as ASK")
        if "private" in e and not isinstance(e["private"], bool): errs.append("event.private must be true or false")
        if e.get("type") in FAMILY_EVENTS and who:
            fams = tree.family_of(who, tree.find(p.get("spouse")))
            if len(fams) != 1:
                errs.append(f"family event needs exactly one family for the subject (found {len(fams)}); add spouse.ancestry_id")
    if act == "add_note" and not p.get("note"): errs.append("note missing")
    if act == "add_media" and not (p.get("citation") or {}).get("image"): errs.append("add_media needs citation.image")
    return errs, waits


# ---------- approval ----------
def new_person_recent(p, tree):
    """The same question as Tree.recent, for a person who is not in the tree yet."""
    pe, rel = p.get("person") or {}, p.get("relations") or {}
    if pe.get("private"):
        return True
    born = (date_key((pe.get("birth") or {}).get("date")) or (0,))[0]
    if born:
        return born >= date.today().year - RECENT_YEARS
    limits = [y for y in ((date_key((pe.get("death") or {}).get("date")) or (0,))[0],) if y]
    for k, add in (("father_ancestry_id", 70), ("mother_ancestry_id", 70), ("spouse_ancestry_id", 40)):
        other = tree.find(str(rel.get(k) or ""))
        if other:
            limits += [y + add for y in tree.years(other, {"Birth", "Baptism", "Christening"})]
            if add == 70:
                limits += [y + 1 for y in tree.years(other, {"Death", "Burial", "Cremation"})]
    return not limits or min(limits) >= date.today().year - RECENT_YEARS


def people_touched(p, tree):
    """The people already in the tree that a proposal is about, each with the limits on their year of birth that
    the proposal itself implies (a woman filed as the mother of a child born in 1882 was not born after 1870)."""
    act = p.get("action")
    born = lambda person: tree.years(person, {"Birth", "Baptism", "Christening"})
    died = lambda person: tree.years(person, {"Death", "Burial", "Cremation"})
    out = []
    if act == "add_person":
        pe, rel = p.get("person") or {}, p.get("relations") or {}
        year = (date_key((pe.get("birth") or {}).get("date")) or (0,))[0]
        for k, gap in (("father_ancestry_id", -12), ("mother_ancestry_id", -12), ("spouse_ancestry_id", 40)):
            person = tree.find(str(rel.get(k) or ""))
            if person:
                out.append((person, [year + gap] if year else []))
    elif act == "add_family":
        fam = p.get("family") or {}
        parents = [x for x in (tree.find(str(fam.get(k) or "")) for k in ("father_ancestry_id", "mother_ancestry_id")) if x]
        kids = [x for x in (tree.find(str(c)) for c in fam.get("children_ancestry_ids") or []) if x]
        wed = (date_key((fam.get("marriage") or {}).get("date")) or (0,))[0]
        for person in parents:
            out.append((person, [y - 12 for k in kids for y in born(k)] + ([wed - 12] if wed else []) +
                        [y + 40 for o in parents if o is not person for y in born(o)]))
        for person in kids:
            out.append((person, [y + 70 for o in parents for y in born(o)] + [y + 1 for o in parents for y in died(o)]))
    else:
        out = [(x, []) for x in (tree.find(p.get("subject")), tree.find(p.get("spouse"))) if x]
    return out


def needs_approval(p, tree):
    """Why Chris has to approve this proposal first, or None if it may be applied as it is."""
    if p.get("action") in CHANGES:
        return "changes a record that is already in the tree"
    if p.get("action") == "add_person" and (p.get("person") or {}).get("private"):
        return "adds a private person"
    for person, limits in people_touched(p, tree):
        if person.get("private"):
            return f"about a private record ({person_label(person)})"
        if tree.recent(person, limits):
            return f"about someone living or recent ({person_label(person)})"
    if p.get("action") == "add_person" and new_person_recent(p, tree):
        return "adds someone living or recent"
    return None


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def approvals():
    """What Chris has approved: (content hash, file name) pairs."""
    if not APPROVALS.exists():
        return set()
    return {tuple(ln.split("\t")[:2]) for ln in APPROVALS.read_text(encoding="utf-8").splitlines() if "\t" in ln}

def is_approved(path, approved):
    return (file_hash(path), Path(path).name) in approved

def revoke(name):
    """Forget the approval of a file (it was refused or undone): filing it again must be approved again."""
    if APPROVALS.exists():
        keep = [ln for ln in APPROVALS.read_text(encoding="utf-8").splitlines() if ln.split("\t")[1:2] != [name]]
        APPROVALS.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")

def seen_file():
    return APPROVALS.with_name("held-seen.txt")


# ---------- plain words ----------
def proposal_date(e):
    e = e or {}
    if not e.get("date"):
        return ""
    q = e.get("date_quality", "exact")
    return ymd_text(date_key(e["date"]), MODIFIER.get(q, 0), QUALITY.get(q, 0))

def describe(p, tree):
    """What a proposal would do, in plain words, with every name taken from Gramps or from the proposal itself."""
    act, lines = p.get("action"), []

    def who(ref):
        if tree.find(ref):
            return tree.person_text(tree.find(ref))
        name = tree.batch_names.get(ref_anc(ref))
        return f"{name} (also new, in this batch)" if name else f"someone not in the tree yet ({ref_text(ref)})"
    if act == "add_person":
        pe, rel = p.get("person") or {}, p.get("relations") or {}
        name = " ".join(x for x in (pe.get("first_name", ""), pe.get("surname", ""), pe.get("suffix", "")) if x)
        if pe.get("married_surname"):
            name += f" (married name {pe['married_surname']})"
        facts = [GENDER_WORD.get(GENDER.get(pe.get("gender")), "sex unknown")]
        for which, word in (("birth", "born"), ("death", "died")):
            e = pe.get(which) or {}
            if e.get("date") or e.get("place"):
                facts.append(" ".join(x for x in (word, proposal_date(e), ("in " + e["place"]) if e.get("place") else "") if x))
        lines.append(f"ADD PERSON  {name}: {'; '.join(facts)}" + ("  [private]" if pe.get("private") else ""))
        fa, mo = rel.get("father_ancestry_id"), rel.get("mother_ancestry_id")
        if fa or mo:
            how = {("Birth", "Birth"): "by birth", ("Adopted", "Adopted"): "adopted by both"}.get(
                (rel.get("father_rel", "Birth"), rel.get("mother_rel", "Birth")),
                f"father: {rel.get('father_rel', 'Birth').lower()}, mother: {rel.get('mother_rel', 'Birth').lower()}")
            lines.append(f"    child of {who(str(fa)) if fa else '—'} × {who(str(mo)) if mo else '—'}  ({how})")
        if rel.get("spouse_ancestry_id"):
            lines.append(f"    spouse of {who(str(rel['spouse_ancestry_id']))}")
    elif act == "add_family":
        fam = p.get("family") or {}
        fa, mo = fam.get("father_ancestry_id"), fam.get("mother_ancestry_id")
        lines.append(f"LINK FAMILY  {who(str(fa)) if fa else '—'} × {who(str(mo)) if mo else '—'}" +
                     ("  [private]" if fam.get("private") else ""))
        mar = fam.get("marriage") or {}
        if mar:
            lines.append("    married " + " ".join(x for x in (proposal_date(mar), ("in " + mar["place"]) if mar.get("place") else "") if x) +
                         ("  [private]" if mar.get("private") else ""))
        for c in fam.get("children_ancestry_ids") or []:
            lines.append(f"    child (by birth): {who(str(c))}")
    elif act == "update_person":
        s = p.get("set") or {}
        lines.append(f"CHANGE PERSON  {who(p.get('subject'))}")
        person = tree.find(p.get("subject")) or {}
        old = person.get("primary_name") or {}
        new = dict(old, first_name=s.get("first_name", old.get("first_name", "")), suffix=s.get("suffix", old.get("suffix", "")))
        if "surname" in s:
            new["surname_list"] = [{"surname": s["surname"]}]
        if name_text(new) != name_text(old):
            lines.append(f"    name: {name_text(old) or '(none)'}  →  {name_text(new)}")
        if "married_surname" in s: lines.append(f"    adds the married name {s['married_surname']}")
        for aka in as_list(s.get("also_known_as")): lines.append(f"    adds “also known as” {' '.join(x for x in split_name(aka) if x)}")
        for k, word in (("call_name", "call name"), ("nickname", "nickname")):
            if k in s: lines.append(f"    {word}: {s[k] or '(none)'}")
        if "gender" in s: lines.append(f"    sex: {GENDER_WORD.get(person.get('gender'), '?')}  →  {GENDER_WORD[GENDER[s['gender']]]}")
        if "private" in s: lines.append(f"    private: {'yes' if person.get('private') else 'no'}  →  {'yes' if s['private'] else 'no'}")
        if "prefer_event" in s and tree.event(s["prefer_event"]):
            lines.append(f"    shows {event_text(tree.event(s['prefer_event']), tree)} as the main one of its kind")
    elif act == "update_family":
        fam, _ = tree.find_family(p.get("family"))
        s = p.get("set") or {}
        lines.append(f"CHANGE FAMILY  {tree.family_text(fam) if fam else p.get('family')}")
        for side in ("father", "mother"):
            if s.get(side): lines.append(f"    {side}: {who(s[side])}")
        swap = bool(fam and s.get("father") and s.get("mother") and
                    ((tree.find(s["father"]) or {}).get("handle"), (tree.find(s["mother"]) or {}).get("handle")) ==
                    (fam.get("mother_handle") or None, fam.get("father_handle") or None))
        if swap:
            lines.append("    (the two change places; nobody is added or removed)")
        elif fam and (s.get("father") or s.get("mother")):
            kids = [tree.person_text(tree.people_h[c["ref"]]) for c in fam.get("child_ref_list", []) if c["ref"] in tree.people_h]
            if kids:
                lines.append(f"    so the children already in this family get that parent too: {', '.join(kids)}")
        for c in p.get("add_children") or []:
            frel, mrel = child_rels(c)
            how = "by birth" if (frel or "Birth", mrel or "Birth") == ("Birth", "Birth") else \
                  f"father: {(frel or 'Birth').lower()}, mother: {(mrel or 'Birth').lower()}"
            lines.append(f"    child: {who(c)}  ({how})")
        if "private" in s: lines.append(f"    private: {'yes' if s['private'] else 'no'}")
        if "type" in s: lines.append(f"    type: {s['type']}")
    elif act == "merge_family":
        for what, word in (("keep", "keep"), ("drop", "fold in and delete")):
            fam, _ = tree.find_family(p.get(what))
            if fam:
                kids = [tree.person_text(tree.people_h[c["ref"]]) for c in fam.get("child_ref_list", []) if c["ref"] in tree.people_h]
                lines.append(f"{'MERGE FAMILIES' if what == 'keep' else '              '}  {word} {tree.family_text(fam)}; "
                             f"children: {', '.join(kids) or 'none'}")
            else:
                lines.append(f"MERGE FAMILIES  {word} {p.get(what)} (not found)")
    elif act == "merge_event":
        owner = tree.find_family(p["family"])[0] if p.get("family") else tree.find(p.get("subject"))
        owner_text = (tree.family_text(owner) if p.get("family") else tree.person_text(owner)) if owner else "?"
        lines.append(f"MERGE EVENTS  of {owner_text}")
        keep, drop, why = merge_pair(p, owner, tree) if owner else (None, None, "owner not found")
        if why:
            lines.append(f"    (cannot be done as it stands: {why})")
        for ev, word in ((keep, "keep"), (drop, "fold in and delete")):
            if ev:
                n = len(ev.get("citation_list") or [])
                lines.append(f"    {word} {event_text(ev, tree)}; {n or 'no'} source{'' if n == 1 else 's'}")
        if keep and drop and p.get("allow_different") is True:
            lines.append("    (they differ; a note on the one kept records what the other said)")
    elif act == "update_event":
        owner = tree.find_family(p["family"])[0] if p.get("family") else tree.find(p.get("subject"))
        owner_text = (tree.family_text(owner) if p.get("family") else tree.person_text(owner)) if owner else "?"
        ev, why = pick_event(p.get("event"), owner, tree) if owner else (None, "owner not found")
        lines.append(f"CHANGE EVENT  {event_text(ev, tree) if ev else p.get('event')}  of {owner_text}")
        if why:
            lines.append(f"    (cannot be done as it stands: {why})")
        else:
            changes = event_changes(ev, p.get("set") or {}, tree)
            for field, old, new in changes:
                lines.append(f"    {field}: {old}  →  {new}")
            if not changes:
                lines.append("    nothing to change: it already says this")
            shared = [x for x in tree.owners_of(ev) if not x.startswith(owner["gramps_id"] + " ")]
            if shared:
                lines.append(f"    (the same event also belongs to: {', '.join(shared)})")
    elif act == "set_private":
        want = p.get("private") is True
        lines.append("MARK PRIVATE  (these are then left off the public site altogether)" if want else
                     "MAKE PUBLIC  (no longer private: a living person then shows by name and relationship, anyone else in full)")
        for ref in p.get("records") or []:
            endpoint, obj, why = tree.record(ref)
            if not obj:
                lines.append(f"    {why}")
                continue
            if endpoint == "people": text = "person  " + tree.person_text(obj).replace(" [private]", "")
            elif endpoint == "families":
                kids = [person_label(tree.people_h[c["ref"]]) for c in obj.get("child_ref_list", []) if c["ref"] in tree.people_h]
                text = f"family  {family_label(obj, tree)}; children: {', '.join(kids) or 'none'}"
            elif endpoint == "events": text = f"event   {event_text(obj, tree)} of {', '.join(tree.owners_of(obj)) or 'nobody'}"
            else:
                links = obj.get("backlinks") or {}
                on = [person_label(tree.people_h[h]) for h in links.get("person", []) if h in tree.people_h] + \
                     [family_label(tree.fam_h[h], tree) for h in links.get("family", []) if h in tree.fam_h] + \
                     [event_text(tree.events_h[h], tree) for h in links.get("event", []) if h in tree.events_h] + \
                     [f"{len(v)} {k}(s)" for k, v in sorted(links.items()) if k not in ("person", "family", "event")]
                words = " ".join(str((obj.get("text") or {}).get("string") or "").split())
                text = f"note    {obj.get('gramps_id')} on {', '.join(on) or 'nothing'}: “{words[:110]}{'…' if len(words) > 110 else ''}”"
            lines.append("    " + text + ("   (already so)" if bool(obj.get("private")) == want else ""))
    else:
        subject = who(p.get("subject"))
        if act == "add_event":
            e = p.get("event") or {}
            what = " ".join(x for x in (e.get("type", "?"), proposal_date(e), ("in " + e["place"]) if e.get("place") else "") if x)
            lines.append(f"ADD EVENT  {what}" + ("  [private]" if e.get("private") else "") + f"  for {subject}")
        elif act == "add_note":
            n = p.get("note")
            text = (n.get("text") if isinstance(n, dict) else n) or ""
            lines.append(f"ADD NOTE  for {subject}: {' '.join(str(text).split())[:160]}")
        else:
            lines.append(f"{'ADD CITATION' if act == 'add_citation' else 'ADD MEDIA'}  for {subject}")
    c = p.get("citation") or {}
    if c:
        lines.append(f"    source: {c.get('source_title', '?')}, {c.get('page', '')}".rstrip(", "))
    if p.get("reasoning"):
        lines.append(f"    Karel's reason: {' '.join(str(p['reasoning']).split())[:300]}")
    return "\n".join(lines)


# ---------- building blocks ----------
def build_citation(p, tree, api, tags, fname, handles):
    """Source (reuse or create), notes, media, citation. Returns citation handle or None."""
    c = p.get("citation") or {}
    if not c: return None
    key = c["source_title"].strip().lower()
    src_h = tree.sources.get(key)
    if not src_h:
        repo_h = None
        if c.get("repository"):
            repo_h = tree.repos.get(c["repository"].strip().lower())
            if not repo_h:
                repo_h = api.post("repositories/", {"_class": "Repository", "name": c["repository"],
                                                    "type": "Archive", "private": False, "tag_list": tags})
                tree.repos[c["repository"].strip().lower()] = repo_h
        src = {"_class": "Source", "title": c["source_title"], "author": c.get("author", ""),
               "pubinfo": c.get("pubinfo", ""), "private": False, "tag_list": tags, "reporef_list": []}
        if repo_h:
            src["reporef_list"] = [{"_class": "RepoRef", "ref": repo_h, "media_type": "Electronic", "call_number": "", "private": False}]
        src_h = api.post("sources/", src); tree.sources[key] = src_h; handles["source(new)"] = src_h
    note_hs = []
    if c.get("transcription"):
        note_hs.append(api.post("notes/", note("Transcription (as written): " + c["transcription"], "Transcript")))
    note_hs.append(api.post("notes/", note(f"{date.today()} ({p['agent']}): {p['reasoning']}"
                                         + (f"\nConflicts: {p['conflicts']}" if p.get("conflicts") else ""))))
    media_h = None
    if c.get("image"):
        img = next(d / c["image"] for d in (INBOX, HELD, APPLIED) if (d / c["image"]).exists())
        mime = mimetypes.guess_type(img.name)[0] or "application/octet-stream"
        media_h = api.post("media/", None, raw=img.read_bytes(), mime=mime)
        if not DRY:
            def describe_media(m):
                m["desc"] = f"{c['source_title']} — {c.get('page', '')}".strip(" —")
                add_tags(m, tags)
                m.setdefault("attribute_list", []).append(attr("Original file", c["image"]))
            api.modify("media", media_h, describe_media)
        handles["media"] = media_h
    cit = {"_class": "Citation", "source_handle": src_h, "page": c["page"], "confidence": int(c["confidence"]),
           "private": False, "tag_list": tags, "note_list": note_hs,
           "media_list": [{"_class": "MediaRef", "ref": media_h, "private": False, "rect": []}] if media_h else [],
           "attribute_list": [attr("Tier", c["tier"], "SrcAttribute"),
                              attr("Source", c.get("source_type", ""), "SrcAttribute"),
                              attr("Information", c.get("information", ""), "SrcAttribute"),
                              attr("Evidence", c.get("evidence", ""), "SrcAttribute"),
                              attr("Ancestry record ID", c.get("ancestry_record_id", ""), "SrcAttribute"),
                              attr("URL", c.get("url", ""), "SrcAttribute"),
                              attr("Proposal file", fname, "SrcAttribute")]}
    if c.get("date"): cit["date"] = gramps_date(c["date"])
    cit_h = api.post("citations/", cit); handles["citation"] = cit_h
    return cit_h

def place_handle(name, tree, api, tags, handles):
    if not name: return None
    h = tree.places.get(name.strip().lower())
    if not h:
        h = api.post("places/", {"_class": "Place", "title": name,
                                 "name": {"_class": "PlaceName", "value": name, "lang": ""},
                                 "place_type": "Unknown", "private": False,
                                 "tag_list": tags + [tree.tag("needs-place")]})
        tree.places[name.strip().lower()] = h; tree.place_names[h] = name; handles["place(new)"] = h
    return h

def new_event(etype, e, cit_h, place_h, tags, tree, private=False):
    ev = {"_class": "Event", "type": etype, "description": e.get("description", ""), "private": bool(private or e.get("private")),
          "citation_list": [cit_h] if cit_h else [], "tag_list": tags, "note_list": []}
    if e.get("date"): ev["date"] = gramps_date(e["date"], e.get("date_quality", "exact"))
    if place_h: ev["place"] = place_h
    return tree.create("events", ev)["handle"]

def reuse_event(existing, cit_h, e, place_h, tags, api):
    def fn(ev):
        if cit_h and cit_h not in ev.get("citation_list", []): ev["citation_list"] = ev.get("citation_list", []) + [cit_h]
        if not ev.get("description") and e.get("description"): ev["description"] = e["description"]
        if not ev.get("place") and place_h: ev["place"] = place_h
        if sharper(date_key(e.get("date")), event_ymd(ev)): ev["date"] = gramps_date(e["date"], e.get("date_quality", "exact"))
        if e.get("private"): ev["private"] = True
        add_tags(ev, tags)
    api.modify("events", existing["handle"], fn)

def ensure_family(father, mother, tree, api, tags, handles, private=False):
    """Family with these parents: reuse or create (and link both parents to it).

    The family is private when asked, or when either partner is private: a family left public around a hidden
    partner shows on the site as a family with a blank partner. A family that an earlier proposal of this run
    made (the other partner's add_person, say) is new to this batch and is made private here when it should be;
    a family that was in the tree before this run is left as it is (changing it is set_private's job)."""
    want = bool(private) or any(bool((p or {}).get("private")) for p in (father, mother))
    fams = tree.family_with(father, mother)
    if fams:
        fam = fams[0]
        if want and not fam.get("private") and fam["handle"] in tree.made:
            api.modify("families", fam["handle"], lambda f: f.update(private=True))
            fam = tree.cached("families", fam["handle"]) or fam
            handles["family(private)"] = fam.get("gramps_id")
        return fam
    fam = {"_class": "Family", "father_handle": (father or {}).get("handle"), "mother_handle": (mother or {}).get("handle"),
           "type": "Married" if father and mother else "Unknown", "child_ref_list": [], "event_ref_list": [],
           "private": want, "tag_list": tags}
    fam = tree.create("families", fam)
    for par in (father, mother):
        if par: api.add_ref("people", par["handle"], "family_list", fam["handle"], tags)
    handles["family(new)"] = fam["handle"]
    return fam

def add_child(family, child, tree, api, tags, frel="Birth", mrel="Birth"):
    ref = {"_class": "ChildRef", "ref": child["handle"], "frel": frel, "mrel": mrel, "private": False}
    api.add_ref("families", family["handle"], "child_ref_list", ref, tags)
    api.add_ref("people", child["handle"], "parent_family_list", family["handle"], tags)

def settle_family(fam_h, tree, api):
    """After a family changed: see that each parent and child points back at it (the API normally does that
    itself, in the same transaction) and bring what we remember up to date. Returns the family as it is now."""
    fam = tree.refresh("families", fam_h)
    for h in (fam.get("father_handle"), fam.get("mother_handle")):
        if h: api.add_ref("people", h, "family_list", fam_h)
    for c in fam.get("child_ref_list", []):
        api.add_ref("people", c["ref"], "parent_family_list", fam_h)
    return fam

def family_event(family, e, cit_h, place_h, tree, api, tags, handles, private=False):
    """Put an event on a family: the citation goes onto one it already has of that type and date, else a new one is made."""
    existing = tree.matching_event(family, e["type"], date_key(e.get("date")))
    if existing:
        reuse_event(existing, cit_h, e, place_h, tags, api); handles["event(existing)"] = existing["gramps_id"]
        api.modify("families", family["handle"], lambda f: add_tags(f, tags))
    else:
        ev_h = new_event(e["type"], e, cit_h, place_h, tags, tree, private); handles["event(new)"] = ev_h
        api.add_ref("families", family["handle"], "event_ref_list",
                    {"_class": "EventRef", "ref": ev_h, "role": "Family", "private": False}, tags)
        handles["family"] = family.get("gramps_id")

def couple(a, b):
    """(father, mother) for two partners, by the sex recorded for them."""
    return (a, b) if a.get("gender") == 1 or (b.get("gender") == 0 and a.get("gender") != 0) else (b, a)


# ---------- apply ----------
def apply(p, tree, api, fname):
    tags = [tree.tag(t) for t in AGENT_TAGS[p["agent"]]]
    handles = {}
    act = p["action"]

    if act == "add_person":
        pe = p["person"]; rel = p.get("relations") or {}
        private = bool(pe.get("private"))
        cit_h = build_citation(p, tree, api, tags, fname, handles)
        primary = name_obj(pe["first_name"], pe.get("surname", ""), "Birth Name", pe.get("suffix", ""))
        for key, field in (("call_name", "call"), ("nickname", "nick")):
            if pe.get(key): primary[field] = pe[key]
        person = {"_class": "Person", "gender": GENDER[pe["gender"]], "private": private, "tag_list": tags,
                  "primary_name": primary,
                  "alternate_names": [name_obj(pe["first_name"], pe["married_surname"], "Married Name")] if pe.get("married_surname") else [],
                  "attribute_list": [attr("Ancestry ID", pe["ancestry_id"])],
                  "citation_list": [cit_h] if cit_h else [], "event_ref_list": [],
                  "birth_ref_index": -1, "death_ref_index": -1}
        for which in ("Birth", "Death"):
            e = pe.get(which.lower()) or {}
            if e.get("date") or e.get("place"):
                pl = place_handle(e.get("place"), tree, api, tags, handles)
                ev_h = new_event(which, e, cit_h, pl, tags, tree, private)
                person["event_ref_list"].append({"_class": "EventRef", "ref": ev_h, "role": "Primary", "private": False})
                person[f"{which.lower()}_ref_index"] = len(person["event_ref_list"]) - 1
                handles[f"{which.lower()}(new)"] = ev_h
        created = tree.create("people", person); handles["person(new)"] = created["handle"]
        fa, mo = tree.find(str(rel.get("father_ancestry_id", ""))), tree.find(str(rel.get("mother_ancestry_id", "")))
        if fa or mo:
            fam = ensure_family(fa, mo, tree, api, tags, handles)
            add_child(fam, created, tree, api, tags, rel.get("father_rel", "Birth"), rel.get("mother_rel", "Birth"))
            handles["parents"] = fam.get("gramps_id")
        sp = tree.find(str(rel.get("spouse_ancestry_id", "")))
        if sp:
            father, mother = (created, sp) if pe["gender"] == "M" else (sp, created)
            fam = ensure_family(father, mother, tree, api, tags, handles); handles["spouse"] = fam.get("gramps_id")
        elif rel.get("spouse_ancestry_id"):
            tree.deferred.setdefault(str(rel["spouse_ancestry_id"]), []).append(created)
            handles["spouse"] = "deferred (spouse is added later in this batch)"
        for waiting in tree.deferred.pop(str(pe["ancestry_id"]), []):     # someone added earlier named this person as spouse
            father, mother = couple(created, waiting)
            ensure_family(father, mother, tree, api, tags, handles)
            handles.setdefault("spouse", person_label(waiting))
        return handles, person_label(created)

    if act == "add_family":
        f = p["family"]
        fa, mo = tree.find(str(f.get("father_ancestry_id", ""))), tree.find(str(f.get("mother_ancestry_id", "")))
        cit_h = build_citation(p, tree, api, tags, fname, handles)
        fam = ensure_family(fa, mo, tree, api, tags, handles, f.get("private"))
        if f.get("private") and not fam.get("private"):
            raise Refused(f"{family_label(fam, tree)} was in the tree before this batch and is public; \"private\": true is "
                          f"not applied to an existing family here: file set_private for it")
        if cit_h: api.add_ref("families", fam["handle"], "citation_list", cit_h, tags)
        for c in f.get("children_ancestry_ids", []):
            child = tree.find(str(c))
            if child["handle"] not in [r["ref"] for r in fam.get("child_ref_list", [])]:
                add_child(fam, child, tree, api, tags)
        if f.get("marriage"):
            mar = dict(f["marriage"], type="Marriage")
            family_event(fam, mar, cit_h, place_handle(mar.get("place"), tree, api, tags, handles), tree, api, tags, handles,
                         private=bool(fam.get("private")))
        handles["family"] = fam.get("gramps_id")
        return handles, person_label(fa or mo)

    if act == "update_family":
        fam, _ = tree.find_family(p["family"]); s = p.get("set") or {}
        cit_h = build_citation(p, tree, api, tags, fname, handles)
        want_f, want_m = (tree.find(s.get("father")) or {}).get("handle"), (tree.find(s.get("mother")) or {}).get("handle")
        kids = [(tree.find(c), child_rels(c)) for c in p.get("add_children") or []]
        done = []
        def fn(f):
            cur = (f.get("father_handle") or None, f.get("mother_handle") or None)
            try:
                new = parents_after(cur[0], cur[1], want_f, want_m)
            except ValueError as ex:
                raise Refused(f"{f.get('gramps_id')}: {ex}")
            if new != cur:
                f["father_handle"], f["mother_handle"] = new
                done.append("parents")
            for child, (frel, mrel) in kids:
                ref = next((r for r in f.setdefault("child_ref_list", []) if r["ref"] == child["handle"]), None)
                if ref is None:
                    f["child_ref_list"].append({"_class": "ChildRef", "ref": child["handle"], "frel": frel or "Birth",
                                                "mrel": mrel or "Birth", "private": False})
                    done.append(f"child {child['gramps_id']}")
                else:
                    for key, rel in (("frel", frel), ("mrel", mrel)):
                        if rel and tname({"type": ref.get(key)}) != rel:
                            ref[key] = rel
                            done.append(f"child {child['gramps_id']} {key}")
            if "private" in s and bool(f.get("private")) != s["private"]:
                f["private"] = s["private"]; done.append("private")
            if "type" in s and tname(f) != s["type"]:
                f["type"] = s["type"]; done.append("type")
            if cit_h and cit_h not in f.get("citation_list", []):
                f["citation_list"] = f.get("citation_list", []) + [cit_h]
            if done or cit_h: add_tags(f, tags)
        api.modify("families", fam["handle"], fn)
        fam = settle_family(fam["handle"], tree, api)
        handles["family"] = fam.get("gramps_id"); handles["changed"] = done or "nothing (already so)"
        return handles, family_label(fam, tree)

    if act == "merge_family":
        keep, drop = tree.find_family(p["keep"])[0], tree.find_family(p["drop"])[0]
        handles.update(kept=keep.get("gramps_id"), merged=drop.get("gramps_id"))
        if DRY:
            have = {c["ref"] for c in keep.get("child_ref_list", [])}
            keep.setdefault("child_ref_list", []).extend(c for c in drop.get("child_ref_list", []) if c["ref"] not in have)
            for side in ("father_handle", "mother_handle"):
                keep[side] = keep.get(side) or drop.get(side)
            tree.forget("families", drop["handle"]); api.writes += 1
            return handles, family_label(keep, tree)
        # look again, straight from Gramps: the merge itself must never be sent for families whose parents differ
        k, d = api.get(f"families/{keep['handle']}"), api.get(f"families/{drop['handle']}")
        problem = merge_family_problem(k, d, tree)
        if problem: raise Refused(problem)
        people = sorted({h for f in (k, d) for h in [f.get("father_handle"), f.get("mother_handle")] +
                         [c["ref"] for c in f.get("child_ref_list", [])] if h})
        before = [("update", "people", h, api.get(f"people/{h}")) for h in people]
        before += [("update", "families", k["handle"], k), ("delete", "families", d["handle"], d)]
        body = {f"phoenix_{side}_handle": d[f"{side}_handle"] for side in ("father", "mother")
                if d.get(f"{side}_handle") and not k.get(f"{side}_handle")}
        r = api._send("POST", f"families/{k['handle']}/merge/{d['handle']}", json=body)
        if r.status_code >= 300:
            raise ApiError(f"merge {d.get('gramps_id')} into {k.get('gramps_id')}: {r.status_code} {r.text[:300]}", r.status_code)
        api.journal += before; api.writes += 1
        tree.forget("families", d["handle"])
        api.modify("families", k["handle"], lambda f: add_tags(f, tags))
        fam = settle_family(k["handle"], tree, api)
        kids = {c["ref"] for c in fam.get("child_ref_list", [])}
        if api.exists(f"families/{d['handle']}"):
            raise RuntimeError(f"{d.get('gramps_id')} is still there after the merge")
        if any(c["ref"] not in kids for c in k.get("child_ref_list", []) + d.get("child_ref_list", [])):
            raise RuntimeError("a child went missing in the merge")
        for h in people:
            person = tree.refresh("people", h)
            if d["handle"] in person.get("family_list", []) + person.get("parent_family_list", []):
                raise RuntimeError(f"{person_label(person)} still points at the merged family")
        handles["children"] = len(kids)
        return handles, family_label(fam, tree)

    if act == "merge_event":
        owner = tree.find_family(p["family"])[0] if p.get("family") else tree.find(p["subject"])
        owner_label = family_label(owner, tree) if p.get("family") else person_label(owner)
        keep, drop, why = merge_pair(p, owner, tree)
        if why: raise Refused(why)
        handles.update(kept=keep.get("gramps_id"), merged=drop.get("gramps_id"))
        if DRY:
            owner["event_ref_list"] = [r for r in owner.get("event_ref_list", []) if r["ref"] != drop["handle"]]
            tree.forget("events", drop["handle"]); api.writes += 1
            return handles, owner_label
        k, d = api.get(f"events/{keep['handle']}"), api.get(f"events/{drop['handle']}", backlinks=1)
        links = d.pop("backlinks", None) or {}
        problem = merge_event_problem(k, d, p.get("allow_different") is True)
        if problem: raise Refused(problem)
        lost = []                       # what the dropped event says that the kept one will not
        if event_ymd(d) != (0, 0, 0) and event_ymd(d) != event_ymd(k) and not sharper(event_ymd(k), event_ymd(d)):
            lost.append(f"date {ymd_text(event_ymd(d), date_modifier(d))}")
        if d.get("place") and k.get("place") and d["place"] != k["place"]:
            lost.append(f"place {tree.place_text(d['place']) or d['place']}")
        if (d.get("description") or "").strip() and (k.get("description") or "").strip() and d["description"].strip() != k["description"].strip():
            lost.append(f"description “{d['description'].strip()}”")
        note_h = None
        if lost:
            n = note(f"{date.today()} ({p['agent']}): a second {d.get('type')} record ({d.get('gramps_id')}) was merged into this one. "
                     f"It said: {'; '.join(lost)}. {p['reasoning']}")
            n["private"] = bool(k.get("private") or d.get("private"))
            note_h = api.post("notes/", n); handles["note"] = note_h
        def fill(ev):
            if event_ymd(ev) == (0, 0, 0) and event_ymd(d) != (0, 0, 0): ev["date"] = d["date"]
            if not ev.get("place") and d.get("place"): ev["place"] = d["place"]
            if not (ev.get("description") or "").strip() and d.get("description"): ev["description"] = d["description"]
            if note_h: ev["note_list"] = ev.get("note_list", []) + [note_h]
            add_tags(ev, tags)
        k = api.modify("events", k["handle"], fill)
        people, fams = sorted(set(links.get("person") or [])), sorted(set(links.get("family") or []))
        before = [("update", "people", h, api.get(f"people/{h}")) for h in people]
        before += [("update", "families", h, api.get(f"families/{h}")) for h in fams]
        before += [("update", "events", k["handle"], copy.deepcopy(k)), ("delete", "events", d["handle"], d)]
        r = api._send("POST", f"events/{k['handle']}/merge/{d['handle']}")
        if r.status_code >= 300:
            raise ApiError(f"merge {d.get('gramps_id')} into {k.get('gramps_id')}: {r.status_code} {r.text[:300]}", r.status_code)
        api.journal += before; api.writes += 1
        tree.forget("events", d["handle"]); tree.refresh("events", k["handle"])
        if api.exists(f"events/{d['handle']}"):
            raise RuntimeError(f"{d.get('gramps_id')} is still there after the merge")
        for endpoint, hs in (("people", people), ("families", fams)):
            for h in hs:
                refs = [x["ref"] for x in tree.refresh(endpoint, h).get("event_ref_list", [])]
                if d["handle"] in refs or refs.count(k["handle"]) != 1:
                    raise RuntimeError(f"the event list of {h} is not right after the merge")
        return handles, owner_label

    if act == "update_event":
        owner = tree.find_family(p["family"])[0] if p.get("family") else tree.find(p["subject"])
        owner_label = family_label(owner, tree) if p.get("family") else person_label(owner)
        ev, why = pick_event(p.get("event"), owner, tree)
        if why: raise Refused(why)
        s = p["set"]
        handles["event"] = ev.get("gramps_id")
        cur = ev if DRY else api.get(f"events/{ev['handle']}")
        changes = event_changes(cur, s, tree)
        if not changes:
            handles["changed"] = "nothing (already so)"
            return handles, owner_label
        cit_h = build_citation(p, tree, api, tags, fname, handles)
        fields = {field for field, _, _ in changes}
        place_h = place_handle(str(s["place"]).strip(), tree, api, tags, handles) if "place" in fields else None
        was = [f"{field} {old}" for field, old, _ in changes if field != "private" and old not in ("none", "no date")]
        note_h = None
        if was:                                             # what it said before stays on the record
            n = note(f"{date.today()} ({p['agent']}): this {cur.get('type')} record was corrected ({fname}). "
                     f"Before, it said: {'; '.join(was)}. {p['reasoning']}")
            n["private"] = bool(s.get("private", cur.get("private")))
            note_h = api.post("notes/", n); handles["note"] = note_h
        def fn(e):
            if "date" in fields: e["date"] = gramps_date(s["date"], s.get("date_quality") or "exact")
            if "place" in fields: e["place"] = place_h
            if "description" in fields: e["description"] = s["description"]
            if "private" in fields: e["private"] = s["private"]
            if cit_h and cit_h not in e.get("citation_list", []): e["citation_list"] = e.get("citation_list", []) + [cit_h]
            if note_h: e["note_list"] = e.get("note_list", []) + [note_h]
            add_tags(e, tags)
        api.modify("events", ev["handle"], fn)
        handles["changed"] = [f"{field}: {old} -> {new}" for field, old, new in changes]
        return handles, owner_label

    if act == "set_private":
        want, done, seen = p["private"], [], set()
        for ref in p["records"]:
            endpoint, obj, why = tree.record(ref)
            if why: raise Refused(why)
            if (endpoint, obj["handle"]) in seen:
                continue
            seen.add((endpoint, obj["handle"]))
            def fn(o):
                if bool(o.get("private")) != want:
                    o["private"] = want; done.append(o.get("gramps_id"))
            now = api.modify(endpoint, obj["handle"], fn)
            if endpoint == "notes" and now:
                tree.notes[obj["gramps_id"]] = {**obj, **now}
        handles["private" if want else "public"] = done or "nothing (already so)"
        return handles, f"{len(seen)} record(s)"

    person = tree.find(p["subject"])
    cit_h = build_citation(p, tree, api, tags, fname, handles) \
        if p.get("citation") and act in {"add_event", "add_citation", "add_media", "update_person"} else None

    if act == "update_person":
        s = p["set"]; done = []
        def fn(o):
            pn = o.setdefault("primary_name", name_obj("", "", "Birth Name"))
            was = name_text(pn)
            for key, field in (("first_name", "first_name"), ("suffix", "suffix"), ("call_name", "call"), ("nickname", "nick")):
                if key in s and (pn.get(field) or "") != s[key]:
                    pn[field] = s[key]
                    if field in ("call", "nick"): done.append(key)
            if "surname" in s:
                main = next((x for x in pn.get("surname_list") or [] if x.get("primary")), None) or \
                       ((pn.get("surname_list") or [None])[0])
                if main is None: pn["surname_list"] = [surname_obj(s["surname"])]
                elif main.get("surname") != s["surname"]: main["surname"] = s["surname"]
            if name_text(pn) != was: done.append(f"name {was!r} -> {name_text(pn)!r}")
            def add_name(ntype, first, sur, same_first=True):
                for n in o.get("alternate_names") or []:
                    theirs = " ".join(x.get("surname", "") for x in n.get("surname_list") or []).casefold()
                    if tname(n) == ntype and theirs == sur.casefold() and \
                            (not same_first or (n.get("first_name") or "").casefold() == first.casefold()):
                        return
                o.setdefault("alternate_names", []).append(name_obj(first, sur, ntype))
                done.append(f"{ntype} {' '.join(x for x in (first, sur) if x)}")
            if "married_surname" in s: add_name("Married Name", pn.get("first_name", ""), s["married_surname"], same_first=False)
            for aka in as_list(s.get("also_known_as")): add_name("Also Known As", *split_name(aka))
            if "gender" in s and o.get("gender") != GENDER[s["gender"]]:
                o["gender"] = GENDER[s["gender"]]; done.append(f"gender {s['gender']}")
            if "private" in s and bool(o.get("private")) != s["private"]:
                o["private"] = s["private"]; done.append(f"private {s['private']}")
            if "prefer_event" in s:
                h = tree.event(s["prefer_event"])["handle"]
                refs = o.get("event_ref_list") or []
                at = next((i for i, r in enumerate(refs) if r["ref"] == h), None)
                if at:                                   # not there, or already first: nothing to move
                    refs.insert(0, refs.pop(at)); done.append(f"prefer {s['prefer_event']}")
            if cit_h and cit_h not in o.get("citation_list", []): o["citation_list"] = o.get("citation_list", []) + [cit_h]
            if done or cit_h: add_tags(o, tags)
        person = api.modify("people", person["handle"], fn)
        handles["changed"] = done or "nothing (already so)"
        return handles, person_label(person)

    if act == "add_event":
        e = p["event"]
        key = date_key(e.get("date"))
        place_h = place_handle(e.get("place"), tree, api, tags, handles)
        if e["type"] in FAMILY_EVENTS:
            family_event(tree.family_of(person, tree.find(p.get("spouse")))[0], e, cit_h, place_h, tree, api, tags, handles)
        else:
            existing = tree.matching_event(person, e["type"], key)
            if existing:
                reuse_event(existing, cit_h, e, place_h, tags, api); handles["event(existing)"] = existing["gramps_id"]
                api.modify("people", person["handle"], lambda o: add_tags(o, tags))
            else:
                ev_h = new_event(e["type"], e, cit_h, place_h, tags, tree); handles["event(new)"] = ev_h
                api.add_ref("people", person["handle"], "event_ref_list",
                            {"_class": "EventRef", "ref": ev_h, "role": e.get("role", "Primary"), "private": False}, tags)

    elif act == "add_citation":
        if p.get("event_gramps_id"):
            ev = tree.events.get(p["event_gramps_id"])
            if not ev: raise Refused(f"event {p['event_gramps_id']} not found")
            api.add_ref("events", ev["handle"], "citation_list", cit_h, tags)
        else:
            api.add_ref("people", person["handle"], "citation_list", cit_h, tags)

    elif act == "add_note":
        n_h = api.post("notes/", note_from_proposal(p))
        api.add_ref("people", person["handle"], "note_list", n_h, tags); handles["note"] = n_h

    elif act == "add_media":
        api.add_ref("people", person["handle"], "media_list",
                    {"_class": "MediaRef", "ref": handles["media"], "private": False, "rect": []}, tags)

    return handles, person_label(person)


def repair_links(api, tree):
    """Remove family_list / parent_family_list entries that are not family handles."""
    fam_h = {f["handle"] for f in tree.families}
    fixed = 0
    for p in tree.people:
        bad = [h for h in p.get("family_list", []) + p.get("parent_family_list", []) if h not in fam_h]
        if not bad: continue
        print(f"{person_label(p)}: {len(bad)} bad link(s)")
        if not DRY:
            obj = api.get(f"people/{p['handle']}")
            obj["family_list"] = [h for h in obj.get("family_list", []) if h in fam_h]
            obj["parent_family_list"] = [h for h in obj.get("parent_family_list", []) if h in fam_h]
            api.put(f"people/{p['handle']}", obj)
        fixed += 1
    print(f"{fixed} people {'would be' if DRY else ''} repaired")


def apply_verdicts(api, tree):
    for f in sorted(VALID.glob("*.json")):
        v = json.loads(f.read_text(encoding="utf-8"))
        obj = api.get(f"citations/{v['gramps_handle']}") if not DRY else {"tag_list": []}
        ok = v["verdict"] == "supports" and not v.get("flags")
        new_tag = tree.tag("ai-validated" if ok else "needs-human")
        drop = {tree.tags.get("agent-proposed"), tree.tags.get("unreviewed")}
        obj["tag_list"] = [t for t in obj.get("tag_list", []) if t not in drop] + [new_tag, tree.tag("agent-validator")]
        if ok and v.get("confidence_suggested") is not None:
            obj["confidence"] = int(v["confidence_suggested"])
        n_h = api.post("notes/", note(f"{date.today()} (validator): {v['verdict']}; match points: {v.get('match_points')}; "
                                      f"flags: {v.get('flags')}. {v.get('note', '')}"))
        obj["note_list"] = obj.get("note_list", []) + [n_h]
        api.put(f"citations/{v['gramps_handle']}", obj)
        print(f"{f.name}: {'ai-validated' if ok else 'needs-human'}")
        if not DRY: shutil.move(str(f), APPLIED / f.name)


# ---------- the queue ----------
def shape_problem(p):
    """Why a file cannot even be considered: a field of the wrong kind (a list where an object belongs, say) or a
    date that cannot be read. None if it is in order. Checked when the file is read, before anything looks inside."""
    def wrong():
        for k in ("person", "relations", "event", "citation", "set"):
            if p.get(k) is not None and not isinstance(p[k], (dict, str) if (k, p.get("action")) == ("event", "update_event") else dict):
                return f"'{k}' must be an object"
        if p.get("family") is not None and not isinstance(p["family"], (dict, str)):
            return "'family' must be an object or a Gramps ID"
        if p.get("add_children") is not None and not isinstance(p["add_children"], list):
            return "'add_children' must be a list"
        for k in ("birth", "death"):
            if isinstance(p.get("person"), dict) and p["person"].get(k) is not None and not isinstance(p["person"][k], dict):
                return f"'person.{k}' must be an object"
        if isinstance(p.get("family"), dict):
            if p["family"].get("marriage") is not None and not isinstance(p["family"]["marriage"], dict):
                return "'family.marriage' must be an object"
            if not isinstance(p["family"].get("children_ancestry_ids") or [], list):
                return "'family.children_ancestry_ids' must be a list"
        return None
    if wrong():
        return f"malformed proposal: {wrong()}"
    dates = [d for k in ("person", "event", "citation", "set", "family") for d in date_problems(p.get(k), k)]
    return "; ".join(dates) or None


def load_queue(say=print):
    """Proposals waiting in held/ and inbox/, as (path, proposal), in the order they are worked."""
    for f in sorted(INBOX.glob("*.json")):              # a corrected file replaces the held one of the same name
        if (HELD / f.name).exists():
            say(f"NOTE {f.name}: the inbox has a newer version; it replaces the one in held/")
            if not DRY: (HELD / f.name).unlink()
    loaded = []
    for d in (HELD, INBOX):
        for f in sorted(d.glob("*.json")):
            if DRY and d is HELD and (INBOX / f.name).exists():
                continue
            try:
                p = json.loads(f.read_text(encoding="utf-8-sig"))      # -sig: a file saved on Windows may begin with a BOM
                why = None if isinstance(p, dict) else "not valid JSON (the file must hold one JSON object)"
                why = why or shape_problem(p)
            except Exception as ex:
                why = f"not valid JSON ({ex})"
            if why:
                say(f"REJECT {f.name}: {why}")
                if not DRY:
                    shutil.move(str(f), REJECTED / f.name)
                    (REJECTED / (f.stem + ".reason.txt")).write_text(why)
            else:
                loaded.append((f, p))
    loaded.sort(key=lambda fp: (FIRST.get(fp[1].get("action"), 9), fp[0].name))
    persons = [fp for fp in loaded if fp[1].get("action") == "add_person"]
    return loaded, persons


def order_people(persons, tree):
    """add_person proposals ordered so that anyone whose parents are new in this batch comes after those parents,
    and anyone who names a new spouse comes after that spouse (unless the two name each other: then the second
    one added links them). Also returns {Ancestry ID: the spouse's Ancestry ID that person names, or ""}."""
    me = lambda fp: str((fp[1].get("person") or {}).get("ancestry_id", ""))
    batch_ids = {me(fp): str((fp[1].get("relations") or {}).get("spouse_ancestry_id") or "") for fp in persons}
    ordered, pending = [], list(persons)
    while pending:
        progress = False
        for fp in list(pending):
            rel = fp[1].get("relations") or {}
            first = [str(rel.get(k, "")) for k in ("father_ancestry_id", "mother_ancestry_id") if rel.get(k)]
            spouse = batch_ids[me(fp)]
            if spouse and batch_ids.get(spouse) != me(fp):
                first.append(spouse)
            done = {me(x) for x in ordered}
            if all(tree.find(pid) or pid not in batch_ids or pid in done for pid in first):
                ordered.append(fp); pending.remove(fp); progress = True
        if not progress:
            ordered += pending; break   # circular references: let validation/apply report them
    return ordered, batch_ids


def mark_pending(persons, tree, approved):
    """Before anything is applied: which new people will have to wait for approval, and so who depends on them."""
    held = set()
    for f, p in persons:
        try:
            need = needs_approval(p, tree)
        except Exception:
            need = None                     # the main pass rejects it as malformed
        if need and not is_approved(f, approved):
            held.add(str((p.get("person") or {}).get("ancestry_id", "")))
    grew = True
    while grew:
        grew = False
        for f, p in persons:
            me, rel = str((p.get("person") or {}).get("ancestry_id", "")), p.get("relations") or {}
            needs = [str(rel.get(k) or "") for k in ("father_ancestry_id", "mother_ancestry_id", "spouse_ancestry_id")]
            if me not in held and any(n and n in held and not tree.find(n) for n in needs):
                held.add(me); grew = True
    tree.pending = held - {""}


def move_image(p, dest):
    img = (p.get("citation") or {}).get("image")
    for d in (INBOX, HELD):
        if img and d != dest and (d / img).exists():
            shutil.move(str(d / img), dest / img)


def write_undo(f, p, api):
    """What a change proposal altered: each object as it was before and as it is now."""
    entries = []
    for kind, endpoint, handle, before in api.journal:
        after = None
        if kind != "delete":
            r = api._send("GET", f"{endpoint}/{handle}")
            after = r.json() if r.status_code == 200 else None
        entries.append({"kind": kind, "endpoint": endpoint, "handle": handle, "before": before, "after": after})
    UNDO.mkdir(parents=True, exist_ok=True)
    (UNDO / (f.stem + ".undo.json")).write_text(json.dumps(
        {"proposal": f.name, "action": p.get("action"), "applied": datetime.now().isoformat(timespec="seconds"),
         "journal": entries}, ensure_ascii=False, indent=1), encoding="utf-8")


def records_changed(p, tree):
    """Gramps IDs of the records a change proposal alters, so that proposals about the same record can be read together."""
    act, out = p.get("action"), []
    if act == "update_person":
        out.append(tree.find(p.get("subject")))
    elif act == "update_family":
        s = p.get("set") or {}
        out += [tree.find_family(p.get("family"))[0], tree.find(s.get("father")), tree.find(s.get("mother"))]
        out += [tree.find(c) for c in p.get("add_children") or []]
    elif act == "merge_family":
        out += [tree.find_family(p.get("keep"))[0], tree.find_family(p.get("drop"))[0]]
    elif act in ("merge_event", "update_event"):
        out.append(tree.find_family(p["family"])[0] if p.get("family") else tree.find(p.get("subject")))
    elif act == "set_private":
        out += [tree.record(ref)[1] for ref in p.get("records") or []]
    return {x["gramps_id"] for x in out if x}


def waiting_report(held, tree):
    """The plain-words list of what is waiting, as --held prints it and held/_WAITING.txt keeps it."""
    about = []
    for h in held:
        try:
            about.append(records_changed(h["proposal"], tree))
        except Exception:
            about.append(set())
    need = [h for h in held if not h["waits"]]
    out = [f"{len(held)} proposal(s) waiting" + (f", {len(need)} for Chris's approval" if len(need) != len(held) else " for Chris's approval"),
           "  see them:  python3 apply_proposals.py --held",
           "  approve:   python3 apply_proposals.py --approve all      (or --approve NAME ...)",
           "  refuse:    python3 apply_proposals.py --reject NAME --reason \"why\"", ""]
    for i, h in enumerate(held, 1):
        out.append(f"{i}. {h['file'].stem}  —  {h['why']}")
        try:
            out += ["   " + ln for ln in describe(h["proposal"], tree).splitlines()]
        except Exception as ex:
            out.append(f"   (could not be put into words: {type(ex).__name__}: {ex}) — read the file itself")
        same = [str(j) for j, ids in enumerate(about, 1) if j != i and ids & about[i - 1]]
        if same:
            out.append(f"   read together with no. {', '.join(same)}: they change the same record"
                       f" ({', '.join(sorted(set().union(*(about[int(j) - 1] for j in same)) & about[i - 1]))})")
        out.append("")
    return "\n".join(out)


def run():
    """One pass over the queue. Returns a summary dict (for --held and the tests)."""
    for d in (INBOX, APPLIED, REJECTED, VALID, HELD, UNDO): d.mkdir(parents=True, exist_ok=True)
    api = Api(); tree = Tree(api)
    print(f"tree: {len(tree.people)} people, {len(tree.by_anc)} with Ancestry ID, {len(tree.sources)} sources, {len(tree.places)} place names")
    loaded, persons = load_queue()
    approved = approvals()
    ordered, batch_ids = order_people(persons, tree)
    loaded = ordered + [fp for fp in loaded if fp[1].get("action") != "add_person"]
    batch_links = {}
    for f, p in loaded:
        for anc in links_named(p):
            batch_links.setdefault(anc, set()).add(f.name)
    mark_pending(persons, tree, approved)
    for f, p in persons:
        pe = p.get("person") or {}
        year = (date_key((pe.get("birth") or {}).get("date")) or (0,))[0]
        tree.batch_names[str(pe.get("ancestry_id", ""))] = " ".join(
            str(x) for x in (pe.get("first_name"), pe.get("surname") or pe.get("married_surname"), pe.get("suffix")) if x) + \
            (f", born {year}" if year else "")
    print(f"{sum(1 for f, _ in loaded if f.parent == INBOX)} proposal(s) in inbox" +
          (f", {sum(1 for f, _ in loaded if f.parent == HELD)} held" if any(f.parent == HELD for f, _ in loaded) else ""))
    applied = rejected = 0
    held, stopped = [], False
    for f, p in loaded:
        me = str((p.get("person") or {}).get("ancestry_id", "")) if p.get("action") == "add_person" else ""
        others = {anc for anc, names in batch_links.items() if names - {f.name}}
        need = None
        try:
            errs, waits = validate(p, tree, batch_ids, others)
            if not (errs or waits):
                need = needs_approval(p, tree)
        except Exception as ex:                         # a field of the wrong kind, say a list where an object belongs
            errs, waits = [f"malformed proposal ({type(ex).__name__}: {ex})"], []
        if errs:
            print(f"REJECT {f.name}: " + "; ".join(errs)); rejected += 1
            tree.pending.discard(me)
            if not DRY:
                shutil.move(str(f), REJECTED / f.name)
                (REJECTED / (f.stem + ".reason.txt")).write_text("\n".join(errs))
            continue
        why = None
        if waits:
            why = "waits for " + ", ".join(tree.batch_names.get(a) or f"Ancestry {a}" for a in sorted(set(waits))) + \
                  ", who is not approved yet"
        elif need and not is_approved(f, approved):
            why = need
        if why:
            if me: tree.pending.add(me)
            held.append({"file": f, "proposal": p, "why": why, "waits": bool(waits)})
            if f.parent != HELD:
                print(f"{'WOULD HOLD' if DRY else 'HOLD'} {f.name}: {why}")
                if not DRY:
                    move_image(p, HELD)
                    shutil.move(str(f), HELD / f.name)
                    held[-1]["file"] = HELD / f.name
            continue
        tree.pending.discard(me)
        api.journal = []
        try:
            handles, who = apply(p, tree, api, f.name)
        except Exception as ex:
            # Final: trying again cannot help. The tree says no (Refused), the API says the request is wrong (4xx),
            # or the file holds something the applier cannot work with (a Python error that is not about the
            # connection). Anything else, a busy or broken server above all, stops the run for the next one to retry.
            status = getattr(ex, "status", None) or 0
            final = isinstance(ex, Refused) or (400 <= status < 500 and status not in (401, 403, 408, 429)) or \
                (isinstance(ex, (ValueError, KeyError, TypeError, AttributeError, IndexError))
                 and not isinstance(ex, requests.RequestException))
            print(f"{'FAILED' if final else 'STOP at'} {f.name}: {ex}")
            clean = True
            if not DRY and api.journal:
                print(f"  rolling back {len(api.journal)} step(s) of this proposal")
                clean = api.rollback()
            if not (final and clean):               # a busy or broken server, or a rollback that did not finish: stop here
                stopped = True
                break
            print(f"REJECT {f.name}: could not be applied; the tree is as it was"); rejected += 1
            if not DRY:
                shutil.move(str(f), REJECTED / f.name)
                (REJECTED / (f.stem + ".reason.txt")).write_text(
                    f"could not be applied: {ex if isinstance(ex, (Refused, ApiError)) else f'{type(ex).__name__}: {ex} (tell Claude)'}")
            was = tree                                # what we remembered may include the half-made proposal: read it all again
            tree = Tree(api)
            tree.pending, tree.deferred, tree.batch_names = was.pending, was.deferred, was.batch_names
            continue
        print(f"{'WOULD APPLY' if DRY else 'APPLIED'} {f.name}: {p['action']} for {who} -> {handles}"); applied += 1
        if not DRY:
            if p["action"] in CHANGES:
                write_undo(f, p, api)
            move_image(p, APPLIED)
            shutil.move(str(f), APPLIED / f.name)
            if ONE: print("stopped after one; check it in Gramps Web"); stopped = True; break
    for anc, people in tree.deferred.items():
        for person in people:
            print(f"WARNING {person_label(person)} was added, but the spouse named for them (Ancestry {anc}) was not: "
                  "link the two with an add_family proposal once that person is in")
    if not stopped:
        print(f"{applied} applied, {rejected} rejected, {api.writes} API write(s) {'planned' if DRY else 'made'}")
    report = waiting_report(held, tree) if held else ""
    if held and not stopped:
        print(f"{len(held)} waiting in held/ for Chris  (python3 apply_proposals.py --held)")
    if not DRY and not stopped:
        note_file = HELD / "_WAITING.txt"
        if report and (not note_file.exists() or note_file.read_text(encoding="utf-8") != report):
            note_file.write_text(report, encoding="utf-8")
        elif not report and note_file.exists():
            note_file.unlink()
    return {"applied": applied, "rejected": rejected, "held": held, "report": report, "stopped": stopped, "tree": tree}


def show_held():
    global DRY
    was, DRY = DRY, True
    try:
        result = run_quietly()
    finally:
        DRY = was
    held = result["held"]
    if not held:
        print("Nothing is waiting for approval.")
        return
    print(result["report"])
    seen_file().parent.mkdir(parents=True, exist_ok=True)
    seen_file().write_text("\n".join(file_hash(h["file"]) for h in held if h["file"].exists()) + "\n", encoding="utf-8")


def run_quietly():
    """A dry pass that prints nothing, used to list what is held."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        return run()


def queue_files():
    return sorted(HELD.glob("*.json")) + [f for f in sorted(INBOX.glob("*.json")) if not (HELD / f.name).exists()]


def pick(names):
    """Waiting files that the given names mean: a file name, with or without .json, or a unique beginning of one."""
    files = queue_files()
    out = []
    for n in names:
        stem = n[:-5] if n.endswith(".json") else n
        hits = [f for f in files if f.stem == stem] or [f for f in files if f.stem.startswith(stem)]
        if len(hits) != 1:
            sys.exit(f"{n}: {'no' if not hits else 'more than one'} waiting proposal by that name (see --held)")
        out.append(hits[0])
    return out


def approve(names):
    if names == ["all"]:
        seen = set(seen_file().read_text(encoding="utf-8").split()) if seen_file().exists() else set()
        files = [f for f in queue_files() if file_hash(f) in seen]
        new = [f for f in sorted(HELD.glob("*.json")) if file_hash(f) not in seen]
        if new:
            print(f"{len(new)} held proposal(s) arrived or changed since you last ran --held; they are NOT approved:")
            for f in new: print(f"   {f.stem}")
            print("Run --held again to see them.")
    else:
        files = pick(names)
    if not files:
        print("Nothing approved." + ("" if names != ["all"] else " (Run --held first: 'all' means everything that list showed.)"))
        return False
    APPROVALS.parent.mkdir(parents=True, exist_ok=True)
    with open(APPROVALS, "a", encoding="utf-8") as out:
        for f in files:
            out.write(f"{file_hash(f)}\t{f.name}\t{datetime.now():%Y-%m-%d %H:%M}\n")
            print(f"approved {f.stem}")
    return True


def refuse(names, reason):
    for d in (REJECTED,): d.mkdir(parents=True, exist_ok=True)
    for f in pick(names):
        revoke(f.name)
        shutil.move(str(f), REJECTED / f.name)
        (REJECTED / (f.stem + ".reason.txt")).write_text(f"refused by Chris: {reason or 'no reason given'}")
        print(f"refused {f.stem}")


def undo(name, force=False):
    files = sorted(UNDO.glob("*.undo.json"))
    stem = name[:-5] if name.endswith(".json") else name
    hits = [f for f in files if f.name == stem + ".undo.json"] or [f for f in files if f.name.startswith(stem)]
    if len(hits) != 1:
        sys.exit(f"{name}: {'no' if not hits else 'more than one'} undo record by that name in {UNDO}")
    rec = json.loads(hits[0].read_text(encoding="utf-8"))
    api = Api()
    # A source, repository, place or tag that the proposal made is left where it is: later proposals look these up
    # by name and use them again, so taking one away would break records that have nothing to do with this undo.
    shared = [e for e in rec["journal"] if e["kind"] == "create" and e["endpoint"] in SHARED]
    rec["journal"] = [e for e in rec["journal"] if e not in shared]
    moved = []
    for e in rec["journal"]:
        r = api._send("GET", f"{e['endpoint']}/{e['handle']}")
        now = r.json() if r.status_code == 200 else None
        if e["kind"] == "delete":
            if now is not None: moved.append(f"{e['endpoint']} {e['before'].get('gramps_id')} exists again")
        elif now is None:
            moved.append(f"{e['endpoint']} {e['handle']} is gone")
        elif e.get("after") is not None and {k: v for k, v in now.items() if k != "change"} != \
                {k: v for k, v in e["after"].items() if k != "change"}:
            moved.append(f"{e['endpoint']} {now.get('gramps_id')} has been changed since")
    if moved and not force:
        print(f"NOT undone: the tree has moved on since {rec['proposal']} was applied:")
        for m in dict.fromkeys(moved): print("   " + m)
        print("Undoing now would overwrite those later changes. Ask Claude; --force overrides.")
        return False
    journal = [(e["kind"], e["endpoint"], e["handle"], e["before"]) for e in rec["journal"]]
    ok = restore(api, journal, say=lambda s: print("  " + s))
    if ok and shared:
        kinds = sorted({e["endpoint"] for e in shared})
        print(f"  left in place (other records may use them by now): {len(shared)} of {', '.join(kinds)}")
    if ok:
        hits[0].rename(hits[0].with_name(hits[0].name.replace(".undo.json", ".undone.json")))
        revoke(rec["proposal"])
        applied = APPLIED / rec["proposal"]
        if applied.exists():
            shutil.move(str(applied), REJECTED / rec["proposal"])
            (REJECTED / (Path(rec["proposal"]).stem + ".reason.txt")).write_text("undone by Chris after it was applied")
    print(f"{rec['proposal']}: {'undone' if ok else 'NOT fully undone — see the lines above, and tell Claude'}")
    return ok


def take_lock(wait=90):
    """Only one applier at a time: two writers make SQLite refuse the second."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "w")
    until = time.time() + wait
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.time() > until:
                return None
            time.sleep(2)


def main(argv=None):
    global DRY, ONE
    argv = list(sys.argv[1:] if argv is None else argv)
    known = {"--dry-run", "--one", "--resubmit", "--verdicts", "--repair-links", "--held", "--approve", "--reject",
             "--reason", "--undo", "--force", "--version"}
    flags = [a for a in argv if a.startswith("--")]
    if set(flags) - known:
        sys.exit(f"unknown option {sorted(set(flags) - known)[0]}; see the top of this file")
    if "--version" in argv:
        print(VERSION); return 0
    DRY, ONE = "--dry-run" in argv, "--one" in argv

    def after(flag):
        i = argv.index(flag) + 1
        out = []
        while i < len(argv) and not argv[i].startswith("--"):
            out.append(argv[i]); i += 1
        return out

    for d in (INBOX, APPLIED, REJECTED, VALID, HELD, UNDO): d.mkdir(parents=True, exist_ok=True)
    if "--held" in argv:
        show_held(); return 0
    lock = None if DRY else take_lock()
    if not DRY and lock is None:
        print("another applier run is still going; nothing done this time")
        return 0
    if "--reject" in argv:
        refuse(after("--reject"), " ".join(after("--reason")) if "--reason" in argv else "")
        return 0
    if "--undo" in argv:
        names = after("--undo")
        if len(names) != 1: sys.exit("--undo takes one name")
        return 0 if undo(names[0], "--force" in argv) else 1
    if "--approve" in argv:
        names = after("--approve")
        if not names: sys.exit("--approve all, or --approve NAME [NAME ...]")
        if not approve(names): return 0
    if "--resubmit" in argv and not DRY:
        n = 0
        for f in list(REJECTED.glob("*.json")):
            shutil.move(str(f), INBOX / f.name); n += 1
            r = REJECTED / (f.stem + ".reason.txt")
            if r.exists(): r.unlink()
        print(f"resubmitted {n} file(s) from rejected/")
    if "--repair-links" in argv or "--verdicts" in argv:
        api = Api(); tree = Tree(api)
        print(f"tree: {len(tree.people)} people, {len(tree.by_anc)} with Ancestry ID, {len(tree.sources)} sources, {len(tree.places)} place names")
        if "--repair-links" in argv: repair_links(api, tree)
        else: apply_verdicts(api, tree)
        return 0
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
