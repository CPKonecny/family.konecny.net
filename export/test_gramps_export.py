#!/usr/bin/env python3
"""Tests for export/gramps_export.py and what site/build.py makes of its bundle.

    python3 export/test_gramps_export.py

Builds a small invented tree as a raw cache (the form --raw-dir writes), runs the export on it with
--from-raw, then builds the site from the bundle. Every name here is invented. Covers:

  * children under 18 become the "Living child" placeholder: tagged by the applier, or with a visible birth;
    no name, gender or page; text naming them is held back (notes, event descriptions, media descriptions),
    and so are the parts of content/ that name them, which the build then leaves out;
  * adopted, step and foster children are labelled, in living families too, but never a child under 18;
  * the leak audit passes on all that, and fails when a child's name gets into the bundle another way.

Needs only the standard library for the export; the site build needs jinja2 and markdown
(site/requirements.txt), and that part is skipped when they are missing.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import re
import unicodedata
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
spec = importlib.util.spec_from_file_location("gramps_export", HERE / "gramps_export.py")
gx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gx)

TODAY = "2026-11-23"


def fold(t):
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c)).lower()


# ---------------------------------------------------------------- an invented tree
class Tree:
    def __init__(self):
        self.t = {k: [] for k in gx.TABLES}
        self.n = {k: 0 for k in gx.TABLES}
        self.tag_h = {}

    def _h(self, table):
        self.n[table] += 1
        return f"{table[:3]}{self.n[table]:04d}handle"

    def tag(self, name):
        if name not in self.tag_h:
            h = self._h("tags")
            self.tag_h[name] = h
            self.t["tags"].append({"handle": h, "name": name})
        return self.tag_h[name]

    def event(self, etype, y=0, m=0, d=0, desc="", private=False, modifier=0, quality=0):
        h = self._h("events")
        date = {"dateval": [d, m, y, False], "modifier": modifier, "quality": quality, "calendar": 0,
                "sortval": (y * 400 + m * 31 + d) if y else 0, "text": ""} if y else None
        self.t["events"].append({"handle": h, "gramps_id": f"E{self.n['events']:04d}", "type": etype, "date": date,
                                 "place": "", "description": desc, "private": private, "citation_list": [],
                                 "note_list": [], "media_list": [], "attribute_list": [], "tag_list": [], "change": 1})
        return h

    def person(self, first, surname, gender=1, events=(), tags=(), private=False, notes=(), media=(), alt=()):
        """alt: (surname, type) or (surname, type, year) for each alternate name, e.g. ("Lindqvist", "Married Name")"""
        h = self._h("people")
        alternates = []
        for a in alt:
            sur, typ, yr = (tuple(a) + (0,))[:3]
            alternates.append({"first_name": first, "surname_list": [{"surname": sur, "primary": True, "prefix": ""}],
                               "title": "", "suffix": "", "call": "", "nick": "", "type": typ, "private": False,
                               "date": {"dateval": [0, 0, yr, False], "modifier": 0, "quality": 0, "calendar": 0,
                                        "sortval": yr * 400, "text": ""} if yr else None,
                               "citation_list": [], "note_list": []})
        self.t["people"].append({
            "handle": h, "gramps_id": f"I{self.n['people']:04d}", "gender": gender, "private": private,
            "primary_name": {"first_name": first, "surname_list": [{"surname": surname, "primary": True, "prefix": ""}],
                             "title": "", "suffix": "", "call": "", "nick": "", "type": "Birth Name",
                             "citation_list": [], "note_list": [], "private": False},
            "alternate_names": alternates, "event_ref_list": [{"ref": e, "role": "Primary", "private": False} for e in events],
            "family_list": [], "parent_family_list": [], "tag_list": [self.tag(t) for t in tags],
            "media_list": [{"ref": m, "private": False} for m in media], "citation_list": [],
            "note_list": list(notes), "attribute_list": [], "urls": [], "change": 1})
        return h

    def family(self, father, mother, children=(), events=()):
        """children: handle, or (handle, frel, mrel)"""
        h = self._h("families")
        refs = []
        for c in children:
            c, fr, mr = (c, "Birth", "Birth") if isinstance(c, str) else c
            refs.append({"ref": c, "frel": fr, "mrel": mr, "private": False, "citation_list": [], "note_list": []})
        self.t["families"].append({
            "handle": h, "gramps_id": f"F{self.n['families']:04d}", "father_handle": father or "", "mother_handle": mother or "",
            "child_ref_list": refs, "event_ref_list": [{"ref": e, "role": "Family", "private": False} for e in events],
            "type": "Married", "private": False, "tag_list": [], "media_list": [], "citation_list": [], "note_list": [],
            "attribute_list": [], "change": 1})
        for p in self.t["people"]:
            if p["handle"] in (father, mother):
                p["family_list"].append(h)
            if p["handle"] in [r["ref"] for r in refs]:
                p["parent_family_list"].append(h)
        return h

    def note(self, text, private=False):
        h = self._h("notes")
        self.t["notes"].append({"handle": h, "gramps_id": f"N{self.n['notes']:04d}", "type": "General",
                                "text": {"string": text, "tags": []}, "private": private, "tag_list": [], "change": 1})
        return h

    def medium(self, desc):
        h = self._h("media")
        self.t["media"].append({"handle": h, "gramps_id": f"O{self.n['media']:04d}", "path": "photos/x.jpg",
                                "mime": "image/jpeg", "desc": desc, "private": False, "tag_list": [],
                                "citation_list": [], "note_list": [], "attribute_list": [], "date": None, "change": 1})
        return h

    def gid(self, h):
        for table in ("people", "families", "events", "notes", "media"):
            for o in self.t[table]:
                if o["handle"] == h:
                    return o["gramps_id"]

    def write(self, raw_dir):
        os.makedirs(raw_dir, exist_ok=True)
        for k, v in self.t.items():
            with open(os.path.join(raw_dir, f"{k}.json"), "w", encoding="utf-8") as f:
                json.dump(v, f)
        for k in ("metadata", "living", "living_dates"):
            with open(os.path.join(raw_dir, f"{k}.json"), "w", encoding="utf-8") as f:
                json.dump({}, f)


def lindqvist():
    """Grandfather Anton (dead); his son Erik and Erik's wife Maja (living adults); their children:
    Tove (tagged minor, her birth private so the export can't see it), Nils (born 2015, adopted, not tagged yet),
    Ingrid (an adult, adopted), and young Anton, who has his dead grandfather's name."""
    t = Tree()
    obit = t.note("Anton Lindqvist farmed by the lake. He is survived by his grandchildren Nils Lindqvist and Lindqvist, Tove.")
    plain = t.note("Anton Lindqvist farmed by the lake for forty years.")
    released = t.note("Anton taught his granddaughter Tove Lindqvist to sail.")      # in overrides.public_text: still held
    photo = t.medium("Tove Lindqvist at the farm")
    farm = t.medium("The Lindqvist farm, 1950")
    anton = t.person("Anton", "Lindqvist", events=[t.event("Birth", 1900, 4, 2),
                                                   t.event("Death", 1970, 1, 9, desc="Mourned by Tove Lindqvist and the parish")],
                     notes=[obit, plain, released], media=[photo, farm])
    erik = t.person("Erik", "Lindqvist", events=[t.event("Birth", 1975, 6, 1)])
    maja = t.person("Maja", "Holm", gender=0, alt=[("Lindqvist", "Married Name", 1999)])   # living adult, married name
    # deceased, married twice: the heading takes the last married name, the also-line the earlier one
    ebba = t.person("Ebba", "Berg", gender=0, alt=[("Nyström", "Married Name"), ("Lindqvist", "Married Name")],
                    events=[t.event("Birth", 1905, 2, 2), t.event("Death", 1980, 3, 3)])
    # living, filed with the married surname as the main one: shown as filed until the birth name is known
    sara = t.person("Sara", "Lindqvist", gender=0, alt=[("Lindqvist", "Married Name")])
    tove = t.person("Tove", "Lindqvist", gender=0, tags=["minor"])
    nils = t.person("Nils", "Lindqvist", events=[t.event("Birth", 2015, 3, 2)])
    ingrid = t.person("Ingrid", "Lindqvist", gender=0, events=[t.event("Birth", 1990, 8, 8)])
    young_anton = t.person("Anton", "Lindqvist", events=[t.event("Birth", 2016, 5, 5)])
    almost = t.person("Saga", "Lindqvist", gender=0, events=[t.event("Birth", 2008, 11, 23)])   # 18 today
    t.family(anton, ebba, [erik])
    t.family("", sara, [])
    t.family(erik, maja, [tove, (nils, "Adopted", "Adopted"), (ingrid, "Adopted", "Adopted"), young_anton, almost])
    return t, {"anton": anton, "erik": erik, "maja": maja, "tove": tove, "nils": nils, "ingrid": ingrid,
               "young_anton": young_anton, "saga": almost, "ebba": ebba, "sara": sara, "obit": obit, "plain": plain, "released": released,
               "photo": photo, "farm": farm}


STORIES = {"title": "Stories", "summary": "Stories of the family.", "further": [],
           "sections": [{"status": "proven", "id": "farm", "heading": "The farm", "kind": "entries", "after": [],
                         "entries": [{"title": "The boat", "detail": "Anton farmed by the lake; Tove Lindqvist still has his boat."}]},
                        {"status": "proven", "id": "lake", "heading": "The lake", "kind": "entries", "after": [],
                         "entries": [{"title": "The farm", "detail": "Anton farmed by the lake for forty years."}]}]}
BOARD = {"_readme": "Nils Lindqvist is not published from here", "reviewed": "2026-11-20", "items": [
    {"id": "R1", "status": "open", "line": "konecny", "title": "Find the school records", "summary": "Where Nils Lindqvist went to school",
     "detail": "", "owner": "Karel", "people": [], "since": "2026-11-01"},
    {"id": "R2", "status": "open", "line": "konecny", "title": "Anton's farm deed", "summary": "The 1931 deed",
     "detail": "", "owner": "Karel", "people": [], "since": "2026-11-01"}]}


def run_export(t, work, overrides=None, content=True):
    raw, out, home = (os.path.join(work, x) for x in ("raw", "out", "home"))
    shutil.rmtree(work, ignore_errors=True)
    t.write(raw)
    os.makedirs(os.path.join(home, "gramps-tools"))
    os.makedirs(out)
    if content:
        os.makedirs(os.path.join(out, "content", "stories"))
        os.makedirs(os.path.join(out, "content", "research"))
        with open(os.path.join(out, "content", "stories", "stories.json"), "w", encoding="utf-8") as f:
            json.dump(STORIES, f)
        with open(os.path.join(out, "content", "research", "board.json"), "w", encoding="utf-8") as f:
            json.dump(BOARD, f)
        with open(os.path.join(out, "content", "about.md"), "w", encoding="utf-8") as f:
            f.write("About the Lindqvists.\n")
        with open(os.path.join(out, "content", "research", "README.md"), "w", encoding="utf-8") as f:
            f.write("Researchers: Tove Lindqvist is under 18, never name her.\n")     # not published: not scanned
    ov = os.path.join(work, "overrides.json")
    with open(ov, "w", encoding="utf-8") as f:
        json.dump(overrides or {}, f)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = gx.main(["--from-raw", raw, "--out", out, "--overrides", ov, "--today", TODAY, "--no-living-api"])
    finally:
        if old_home is not None:
            os.environ["HOME"] = old_home
    bundle_path = os.path.join(out, "data", "bundle.json")
    bundle = json.load(open(bundle_path, encoding="utf-8")) if os.path.exists(bundle_path) else None
    text = open(bundle_path, encoding="utf-8").read() if bundle else ""
    report = buf_out.getvalue()
    return rc, bundle, text, report, buf_err.getvalue(), out


# ---------------------------------------------------------------- tests
def test_children_under_18_are_a_placeholder(work):
    t, h = lindqvist()
    rc, B, text, report, err, _out = run_export(t, work)
    assert rc == 0, report[-3000:] + err
    P = B["people"]
    for key in ("tove", "nils", "young_anton"):
        p = P[t.gid(h[key])]
        assert p.get("minor") is True and p["name"] == gx.MINOR_NAME and p["gender"] == "U", p
        assert set(p) == {"id", "handle", "status", "name", "gender", "parents", "families", "minor"}, sorted(p)
    assert P[t.gid(h["saga"])]["name"]["display"] == "Saga Lindqvist", "18 today: shown by name"
    assert P[t.gid(h["ingrid"])]["name"]["display"] == "Ingrid Lindqvist"
    assert P[t.gid(h["erik"])]["name"]["display"] == "Erik Lindqvist"
    low = fold(text)
    for name in ("tove", "nils"):
        assert name not in low, f"{name} appears in the bundle"
    # young Anton has his grandfather's name, which the site shows for the grandfather: that is allowed
    assert "anton lindqvist" in low
    assert "Living child" in report and "Tove Lindqvist" in report, "the private report lists the children by name"
    assert "tagged minor" in report and "born by 2015-03-02" in report


def test_text_that_names_a_child_is_held_back(work):
    t, h = lindqvist()
    rc, B, _text, report, err, _out = run_export(t, work, overrides={"public_text": {t.gid(h["released"]): "fine"}})
    assert rc == 0, report[-3000:] + err
    notes = {n["id"] for n in B["notes"].values()}
    assert t.gid(h["plain"]) in notes
    assert t.gid(h["obit"]) not in notes and t.gid(h["released"]) not in notes, "public_text does not release a child's name"
    assert all("Mourned" not in (e.get("description") or "") for e in B["events"].values()), "the death event names Tove"
    media = {m["id"] for m in B["media"].values()}
    assert t.gid(h["farm"]) in media and t.gid(h["photo"]) not in media
    assert "public_text does not release these" in report and "description names a child under 18" in report


def test_content_that_names_a_child_is_marked_by_place(work):
    t, h = lindqvist()
    rc, B, _text, report, err, _out = run_export(t, work)
    assert rc == 0, report[-3000:] + err
    got = sorted((e["file"], e["id"]) for e in B["meta"]["minor_text"])
    assert got == [("content/research/board.json", "R1"), ("content/stories/stories.json", "farm")], got
    assert "README" not in json.dumps(B["meta"]["minor_text"])
    assert "names a child under 18" in err and "stories.json (farm)" in report


def test_a_living_adult_carries_her_married_name_and_nothing_more(work):
    t, h = lindqvist()
    rc, B, _text, report, err, _out = run_export(t, work)
    assert rc == 0, report[-3000:] + err
    maja = B["people"][t.gid(h["maja"])]
    assert maja["status"] == "living" and maja["name"]["display"] == "Maja Holm"
    assert [(a["surname"], a["type"]) for a in maja["alt_names"]] == [("Lindqvist", "Married Name")], maja
    assert "date" not in maja["alt_names"][0] and "citations" not in maja["alt_names"][0]
    ebba = B["people"][t.gid(h["ebba"])]
    assert ebba["status"] == "full" and [a["surname"] for a in ebba["alt_names"]] == ["Nyström", "Lindqvist"]
    for key in ("tove", "nils", "young_anton"):
        assert "alt_names" not in B["people"][t.gid(h[key])]


def test_adopted_is_a_relationship_but_not_for_a_child(work):
    t, h = lindqvist()
    rc, B, _text, report, err, _out = run_export(t, work)
    assert rc == 0, report[-3000:] + err
    fam = next(f for f in B["families"].values() if f["father"] == t.gid(h["erik"]))
    assert fam["status"] == "living"
    kids = {c["ref"]: c for c in fam["children"]}
    assert kids[t.gid(h["ingrid"])] == {"ref": t.gid(h["ingrid"]), "frel": "Adopted", "mrel": "Adopted"}
    assert kids[t.gid(h["nils"])] == {"ref": t.gid(h["nils"])}, "a child under 18 carries nothing but the link"


def test_a_tagged_child_is_never_published_in_full(work):
    t, h = lindqvist()
    # even if someone lists her as deceased: without a death event the tag wins
    rc, B, _text, report, err, _out = run_export(t, work, overrides={"deceased": {t.gid(h["tove"]): "mistake"}})
    assert rc == 0, report[-3000:] + err
    assert B["people"][t.gid(h["tove"])].get("minor") is True
    assert "overrides.deceased but tagged minor" in report


def test_the_audit_fails_if_a_childs_name_gets_in_another_way(work):
    t, h = lindqvist()
    # a place name carrying the child's name: nothing holds places back, so the audit must catch it
    t.t["places"].append({"handle": "pla0001handle", "gramps_id": "P0001", "name": {"value": "Tove Lindqvist Farm"},
                          "title": "", "place_type": "Farm", "placeref_list": [], "alt_names": [], "lat": "", "long": "",
                          "private": False, "citation_list": [], "note_list": [], "media_list": [], "urls": [],
                          "tag_list": [], "change": 1})
    anton = next(p for p in t.t["people"] if p["handle"] == h["anton"])
    birth = next(e for e in t.t["events"] if e["handle"] == anton["event_ref_list"][0]["ref"])
    birth["place"] = "pla0001handle"
    rc, B, _text, report, err, _out = run_export(t, work)
    assert rc == 3 and B is None, (rc, report[-2000:])
    assert "names child under 18" in report and "minor_ok" in report
    # Chris decides the name is someone else's: overrides.minor_ok
    rc, B, _text, report, err, _out = run_export(t, work, overrides={"minor_ok": {t.gid(h["tove"]): "the farm is older than she is"}})
    assert rc == 0, report[-2000:] + err
    assert B["people"][t.gid(h["tove"])]["name"] == gx.MINOR_NAME, "minor_ok does not show her name"


def test_the_date_rule_leans_late(work):
    D = lambda y, m=0, d=0, mod=0, q=0, end=None: {"dateval": [d, m, y, False] + (list(end) + [False] if end else []),
                                                  "modifier": mod, "quality": q}
    import datetime as dt
    assert gx.latest_birth_of(D(2008)) == dt.date(2008, 12, 31)
    assert gx.latest_birth_of(D(2008, 2)) == dt.date(2008, 2, 29)
    assert gx.latest_birth_of(D(2008, 11, 23)) == dt.date(2008, 11, 23)
    assert gx.latest_birth_of(D(2005, mod=3)) == dt.date(2007, 12, 31), "about: two years later"
    assert gx.latest_birth_of(D(2005, q=1)) == dt.date(2007, 12, 31), "estimated: two years later"
    assert gx.latest_birth_of(D(2000, mod=2)) == dt.date(2010, 12, 31), "after: ten years later"
    assert gx.latest_birth_of(D(2010, mod=1)) == dt.date(2010, 12, 30), "before"
    assert gx.latest_birth_of(D(2001, mod=4, end=(0, 0, 2009))) == dt.date(2009, 12, 31), "between: the end"
    assert gx.latest_birth_of({"dateval": [0, 0, 0, False], "modifier": 6, "text": "young"}) is None
    assert gx.adult_on(dt.date(2008, 2, 29)) == dt.date(2026, 3, 1)
    # the applier's rule is the same one
    spec2 = importlib.util.spec_from_file_location("apply_proposals", ROOT / "tools" / "apply_proposals.py")
    try:
        ap = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(ap)
    except ImportError:        # the applier needs requests; without it, skip the comparison
        return
    for d in (D(2008), D(2008, 2), D(2008, 11, 23), D(2005, mod=3), D(2005, q=2), D(2000, mod=2), D(2010, mod=1),
              D(2010, 3, 1, mod=8), D(2001, mod=4, end=(0, 0, 2009)), D(2001, mod=5, end=(1, 6, 2009)), D(2012, 7, mod=7)):
        assert gx.latest_birth_of(d) == ap.latest_birth_of(d), d


def test_the_site_shows_the_placeholder_and_the_labels(work):
    try:
        import jinja2, markdown  # noqa: F401
    except ImportError:
        print("    (skipped: jinja2/markdown not installed)")
        return
    t, h = lindqvist()
    rc, B, _text, report, err, out = run_export(t, work)
    assert rc == 0, report[-3000:] + err
    cfg = json.load(open(ROOT / "site" / "config.json", encoding="utf-8"))
    cfg.update({"lines": [], "root_person": t.gid(h["erik"])})
    cfg_path = os.path.join(work, "config.json")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))
    site = os.path.join(work, "_site")
    r = subprocess.run([sys.executable, str(ROOT / "site" / "build.py"), "--bundle", os.path.join(out, "data", "bundle.json"),
                        "--media", os.path.join(out, "media"), "--config", cfg_path, "--content", os.path.join(out, "content"),
                        "--out", site], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "story farm: dropped — names a child under 18" in r.stdout, r.stdout
    assert "board R1: dropped — names a child under 18" in r.stdout, r.stdout
    pages = {}
    for dirpath, _d, files in os.walk(site):
        for fn in files:
            if fn.endswith((".html", ".json")):
                p = os.path.join(dirpath, fn)
                pages[os.path.relpath(p, site)] = open(p, encoding="utf-8").read()
    everything = fold("\n".join(pages.values()))
    for name in ("tove", "nils"):
        assert name not in everything, f"{name} is on the site: " + ", ".join(k for k, v in pages.items() if name in fold(v))
    for key in ("tove", "nils", "young_anton"):
        assert f"person/{t.gid(h[key])}/index.html" not in pages, "a child under 18 has no page"
    fam = next(f for f in B["families"].values() if f["father"] == t.gid(h["erik"]))
    fpage = pages[f"family/{fam['id']}/index.html"]
    assert fpage.count('class="p minor"') == 3 and "Living child" in fpage
    for key in ("tove", "nils", "young_anton"):          # each opens in the research database, behind the family login
        assert f'<a class="p minor" href="{cfg["private_site"]}/person/{t.gid(h[key])}"' in fpage, fpage
    assert f'{cfg["private_site"]}/family/{fam["id"]}' in fpage, "a living family links to the research database too"
    assert 'Ingrid Lindqvist</a> <span class="yrs">living</span> <span class="rel">adopted</span>' in fpage, fpage
    assert "<span class=\"rel\">adopted</span>" not in fpage.split("Ingrid")[0], "no label on the children under 18"
    assert "Children under 18 are shown as" in fpage
    ingrid = pages[f"person/{t.gid(h['ingrid'])}/index.html"]
    assert "<dt>Adoptive parents</dt>" in ingrid, ingrid
    assert f'{cfg["private_site"]}/person/{t.gid(h["ingrid"])}' in ingrid, "a living person's page links to the research database"
    mpage = pages[f"person/{t.gid(h['maja'])}/index.html"]
    assert "<h1>Maja (Holm) Lindqvist</h1>" in mpage, re.search(r"<h1>.*?</h1>", mpage).group(0)
    assert "Erik Lindqvist <span class=\"amp\">&amp;</span> Maja (Holm) Lindqvist" in fpage
    epage = pages[f"person/{t.gid(h['ebba'])}/index.html"]
    assert "<h1>Ebba (Berg) Lindqvist</h1>" in epage and "also Ebba Nyström <small>(married name)</small>" in epage, \
        re.search(r'<p class="aka">.*?</p>', epage, re.S)
    spage = pages[f"person/{t.gid(h['sara'])}/index.html"]
    assert "<h1>Sara Lindqvist</h1>" in spage, "a primary that is already the married surname is shown as filed"
    people_index = pages["people/index.html"]
    assert re.search(r'data-k="maja holm [^"]*maja \(holm\) lindqvist"', people_index), "Lindqvist finds Maja"
    assert "Living child" not in people_index and "Saga Lindqvist" in people_index
    assert "Anton farmed by the lake for forty years." in pages["stories/lake/index.html"]
    assert "stories/farm/index.html" not in pages
    research = pages["research/index.html"]
    assert "last changed on <b>20 November 2026</b>" in research, "outside git, the board's own date"
    assert "deed" in research and "school records" not in research


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    want = sys.argv[1:]
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, fn in enumerate(TESTS):
            if want and not any(w in fn.__name__ for w in want):
                continue
            try:
                fn(os.path.join(tmp, f"t{i}"))
                print(f"ok    {fn.__name__}")
            except Exception:
                failed += 1
                print(f"FAIL  {fn.__name__}")
                traceback.print_exc()
    print(f"{len(TESTS) - failed if not want else ''} passed, {failed} failed" if not want else f"{failed} failed")
    sys.exit(1 if failed else 0)
