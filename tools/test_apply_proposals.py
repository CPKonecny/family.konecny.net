#!/usr/bin/env python3
"""Tests for tools/apply_proposals.py.

    python3 tools/test_apply_proposals.py              # against a built-in imitation of the Gramps Web API
    python3 tools/test_apply_proposals.py NAME ...     # only the tests whose names contain one of these words

The same tests can be run against a real Gramps Web API. Use a scratch tree, never the family's: the
tests add and delete people.

    APPLIER_TEST_API=http://127.0.0.1:5056/api APPLIER_TEST_USER=editor APPLIER_TEST_PASS=... \\
        python3 tools/test_apply_proposals.py

The imitation follows gramps-web-api 3.17 (the version on the server) in what the applier depends on:
an empty parent is "" on the way out; adding, changing or deleting a family rewrites the family lists
of its people; changing a person recomputes which birth and death are the main ones; the merge calls
behave as Gramps's own merges do. Where the imitation and the real thing could differ, the real run is
the judge; both were green on 16 Nov 2026 (applier 0.3.1).
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import random
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
LIVE = os.environ.get("APPLIER_TEST_API")
USER = os.environ.get("APPLIER_TEST_USER", "tester")
PASSWORD = os.environ.get("APPLIER_TEST_PASS", "tester")
BASE = LIVE or "http://mock.invalid/api"

spec = importlib.util.spec_from_file_location("apply_proposals", os.environ.get("APPLIER_UNDER_TEST", HERE / "apply_proposals.py"))
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001"
                    "e221bc330000000049454e44ae426082")


# ---------------------------------------------------------------- the imitation API
class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body if body is not None else {}
        self.text = body if isinstance(body, str) else json.dumps(self._body)

    def json(self):
        return self._body


def strip_class(x):
    if isinstance(x, dict):
        return {k: strip_class(v) for k, v in x.items() if k != "_class"}
    if isinstance(x, list):
        return [strip_class(v) for v in x]
    return x


class MockGramps:
    PREFIX = {"people": "I", "families": "F", "events": "E", "notes": "N", "citations": "C", "sources": "S",
              "places": "P", "repositories": "R", "media": "O", "tags": "T"}
    CLASS = {"people": "Person", "families": "Family", "events": "Event", "notes": "Note", "citations": "Citation",
             "sources": "Source", "places": "Place", "repositories": "Repository", "media": "Media", "tags": "Tag"}
    DATE = {"calendar": 0, "dateval": [0, 0, 0, False], "format": None, "modifier": 0, "newyear": 0, "quality": 0,
            "sortval": 0, "text": "", "year": 0}
    DEFAULTS = {
        "people": {"gender": 2, "private": False, "tag_list": [], "alternate_names": [], "attribute_list": [],
                   "citation_list": [], "event_ref_list": [], "family_list": [], "parent_family_list": [],
                   "note_list": [], "media_list": [], "birth_ref_index": -1, "death_ref_index": -1},
        "families": {"father_handle": None, "mother_handle": None, "type": "Unknown", "child_ref_list": [],
                     "event_ref_list": [], "citation_list": [], "note_list": [], "media_list": [], "attribute_list": [],
                     "tag_list": [], "private": False},
        "events": {"type": "Unknown", "description": "", "place": "", "citation_list": [], "note_list": [],
                   "media_list": [], "attribute_list": [], "tag_list": [], "private": False},
    }

    def __init__(self):
        self.db = {ep: {} for ep in self.PREFIX}
        self.count = {ep: 0 for ep in self.PREFIX}
        self.calls = []

    # ----- storage
    def canon(self, ep, obj):
        o = strip_class(copy.deepcopy(obj))
        for k, v in self.DEFAULTS.get(ep, {}).items():
            o.setdefault(k, copy.deepcopy(v))
        if ep == "families":
            for k in ("father_handle", "mother_handle"):
                o[k] = o.get(k) or None
        if ep == "events":
            o["date"] = {**self.DATE, **(o.get("date") or {})}
        if ep == "people":
            pn = o.setdefault("primary_name", {})
            for k, v in (("first_name", ""), ("surname_list", []), ("suffix", ""), ("call", ""), ("nick", ""), ("type", "Birth Name"), ("private", False)):
                pn.setdefault(k, v)
        return o

    def out(self, ep, o, backlinks=False):
        o = copy.deepcopy(o)
        if ep == "families":
            for k in ("father_handle", "mother_handle"):
                o[k] = o[k] or ""
        if backlinks:
            links = {}
            for kind, e2 in (("person", "people"), ("family", "families"), ("event", "events"), ("citation", "citations"),
                             ("source", "sources"), ("place", "places"), ("media", "media"), ("repository", "repositories")):
                hs = [x["handle"] for x in self.db[e2].values()
                      if any(r.get("ref") == o["handle"] for r in x.get("event_ref_list", [])) or o["handle"] in x.get("note_list", [])]
                if hs:
                    links[kind] = hs
            o["backlinks"] = links
        return o

    def birth_death(self, p):
        b = d = -1
        for i, r in enumerate(p.get("event_ref_list", [])):
            ev = self.db["events"].get(r["ref"])
            if ev and r.get("role", "Primary") == "Primary":
                if ev["type"] == "Birth" and b == -1: b = i
                elif ev["type"] == "Death" and d == -1: d = i
        p["birth_ref_index"], p["death_ref_index"] = b, d

    def txn(self, kind, ep, old, new):
        return {"type": kind, "_class": self.CLASS[ep], "handle": (new or old)["handle"], "old": old, "new": new}

    # ----- the calls
    def request(self, method, url, headers=None, params=None, json=None, data=None, **kw):
        assert url.startswith(BASE + "/"), url
        path = url[len(BASE) + 1:]
        self.calls.append((method, path))
        if not (headers or {}).get("Authorization"):
            return Resp(401, {"message": "no token"})
        parts = [x for x in path.split("/") if x]
        ep = parts[0]
        if ep not in self.db:
            return Resp(404)
        store = self.db[ep]
        params = params or {}
        if len(parts) == 4 and parts[2] == "merge" and method == "POST":
            return self.merge(ep, parts[1], parts[3], json or {})
        if len(parts) == 1 and method == "GET":
            if "gramps_id" in params:
                hits = [o for o in store.values() if o.get("gramps_id") == params["gramps_id"]]
                wanted = str(params.get("backlinks", "")).lower() in ("1", "true")
                return Resp(200, [self.out(ep, hits[0], backlinks=wanted)]) if hits else Resp(404)
            objs = [self.out(ep, o) for o in store.values()]
            page, size = int(params.get("page", 0)), int(params.get("pagesize", 20))
            return Resp(200, objs[(page - 1) * size: page * size] if page else objs)
        if len(parts) == 1 and method == "POST":
            if data is not None:
                json = {"desc": "", "path": "upload", "mime": (headers or {}).get("Content-Type"), "attribute_list": [], "tag_list": []}
            o = self.canon(ep, json)
            o["handle"] = o.get("handle") or ap.new_handle()
            if o["handle"] in store:
                return Resp(400, {"error": {"code": 400, "message": "Error while adding object"}})
            if not o.get("gramps_id"):
                while True:
                    gid = f"{self.PREFIX[ep]}{self.count[ep]:04d}"
                    self.count[ep] += 1
                    if not any(x.get("gramps_id") == gid for x in store.values()):
                        break
                o["gramps_id"] = gid
            elif any(x.get("gramps_id") == o["gramps_id"] for x in store.values()):
                return Resp(400, {"error": {"code": 400, "message": "Error while adding object"}})
            out = []
            if ep == "families":
                for h in (o["father_handle"], o["mother_handle"]):
                    if h:
                        per = self.db["people"][h]
                        if o["handle"] not in per["family_list"]: per["family_list"].append(o["handle"])
                        out.append(self.txn("update", "people", None, self.out("people", per)))
                for c in o["child_ref_list"]:
                    per = self.db["people"][c["ref"]]
                    if o["handle"] not in per["parent_family_list"]: per["parent_family_list"].append(o["handle"])
            store[o["handle"]] = o
            out.append(self.txn("add", ep, None, self.out(ep, o)))
            return Resp(201, out)
        if len(parts) != 2:
            return Resp(404)
        handle = parts[1]
        if handle not in store:
            return Resp(404, {"code": 404, "status": "Not Found"})
        if method == "GET":
            return Resp(200, self.out(ep, store[handle], backlinks=str(params.get("backlinks", "")).lower() in ("1", "true")))
        if method == "PUT":
            old, new = store[handle], self.canon(ep, json)
            new["handle"], new["gramps_id"] = handle, new.get("gramps_id") or old["gramps_id"]
            if ep == "families":
                for side in ("father_handle", "mother_handle"):
                    if old[side] != new[side]:
                        if old[side]:
                            lst = self.db["people"][old[side]]["family_list"]
                            if handle not in lst:
                                return Resp(400, {"error": {"code": 400, "message": "Error while updating object"}})
                            lst.remove(handle)
                        if new[side]:
                            self.db["people"][new[side]]["family_list"].append(handle)
                was, now = {c["ref"] for c in old["child_ref_list"]}, {c["ref"] for c in new["child_ref_list"]}
                for h in was - now:
                    lst = self.db["people"][h]["parent_family_list"]
                    if handle in lst: lst.remove(handle)
                for h in now - was:
                    lst = self.db["people"][h]["parent_family_list"]
                    if handle not in lst: lst.append(handle)
            if ep == "people":
                self.birth_death(new)
            store[handle] = new
            return Resp(200, [self.txn("update", ep, self.out(ep, old), self.out(ep, new))])
        if method == "DELETE":
            old = store.pop(handle)
            if ep == "families":
                for h in (old["father_handle"], old["mother_handle"]):
                    if h and handle in self.db["people"][h]["family_list"]:
                        self.db["people"][h]["family_list"].remove(handle)
                for c in old["child_ref_list"]:
                    lst = self.db["people"][c["ref"]]["parent_family_list"]
                    if handle in lst: lst.remove(handle)
            if ep == "events":
                for e2 in ("people", "families"):
                    for x in self.db[e2].values():
                        x["event_ref_list"] = [r for r in x.get("event_ref_list", []) if r["ref"] != handle]
                        if e2 == "people": self.birth_death(x)
            if ep == "notes":
                for e2 in ("people", "families", "events", "citations"):
                    for x in self.db[e2].values():
                        x["note_list"] = [h for h in x.get("note_list", []) if h != handle]
            return Resp(200, [self.txn("delete", ep, self.out(ep, old), None)])
        return Resp(405)

    def merge(self, ep, keep_h, drop_h, body):
        store = self.db[ep]
        if keep_h not in store or drop_h not in store:
            return Resp(404, {"error": {"code": 404, "message": "handle not found"}})
        keep, drop = store[keep_h], store[drop_h]

        def union(field, key=lambda x: json.dumps(x, sort_keys=True)):
            have = {key(x) for x in keep.get(field, [])}
            keep.setdefault(field, []).extend(x for x in drop.get(field, []) if key(x) not in have)

        if ep == "families":
            for side in ("father", "mother"):
                k, d, given = keep[f"{side}_handle"], drop[f"{side}_handle"], body.get(f"phoenix_{side}_handle")
                if k and d and k != d:
                    # the real API would merge the two people here; the applier must never ask for that
                    raise AssertionError(f"merge of families with different {side}s was sent to the API")
                if d and not k:
                    if given != d:
                        return Resp(409, {"error": {"code": 409, "message": "When merging people where one person doesn't exist, "
                                                    "that \"person\" must be the person that will be deleted from the database."}})
                    keep[f"{side}_handle"] = d
                    self.db["people"][d]["family_list"].append(keep_h)
            union("child_ref_list", key=lambda c: c["ref"])
            union("event_ref_list", key=lambda r: r["ref"])
            if keep["type"] == "Unknown":
                keep["type"] = drop["type"]
            for c in drop["child_ref_list"]:
                lst = self.db["people"][c["ref"]]["parent_family_list"]
                lst[:] = [keep_h if h == drop_h else h for h in lst]
                lst[:] = [h for i, h in enumerate(lst) if h not in lst[:i]]
            for h in (drop["father_handle"], drop["mother_handle"]):
                if h:
                    lst = self.db["people"][h]["family_list"]
                    lst[:] = [x for x in lst if x != drop_h]
        elif ep == "events":
            for e2 in ("people", "families"):
                for x in self.db[e2].values():
                    refs = x.get("event_ref_list", [])
                    if any(r["ref"] == drop_h for r in refs):
                        has_keep = any(r["ref"] == keep_h for r in refs)
                        x["event_ref_list"] = [dict(r, ref=keep_h) if r["ref"] == drop_h else r for r in refs
                                               if not (r["ref"] == drop_h and has_keep)]
                        if e2 == "people": self.birth_death(x)
        else:
            return Resp(404)
        for field in ("citation_list", "note_list", "tag_list"):
            union(field, key=lambda x: x)
        union("media_list", key=lambda m: m["ref"])
        union("attribute_list")
        keep["private"] = bool(keep.get("private") or drop.get("private"))
        del store[drop_h]
        if ep == "events":
            for x in self.db["people"].values():
                self.birth_death(x)
        return Resp(200, {})


# ---------------------------------------------------------------- plumbing shared by both ways of running
REAL_REQUEST, REAL_POST = requests.request, requests.post
MOCK = None
FAULTS = []          # (method, text the path must contain, status, body, how many requests to let through first)


def request(method, url, **kw):
    path = url[len(BASE) + 1:]
    for i, (m, part, status, body, skip) in enumerate(FAULTS):
        if m == method and part in path:
            if skip:
                FAULTS[i] = (m, part, status, body, skip - 1)
                continue
            FAULTS.pop(i)
            return Resp(status, body)
    if LIVE:
        return REAL_REQUEST(method, url, **kw)
    return MOCK.request(method, url, **kw)


def post(url, **kw):
    if url.endswith("/token/"):
        if LIVE:
            for _ in range(8):
                r = REAL_POST(url, **kw)
                if r.status_code != 429:
                    return r
                time.sleep(1.1)
            return r
        return Resp(200, {"access_token": "mock"})
    return request("POST", url, **kw)


requests.request, requests.post = request, post


class World:
    """Makes the people, families and events a test starts from, through the API itself."""

    def __init__(self):
        r = requests.post(f"{BASE}/token/", json={"username": USER, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.h = {"Authorization": f"Bearer {r.json()['access_token']}"}

    def send(self, method, path, **kw):
        r = requests.request(method, f"{BASE}/{path}", headers=self.h, **kw)
        assert r.status_code < 300, f"{method} {path}: {r.status_code} {r.text[:300]}"
        return r.json()

    def get(self, endpoint, handle, **params):
        r = requests.request("GET", f"{BASE}/{endpoint}/{handle}", headers=self.h, params=params)
        return r.json() if r.status_code == 200 else None

    def make(self, endpoint, obj):
        h = ap.new_handle()
        self.send("POST", f"{endpoint}/", json={**obj, "handle": h})
        return h

    def event(self, etype, ymd=None, place=None, modifier=0, **kw):
        e = {"_class": "Event", "type": etype, "description": "", "private": False, "citation_list": [], "tag_list": [], "note_list": []}
        if ymd:
            y, m, d = (list(ymd) + [0, 0])[:3]
            e["date"] = {"_class": "Date", "calendar": 0, "modifier": modifier, "quality": 0, "dateval": [d, m, y, False],
                         "text": "", "sortval": 0, "newyear": 0}
        if place:
            e["place"] = place
        e.update(kw)
        return self.make("events", e)

    def place(self, name):
        return self.make("places", {"_class": "Place", "title": name, "name": {"_class": "PlaceName", "value": name, "lang": ""},
                                    "place_type": "Unknown", "private": False, "tag_list": []})

    def person(self, first, surname, gender="M", born=1850, died=None, private=False, events=()):
        """Returns (handle, Ancestry ID). born/died: a year, a (y, m, d), or None for no such event."""
        anc = "9" + "".join(random.choice("0123456789") for _ in range(11))
        refs = []
        for etype, when in (("Birth", born), ("Death", died)):
            if when:
                refs.append({"_class": "EventRef", "ref": self.event(etype, (when,) if isinstance(when, int) else when),
                             "role": "Primary", "private": False})
        refs += [{"_class": "EventRef", "ref": h, "role": "Primary", "private": False} for h in events]
        p = {"_class": "Person", "gender": ap.GENDER[gender], "private": private, "tag_list": [],
             "primary_name": ap.name_obj(first, surname, "Birth Name"), "alternate_names": [],
             "attribute_list": [ap.attr("Ancestry ID", anc)], "citation_list": [], "event_ref_list": refs,
             "birth_ref_index": 0 if born else -1, "death_ref_index": (1 if born else 0) if died else -1}
        h = self.make("people", p)
        if events:                                      # let the API settle which birth and death are the main ones
            self.send("PUT", f"people/{h}", json=self.get("people", h))
        return h, anc

    def family(self, father=None, mother=None, children=(), **kw):
        f = {"_class": "Family", "father_handle": father, "mother_handle": mother, "type": "Unknown",
             "child_ref_list": [{"_class": "ChildRef", "ref": c, "frel": "Birth", "mrel": "Birth", "private": False} for c in children],
             "event_ref_list": [], "private": False, "tag_list": []}
        f.update(kw)
        return self.make("families", f)

    def note(self, text, on=None, private=False):
        """A note, attached to a person if one is given."""
        h = self.make("notes", {"_class": "Note", "text": {"_class": "StyledText", "string": text, "tags": []}, "type": "Research",
                                "format": 0, "private": private, "tag_list": []})
        if on:
            p = self.get("people", on)
            p["note_list"] = p.get("note_list", []) + [h]
            self.send("PUT", f"people/{on}", json=p)
        return h

    def gid(self, endpoint, handle):
        return self.get(endpoint, handle)["gramps_id"]

    def families_of(self, handle):
        return [f for f in self.send("GET", "families/", params={"page": 1, "pagesize": 2000})
                if handle in (f["father_handle"], f["mother_handle"])]

    def events_of(self, handle, etype=None):
        evs = [self.get("events", r["ref"]) for r in self.get("people", handle)["event_ref_list"]]
        return [e for e in evs if etype is None or e["type"] == etype]

    def by_anc(self, anc):
        for p in self.send("GET", "people/", params={"page": 1, "pagesize": 5000}):
            if any(a.get("type") == "Ancestry ID" and str(a.get("value")) == str(anc) for a in p.get("attribute_list", [])):
                return p
        return None


class Box:
    """A proposals folder of its own for one test, and the applier pointed at it."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="applier-test-"))
        ap.set_root(self.dir / "proposals")
        ap.APPROVALS = self.dir / "home" / "approved-proposals.txt"
        ap.LOCK = self.dir / "home" / "applier.lock"
        ap.BASE = BASE
        os.environ["GRAMPS_USER"], os.environ["GRAMPS_PASS"] = USER, PASSWORD
        for d in (ap.INBOX, ap.APPLIED, ap.REJECTED, ap.VALID, ap.HELD, ap.UNDO):
            d.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def file(self, action, name=None, **fields):
        """Write one proposal to the inbox; returns its name without .json."""
        self.n += 1
        name = name or f"{self.n:03d}-{action}"
        p = {"proposal_version": 1, "agent": "karel", "action": action, "reasoning": "test reason", "ancestry_attached": True}
        p.update(fields)
        (ap.INBOX / f"{name}.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        return name

    def run(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                ap.main(list(argv))
            except SystemExit as ex:
                if ex.code not in (None, 0):
                    print(f"EXIT {ex.code}")
        return out.getvalue()

    def approve_all(self):
        self.run("--held")
        return self.run("--approve", "all")

    def where(self, name):
        for d in (ap.INBOX, ap.HELD, ap.APPLIED, ap.REJECTED):
            if (d / f"{name}.json").exists():
                return d.name
        return None

    def reason(self, name):
        f = ap.REJECTED / f"{name}.reason.txt"
        return f.read_text() if f.exists() else ""


def cit(tier="index", confidence=2, **kw):
    return {"source_title": "Test index of deaths", "page": "entry 1", "confidence": confidence, "tier": tier, **kw}


def anc_id():
    return "8" + "".join(random.choice("0123456789") for _ in range(11))


def new_person(anc, first="Test", surname="Person", gender="M", **kw):
    return {"ancestry_id": anc, "first_name": first, "surname": surname, "gender": gender, **kw}


def eq(got, want, what=""):
    assert got == want, f"{what}: got {got!r}, wanted {want!r}"


def has(text, part):
    assert part in text, f"{part!r} not in:\n{text}"


def lacks(text, part):
    assert part not in text, f"{part!r} unexpectedly in:\n{text}"


# ---------------------------------------------------------------- additions (what the applier did before, plus fixes)
def test_add_person_into_existing_family(w, b):
    fa, fa_anc = w.person("Felix", "Elder", "M", born=1850)
    mo, mo_anc = w.person("Anna", "Elder", "F", born=1855)
    fam = w.family(fa, mo)
    kid = anc_id()
    name = b.file("add_person", person=new_person(kid, "Ernest", "Elder", birth={"date": "1880-05-02", "place": "Testville, Wisconsin"},
                                                  death={"date": "1950"}, married_surname=None, suffix="Jr."),
                  relations={"father_ancestry_id": fa_anc, "mother_ancestry_id": mo_anc}, citation=cit())
    out = b.run()
    has(out, "APPLIED"); eq(b.where(name), "applied")
    p = w.by_anc(kid)
    eq(p["primary_name"]["first_name"], "Ernest"); eq(p["primary_name"]["suffix"], "Jr.")
    eq(p["parent_family_list"], [fam], "child is in the existing couple family")
    eq([c["ref"] for c in w.get("families", fam)["child_ref_list"]], [p["handle"]])
    eq(len(w.families_of(fa)), 1, "no second family was made")
    births = w.events_of(p["handle"], "Birth")
    eq(len(births), 1); eq(births[0]["date"]["dateval"][:3], [2, 5, 1880])
    eq(len(p["citation_list"]), 1)
    eq(len(p["tag_list"]), 2, "agent tags")


def test_one_parent_family_is_reused_on_a_later_run(w, b):
    """The API returns an empty parent as "": before 0.3.0 that made every later run add another father-only family."""
    fa, fa_anc = w.person("Anton", "Single", "M", born=1866)
    k1, k2 = anc_id(), anc_id()
    b.file("add_person", person=new_person(k1, "Lillian", "Single", "F", birth={"date": "1895"}), relations={"father_ancestry_id": fa_anc})
    has(b.run(), "APPLIED")
    b.file("add_person", person=new_person(k2, "Ella", "Single", "F", birth={"date": "1900"}), relations={"father_ancestry_id": fa_anc})
    has(b.run(), "APPLIED")
    fams = w.families_of(fa)
    eq(len(fams), 1, "one father-only family")
    eq(len(fams[0]["child_ref_list"]), 2)
    eq(w.get("people", fa)["family_list"], [fams[0]["handle"]])


def test_no_second_event_in_the_same_batch(w, b):
    """A person added with a birth, and the same birth filed again as an event in the same run: one event, two citations."""
    fa, fa_anc = w.person("Old", "Batch", "M", born=1840)
    kid = anc_id()
    b.file("add_person", person=new_person(kid, "Ruth", "Batch", "F", birth={"date": "1870-06-09"}, death={"date": "1940-01-02"}),
           relations={"father_ancestry_id": fa_anc}, citation=cit())
    b.file("add_event", subject={"ancestry_id": kid}, event={"type": "Birth", "date": "1870-06-09"}, citation=cit(page="entry 2"))
    b.file("add_event", subject={"ancestry_id": kid}, event={"type": "Death", "date": "1940-01-02", "place": "Testville"}, citation=cit(page="entry 3"))
    b.file("add_event", subject={"ancestry_id": kid}, event={"type": "Death", "date": "1940-01-02"}, citation=cit(page="entry 4"))
    out = b.run()
    eq(out.count("APPLIED"), 4, out)
    p = w.by_anc(kid)
    eq(len(w.events_of(p["handle"], "Birth")), 1); eq(len(w.events_of(p["handle"], "Death")), 1)
    eq(len(w.events_of(p["handle"], "Birth")[0]["citation_list"]), 2)
    eq(len(w.events_of(p["handle"], "Death")[0]["citation_list"]), 3)


def test_placeholder_and_year_only_events_are_filled_in(w, b):
    empty_death = w.event("Death")
    per, anc = w.person("Norman", "Placeholder", "M", born=1830, events=[empty_death])
    b.file("add_event", subject={"ancestry_id": anc}, event={"type": "Death", "date": "1901-09-20", "place": "Oconomowoc"}, citation=cit())
    b.file("add_event", subject={"ancestry_id": anc}, event={"type": "Birth", "date": "1830-10-10"}, citation=cit(page="entry 2"))
    b.file("add_event", subject={"ancestry_id": anc}, event={"type": "Residence", "date": "1880"}, citation=cit(page="entry 3"))
    out = b.run()
    eq(out.count("APPLIED"), 3, out)
    deaths = w.events_of(per, "Death")
    eq(len(deaths), 1, "the empty death was filled, not doubled"); eq(deaths[0]["handle"], empty_death)
    eq(deaths[0]["date"]["dateval"][:3], [20, 9, 1901])
    births = w.events_of(per, "Birth")
    eq(len(births), 1, "the year-only birth was sharpened"); eq(births[0]["date"]["dateval"][:3], [10, 10, 1830])
    p = w.get("people", per)
    eq(w.get("events", p["event_ref_list"][p["death_ref_index"]]["ref"])["date"]["dateval"][2], 1901, "main death has the date")
    eq(len(w.events_of(per, "Residence")), 1)


def test_different_dates_are_kept_apart(w, b):
    per, anc = w.person("Josef", "Conflict", "M", born=(1850, 3, 12), died=1914)
    b.file("add_event", subject={"ancestry_id": anc}, event={"type": "Birth", "date": "1850-05-19"}, citation=cit())
    b.file("add_event", subject={"ancestry_id": anc}, event={"type": "Death", "date": "1915-02-01"}, citation=cit(page="entry 2"))
    eq(b.run().count("APPLIED"), 2)
    eq(len(w.events_of(per, "Birth")), 2, "a birth with another date is evidence, not a duplicate")
    eq(len(w.events_of(per, "Death")), 2)


def test_add_family_links_people_and_refuses_unknown_ids(w, b):
    mo, mo_anc = w.person("Mary", "Weihler", "F", born=1855)
    k1, a1 = w.person("Oscar", "Toepel", "M", born=1882)
    k2, a2 = w.person("Alfred", "Toepel", "M", born=1888)
    ok = b.file("add_family", family={"mother_ancestry_id": mo_anc, "children_ancestry_ids": [a1, a2]})
    bad = b.file("add_family", family={"father_ancestry_id": "123", "mother_ancestry_id": mo_anc, "children_ancestry_ids": [a1]})
    out = b.run()
    eq(b.where(ok), "applied"); eq(b.where(bad), "rejected", out)
    has(b.reason(bad), "father_ancestry_id not found")
    fams = w.families_of(mo)
    eq(len(fams), 1); eq({c["ref"] for c in fams[0]["child_ref_list"]}, {k1, k2})
    eq(w.get("people", k2)["parent_family_list"], [fams[0]["handle"]])
    again = b.file("add_family", family={"mother_ancestry_id": mo_anc, "children_ancestry_ids": [a1, a2]})
    b.run()
    eq(b.where(again), "applied"); eq(len(w.families_of(mo)), 1, "filing it again changes nothing")


def test_notes_citations_media(w, b):
    per, anc = w.person("Noted", "Person", "F", born=1850, died=1900)
    death = w.gid("events", w.events_of(per, "Death")[0]["handle"])
    n1 = b.file("add_note", subject={"ancestry_id": anc}, ancestry_attached=False,
                note={"type": "Research", "text": "RESEARCH SUMMARY — a test", "tier": "ai-draft"})
    n2 = b.file("add_note", subject={"gramps_id": w.gid("people", per)}, note="a plain note")
    n3 = b.file("add_note", subject={"ancestry_id": anc}, ancestry_attached=False, note="needs the Ancestry flag")
    c1 = b.file("add_citation", subject={"ancestry_id": anc}, event_gramps_id=death,
                citation=cit(transcription="as written", source_title="Parish register of Testville", repository="Test archive, Brno",
                             date="1900-01-02", url="https://example.org/x", author="A", pubinfo="P"))
    c2 = b.file("add_citation", subject={"ancestry_id": anc}, citation=cit(tier="index", confidence=4))
    (ap.INBOX / "page.png").write_bytes(PNG)
    m1 = b.file("add_media", subject={"ancestry_id": anc}, citation=cit(tier="record", confidence=4, image="page.png"))
    out = b.run()
    for name, place in ((n1, "applied"), (n2, "applied"), (n3, "rejected"), (c1, "applied"), (c2, "rejected"), (m1, "applied")):
        eq(b.where(name), place, f"{name}\n{out}")
    has(b.reason(c2), "too high"); has(b.reason(n3), "ancestry_attached")
    p = w.get("people", per)
    eq(len(p["note_list"]), 2)
    texts = sorted(w.get("notes", h)["text"]["string"] for h in p["note_list"])
    assert any("RESEARCH SUMMARY — a test\n\nTier: ai-draft" in t for t in texts), texts
    eq([w.get("notes", h)["private"] for h in p["note_list"]], [False, False])
    hidden = b.file("add_note", subject={"ancestry_id": anc}, note={"text": "names somebody living", "private": True})
    b.run(); eq(b.where(hidden), "applied")
    eq(w.get("notes", w.get("people", per)["note_list"][-1])["private"], True, "a note filed private is private")
    eq(len(w.events_of(per, "Death")[0]["citation_list"]), 1)
    eq(len(p["media_list"]), 1)
    assert (ap.APPLIED / "page.png").exists() and not (ap.INBOX / "page.png").exists()


def test_rejections(w, b):
    fa, fa_anc = w.person("Known", "Father", "M", born=1850)
    cases = {
        b.file("add_person", person=new_person(anc_id(), "Alone", "Nobody", birth={"date": "1880"})): "not linked to anyone",
        b.file("add_person", person=new_person(fa_anc, "Known", "Father"), relations={"spouse_ancestry_id": fa_anc}): "already exists",
        b.file("add_person", person=new_person(anc_id(), "X", "Y"), relations={"father_ancestry_id": "555"}): "not in Gramps",
        b.file("add_person", person=new_person(anc_id(), "X", "Y", "Q"), relations={"father_ancestry_id": fa_anc}): "gender",
        b.file("add_person", person=new_person(anc_id(), "X", "Y"), relations={"father_ancestry_id": fa_anc, "father_rel": "Godfather"}): "father_rel",
        b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1900"}): "citation missing",
        b.file("add_event", subject={"ancestry_id": "777"}, event={"type": "Death"}, citation=cit()): "subject not found",
        b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death"}, citation=cit(tier="record", confidence=4)): "requires an image",
        b.file("add_note", subject={"ancestry_id": fa_anc}): "note missing",
        b.file("remove_person", subject={"ancestry_id": fa_anc}): "not supported",
        b.file("add_person", person=["not", "an", "object"]): "malformed",
        b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death"}, citation=cit(), reasoning=""): "reasoning missing",
    }
    (ap.INBOX / "broken.json").write_text("{not json")
    (ap.INBOX / "list.json").write_text("[1, 2]")
    (ap.INBOX / "utf16.json").write_text('{"proposal_version": 1}', encoding="utf-16")
    out = b.run()
    for name, why in cases.items():
        eq(b.where(name), "rejected", f"{name}\n{out}")
        has(b.reason(name), why)
    eq(b.where("broken"), "rejected"); eq(b.where("list"), "rejected"); eq(b.where("utf16"), "rejected")
    lacks(out, "APPLIED"); lacks(out, "Traceback")
    # a file that Windows saved with a byte-order mark in front is read like any other
    good = json.dumps({"proposal_version": 1, "agent": "karel", "action": "add_event", "reasoning": "r", "ancestry_attached": True,
                       "subject": {"ancestry_id": fa_anc}, "event": {"type": "Death", "date": "1900"}, "citation": cit()})
    (ap.INBOX / "with-bom.json").write_bytes(b"\xef\xbb\xbf" + good.encode("utf-8"))
    has(b.run(), "APPLIED"); eq(b.where("with-bom"), "applied")


def test_spouse_named_by_only_one_of_two_new_people(w, b):
    fa, fa_anc = w.person("Father", "Inlaw", "M", born=1820)
    h, wf = anc_id(), anc_id()
    b.file("add_person", name="001-husband", person=new_person(h, "John", "Kliment", "M", birth={"date": "1850"}),
           relations={"spouse_ancestry_id": wf})
    b.file("add_person", name="002-wife", person=new_person(wf, "Ella", "Inlaw", "F", birth={"date": "1852"}),
           relations={"father_ancestry_id": fa_anc})
    out = b.run()
    eq(out.count("APPLIED"), 2, out); lacks(out, "WARNING")
    fams = w.families_of(w.by_anc(h)["handle"])
    eq(len(fams), 1); eq(fams[0]["mother_handle"], w.by_anc(wf)["handle"]); eq(fams[0]["father_handle"], w.by_anc(h)["handle"])


def test_spouse_who_never_arrives_is_reported(w, b):
    fa, fa_anc = w.person("Father", "Lone", "M", born=1820)
    h, wf = anc_id(), anc_id()
    b.file("add_person", name="001-husband", person=new_person(h, "John", "Lone", "M", birth={"date": "1850"}),
           relations={"spouse_ancestry_id": wf, "father_ancestry_id": fa_anc})
    b.file("add_person", name="002-wife", person=new_person(wf, "Ella", "Gone", "Z", birth={"date": "1852"}), relations={"spouse_ancestry_id": h})
    out = b.run()
    eq(b.where("001-husband"), "applied"); eq(b.where("002-wife"), "rejected")
    has(out, "WARNING")                                     # they named each other, so he went in before she failed


def test_a_spouse_who_is_refused_takes_the_other_with_them(w, b):
    """Only one names the other: the one named goes first, and if that one fails the other is rejected too."""
    fa, fa_anc = w.person("Father", "Order", "M", born=1820)
    h, wf = anc_id(), anc_id()
    b.file("add_person", name="001-husband", person=new_person(h, "John", "Order", "M", birth={"date": "1850"}),
           relations={"spouse_ancestry_id": wf})
    b.file("add_person", name="002-wife", person=new_person(wf, "Ella", "Order", "Z", birth={"date": "1852"}),
           relations={"father_ancestry_id": fa_anc})
    out = b.run()
    eq(b.where("002-wife"), "rejected", out); eq(b.where("001-husband"), "rejected", out)
    has(b.reason("001-husband"), "was not applied"); lacks(out, "WARNING"); lacks(out, "APPLIED")


def test_marriage_goes_on_the_family(w, b):
    hu, hu_anc = w.person("Hus", "Band", "M", born=1850)
    wi, wi_anc = w.person("Wi", "Fe", "F", born=1852)
    fam = w.family(hu, wi)
    b.file("add_event", subject={"ancestry_id": hu_anc}, spouse={"ancestry_id": wi_anc},
           event={"type": "Marriage", "date": "1875-06-26", "place": "Kewaunee"}, citation=cit())
    b.file("add_event", subject={"ancestry_id": wi_anc}, spouse={"ancestry_id": hu_anc},
           event={"type": "Marriage", "date": "1875-06-26"}, citation=cit(page="entry 2"))
    eq(b.run().count("APPLIED"), 2)
    refs = w.get("families", fam)["event_ref_list"]
    eq(len(refs), 1, "one marriage event"); eq(len(w.get("events", refs[0]["ref"])["citation_list"]), 2)


def test_dry_run_writes_nothing(w, b):
    fa, fa_anc = w.person("Dry", "Run", "M", born=1850)
    before = json.dumps(w.get("people", fa), sort_keys=True)
    name = b.file("add_person", person=new_person(anc_id(), "Kid", "Run", birth={"date": "1880"}), relations={"father_ancestry_id": fa_anc}, citation=cit())
    b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1900"}, citation=cit())
    held = b.file("update_person", subject={"ancestry_id": fa_anc}, set={"surname": "Ran"})
    out = b.run("--dry-run")
    eq(out.count("WOULD APPLY"), 2, out); has(out, "WOULD HOLD"); has(out, "planned")
    eq(json.dumps(w.get("people", fa), sort_keys=True), before)
    eq(b.where(name), "inbox"); eq(b.where(held), "inbox")
    eq(w.families_of(fa), [])


def test_adoption_and_private_on_a_new_person(w, b):
    fa, fa_anc = w.person("Felix", "Adopter", "M", born=1851)
    mo, mo_anc = w.person("Clara", "Adopter", "F", born=1853)
    fam = w.family(fa, mo)
    kid = anc_id()
    name = b.file("add_person", person=new_person(kid, "Tomas", "Adopter", birth={"date": "1894", "place": "Lisbon"},
                                                  death={"date": "1921-07-02"}, private=True),
                  relations={"father_ancestry_id": fa_anc, "mother_ancestry_id": mo_anc, "father_rel": "Adopted", "mother_rel": "Adopted"})
    out = b.run()
    eq(b.where(name), "held", out); has(out, "adds a private person")
    listing = b.run("--held")
    has(listing, "ADD PERSON  Tomas Adopter"); has(listing, "[private]"); has(listing, "adopted by both")
    has(listing, "Felix Adopter (1851–) × "); has(listing, "born 1894 in Lisbon")
    has(b.run("--approve", "all"), "APPLIED")
    p = w.by_anc(kid)
    eq(p["private"], True)
    eq([e["private"] for e in w.events_of(p["handle"])], [True, True], "the events of a private person are private too")
    ref = w.get("families", fam)["child_ref_list"][0]
    eq((ref["frel"], ref["mrel"]), ("Adopted", "Adopted"))
    eq(w.get("families", fam)["private"], False, "the parents' family does not become private")



def test_the_family_of_a_private_couple_is_private(w, b):
    """22 Nov 2026: when the second of a new couple was added, the applier made their family public, and the add_family
    that followed reused it and dropped its "private": true (and the marriage's). The shape of a real batch; invented names."""
    fa, fa_anc = w.person("Orrin", "Brook", "M", born=1924, died=2006)
    mo, mo_anc = w.person("Hedda", "Brook", "F", born=1925, died=1993)
    parents = w.family(fa, mo)
    son, wife = anc_id(), anc_id()
    b.file("add_person", name="001-son", person=new_person(son, "Mattias", "Brook", "M", birth={"date": "1951"}, private=True),
           citation=cit())
    b.file("add_person", name="002-wife", person=new_person(wife, "Ilse", "Varga", "F", private=True),
           relations={"spouse_ancestry_id": son}, citation=cit())
    b.file("add_family", name="003-couple", family={"father_ancestry_id": son, "mother_ancestry_id": wife, "private": True,
                                                    "marriage": {"date": "1990"}}, citation=cit())
    b.file("update_family", name="004-son", family={"gramps_id": w.gid("families", parents)}, add_children=[{"ancestry_id": son}],
           citation=cit())
    b.run()
    has(b.run("--held"), "[private]")
    out = b.run("--approve", "all")
    eq(out.count("APPLIED"), 4, out)
    fams = w.families_of(w.by_anc(son)["handle"])
    eq(len(fams), 1, "one family for the couple")
    eq(fams[0]["private"], True, "the couple's family is private")
    eq([w.get("events", r["ref"])["private"] for r in fams[0]["event_ref_list"]], [True], "and so is their marriage")
    eq(w.get("families", parents)["private"], False, "the public parents' family stays public")
    eq([c["ref"] for c in w.get("families", parents)["child_ref_list"]], [w.by_anc(son)["handle"]])


def test_a_private_partner_makes_a_new_family_private(w, b):
    """A new private husband for someone already in the tree, and the parents a new child names when one is private:
    left public, either family would show on the site with a blank partner."""
    pub, pub_anc = w.person("Ruthanne", "Calder", "F", born=None)
    hus = anc_id()
    b.file("add_person", name="001-husband", person=new_person(hus, "Tobias", "Hale", "M", private=True),
           relations={"spouse_ancestry_id": pub_anc}, citation=cit())
    dad, dad_anc = w.person("Edwin", "Marsh", "M", born=1949, died=2008)
    mum, kid = anc_id(), anc_id()
    b.file("add_person", name="002-mother", person=new_person(mum, "Wilma", "Marsh", "F", private=True), citation=cit())
    b.file("add_person", name="003-child", person=new_person(kid, "Selma", "Marsh", "F", private=True),
           relations={"father_ancestry_id": dad_anc, "mother_ancestry_id": mum}, citation=cit())
    b.run()
    out = b.approve_all()
    eq(out.count("APPLIED"), 3, out)
    eq([f["private"] for f in w.families_of(pub)], [True], "the public wife and her private husband")
    eq([f["private"] for f in w.families_of(dad)], [True], "the father and the private mother of his child")


def test_private_on_a_family_from_before_is_refused(w, b):
    """An add_family may not quietly leave out what it asks for: "private": true on a family that was in the tree before
    the run is set_private's job, so the file is rejected with that reason, and nothing of it stays."""
    hu, hu_anc = w.person("Old", "Husband", "M", born=1850, died=1920)
    wi, wi_anc = w.person("Old", "Wife", "F", born=1852, died=1925)
    fam = w.family(hu, wi)
    name = b.file("add_family", family={"father_ancestry_id": hu_anc, "mother_ancestry_id": wi_anc, "private": True,
                                        "marriage": {"date": "1875"}}, citation=cit())
    b.run()
    b.approve_all()
    eq(b.where(name), "rejected"); has(b.reason(name), "set_private")
    eq((w.get("families", fam)["private"], w.get("families", fam)["event_ref_list"]), (False, []), "nothing half done")

# ---------------------------------------------------------------- the approval gate
def test_changes_wait_for_approval(w, b):
    per, anc = w.person("Joann", "Unknown", "F", born=1834, died=1918)
    name = b.file("update_person", subject={"ancestry_id": anc}, set={"first_name": "JoAnn", "surname": "LaMotte", "married_surname": "Wood"})
    out = b.run()
    has(out, "HOLD"); eq(b.where(name), "held")
    eq(w.get("people", per)["primary_name"]["first_name"], "Joann", "nothing changed yet")
    assert (ap.HELD / "_WAITING.txt").exists()
    lacks(b.run(), "HOLD")                                   # a later run does not repeat itself
    eq(b.where(name), "held")
    has(b.run("--approve", "all"), "Nothing approved")       # 'all' means what --held showed, and it has shown nothing yet
    listing = b.run("--held")
    has(listing, "CHANGE PERSON"); has(listing, "Joann Unknown  →  JoAnn LaMotte"); has(listing, "adds the married name Wood")
    out = b.run("--approve", "all")
    has(out, "APPLIED"); eq(b.where(name), "applied")
    p = w.get("people", per)
    eq(ap.name_text(p["primary_name"]), "JoAnn LaMotte")
    eq([(n["type"], ap.name_text(n)) for n in p["alternate_names"]], [("Married Name", "JoAnn Wood")])
    assert (ap.UNDO / f"{name}.undo.json").exists()
    assert not (ap.HELD / "_WAITING.txt").exists()


def test_an_edit_after_approval_must_be_approved_again(w, b):
    per, anc = w.person("Edit", "After", "M", born=1834)
    name = b.file("update_person", subject={"ancestry_id": anc}, set={"surname": "Before"})
    b.run(); b.run("--held")
    f = ap.HELD / f"{name}.json"
    ap.APPROVALS.parent.mkdir(parents=True, exist_ok=True)
    ap.APPROVALS.write_text(f"{ap.file_hash(f)}\t{f.name}\tnow\n")           # approved as it is now …
    p = json.loads(f.read_text()); p["set"]["surname"] = "Changed"
    f.write_text(json.dumps(p))                                              # … and then edited
    out = b.run()
    lacks(out, "APPLIED"); eq(b.where(name), "held")
    eq(ap.name_text(w.get("people", per)["primary_name"]), "Edit After")


def test_an_approval_is_for_one_file_and_ends_with_an_undo(w, b):
    per, anc = w.person("Once", "Only", "M", born=1834)
    fields = dict(subject={"ancestry_id": anc}, set={"nickname": "Nick"})
    one = b.file("update_person", name="first", **fields)
    b.run(); b.approve_all()
    eq(b.where(one), "applied")
    two = b.file("update_person", name="second", **fields)       # the same content under another name
    eq(b.where(two), "inbox"); b.run(); eq(b.where(two), "held", "it is not covered by the approval of the first")
    has(b.run("--reject", "second"), "refused")
    has(b.run("--undo", "first"), ": undone")
    eq(w.get("people", per)["primary_name"]["nick"], "")
    has(b.run("--resubmit"), "resubmitted 2")
    eq(b.where(one), "held", "after an undo the old approval no longer counts")
    eq(w.get("people", per)["primary_name"]["nick"], "")


def test_new_arrivals_are_not_covered_by_approve_all(w, b):
    per, anc = w.person("Seen", "First", "M", born=1834)
    one = b.file("update_person", subject={"ancestry_id": anc}, set={"surname": "One"})
    b.run(); b.run("--held")
    two = b.file("update_person", subject={"ancestry_id": anc}, set={"nickname": "Two"})
    b.run()
    out = b.run("--approve", "all")
    has(out, "NOT approved"); eq(b.where(one), "applied"); eq(b.where(two), "held")


def test_living_or_recent_people_wait_and_old_ones_do_not(w, b):
    old, old_anc = w.person("Long", "Ago", "M", born=1850)
    nodates, nodates_anc = w.person("No", "Dates", "F", born=None)
    kid_of_nodates, _ = w.person("Child", "Dates", "M", born=1890)
    w.family(None, nodates, [kid_of_nodates])
    recent, recent_anc = w.person("Born", "Lately", "F", born=1958, died=2018)
    priv, priv_anc = w.person("Private", "Person", "M", born=1800, private=True)
    undated, undated_anc = w.person("Nothing", "Known", "M", born=None)
    names = {
        b.file("add_event", subject={"ancestry_id": old_anc}, event={"type": "Death", "date": "1910"}, citation=cit()): "applied",
        b.file("add_event", subject={"ancestry_id": nodates_anc}, event={"type": "Death", "date": "1925"}, citation=cit()): "applied",
        b.file("add_event", subject={"ancestry_id": recent_anc}, event={"type": "Burial", "date": "2018"}, citation=cit()): "held",
        b.file("add_note", subject={"ancestry_id": priv_anc}, note="x"): "held",
        b.file("add_note", subject={"ancestry_id": undated_anc}, note="x"): "held",
        b.file("add_person", person=new_person(anc_id(), "Grand", "Child", birth={"date": "1990"}),
               relations={"father_ancestry_id": old_anc}): "held",
        b.file("add_person", person=new_person(anc_id(), "Old", "Child", birth={"date": "1880"}),
               relations={"father_ancestry_id": old_anc}): "applied",
        b.file("add_person", person=new_person(anc_id(), "Undated", "Child"), relations={"father_ancestry_id": old_anc}): "applied",
        b.file("add_person", person=new_person(anc_id(), "Undated", "Spouse", "F"), relations={"spouse_ancestry_id": recent_anc}): "held",
    }
    # someone with no dates and no relatives yet, filed as the mother of people born in the 1880s, is not recent …
    mary, mary_anc = w.person("Mary", "Weihler", "F", born=None)
    oscar, oscar_anc = w.person("Oscar", "Toepel", "M", born=1882)
    names[b.file("add_family", family={"mother_ancestry_id": mary_anc, "children_ancestry_ids": [oscar_anc]})] = "applied"
    # … nor is the undated father of a new person born in 1890; but the undated mother of someone born in 1960 is
    und_f, und_f_anc = w.person("Undated", "Father", "M", born=None)
    names[b.file("add_person", person=new_person(anc_id(), "Dated", "Child", birth={"date": "1890"}),
                 relations={"father_ancestry_id": und_f_anc})] = "applied"
    und_m, und_m_anc = w.person("Undated", "Mother", "F", born=None)
    kid60, kid60_anc = w.person("Kid", "Sixty", "M", born=1960, died=2001)
    names[b.file("add_family", family={"mother_ancestry_id": und_m_anc, "children_ancestry_ids": [kid60_anc]})] = "held"
    out = b.run()
    for name, place in names.items():
        eq(b.where(name), place, f"{name}\n{out}")
    has(out, "about someone living or recent"); has(out, "about a private record"); has(out, "adds someone living or recent")


def test_what_depends_on_a_waiting_person_waits_too(w, b):
    gp, gp_anc = w.person("Grand", "Parent", "M", born=1900, died=1980)
    parent, child, spouse = anc_id(), anc_id(), anc_id()
    names = [
        b.file("add_person", name="001-parent", person=new_person(parent, "Living", "Parent", "F", birth={"date": "1950"}, private=True),
               relations={"father_ancestry_id": gp_anc}),
        b.file("add_person", name="002-child", person=new_person(child, "Living", "Child", "M", birth={"date": "1975"}, private=True),
               relations={"mother_ancestry_id": parent, "father_ancestry_id": spouse}),
        b.file("add_person", name="003-spouse", person=new_person(spouse, "Living", "Spouse", "M", private=True),
               relations={"spouse_ancestry_id": parent}),
        b.file("add_event", name="004-event", subject={"ancestry_id": child}, event={"type": "Residence", "date": "2019", "private": True}, citation=cit()),
        b.file("update_family", name="005-family", family={"father": {"ancestry_id": spouse}, "mother": {"ancestry_id": parent}},
               set={"private": True}),
    ]
    out = b.run()
    for n in names:
        eq(b.where(n), "held", f"{n}\n{out}")
    lacks(out, "APPLIED"); lacks(out, "REJECT")
    eq(w.by_anc(parent), None)
    listing = b.run("--held")
    has(listing, "5 proposal(s) waiting")
    has(listing, "child of Living Spouse (also new, in this batch) × Living Parent, born 1950 (also new, in this batch)")
    has(listing, "waits for Living Parent, born 1950, who is not approved yet")
    out = b.run("--approve", "001-parent")
    eq(b.where("001-parent"), "applied", out)
    for n in names[1:]:
        eq(b.where(n), "held", f"{n} still waits for its own approval\n{out}")
    b.run("--held")
    out = b.run("--approve", "all")
    for n in names:
        eq(b.where(n), "applied", f"{n}\n{out}")
    p, c, s = w.by_anc(parent), w.by_anc(child), w.by_anc(spouse)
    fam = w.families_of(p["handle"])
    eq(len(fam), 1); eq(fam[0]["father_handle"], s["handle"]); eq([x["ref"] for x in fam[0]["child_ref_list"]], [c["handle"]])
    eq(fam[0]["private"], True)
    eq(w.events_of(c["handle"], "Residence")[0]["private"], True)


def test_refusing_a_proposal(w, b):
    gp, gp_anc = w.person("Grand", "Refuse", "M", born=1900, died=1980)
    parent = anc_id()
    one = b.file("add_person", name="001-wrong", person=new_person(parent, "Wrong", "Generation", birth={"date": "1993"}),
                 relations={"father_ancestry_id": gp_anc, "father_rel": "Adopted"})
    two = b.file("add_event", name="002-event", subject={"ancestry_id": parent}, event={"type": "Death", "date": "2019"}, citation=cit())
    b.run()
    eq(b.where(one), "held"); eq(b.where(two), "held")
    out = b.run("--reject", "001", "--reason", "not their son, he is their nephew")
    has(out, "refused 001-wrong"); eq(b.where(one), "rejected"); has(b.reason(one), "their nephew")
    b.run()
    eq(b.where(two), "rejected", "what depended on it is rejected on the next run"); has(b.reason(two), "subject not found")
    eq(w.by_anc(parent), None)


def test_a_corrected_file_replaces_the_held_one(w, b):
    per, anc = w.person("Correct", "Me", "M", born=1834)
    name = b.file("update_person", name="fix-name", subject={"ancestry_id": anc}, set={"surname": "Wrnog"})
    b.run(); b.run("--held")
    b.file("update_person", name="fix-name", subject={"ancestry_id": anc}, set={"surname": "Right"})
    out = b.run("--approve", "all")                         # the approval was for the old text
    lacks(out, "APPLIED"); eq(b.where(name), "held")
    has(b.run("--held"), "Correct Me  →  Correct Right")
    has(b.run("--approve", "all"), "APPLIED")
    eq(ap.name_text(w.get("people", per)["primary_name"]), "Correct Right")


# ---------------------------------------------------------------- the change actions
def test_update_person(w, b):
    e_wrong, e_right = w.event("Birth", (1898,)), w.event("Birth", (1899, 7, 4))
    per, anc = w.person("Anna", "", "F", born=None, events=[e_wrong, e_right])
    wrong_sex, ws_anc = w.person("Jane", "Wrongsex", "M", born=1880)
    first = b.file("update_person", subject={"ancestry_id": anc},
                   set={"surname": "Suchomel", "married_surname": "Wacek", "also_known_as": ["Annie Wacek", {"first_name": "Anna", "surname": "Sukomel"}],
                        "nickname": "Nan", "call_name": "Ann", "suffix": "Sr.", "private": True, "prefer_event": w.gid("events", e_right)},
                   citation=cit())
    second = b.file("update_person", subject={"gramps_id": w.gid("people", wrong_sex)}, set={"gender": "F"})
    bad = [b.file("update_person", subject={"ancestry_id": anc}, set={"birth_year": 1871}),
           b.file("update_person", subject={"ancestry_id": anc}, set={"surname": ""}),
           b.file("update_person", subject={"ancestry_id": anc}, set={}),
           b.file("update_person", subject={"ancestry_id": anc}, set={"prefer_event": "E99999"}),
           b.file("update_person", subject={"ancestry_id": ws_anc}, set={"prefer_event": w.gid("events", e_right)})]
    out = b.run()
    for n in bad:
        eq(b.where(n), "rejected", out)
    b.approve_all()
    eq(b.where(first), "applied"); eq(b.where(second), "applied")
    p = w.get("people", per)
    pn = p["primary_name"]
    eq((pn["first_name"], pn["surname_list"][0]["surname"], pn["suffix"], pn["call"], pn["nick"]), ("Anna", "Suchomel", "Sr.", "Ann", "Nan"))
    eq(sorted((n["type"], ap.name_text(n)) for n in p["alternate_names"]),
       [("Also Known As", "Anna Sukomel"), ("Also Known As", "Annie Wacek"), ("Married Name", "Anna Wacek")])
    eq(p["private"], True); eq(len(p["citation_list"]), 1)
    eq(p["event_ref_list"][0]["ref"], e_right); eq(p["birth_ref_index"], 0, "the preferred birth is the main one")
    eq(w.get("people", wrong_sex)["gender"], 0)
    bare_anc = anc_id()
    bare = w.make("people", {"_class": "Person", "gender": 0, "primary_name": {"_class": "Name", "first_name": "Joann", "surname_list": []},
                             "attribute_list": [ap.attr("Ancestry ID", bare_anc)],
                             "event_ref_list": [{"_class": "EventRef", "ref": w.event("Birth", (1834, 6, 21)), "role": "Primary"}]})
    b.file("update_person", subject={"ancestry_id": bare_anc}, set={"first_name": "JoAnn", "surname": "LaMotte", "married_surname": "Wood"})
    b.run(); b.approve_all()
    eq(ap.name_text(w.get("people", bare)["primary_name"]), "JoAnn LaMotte", "a name that had no surname entry at all")
    again = b.file("update_person", subject={"ancestry_id": anc}, set={"surname": "Suchomel", "married_surname": "wacek", "also_known_as": "Annie Wacek", "private": True})
    b.run(); out = b.approve_all()
    has(out, "nothing (already so)"); eq(b.where(again), "applied")
    eq(len(w.get("people", per)["alternate_names"]), 3, "filing it again adds nothing")


def test_update_family(w, b):
    fa, fa_anc = w.person("Anton", "Wacek", "M", born=1866)
    mo, mo_anc = w.person("Anna", "Suchomel", "F", born=1871)
    k1, a1 = w.person("Lillian", "Wacek", "F", born=1895)
    k2, a2 = w.person("Caroline", "Wacek", "F", born=1899)
    fam = w.family(fa, None, [k1])
    fid = w.gid("families", fam)
    name = b.file("update_family", family={"gramps_id": fid}, set={"mother": {"ancestry_id": mo_anc}, "type": "Married"},
                  add_children=[{"ancestry_id": a2}], citation=cit())
    out = b.run(); eq(b.where(name), "held", out)
    listing = b.run("--held")
    has(listing, f"CHANGE FAMILY  {fid}"); has(listing, "mother: "); has(listing, "Anna Suchomel"); has(listing, "Caroline Wacek (1899–)  (by birth)")
    has(b.run("--approve", "all"), "APPLIED")
    f = w.get("families", fam)
    eq((f["father_handle"], f["mother_handle"], f["type"]), (fa, mo, "Married"))
    eq([c["ref"] for c in f["child_ref_list"]], [k1, k2]); eq(len(f["citation_list"]), 1)
    eq(w.get("people", mo)["family_list"], [fam]); eq(w.get("people", k2)["parent_family_list"], [fam])
    eq(w.get("people", fa)["family_list"], [fam])

    # swap father and mother; a parent is never replaced
    stranger, st_anc = w.person("Some", "Stranger", "M", born=1860)
    swap = b.file("update_family", family={"gramps_id": fid}, set={"father": {"ancestry_id": mo_anc}, "mother": {"ancestry_id": fa_anc}})
    replace = b.file("update_family", family={"gramps_id": fid}, set={"father": {"ancestry_id": st_anc}})
    half = b.file("update_family", family={"gramps_id": fid}, set={"mother": {"ancestry_id": fa_anc}})
    as_child = b.file("update_family", family={"gramps_id": fid}, add_children=[{"ancestry_id": fa_anc}])
    out = b.run()
    eq(b.where(replace), "rejected", out); has(b.reason(replace), "replace a parent")
    eq(b.where(half), "rejected"); has(b.reason(half), "same person")
    eq(b.where(as_child), "rejected"); has(b.reason(as_child), "cannot also be its child")
    b.approve_all()
    eq(b.where(swap), "applied")
    f = w.get("families", fam)
    eq((f["father_handle"], f["mother_handle"]), (mo, fa))
    eq(w.get("people", mo)["family_list"], [fam]); eq(w.get("people", fa)["family_list"], [fam])


def test_update_family_adoption_and_second_parents(w, b):
    birth_f, bf_anc = w.person("Hugo", "Birth", "M", born=1849)
    birth_m, bm_anc = w.person("Ida", "Birth", "F", born=1850)
    adopt_f, af_anc = w.person("Emil", "Adopt", "M", born=1845)
    adopt_m, am_anc = w.person("Olga", "Adopt", "F", born=1846)
    child, child_anc = w.person("Vera", "Adopt", "F", born=1870)
    birth_fam = w.family(birth_f, birth_m, [child])
    adopt_fam = w.family(adopt_f, adopt_m)
    wrong = b.file("update_family", family={"gramps_id": w.gid("families", adopt_fam)}, add_children=[{"ancestry_id": child_anc}])
    right = b.file("update_family", family={"gramps_id": w.gid("families", adopt_fam)}, add_children=[{"ancestry_id": child_anc, "rel": "Adopted"}])
    step = b.file("update_family", family={"father": {"ancestry_id": af_anc}, "mother": {"ancestry_id": am_anc}},
                  add_children=[{"ancestry_id": child_anc, "father_rel": "Adopted", "mother_rel": "Stepchild"}])
    out = b.run()
    eq(b.where(wrong), "rejected", out); has(b.reason(wrong), "already has a father by birth")
    b.approve_all()
    eq(b.where(right), "applied"); eq(b.where(step), "applied")
    eq(sorted(w.get("people", child)["parent_family_list"]), sorted([birth_fam, adopt_fam]))
    ref = w.get("families", adopt_fam)["child_ref_list"][0]
    eq((ref["frel"], ref["mrel"]), ("Adopted", "Stepchild"), "the relationship of a child already there can be corrected")
    # the same father in two families: merge, do not link twice
    half_fam = w.family(birth_f, None)
    twice = b.file("update_family", family={"gramps_id": w.gid("families", half_fam)}, add_children=[{"ancestry_id": child_anc}])
    b.run()
    eq(b.where(twice), "rejected"); has(b.reason(twice), "merge the two families")
    # and a couple that already has a family is not given a second one
    lone = w.family(adopt_f, None)
    dup = b.file("update_family", family={"gramps_id": w.gid("families", lone)}, set={"mother": {"ancestry_id": am_anc}})
    b.run()
    eq(b.where(dup), "rejected"); has(b.reason(dup), "use merge_family")


def test_merge_family(w, b):
    fa, fa_anc = w.person("Anton", "Merge", "M", born=1866)
    mo, mo_anc = w.person("Anna", "Merge", "F", born=1871)
    k1, _ = w.person("Lillian", "Merge", "F", born=1895)
    k2, _ = w.person("Emma", "Merge", "F", born=1894)
    k3, _ = w.person("Ella", "Merge", "F", born=1900)
    marriage = w.event("Marriage", (1890,))
    keep = w.family(fa, None, [k1, k2])
    drop = w.family(fa, None, [k3, k1], event_ref_list=[{"_class": "EventRef", "ref": marriage, "role": "Family", "private": False}])
    with_mother = w.family(fa, mo)
    kid, did, mid = (w.gid("families", h) for h in (keep, drop, with_mother))
    name = b.file("merge_family", keep={"gramps_id": kid}, drop={"gramps_id": did})
    out = b.run(); eq(b.where(name), "held", out)
    listing = b.run("--held")
    has(listing, f"MERGE FAMILIES  keep {kid}"); has(listing, f"fold in and delete {did}"); has(listing, "Ella Merge")
    has(b.run("--approve", "all"), "APPLIED")
    eq(w.get("families", drop), None, "the duplicate is gone")
    f = w.get("families", keep)
    eq(sorted(c["ref"] for c in f["child_ref_list"]), sorted([k1, k2, k3])); eq(len(f["event_ref_list"]), 1)
    eq(sorted(w.get("people", fa)["family_list"]), sorted([keep, with_mother]))
    for k in (k1, k2, k3):
        eq(w.get("people", k)["parent_family_list"], [keep])
    # now fold the father-only family into the couple's family: the mother is kept
    second = b.file("merge_family", keep=mid, drop=kid)
    b.run(); out = b.approve_all()
    eq(b.where(second), "applied", out)
    f = w.get("families", with_mother)
    eq((f["father_handle"], f["mother_handle"]), (fa, mo)); eq(len(f["child_ref_list"]), 3)
    eq(w.get("people", fa)["family_list"], [with_mother]); eq(w.get("people", mo)["family_list"], [with_mother])
    eq(w.get("people", k3)["parent_family_list"], [with_mother])


def test_merge_family_takes_the_parent_the_kept_one_lacks(w, b):
    fa, _ = w.person("Felix", "Takes", "M", born=1886)
    mo, _ = w.person("Anna", "Takes", "F", born=1889)
    k1, _ = w.person("Ernest", "Takes", "M", born=1910)
    k2, _ = w.person("Gladys", "Takes", "F", born=1922)
    keep, drop = w.family(fa, None, [k1]), w.family(fa, mo, [k2])
    name = b.file("merge_family", keep=w.gid("families", keep), drop=w.gid("families", drop))
    b.run(); out = b.approve_all()
    eq(b.where(name), "applied", out)
    f = w.get("families", keep)
    eq((f["father_handle"], f["mother_handle"]), (fa, mo)); eq(len(f["child_ref_list"]), 2)
    eq(w.get("people", mo)["family_list"], [keep]); eq(w.get("people", fa)["family_list"], [keep])


def test_merge_family_never_merges_people(w, b):
    a, _ = w.person("Aaa", "Different", "M", born=1850)
    c, _ = w.person("Bbb", "Different", "M", born=1851)
    mo, _ = w.person("Shared", "Mother", "F", born=1855)
    lone_m, _ = w.person("Other", "Mother", "F", born=1856)
    one, two = w.family(a, mo), w.family(c, mo)
    three, four = w.family(a, None), w.family(None, lone_m)
    names = {
        b.file("merge_family", keep=w.gid("families", one), drop=w.gid("families", two)): "different fathers",
        b.file("merge_family", keep=w.gid("families", three), drop=w.gid("families", four)): "share no parent",
        b.file("merge_family", keep=w.gid("families", one), drop=w.gid("families", one)): "same family",
        b.file("merge_family", keep=w.gid("families", one), drop="F99999"): "not found",
    }
    out = b.run()
    for n, why in names.items():
        eq(b.where(n), "rejected", out); has(b.reason(n), why)
    assert w.get("people", a) and w.get("people", c), "both men are still there"
    assert w.get("families", one) and w.get("families", two)


def test_merge_family_checks_again_just_before_merging(w, b):
    """If the tree changes between validation and the merge, the merge is not sent."""
    a, _ = w.person("Aaa", "Race", "M", born=1850)
    c, _ = w.person("Ccc", "Race", "M", born=1851)
    keep, drop = w.family(a, None), w.family(a, None)
    name = b.file("merge_family", keep=w.gid("families", keep), drop=w.gid("families", drop))
    b.run(); b.run("--held")
    real_get = ap.Api.get
    def get(self, path, **params):
        obj = real_get(self, path, **params)
        if path == f"families/{drop}":
            obj = dict(obj, father_handle=c)            # as if someone had changed the father meanwhile
        return obj
    ap.Api.get = get
    try:
        out = b.run("--approve", "all")
    finally:
        ap.Api.get = real_get
    has(out, "FAILED"); has(out, "different fathers"); eq(b.where(name), "rejected"); has(b.reason(name), "different fathers")
    assert w.get("people", c) and w.get("families", drop), "nothing was merged"


def test_merge_event(w, b):
    place = w.place("Oconomowoc, Wisconsin")
    empty, dated = w.event("Death"), w.event("Death", (2018, 9, 20), place=place)
    b1, b2 = w.event("Birth", (1691, 7, 1), place=place), w.event("Birth", (1691, 7, 1))
    per, anc = w.person("Norman", "Wood", "M", born=None, events=[empty, dated, b2, b1])
    gid = w.gid("people", per)
    one = b.file("merge_event", subject={"gramps_id": gid}, keep=w.gid("events", dated), drop=w.gid("events", empty))
    two = b.file("merge_event", subject={"gramps_id": gid}, keep=w.gid("events", b1), drop=w.gid("events", b2))
    wrong_way = b.file("merge_event", subject={"gramps_id": gid}, keep=w.gid("events", empty), drop=w.gid("events", dated))
    mixed = b.file("merge_event", subject={"gramps_id": gid}, keep=w.gid("events", dated), drop=w.gid("events", b1))
    out = b.run()
    eq(b.where(wrong_way), "rejected", out); has(b.reason(wrong_way), "swap keep and drop")
    eq(b.where(mixed), "rejected"); has(b.reason(mixed), "different types")
    listing = b.run("--held")
    has(listing, "MERGE EVENTS  of"); has(listing, "20 Sep 2018, Oconomowoc, Wisconsin"); has(listing, "no date")
    has(b.run("--approve", "all"), "APPLIED")
    eq(b.where(one), "applied"); eq(b.where(two), "applied")
    p = w.get("people", per)
    eq(sorted(r["ref"] for r in p["event_ref_list"]), sorted([dated, b1]))
    eq(p["event_ref_list"][p["death_ref_index"]]["ref"], dated); eq(p["event_ref_list"][p["birth_ref_index"]]["ref"], b1)
    eq(w.get("events", empty), None); eq(w.get("events", b2), None)


def test_merge_event_with_different_details_keeps_a_note(w, b):
    p1, p2 = w.place("Middleborough, Plymouth, Massachusetts"), w.place("Middleborough, Plymouth, Massachusetts, USA")
    est, exact = w.event("Birth", (1867,), place=p1, description="age 83 in 1950"), w.event("Birth", (1866, 5, 30), place=p2, description="obituary")
    per, anc = w.person("Anton", "Estimate", "M", born=None, events=[est, exact])
    plain = b.file("merge_event", subject={"ancestry_id": anc}, keep=w.gid("events", exact), drop=w.gid("events", est))
    out = b.run(); eq(b.where(plain), "rejected", out); has(b.reason(plain), "allow_different")
    name = b.file("merge_event", subject={"ancestry_id": anc}, keep=w.gid("events", exact), drop=w.gid("events", est), allow_different=True)
    b.run(); out = b.approve_all()
    eq(b.where(name), "applied", out)
    ev = w.get("events", exact)
    eq(len(ev["note_list"]), 1)
    text = w.get("notes", ev["note_list"][0])["text"]["string"]
    has(text, "date 1867"); has(text, "Middleborough, Plymouth, Massachusetts"); has(text, "age 83 in 1950")
    eq(ev["date"]["dateval"][:3], [30, 5, 1866]); eq(ev["place"], p2)
    eq([r["ref"] for r in w.get("people", per)["event_ref_list"]], [exact])


def test_merge_event_on_a_family_and_a_shared_event(w, b):
    hu, _ = w.person("Hus", "Shared", "M", born=1850)
    wi, _ = w.person("Wi", "Shared", "F", born=1852)
    m1, m2 = w.event("Marriage", (1875, 6, 26)), w.event("Marriage", (1875,))
    fam = w.family(hu, wi, event_ref_list=[{"_class": "EventRef", "ref": h, "role": "Family", "private": False} for h in (m1, m2)])
    c1, c2 = w.event("Census", (1880,)), w.event("Census", (1880,))
    for person in (hu, wi):
        p = w.get("people", person)
        p["event_ref_list"] += [{"_class": "EventRef", "ref": h, "role": "Primary", "private": False} for h in ((c1, c2) if person == hu else (c2,))]
        w.send("PUT", f"people/{person}", json=p)
    one = b.file("merge_event", family={"gramps_id": w.gid("families", fam)}, keep=w.gid("events", m1), drop=w.gid("events", m2))
    two = b.file("merge_event", subject={"gramps_id": w.gid("people", hu)}, keep=w.gid("events", c1), drop=w.gid("events", c2))
    b.run(); out = b.approve_all()
    eq(b.where(one), "applied", out); eq(b.where(two), "applied", out)
    eq([r["ref"] for r in w.get("families", fam)["event_ref_list"]], [m1])
    eq([r["ref"] for r in w.get("people", wi)["event_ref_list"]][-1], c1, "the wife's reference moved to the kept event")
    eq(sum(1 for r in w.get("people", hu)["event_ref_list"] if r["ref"] == c1), 1)


def test_merge_event_by_type(w, b):
    """Without event IDs (a living person's events are not in the published bundle): 'type' means the two of that type."""
    place = w.place("Oconomowoc, Waukesha, Wisconsin")
    empty, dated = w.event("Death"), w.event("Death", (2018, 9, 20), place=place)
    b_bare, b_full = w.event("Birth", (1930, 10, 10)), w.event("Birth", (1930, 10, 10), place=place)
    per, anc = w.person("Norman", "Bytype", "M", born=None, events=[empty, dated, b_bare, b_full])
    one = b.file("merge_event", subject={"ancestry_id": anc}, type="Death")
    two = b.file("merge_event", subject={"ancestry_id": anc}, type="Birth")
    out = b.run(); eq(b.where(one), "held", out); eq(b.where(two), "held", out)
    listing = b.run("--held")
    has(listing, f"keep Death {w.gid('events', dated)} (20 Sep 2018, Oconomowoc, Waukesha, Wisconsin)")
    has(listing, f"fold in and delete Death {w.gid('events', empty)} (no date)")
    has(listing, f"keep Birth {w.gid('events', b_full)}")
    has(b.run("--approve", "all"), "APPLIED")
    p = w.get("people", per)
    eq(sorted(r["ref"] for r in p["event_ref_list"]), sorted([dated, b_full]), "the fuller of each pair is the one kept")
    eq(p["event_ref_list"][p["death_ref_index"]]["ref"], dated)
    # what 'type' alone may not decide
    c1, c2 = w.event("Birth", (1850, 3, 12)), w.event("Birth", (1850, 5, 19))
    r1, r2, r3 = (w.event("Residence", (y,)) for y in (1880, 1880, 1880))
    p1, p2 = w.place("Middleborough, Plymouth, Massachusetts"), w.place("Middleborough, Plymouth, Massachusetts, USA")
    j1, j2 = w.event("Baptism", (1691, 7, 1), place=p1), w.event("Baptism", (1691, 7, 1), place=p2)
    other, o_anc = w.person("Jabez", "Bytype", "M", born=None, events=[c1, c2, r1, r2, r3, j1, j2])
    cases = {
        b.file("merge_event", subject={"ancestry_id": o_anc}, type="Birth"): "different dates",
        b.file("merge_event", subject={"ancestry_id": o_anc}, type="Residence"): "not two",
        b.file("merge_event", subject={"ancestry_id": o_anc}, type="Burial"): "not two",
        b.file("merge_event", subject={"ancestry_id": o_anc}, type="Baptism"): "different place records",
        b.file("merge_event", subject={"ancestry_id": o_anc}): "give keep and drop",
        b.file("merge_event", subject={"ancestry_id": o_anc}, keep=w.gid("events", c1)): "give keep and drop",
    }
    out = b.run()
    for name, why in cases.items():
        eq(b.where(name), "rejected", f"{name}\n{out}"); has(b.reason(name), why)
    ok = b.file("merge_event", subject={"ancestry_id": o_anc}, type="Baptism", allow_different=True)
    b.run(); out = b.approve_all()
    eq(b.where(ok), "applied", out)
    left = w.events_of(other, "Baptism")
    eq([e["handle"] for e in left], [j1], "on a tie the one listed first is kept"); eq(len(left[0]["note_list"]), 1)


def test_update_event(w, b):
    """Anton Wacek's birth: 'about 1867' from a census, to be replaced by the date on a record."""
    bohemia = w.place("Bohemia")
    birth = w.event("Birth", (1867,), place=bohemia, modifier=3)
    res1, res2 = w.event("Residence", (1900,)), w.event("Residence", (1910,))
    per, anc = w.person("Anton", "Corrected", "M", born=None, events=[birth, res1, res2])
    other, other_anc = w.person("Some", "Oneelse", "M", born=1850)
    wife, _ = w.person("Anna", "Corrected", "F", born=1871)
    wedding = w.event("Marriage", (1890,))
    fam = w.family(per, wife, event_ref_list=[{"_class": "EventRef", "ref": wedding, "role": "Family", "private": False}])
    gid = {"gramps_id": w.gid("people", per)}
    was = snapshot(w, [per], [fam], [birth, res1, res2, wedding])
    bad = {
        b.file("update_event", subject=gid, event={"type": "Birth"}, set={"date": "1866-05-30"}): "citation missing",
        b.file("update_event", subject=gid, event={"type": "Residence"}, set={"description": "x"}): "has 2 Residence events",
        b.file("update_event", subject=gid, event={"type": "Burial"}, set={"description": "x"}): "has no Burial event",
        b.file("update_event", subject=gid, event="E99999", set={"description": "x"}): "not found",
        b.file("update_event", subject=gid, event=w.gid("events", w.events_of(other, "Birth")[0]["handle"]), set={"description": "x"}): "is not an event of",
        b.file("update_event", subject=gid, event={"type": "Birth"}, set={}): "set must name",
        b.file("update_event", subject=gid, event={"type": "Birth"}, set={"type": "Baptism"}): "may contain only",
        b.file("update_event", subject=gid, event={"type": "Birth"}, set={"date": ""}, citation=cit()): "may not be empty",
        b.file("update_event", subject=gid, event={"type": "Birth"}, set={"date_quality": "about"}): "needs set.date",
        b.file("update_event", subject=gid, set={"description": "x"}): "give its Gramps ID",
    }
    main = b.file("update_event", name="100-birth", subject=gid, event={"type": "Birth"},
                  set={"date": "1866-05-30", "place": "Kutná Hora, Bohemia"}, citation=cit(page="death record"))
    second = b.file("update_event", name="101-residence", subject=gid, event={"type": "Residence", "date": "1910"},
                    set={"description": "on the farm", "private": True})
    third = b.file("update_event", name="102-wedding", family={"gramps_id": w.gid("families", fam)}, event={"type": "Marriage"},
                   set={"date": "1890-02-11", "date_quality": "about"}, citation=cit(page="register"))
    out = b.run()
    for name, why in bad.items():
        eq(b.where(name), "rejected", f"{name}\n{out}"); has(b.reason(name), why)
    for name in (main, second, third):
        eq(b.where(name), "held", out)
    eq(snapshot(w, [per], [fam], [birth, res1, res2, wedding]), was, "nothing is changed before Chris approves")
    listing = b.run("--held")
    has(listing, f"CHANGE EVENT  Birth {w.gid('events', birth)} (about 1867, Bohemia)  of {w.gid('people', per)} Anton Corrected")
    has(listing, "date: about 1867  →  30 May 1866"); has(listing, "place: Bohemia  →  Kutná Hora, Bohemia")
    has(listing, "description: none  →  on the farm"); has(listing, "private: no  →  yes"); has(listing, "date: 1890  →  about 11 Feb 1890")
    has(b.run("--approve", "all"), "APPLIED")
    ev = w.get("events", birth)
    eq(ev["date"]["dateval"][:3], [30, 5, 1866]); eq(ev["date"]["modifier"], 0, "no longer 'about'")
    eq(w.get("places", ev["place"])["name"]["value"], "Kutná Hora, Bohemia"); eq(len(ev["citation_list"]), 1)
    eq(len(ev["note_list"]), 1)
    text = w.get("notes", ev["note_list"][0])["text"]["string"]
    has(text, "Before, it said: date about 1867; place Bohemia"); has(text, "100-birth.json")
    p = w.get("people", per)
    eq(p["event_ref_list"][p["birth_ref_index"]]["ref"], birth, "still the same event, and still the main birth")
    ev2 = w.get("events", res2)
    eq((ev2["description"], ev2["private"], ev2["note_list"]), ("on the farm", True, []), "nothing to remember, so no note")
    eq(w.get("events", res1)["description"], "", "the other residence was not touched")
    wed = w.get("events", wedding)
    eq((wed["date"]["dateval"][:3], wed["date"]["modifier"]), ([11, 2, 1890], 3))
    assert all((ap.UNDO / f"{n}.undo.json").exists() for n in (main, second, third))
    # the same again changes nothing and adds no second citation
    again = b.file("update_event", subject=gid, event={"type": "Birth"}, set={"date": "1866-05-30", "place": "Kutná Hora, Bohemia"},
                   citation=cit(page="death record"))
    b.run(); out = b.approve_all()
    has(out, "nothing (already so)"); eq(len(w.get("events", birth)["citation_list"]), 1)
    # and each can be put back
    for name in (again, third, second, main):
        has(b.run("--undo", name), ": undone")
    strip_tags = lambda s: re.sub(r'"tag_list": \[[^\]]*\]', '"tag_list": []', s)
    eq(strip_tags(snapshot(w, [per], [fam], [birth, res1, res2, wedding])), strip_tags(was), "everything is as it was")


def test_set_private(w, b):
    fa, fa_anc = w.person("Adam", "Household", "M", born=1850)
    mo, mo_anc = w.person("Beth", "Household", "F", born=1852)
    kid, kid_anc = w.person("Cora", "Household", "F", born=1880)
    fam = w.family(fa, mo, [kid])
    res = w.event("Residence", (1900,))
    p = w.get("people", fa); p["event_ref_list"].append({"_class": "EventRef", "ref": res, "role": "Primary", "private": False})
    w.send("PUT", f"people/{fa}", json=p)
    n1 = w.note("A research note that names somebody who is still alive and should not be on the public site.", on=fa)
    ids = [w.gid("people", fa), w.gid("families", fam), w.gid("events", res), w.gid("notes", n1)]
    bad = {
        b.file("set_private", records=[], private=True): "records must be a list",
        b.file("set_private", records=ids[0], private=True): "records must be a list",
        b.file("set_private", records=ids): "private must be true or false",
        b.file("set_private", records=ids, private="yes"): "private must be true or false",
        b.file("set_private", records=["I99999"], private=True): "I99999 not found",
        b.file("set_private", records=["N99999"], private=True): "N99999 not found",
        b.file("set_private", records=["S0001"], private=True): "name a person",
        b.file("set_private", records=[{"ancestry_id": "555"}], private=True): "person not found",
    }
    name = b.file("set_private", name="100-household", records=ids + [{"ancestry_id": kid_anc}, ids[0]], private=True)
    out = b.run()
    for n, why in bad.items():
        eq(b.where(n), "rejected", f"{n}\n{out}"); has(b.reason(n), why)
    eq(b.where(name), "held", out)
    listing = b.run("--held")
    has(listing, "MARK PRIVATE"); has(listing, f"person  {ids[0]} Adam Household (1850–)")
    has(listing, f"family  {ids[1]} ("); has(listing, "children: "); has(listing, "Cora Household")
    has(listing, f"event   Residence {ids[2]} (1900) of {ids[0]} Adam Household")
    has(listing, f"note    {ids[3]} on {ids[0]} Adam Household: “A research note that names somebody")
    has(b.run("--approve", "all"), "APPLIED")
    for ep, h in (("people", fa), ("families", fam), ("events", res), ("notes", n1), ("people", kid)):
        eq(w.get(ep, h)["private"], True, f"{ep} {h}")
    eq(w.get("people", mo)["private"], False, "only what was named")
    eq(w.get("people", fa)["tag_list"], [], "marking private is not research: no agent tags")
    if LIVE and os.environ.get("APPLIER_TEST_GUEST"):
        gu, gp = os.environ["APPLIER_TEST_GUEST"].split(":", 1)
        token = requests.post(f"{BASE}/token/", json={"username": gu, "password": gp}).json()["access_token"]
        guest = lambda path: requests.request("GET", f"{BASE}/{path}", headers={"Authorization": f"Bearer {token}"}).json()
        eq(guest(f"people/{fa}"), {}, "a guest (the public export) gets nothing for him now")
        eq(guest(f"notes/{n1}"), {}); eq(guest(f"families/{fam}"), {})
    # making public again, and filing the first one again
    back = b.file("set_private", name="200-public", records=[ids[0], ids[3]], private=False)
    b.run()
    listing = b.run("--held")
    has(listing, "MAKE PUBLIC"); has(listing, f"person  {ids[0]} Adam Household")
    has(b.run("--approve", "all"), "APPLIED")
    eq((w.get("people", fa)["private"], w.get("notes", n1)["private"], w.get("events", res)["private"]), (False, False, True))
    again = b.file("set_private", name="300-again", records=[ids[1], ids[2]], private=True)
    b.run(); listing = b.run("--held")
    eq(listing.count("(already so)"), 2, listing)
    has(b.run("--approve", "all"), "nothing (already so)")
    has(b.run("--undo", "200-public"), ": undone")
    eq((w.get("people", fa)["private"], w.get("notes", n1)["private"]), (True, True))


def test_add_family_with_the_marriage(w, b):
    """A couple and their marriage in one file."""
    hu, hu_anc = w.person("Georg", "Marriage", "M", born=1850)
    wi, wi_anc = w.person("Lotte", "Marriage", "F", born=1852)
    one = b.file("add_family", family={"father_ancestry_id": hu_anc, "mother_ancestry_id": wi_anc,
                                       "marriage": {"date": "1875-12-27", "place": "Manitowoc, Wisconsin"}}, citation=cit())
    bad = {
        b.file("add_family", family={"father_ancestry_id": hu_anc, "marriage": {"date": "1875-12-27"}}, citation=cit()): "needs both partners",
        b.file("add_family", family={"father_ancestry_id": hu_anc, "mother_ancestry_id": wi_anc, "marriage": {"date": "1875-12-27"}}): "citation missing",
        b.file("add_family", family={"father_ancestry_id": hu_anc, "mother_ancestry_id": wi_anc, "marriage": {}}, citation=cit()): "needs a date or a place",
        b.file("add_family", family={"father_ancestry_id": hu_anc, "mother_ancestry_id": wi_anc, "marriage": "1875"}, citation=cit()): "must be an object",
    }
    out = b.run()
    eq(b.where(one), "applied", out)
    for n, why in bad.items():
        eq(b.where(n), "rejected", f"{n}\n{out}"); has(b.reason(n), why)
    fams = w.families_of(hu)
    eq(len(fams), 1); eq(fams[0]["mother_handle"], wi)
    refs = fams[0]["event_ref_list"]
    eq(len(refs), 1); eq(refs[0]["role"], "Family")
    ev = w.get("events", refs[0]["ref"])
    eq((ev["type"], ev["date"]["dateval"][:3], len(ev["citation_list"])), ("Marriage", [27, 12, 1875], 1))
    eq(w.get("places", ev["place"])["name"]["value"], "Manitowoc, Wisconsin")
    # filed again from a second source: the same marriage, one more citation; the family is not doubled
    two = b.file("add_family", family={"father_ancestry_id": hu_anc, "mother_ancestry_id": wi_anc, "marriage": {"date": "1875"}},
                 citation=cit(page="entry 2"))
    b.run(); eq(b.where(two), "applied")
    fams = w.families_of(hu)
    eq(len(fams), 1); eq(len(fams[0]["event_ref_list"]), 1)
    eq(len(w.get("events", refs[0]["ref"])["citation_list"]), 2)
    # a marriage long ago shows that two people without dates are not living; a recent one waits for Chris
    a, a_anc = w.person("Undated", "Groom", "M", born=None)
    c, c_anc = w.person("Undated", "Bride", "F", born=None)
    old = b.file("add_family", family={"father_ancestry_id": a_anc, "mother_ancestry_id": c_anc, "marriage": {"date": "1875"}}, citation=cit())
    d, d_anc = w.person("Undated", "Husband", "M", born=None)
    e, e_anc = w.person("Undated", "Wife", "F", born=None)
    recent = b.file("add_family", family={"father_ancestry_id": d_anc, "mother_ancestry_id": e_anc, "private": True,
                                          "marriage": {"date": "1947-12-27", "place": "Manitowoc"}}, citation=cit())
    out = b.run()
    eq(b.where(old), "applied", out); eq(b.where(recent), "held", out)
    later = b.file("add_note", subject={"ancestry_id": a_anc}, note="a later note about the groom of 1875")
    b.run(); eq(b.where(later), "applied", "once the marriage is in the tree it dates them for later files too")
    listing = b.run("--held")
    has(listing, "married 27 Dec 1947 in Manitowoc"); has(listing, "[private]")
    has(b.run("--approve", "all"), "APPLIED")
    fam = w.families_of(d)[0]
    eq(fam["private"], True); eq(w.get("events", fam["event_ref_list"][0]["ref"])["private"], True, "the marriage of a private family is private")


def test_names_from_elsewhere_get_a_pointer(w, b):
    """An agent that guesses an action's name is told what it is called here."""
    fa, fa_anc = w.person("Guess", "Work", "M", born=1850)
    cases = {
        b.file("link_child", subject={"ancestry_id": fa_anc}): "use update_family with add_children",
        b.file("rename_person", subject={"ancestry_id": fa_anc}): "use update_person",
        b.file("merge_families", keep="F0001", drop="F0002"): "use merge_family",
        b.file("delete_event", subject={"ancestry_id": fa_anc}): "use merge_event",
        b.file("set_family_parents", family="F0001"): "use update_family",
    }
    out = b.run()
    for name, why in cases.items():
        eq(b.where(name), "rejected", out); has(b.reason(name), why); has(b.reason(name), "PROPOSALS.md")


# ---------------------------------------------------------------- undo, failures, housekeeping
def snapshot(w, people=(), families=(), events=()):
    def clean(o):
        return None if o is None else {k: v for k, v in o.items() if k != "change"}
    return json.dumps([[clean(w.get(ep, h)) for h in hs] for ep, hs in (("people", people), ("families", families), ("events", events))], sort_keys=True)


def test_undo_puts_everything_back(w, b):
    fa, _ = w.person("Anton", "Undo", "M", born=1866)
    mo, _ = w.person("Anna", "Undo", "F", born=1871)
    k1, _ = w.person("Lillian", "Undo", "F", born=1895)
    k2, _ = w.person("Ella", "Undo", "F", born=1900)
    keep, drop = w.family(fa, None, [k1]), w.family(fa, mo, [k2])
    e1, e2 = w.event("Death", (1953, 1, 15)), w.event("Death")
    per, anc = w.person("Events", "Undo", "M", born=1866, events=[e2, e1])
    was = snapshot(w, [fa, mo, k1, k2, per], [keep, drop], [e1, e2])
    m1 = b.file("merge_family", keep=w.gid("families", keep), drop=w.gid("families", drop))
    m2 = b.file("merge_event", subject={"ancestry_id": anc}, keep=w.gid("events", e1), drop=w.gid("events", e2))
    m3 = b.file("update_person", subject={"ancestry_id": anc}, set={"surname": "Changed", "private": True})
    b.run(); out = b.approve_all()
    eq(out.count("APPLIED"), 3, out)
    assert snapshot(w, [fa, mo, k1, k2, per], [keep, drop], [e1, e2]) != was
    for name in (m3, m2, m1):
        has(b.run("--undo", name), ": undone")
        eq(b.where(name), "rejected")
    now = snapshot(w, [fa, mo, k1, k2, per], [keep, drop], [e1, e2])
    strip_tags = lambda s: re.sub(r'"tag_list": \[[^\]]*\]', '"tag_list": []', s)
    eq(strip_tags(now), strip_tags(was), "everything is as it was")


def test_undo_leaves_what_later_proposals_share(w, b):
    """The first proposal to cite a source creates it; later ones use it. Undoing the first must not take it away."""
    per, anc = w.person("First", "Citer", "M", born=1834)
    other, other_anc = w.person("Later", "Citer", "M", born=1840)
    once = anc_id()[-6:]                    # new names each time: on a real scratch tree the last run's are still there
    shared = dict(source_title=f"A book both proposals cite {once}", repository=f"A shared archive {once}")
    town = f"A town both use {once}"
    first = b.file("update_event", name="001-first", subject={"ancestry_id": anc}, event={"type": "Birth"},
                   set={"date": "1834-02-03", "place": town}, citation=cit(page="p. 1", **shared))
    b.run(); out = b.approve_all(); eq(b.where(first), "applied", out)
    second = b.file("add_event", name="002-second", subject={"ancestry_id": other_anc},
                    event={"type": "Death", "date": "1900", "place": town}, citation=cit(page="p. 2", **shared))
    has(b.run(), "APPLIED")
    death = w.events_of(other, "Death")[0]
    c = w.get("citations", death["citation_list"][0])
    out = b.run("--undo", "001-first")
    has(out, ": undone"); has(out, "left in place")
    birth = w.events_of(per, "Birth")[0]
    eq((birth["date"]["dateval"][:3], birth["place"], birth["citation_list"], birth["note_list"]), ([0, 0, 1834], "", [], []), "the event is as it was")
    src = w.get("sources", c["source_handle"])
    assert src and src["title"] == shared["source_title"], "the second proposal still has its source"
    assert w.get("repositories", src["reporef_list"][0]["ref"]), "and the source its repository"
    assert w.get("places", death["place"]), "and its event the place"
    eq(w.get("citations", death["citation_list"][0])["page"], "p. 2")


def test_undo_refuses_if_the_tree_has_moved_on(w, b):
    per, anc = w.person("Moved", "On", "M", born=1866)
    name = b.file("update_person", subject={"ancestry_id": anc}, set={"surname": "First"})
    b.run(); b.approve_all()
    p = w.get("people", per); p["primary_name"]["first_name"] = "Later"; w.send("PUT", f"people/{per}", json=p)
    out = b.run("--undo", name)
    has(out, "NOT undone"); has(out, "has been changed since")
    eq(ap.name_text(w.get("people", per)["primary_name"]), "Later First")


def test_a_failure_in_the_middle_is_rolled_back(w, b):
    fa, fa_anc = w.person("Roll", "Back", "M", born=1850)
    was = snapshot(w, [fa])
    kid = anc_id()
    name = b.file("add_person", person=new_person(kid, "Half", "Done", birth={"date": "1880", "place": "Nowhere"}, death={"date": "1940"}),
                  relations={"father_ancestry_id": fa_anc}, citation=cit())
    later = b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1910"}, citation=cit())
    FAULTS.append(("POST", "families/", 500, "boom", 0))
    out = b.run()
    has(out, "STOP at"); has(out, "rolling back"); lacks(out, "APPLIED")
    eq(b.where(name), "inbox", "it stays in the inbox for the next run"); eq(b.where(later), "inbox", "and the queue stops behind it")
    eq(w.by_anc(kid), None, "the half-made person is gone")
    eq(snapshot(w, [fa]), was)
    out = b.run()                                            # the next run, with nothing in the way, does it all
    eq(out.count("APPLIED"), 2, out)
    assert w.by_anc(kid)


def test_a_failed_change_is_rolled_back(w, b):
    fa, _ = w.person("Anton", "Fail", "M", born=1866)
    mo, mo_anc = w.person("Anna", "Fail", "F", born=1871)
    k1, k1_anc = w.person("Kid", "Fail", "F", born=1895)
    fam = w.family(fa, None)
    was = snapshot(w, [fa, mo, k1], [fam])
    name = b.file("update_family", family=w.gid("families", fam), set={"mother": {"ancestry_id": mo_anc}}, add_children=[{"ancestry_id": k1_anc}], citation=cit())
    b.run(); b.run("--held")
    FAULTS.append(("GET", f"people/{k1}", 500, "boom", 0))  # fails after the family was changed, while the applier checks the result
    out = b.run("--approve", "all")
    has(out, "STOP at"); has(out, "rolling back"); eq(b.where(name), "held")
    strip_tags = lambda s: re.sub(r'"tag_list": \[[^\]]*\]', '"tag_list": []', s)
    eq(strip_tags(snapshot(w, [fa, mo, k1], [fam])), strip_tags(was), "the family is as it was")
    out = b.run()
    has(out, "APPLIED"); eq(w.get("families", fam)["mother_handle"], mo)


def test_a_failure_that_will_not_go_away_rejects_and_the_queue_goes_on(w, b):
    fa, fa_anc = w.person("Queue", "Goes", "M", born=1850)
    was = snapshot(w, [fa])
    kid = anc_id()
    bad = b.file("add_person", name="001-bad", person=new_person(kid, "Half", "Made", birth={"date": "1880"}),
                 relations={"father_ancestry_id": fa_anc}, citation=cit())
    good = b.file("add_event", name="002-good", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1910"}, citation=cit())
    wrong_gid = b.file("update_family", name="003-nofamily", family="F99999", set={"private": True})
    FAULTS.append(("POST", "families/", 400, {"error": {"code": 400, "message": "Schema validation failed"}}, 0))
    out = b.run()
    has(out, "FAILED 001-bad"); has(out, "rolling back"); has(out, "REJECT 001-bad"); lacks(out, "STOP")
    eq(b.where(bad), "rejected"); has(b.reason(bad), "Schema validation failed")
    eq(w.by_anc(kid), None, "nothing of the failed proposal is left")
    eq(b.where(good), "applied", "and the next one was still applied")
    eq(len(w.events_of(fa, "Death")), 1); eq(len(w.families_of(fa)), 0)
    assert json.loads(was)[0][0]["event_ref_list"] == w.get("people", fa)["event_ref_list"][:1]
    eq(b.where(wrong_gid), "rejected"); has(b.reason(wrong_gid), "family F99999 not found")


def test_a_date_that_cannot_be_read_rejects_that_file_only(w, b):
    """Before 0.3.1 one unreadable date stopped the whole queue, or the run itself, every half hour."""
    fa, fa_anc = w.person("Known", "Father", "M", born=1850)
    ev = lambda **e: dict(subject={"ancestry_id": fa_anc}, event={"type": "Residence", **e}, citation=cit())
    cases = {
        b.file("add_person", person=new_person(anc_id(), "Bad", "Date", birth={"date": "abt 1880"}), relations={"father_ancestry_id": fa_anc}): "is not a date",
        b.file("add_person", person=new_person(anc_id(), "Bad", "Shape", birth="1880"), relations={"father_ancestry_id": fa_anc}): "must be an object",
        b.file("add_event", **ev(date="June 1910")): "is not a date",
        b.file("add_event", **ev(date="1910-13-45")): "is not a date",
        b.file("add_event", **ev(date="1910-02-30")): "is not a date",
        b.file("add_event", **ev(date=1910)): "in quotes",
        b.file("add_event", **ev(date="2999")): "in the future",
        b.file("add_event", **ev(date="1910", date_quality="circa")): "must be one of",
        b.file("add_event", **ev(date="1910", date_quality="between")): "not supported",
        b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Residence", "date": "1910"}, citation=cit(date="16 April 1979")): "citation.date",
        b.file("update_event", subject={"ancestry_id": fa_anc}, event={"type": "Birth"}, set={"date": "30 May 1866"}, citation=cit()): "set.date",
        b.file("add_family", family={"father_ancestry_id": fa_anc, "marriage": {"date": "1875-6-26"}}, citation=cit()): "family.marriage.date",
    }
    good = b.file("add_event", name="zzz-good", **ev(date="1904-02-29", date_quality="about"))
    out = b.run()
    for name, why in cases.items():
        eq(b.where(name), "rejected", f"{name}\n{out}"); has(b.reason(name), why)
    eq(b.where(good), "applied", "the file behind them was still applied"); lacks(out, "STOP"); lacks(out, "Traceback")
    eq(len(w.get("people", fa)["event_ref_list"]), 2, "and nothing of the bad ones was written")


def test_a_fault_in_one_file_does_not_block_the_queue_but_a_lost_connection_does(w, b):
    fa, fa_anc = w.person("Queue", "Robust", "M", born=1850)
    was = snapshot(w, [fa])
    real = ap.place_handle
    def place_handle(name, *a, **kw):
        if name == "BOOM": raise KeyError("something the applier did not foresee")
        if name == "DOWN": raise requests.exceptions.ConnectionError("connection refused")
        return real(name, *a, **kw)
    ap.place_handle = place_handle
    try:
        bad = b.file("add_event", name="001-bad", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1910", "place": "BOOM"}, citation=cit())
        good = b.file("add_event", name="002-good", subject={"ancestry_id": fa_anc}, event={"type": "Burial", "date": "1910"}, citation=cit(page="p2"))
        out = b.run()
        has(out, "FAILED 001-bad"); has(out, "rolling back"); lacks(out, "STOP")
        eq(b.where(bad), "rejected"); has(b.reason(bad), "KeyError"); has(b.reason(bad), "tell Claude")
        eq(b.where(good), "applied"); eq(len(w.events_of(fa, "Death")), 0); eq(len(w.events_of(fa, "Burial")), 1)
        down = b.file("add_event", name="003-down", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1911", "place": "DOWN"}, citation=cit(page="p3"))
        behind = b.file("add_event", name="004-behind", subject={"ancestry_id": fa_anc}, event={"type": "Residence", "date": "1900"}, citation=cit(page="p4"))
        out = b.run()
        has(out, "STOP at 003-down"); eq(b.where(down), "inbox", "it is tried again next time"); eq(b.where(behind), "inbox")
    finally:
        ap.place_handle = real
    out = b.run()
    eq(out.count("APPLIED"), 2, out)


def test_busy_database_and_expired_token(w, b):
    fa, fa_anc = w.person("Busy", "Database", "M", born=1850)
    real_time, naps = ap.time, []
    ap.time = type("T", (), {"sleep": staticmethod(naps.append), "time": staticmethod(time.time)})
    try:
        FAULTS.append(("PUT", f"people/{fa}", 500, "sqlite3.OperationalError: database is locked", 0))
        FAULTS.append(("GET", "events/", 401, {"message": "Token has expired"}, 0))
        name = b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1910"}, citation=cit())
        out = b.run()
    finally:
        ap.time = real_time
    has(out, "APPLIED"); eq(b.where(name), "applied"); assert 2 in naps, naps
    eq(len(w.events_of(fa, "Death")), 1)


def test_only_one_run_at_a_time(w, b):
    fa, fa_anc = w.person("Lock", "Ed", "M", born=1850)
    name = b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1910"}, citation=cit())
    held = ap.take_lock(wait=0)
    real = ap.take_lock
    ap.take_lock = lambda wait=0: real(wait=0)
    try:
        out = b.run()
    finally:
        ap.take_lock = real
        held.close()
    has(out, "another applier run"); eq(b.where(name), "inbox")
    has(b.run(), "APPLIED")


def test_verdicts_and_repair_links_still_work(w, b):
    fa, fa_anc = w.person("Verdict", "Case", "M", born=1850)
    b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Death", "date": "1910"}, citation=cit())
    b.run()
    cit_h = w.events_of(fa, "Death")[0]["citation_list"][0]
    (ap.VALID / "v1.json").write_text(json.dumps({"gramps_handle": cit_h, "verdict": "supports", "flags": [], "confidence_suggested": 3,
                                                  "match_points": 4, "note": "ok"}))
    out = b.run("--verdicts")
    has(out, "ai-validated"); eq(w.get("citations", cit_h)["confidence"], 3)
    p = w.get("people", fa); p["family_list"].append("nosuchfamily"); w.send("PUT", f"people/{fa}", json=p)
    has(b.run("--repair-links"), "1 people")
    eq(w.get("people", fa)["family_list"], [])


def test_private_records_are_invisible_to_a_guest(w, b):
    """Only meaningful against the real API: the public export logs in as a Guest."""
    if not LIVE or not os.environ.get("APPLIER_TEST_GUEST"):
        return "skipped (needs the real API and APPLIER_TEST_GUEST=user:password)"
    gu, gp = os.environ["APPLIER_TEST_GUEST"].split(":", 1)
    fa, fa_anc = w.person("Public", "Father", "M", born=1850)
    kid = anc_id()
    b.file("add_person", person=new_person(kid, "Hidden", "Child", birth={"date": "1990"}, private=True), relations={"father_ancestry_id": fa_anc})
    b.file("add_event", subject={"ancestry_id": fa_anc}, event={"type": "Residence", "date": "1900", "private": True}, citation=cit())
    b.run(); b.approve_all()
    token = requests.post(f"{BASE}/token/", json={"username": gu, "password": gp}).json()["access_token"]
    guest = lambda path: requests.request("GET", f"{BASE}/{path}", headers={"Authorization": f"Bearer {token}"}).json()
    p = w.by_anc(kid)
    eq(guest(f"people/{p['handle']}"), {}, "a guest gets nothing for the private person")
    eq(guest(f"families/{p['parent_family_list'][0]}")["child_ref_list"], [], "nor the link to them")
    eq(len(guest(f"people/{fa}")["event_ref_list"]), 1, "nor the private event (only the birth is left)")


def main():
    global MOCK
    wanted = sys.argv[1:]
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and (not wanted or any(x in n for x in wanted))]
    failed = 0
    started = time.time()
    for name, fn in tests:
        MOCK = MockGramps()
        FAULTS.clear()
        try:
            note = fn(World(), Box())
            print(f"ok    {name}" + (f"  — {note}" if note else ""))
        except Exception:
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} passed, {failed} failed ({'real API at ' + LIVE if LIVE else 'imitation API'}, "
          f"applier {ap.VERSION}, {time.time() - started:.0f}s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
