#!/usr/bin/env python3
"""
validator.py — the second AI agent of the operating model (§7): checks each citation
against the claims it is attached to and says whether the record supports them.

Runs on the server next to gramps_export.py. Reads the tree through the loopback API,
fetches the record image through the API's thumbnail endpoint, asks a Claude model
(a different model family from Karel's, as the operating model requires), and writes
one verdict file per citation. With --apply it also swaps the review tags in Gramps:

    supports / consistent, no flags  ->  ai-validated
    anything else                    ->  needs-human
and attaches its reasoning to the citation as a Research note, adds the Tier attribute
when the citation has none, and tags everything it touched agent-validator.

    python3 validator.py --dry-run                      # queue, order, cost estimate; no model calls
    python3 validator.py --limit 20                     # verdict files only (read-only account is enough)
    python3 validator.py --limit 20 --apply             # + tags, notes, tiers in Gramps (Editor account)
    python3 validator.py --all --apply                  # the whole queue

Credentials: --user / GRAMPS_USER + prompt, GRAMPS_PASSWORD or --password-file;
model key: ANTHROPIC_API_KEY or --key-file (chmod 600). Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gramps_export import (Api, ApiError, TABLES, date_obj, name_of, plain_text, tag_names, typestr)  # noqa: E402

VERSION = "0.3.0"
DONE_TAGS = {"reviewed", "ai-validated", "needs-human"}
QUEUE_TAGS = {"unreviewed", "agent-proposed"}
VERDICTS = ("supports", "consistent", "partial", "contradicts", "unreadable", "irrelevant")
TIERS = ("record", "index", "compiled", "tree", "oral", "ai-draft")
API_URL = "https://api.anthropic.com/v1"
# approximate list prices per million tokens, for the running estimate only
PRICES = {"sonnet": (3.0, 15.0), "haiku": (1.0, 5.0), "opus": (15.0, 75.0)}
# citations whose record image is hard to read — old Czech/German/Latin script — go to the stronger model when --model-hard is set
HARD_SOURCE = re.compile(r"parish|register|matri[kc]|acta publica|porta fontium|kirchenbuch|pfarr|passenger|manifest|"
                         r"hamburg|naturalization record|petition|declaration of intention|drouin", re.I)

SYSTEM = """You are a careful genealogical evidence checker for a family research database.
You receive ONE citation — the source, the citation detail, any transcription or research notes,
and where available an image of the record — and the claim(s) in the database that this citation
is attached to. Decide whether the record supports each claim.

Rules
- Read what the record actually says. Do not assume facts that are not in the evidence.
- Names vary across Czech, German and English forms (Konečný / Konecny, Taubr / Tauber / Taubrová,
  Jan / Johann / John, Františka / Frances). Treat such variants as the same name when the other
  details fit.
- An age in a record implies a birth year; a difference of more than one year from the claim is a flag.
- Historical and modern place names may differ (Štítná nad Vláří was Schittna; Svatý Štěpán is a
  hamlet of Bylnice). Treat that as consistent, and say so.
- If the image does not show the relevant entry, or cannot be read, say so rather than guessing.
- Index entries (no image) can at most be "consistent" unless they state the claim explicitly.

Answer with JSON only, no prose around it, with exactly these keys:
  "verdict": one of "supports" (the record states the claim), "consistent" (nothing contradicts it but
             the evidence does not fully state it), "partial" (some claims supported, others not),
             "contradicts", "unreadable", "irrelevant" (the record is not about this person)
  "match_points": list of short strings — what in the record matches the claim
  "flags": list of short strings — discrepancies or doubts; empty list if none
  "tier": one of "record" (an image of an original record is present and readable), "index"
          (index entry or abstract without the image), "compiled" (published genealogy, Find a Grave
          memorial, family history), "tree" (member tree, hint, reunion book), "oral", "ai-draft"
  "confidence": integer 0-4 (4 = original record read, matches on name, date and place;
                3 = matches on most points; 2 = consistent but thin; 1 = weak; 0 = no support)
  "note": two to four sentences of reasoning that a person could check against the record
"""


# --------------------------------------------------------------------------- #
# model client (Anthropic Messages API, stdlib only)
# --------------------------------------------------------------------------- #
class Model:
    def __init__(self, key: str, model: str, timeout: int = 180):
        self.key, self.model, self.timeout = key, model, timeout
        self.in_tokens = self.out_tokens = self.calls = 0

    def _headers(self):
        return {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    def resolve_model(self) -> str:
        """Use the requested model if the API lists it; otherwise the newest Sonnet it does list."""
        try:
            req = urllib.request.Request(f"{API_URL}/models?limit=100", headers=self._headers())
            with urllib.request.urlopen(req, timeout=30) as r:
                ids = [m["id"] for m in json.loads(r.read().decode()).get("data", [])]
        except (urllib.error.URLError, ValueError, KeyError):
            return self.model
        if self.model in ids:
            return self.model
        sonnets = sorted((i for i in ids if "sonnet" in i), reverse=True)
        if sonnets:
            print(f"model {self.model} not available; using {sonnets[0]}", file=sys.stderr)
            self.model = sonnets[0]
        return self.model

    def ask(self, text: str, images: list[tuple[str, bytes]]) -> dict:
        content = []
        for media_type, data in images:
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                        "data": base64.b64encode(data).decode()}})
        content.append({"type": "text", "text": text})
        body = {"model": self.model, "max_tokens": 900, "system": SYSTEM,
                "messages": [{"role": "user", "content": content}]}
        req = urllib.request.Request(f"{API_URL}/messages", data=json.dumps(body).encode(), headers=self._headers())
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                    time.sleep(8 * (attempt + 1))
                    continue
                raise ApiError(f"model API: HTTP {e.code} {e.read()[:300]!r}") from None
        self.calls += 1
        usage = out.get("usage", {})
        self.in_tokens += usage.get("input_tokens", 0)
        self.out_tokens += usage.get("output_tokens", 0)
        raw = "".join(c.get("text", "") for c in out.get("content", []) if c.get("type") == "text")
        return parse_json(raw)

    def cost(self) -> float | None:
        for k, (pi, po) in PRICES.items():
            if k in self.model:
                return self.in_tokens / 1e6 * pi + self.out_tokens / 1e6 * po
        return None

    @staticmethod
    def price_per_m(model_id: str) -> tuple[float, float] | None:
        for k, v in PRICES.items():
            if k in model_id:
                return v
        return None


def sniff_image(data: bytes) -> str | None:
    """media type from the bytes; None for anything the model API does not take (PDF, TIFF, HTML errors)"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def parse_json(raw: str) -> dict:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return {"verdict": "unreadable", "match_points": [], "flags": ["model returned no usable JSON"],
            "tier": None, "confidence": 0, "note": s[:500]}


class MockModel(Model):
    def __init__(self):
        super().__init__("none", "mock")

    def resolve_model(self):
        return "mock"

    def ask(self, text, images):
        self.calls += 1
        return {"verdict": "consistent", "match_points": ["mock run"], "flags": [], "tier": "index" if not images else "record",
                "confidence": 2, "note": "Mock verdict: no model was called."}


# --------------------------------------------------------------------------- #
# tree access
# --------------------------------------------------------------------------- #
class Tree:
    def __init__(self, api: Api, progress):
        self.api = api
        self.data = {}
        for table in TABLES:
            progress(f"fetching {table} …")
            self.data[table] = list(api.paged(table))
        self.people = {o["handle"]: o for o in self.data["people"]}
        self.families = {o["handle"]: o for o in self.data["families"]}
        self.events = {o["handle"]: o for o in self.data["events"]}
        self.places = {o["handle"]: o for o in self.data["places"]}
        self.citations = {o["handle"]: o for o in self.data["citations"]}
        self.sources = {o["handle"]: o for o in self.data["sources"]}
        self.notes = {o["handle"]: o for o in self.data["notes"]}
        self.media = {o["handle"]: o for o in self.data["media"]}
        self.tags = {o["handle"]: o["name"] for o in self.data["tags"]}
        self.tag_handle = {o["name"]: o["handle"] for o in self.data["tags"]}
        # who cites what
        self.owners = collections.defaultdict(list)   # citation handle -> [(kind, handle, extra)]
        for h, e in self.events.items():
            for c in e.get("citation_list", []):
                self.owners[c].append(("event", h, None))
        for h, p in self.people.items():
            for c in p.get("citation_list", []):
                self.owners[c].append(("person", h, None))
            for c in (p.get("primary_name") or {}).get("citation_list", []):
                self.owners[c].append(("name", h, None))
            for ref in p.get("event_ref_list", []):
                for c in ref.get("citation_list", []):
                    self.owners[c].append(("event", ref["ref"], h))
        for h, f in self.families.items():
            for c in f.get("citation_list", []):
                self.owners[c].append(("family", h, None))
            for ref in f.get("event_ref_list", []):
                for c in ref.get("citation_list", []):
                    self.owners[c].append(("event", ref["ref"], None))
        # event -> participants
        self.participants = collections.defaultdict(list)
        for h, p in self.people.items():
            for ref in p.get("event_ref_list", []):
                self.participants[ref["ref"]].append((h, typestr(ref.get("role")) or "Primary"))
        self.event_family = {}
        for h, f in self.families.items():
            for ref in f.get("event_ref_list", []):
                self.event_family[ref["ref"]] = h

    def tags_of(self, obj) -> list[str]:
        return tag_names(obj, self.tags)

    def pname(self, h) -> str:
        p = self.people.get(h)
        if not p:
            return "?"
        n = name_of(p)["display"]
        b = self.year_of(p, ("Birth", "Baptism", "Christening"))
        d = self.year_of(p, ("Death", "Burial", "Cremation"))
        span = f"{b or '?'}–{d or '?'}" if (b or d) else ""
        return f"{n} ({span})" if span else n

    def year_of(self, p, types):
        for ref in p.get("event_ref_list", []):
            e = self.events.get(ref["ref"])
            if e and typestr(e.get("type")) in types and typestr(ref.get("role")) == "Primary":
                d = date_obj(e.get("date"))
                if d and d.get("year"):
                    return d["year"]
        return None

    def place_text(self, h) -> str:
        pl = self.places.get(h)
        if not pl:
            return ""
        parts = []
        seen = set()
        while pl and pl["handle"] not in seen and len(parts) < 8:
            seen.add(pl["handle"])
            parts.append((pl.get("name") or {}).get("value") or pl.get("title") or "")
            refs = pl.get("placeref_list") or []
            pl = self.places.get(refs[0]["ref"]) if refs else None
        return ", ".join(x for x in parts if x)

    def ancestors(self, root_handle: str) -> dict:
        depth, queue = {root_handle: 0}, collections.deque([root_handle])
        while queue:
            cur = queue.popleft()
            for fid in self.people.get(cur, {}).get("parent_family_list", []):
                f = self.families.get(fid) or {}
                for key in ("father_handle", "mother_handle"):
                    par = f.get(key)
                    if par and par in self.people and par not in depth:
                        depth[par] = depth[cur] + 1
                        queue.append(par)
        return depth

    # ---- claims ---------------------------------------------------------- #
    def claims_for(self, ch: str) -> list[dict]:
        out, seen = [], set()
        for kind, h, extra in self.owners.get(ch, []):
            key = (kind, h)
            if key in seen:
                continue
            seen.add(key)
            if kind == "event":
                e = self.events.get(h)
                if not e:
                    continue
                d = date_obj(e.get("date"))
                who = [ph for ph, role in self.participants.get(h, []) if role == "Primary"]
                fam = self.event_family.get(h)
                if fam:
                    f = self.families[fam]
                    who = [x for x in (f.get("father_handle"), f.get("mother_handle")) if x] + who
                text = f"{typestr(e.get('type'))} — " + "; ".join(self.pname(x) for x in dict.fromkeys(who)) \
                    + (f" — date: {d['text']}" if d and d.get("text") else " — date: none recorded") \
                    + (f" — place: {self.place_text(e['place'])}" if e.get("place") else "") \
                    + (f" — note: {e['description']}" if e.get("description") else "")
                out.append({"kind": "event", "id": e.get("gramps_id"), "handle": h, "people": [self.people[x].get("gramps_id") for x in who if x in self.people], "text": text})
            elif kind in ("person", "name"):
                p = self.people.get(h)
                if p:
                    label = "This record is about" if kind == "person" else "The name of"
                    out.append({"kind": kind, "id": p.get("gramps_id"), "handle": h, "people": [p.get("gramps_id")],
                                "text": f"{label} {self.pname(h)}"})
            elif kind == "family":
                f = self.families.get(h)
                if f:
                    who = [x for x in (f.get("father_handle"), f.get("mother_handle")) if x]
                    out.append({"kind": "family", "id": f.get("gramps_id"), "handle": h, "people": [self.people[x].get("gramps_id") for x in who if x in self.people],
                                "text": "The family of " + " and ".join(self.pname(x) for x in who)})
        return out

    def evidence_text(self, c: dict) -> str:
        src = self.sources.get(c.get("source_handle")) or {}
        lines = [f"Source: {re.sub(r'</?i>', '', src.get('title') or '', flags=re.I).strip(' ,')}"]
        if src.get("author"):
            lines.append(f"Author: {src['author']}")
        if src.get("pubinfo"):
            lines.append(f"Publication: {src['pubinfo']}")
        if c.get("page"):
            lines.append(f"Citation detail: {c['page']}")
        d = date_obj(c.get("date"))
        if d and d.get("text"):
            lines.append(f"Citation date: {d['text']}")
        for a in c.get("attribute_list") or []:
            lines.append(f"Attribute {typestr(a.get('type'))}: {a.get('value')}")
        for nh in c.get("note_list") or []:
            n = self.notes.get(nh)
            if n:
                lines.append(f"Note ({typestr(n.get('type'))}): {plain_text(n.get('text'))[:3000]}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"))
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"))
    ap.add_argument("--password-file")
    ap.add_argument("--key-file", default=os.path.expanduser("~/.config/family-site/anthropic.key"))
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--model-hard", default=None, help="stronger model for citations with a register/manifest image, e.g. an Opus model (default: same as --model)")
    ap.add_argument("--out", default=os.path.expanduser("~/gramps-tools/validation"), help="verdict files (default ~/gramps-tools/validation)")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "site", "config.json"),
                    help="site config, for root_person (direct lines first)")
    ap.add_argument("--dry-run", action="store_true", help="show the queue and estimate; call no model")
    ap.add_argument("--limit", type=int, default=25, help="citations per run (default 25)")
    ap.add_argument("--all", action="store_true", help="no limit")
    ap.add_argument("--only-direct", action="store_true", help="only citations on the direct lines")
    ap.add_argument("--ids", help="comma-separated citation Gramps IDs to check (ignores the queue order)")
    ap.add_argument("--redo", action="store_true", help="re-check citations that already have a verdict file")
    ap.add_argument("--apply", action="store_true", help="write tags, notes and tiers to Gramps (needs an Editor account)")
    ap.add_argument("--max-images", type=int, default=2)
    ap.add_argument("--image-px", type=int, default=2200, help="longest side of the image sent to the model")
    ap.add_argument("--mock", action="store_true", help="no model: fake verdicts (to test the pipeline)")
    ap.add_argument("--include-living", action="store_true", help="also check citations about living people (off: they never reach the site)")
    ap.add_argument("--include-bare", action="store_true",
                    help="also check citations with no image and no transcription (off: there is nothing to check them against)")
    ap.add_argument("--overrides", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "overrides.json"))
    ap.add_argument("--revert", help="comma-separated citation IDs: put them (and their facts) back to 'unreviewed'")
    ap.add_argument("--max-age", type=int, default=110)
    args = ap.parse_args(argv)

    def progress(msg):
        print(msg, file=sys.stderr, flush=True)

    # model
    if args.mock:
        model = MockModel()
    elif not args.dry_run and not args.revert:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key and os.path.exists(args.key_file):
            with open(args.key_file, encoding="utf-8") as f:
                key = f.read().strip()
        if not key:
            print(f"ERROR: no model key — set ANTHROPIC_API_KEY or put the key in {args.key_file} (chmod 600)", file=sys.stderr)
            return 2
        model = Model(key, args.model)
        model.resolve_model()
        hard_model = Model(key, args.model_hard) if args.model_hard else None
        if hard_model:
            hard_model.resolve_model()
    else:
        model = None
        hard_model = None
    if args.mock:
        hard_model = None

    # gramps
    user = args.user or input("Gramps user: ")
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as f:
            password = f.read().strip()
    else:
        password = os.environ.get("GRAMPS_PASSWORD") or getpass.getpass("Gramps password: ")
    api = Api(args.api)
    try:
        api.login(user, password)
        tree = Tree(api, progress)
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # direct lines
    root = None
    try:
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
        rp = str(cfg.get("root_person", ""))
        if rp.startswith("name:"):
            root = next((h for h, p in tree.people.items() if name_of(p)["display"].lower() == rp[5:].lower()), None)
        elif rp.startswith("ancestry:"):
            root = next((h for h, p in tree.people.items() if any(typestr(a.get("type")) == "Ancestry ID" and a.get("value") == rp[9:]
                                                                   for a in p.get("attribute_list") or [])), None)
        else:
            root = next((h for h, p in tree.people.items() if p.get("gramps_id") == rp), None)
    except (OSError, ValueError):
        pass
    depth = tree.ancestors(root) if root else {}
    direct_people = set(depth)
    direct_families = {h for h, f in tree.families.items()
                       if f.get("father_handle") in direct_people or f.get("mother_handle") in direct_people}

    def is_direct(ch) -> tuple[bool, int]:
        best = None
        for kind, h, extra in tree.owners.get(ch, []):
            if kind == "event":
                people = [ph for ph, _r in tree.participants.get(h, [])]
                if tree.event_family.get(h) in direct_families:
                    people += [x for x in (tree.families[tree.event_family[h]].get("father_handle"), tree.families[tree.event_family[h]].get("mother_handle")) if x]
            elif kind == "family":
                f = tree.families.get(h) or {}
                people = [x for x in (f.get("father_handle"), f.get("mother_handle")) if x]
            else:
                people = [h]
            for ph in people:
                if ph in depth and (best is None or depth[ph] < best):
                    best = depth[ph]
        return (best is not None, best if best is not None else 999)

    def is_hard(c: dict) -> bool:
        if not c.get("media_list"):
            return False
        src = tree.sources.get(c.get("source_handle")) or {}
        return bool(HARD_SOURCE.search(src.get("title") or "")) or any(
            HARD_SOURCE.search(typestr(a.get("type")) + " " + str(a.get("value"))) for a in c.get("attribute_list") or [])

    # living people (local rule + overrides.json), so their records are not sent anywhere
    ov_living, ov_dead = set(), set()
    try:
        with open(args.overrides, encoding="utf-8") as f:
            ov = json.load(f)
        ov_living = set(ov.get("living", {})) | set(ov.get("exclude", {}))
        ov_dead = set(ov.get("deceased", {}))
    except (OSError, ValueError):
        pass
    cutoff = dt.date.today().year - args.max_age

    def is_living(ph: str) -> bool:
        p = tree.people.get(ph) or {}
        gid = p.get("gramps_id")
        if gid in ov_dead:
            return False
        if gid in ov_living:
            return True
        for ref in p.get("event_ref_list", []):
            e = tree.events.get(ref["ref"])
            if e and typestr(e.get("type")) in ("Death", "Burial", "Cremation", "Probate") and typestr(ref.get("role")) == "Primary":
                return False
        b = tree.year_of(p, ("Birth", "Baptism", "Christening"))
        return b is None or b > cutoff

    def claim_people(ch: str) -> set:
        out = set()
        for kind, h, extra in tree.owners.get(ch, []):
            if kind == "event":
                out.update(ph for ph, _r in tree.participants.get(h, []))
                fam = tree.event_family.get(h)
                if fam:
                    out.update(x for x in (tree.families[fam].get("father_handle"), tree.families[fam].get("mother_handle")) if x)
            elif kind == "family":
                f = tree.families.get(h) or {}
                out.update(x for x in (f.get("father_handle"), f.get("mother_handle")) if x)
            else:
                out.add(h)
        return out

    def is_bare(c: dict) -> bool:
        return not c.get("media_list") and not c.get("note_list")

    # revert
    if args.revert:
        ids = [x.strip() for x in args.revert.split(",") if x.strip()]
        drop = {tree.tag_handle.get(n) for n in ("ai-validated", "needs-human", "agent-validator")} - {None}
        un = tree.tag_handle.get("unreviewed")
        n = 0
        for ch, c in tree.citations.items():
            if c.get("gramps_id") not in ids:
                continue
            for kind, h, cl in [("citations", ch, None)] + [("events" if k == "event" else "families", hh, None)
                                                           for k, hh, _x in tree.owners.get(ch, []) if k in ("event", "family")]:
                obj = api.get(f"{kind}/{h}")
                tl = [t for t in obj.get("tag_list", []) if t not in drop]
                if un and un not in tl and tree.tag_handle.get("reviewed") not in tl:
                    tl.append(un)
                if tl != obj.get("tag_list"):
                    obj["tag_list"] = tl
                    api.send("PUT", f"{kind}/{h}", obj)
            try:
                os.remove(os.path.join(args.out, f"{c.get('gramps_id')}.json"))
            except OSError:
                pass
            n += 1
        print(f"reverted {n} citations (and their facts) to unreviewed; validator notes stay on the citations")
        return 0

    # queue
    os.makedirs(args.out, exist_ok=True)
    done_files = {fn[:-5] for fn in os.listdir(args.out) if fn.endswith(".json")}
    queue = []
    skipped = collections.Counter()
    wanted = set(x.strip() for x in args.ids.split(",")) if args.ids else None
    for ch, c in tree.citations.items():
        gid = c.get("gramps_id") or ch
        if wanted is not None and gid not in wanted:
            continue
        tags = set(tree.tags_of(c))
        if c.get("private"):
            skipped["private"] += 1
            continue
        if wanted is None and tags & DONE_TAGS:
            skipped["already reviewed / validated / flagged"] += 1
            continue
        if not tree.owners.get(ch):
            skipped["not attached to anything"] += 1
            continue
        if not args.include_living and wanted is None:
            people = claim_people(ch)
            if people and all(is_living(ph) for ph in people):
                skipped["about living people only"] += 1
                continue
        if not args.include_bare and wanted is None and is_bare(c):
            skipped["no image and no transcription (nothing to check)"] += 1
            continue
        if gid in done_files and not args.redo and wanted is None:
            skipped["verdict file exists (use --redo)"] += 1
            continue
        direct, gen = is_direct(ch)
        if args.only_direct and not direct:
            skipped["not on a direct line (--only-direct)"] += 1
            continue
        queue.append((0 if direct else 1, gen, gid, ch))
    queue.sort()
    total_queue = len(queue)
    if not args.all and wanted is None:
        queue = queue[:args.limit]

    progress(f"queue: {total_queue} citations to check ({sum(1 for q in queue if q[0] == 0)} on direct lines in this run); "
             f"skipped: " + ", ".join(f"{k} {v}" for k, v in skipped.items()))
    if args.dry_run:
        with_img = sum(1 for _d, _g, _gid, ch in queue if tree.citations[ch].get("media_list"))
        print(f"\nDry run — would check {len(queue)} citations now ({with_img} with a record image), {total_queue} in the whole queue.")
        print("Order (direct lines first, closest generations first):")
        for _d, gen, gid, ch in queue[:60]:
            c = tree.citations[ch]
            src = tree.sources.get(c.get("source_handle")) or {}
            claims = tree.claims_for(ch)
            print(f"  {gid}  gen {gen if gen < 999 else '-'}  {'img' if c.get('media_list') else '   '}  "
                  f"{re.sub(r'</?i>', '', src.get('title') or '')[:60]} | {claims[0]['text'][:90] if claims else '(no claim)'}")
        def est(model_id, items):
            tok_in = sum((5000 if tree.citations[ch].get("media_list") else 900) for ch in items)
            price = Model.price_per_m(model_id)
            if price is None:
                return f"{len(items)} citations on {model_id}: ~{tok_in:,} input tokens (no price on file for this model)"
            usd = tok_in / 1e6 * price[0] + len(items) * 300 / 1e6 * price[1]
            return f"{len(items)} citations on {model_id}: ~{usd:.2f} USD"
        chs = [ch for _d, _g, _gid, ch in queue]
        hard = [ch for ch in chs if is_hard(tree.citations[ch])]
        print(f"\nRecord images with old handwriting (registers, manifests): {len(hard)} of {len(queue)} in this run.")
        if args.model_hard:
            print("Estimate — " + est(args.model, [ch for ch in chs if ch not in set(hard)]) + "; " + est(args.model_hard, hard))
        else:
            print("Estimate — " + est(args.model, chs) + "  (add --model-hard <opus model> to send the "
                  f"{len(hard)} hard ones to a stronger model)")
        all_hard = sum(1 for _d, _g, _gid, ch in sorted(queue) if is_hard(tree.citations[ch]))
        print(f"Whole queue: {total_queue} citations, of which {all_hard if args.all else '?'} hard (run --dry-run --all for the full split).")
        return 0

    # tags needed for --apply
    needed = {}
    if args.apply:
        for name, color in (("ai-validated", "#4caf50"), ("needs-human", "#f44336"), ("agent-validator", "#9c27b0")):
            if name not in tree.tag_handle:
                try:
                    api.send("POST", "tags/", {"name": name, "color": color, "priority": 0})
                except ApiError as e:
                    print(f"ERROR: cannot create tag {name}: {e}", file=sys.stderr)
                    return 2
                tree.tag_handle = {t["name"]: t["handle"] for t in api.get("tags/")}
            needed[name] = tree.tag_handle[name]

    # run
    results = collections.Counter()
    errors_in_a_row = 0
    flagged = []
    t0 = time.time()
    for i, (_d, gen, gid, ch) in enumerate(queue, 1):
        c = tree.citations[ch]
        claims = tree.claims_for(ch)
        images = []
        for m in (c.get("media_list") or [])[:args.max_images]:
            med = tree.media.get(m.get("ref"))
            if not med or med.get("private"):
                continue
            try:
                data = api.get_bytes(f"media/{med['handle']}/thumbnail/{args.image_px}", {"square": "0"})
                kind = sniff_image(data)
                if kind:
                    images.append((kind, data))
                else:
                    progress(f"  {gid}: image {med.get('gramps_id')} is not a JPEG/PNG/GIF/WebP ({med.get('mime')}); skipped")
            except ApiError as e:
                progress(f"  {gid}: image {med.get('gramps_id')} not fetched ({e})")
        text = "CLAIMS this citation is attached to:\n" + "\n".join(f"{k + 1}. {cl['text']}" for k, cl in enumerate(claims)) \
            + "\n\nEVIDENCE:\n" + tree.evidence_text(c) \
            + (f"\n\nIMAGES: {len(images)} attached" if images else "\n\nIMAGES: none (no record image attached; judge the text only)")
        use = hard_model if (hard_model and images and is_hard(c)) else model
        try:
            v = use.ask(text, images)
            errors_in_a_row = 0
        except ApiError as e:
            errors_in_a_row += 1
            progress(f"  {gid}: model error — {e}")
            results["error"] += 1
            if errors_in_a_row >= 3:
                progress("  three errors in a row; stopping")
                break
            continue
        verdict = v.get("verdict") if v.get("verdict") in VERDICTS else "unreadable"
        flags = [str(x) for x in (v.get("flags") or [])]
        tier = v.get("tier") if v.get("tier") in TIERS else None
        try:
            conf = max(0, min(4, int(v.get("confidence", 0))))
        except (TypeError, ValueError):
            conf = 0
        outcome = "ai-validated" if verdict in ("supports", "consistent") and not flags else "needs-human"
        rec = {
            "gramps_handle": ch, "gramps_id": gid, "object": "citation", "verdict": verdict,
            "match_points": [str(x) for x in (v.get("match_points") or [])], "flags": flags,
            "tier_suggested": tier, "tier_ok": (tier is None) or (next((a.get("value") for a in c.get("attribute_list") or [] if typestr(a.get("type")) == "Tier"), tier) == tier),
            "confidence_suggested": conf, "note": str(v.get("note") or "").strip(),
            "claims": [{"kind": cl["kind"], "id": cl["id"], "people": cl["people"], "text": cl["text"]} for cl in claims],
            "images_seen": len(images), "outcome": outcome, "applied": False,
            "model": use.model, "checked": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        }
        results[verdict] += 1
        if outcome == "needs-human":
            flagged.append(f"{gid}: {verdict} — {'; '.join(flags) or rec['note'][:120]}")
        if args.apply:
            try:
                apply_verdict(api, tree, c, claims, rec, needed)
                rec["applied"] = True
            except ApiError as e:
                progress(f"  {gid}: apply failed — {e}")
                rec["apply_error"] = str(e)
        with open(os.path.join(args.out, f"{gid}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        progress(f"  {i}/{len(queue)} {gid} {verdict:11s} conf {conf} {'img' if images else '   '} -> {outcome}")

    print(f"\nChecked {sum(results.values())} citations in {time.time() - t0:.0f}s: "
          + ", ".join(f"{k} {n}" for k, n in results.most_common()))
    for m in [x for x in (model, hard_model) if x and x.calls]:
        cost = m.cost()
        print(f"Model {m.model}: {m.calls} calls, {m.in_tokens:,} in / {m.out_tokens:,} out tokens"
              + (f" ≈ {cost:.2f} USD" if cost is not None else " (no price on file)"))
    if flagged:
        print(f"\nNeeds a human ({len(flagged)}):")
        for line in flagged:
            print("  - " + line)
    print(f"\nVerdict files: {args.out}  ·  {'tags written to Gramps' if args.apply else 'nothing written to Gramps (add --apply)'}")
    return 0


def apply_verdict(api: Api, tree: Tree, c: dict, claims: list, rec: dict, tags: dict):
    """Swap review tags on the citation and its claims, add the Tier when missing, attach the reasoning."""
    outcome = rec["outcome"]
    add = [tags[outcome], tags["agent-validator"]]
    review_handles = {tree.tag_handle.get(n) for n in ("unreviewed", "agent-proposed", "ai-validated", "needs-human")} - {None}
    human = tree.tag_handle.get("reviewed")

    def retag(obj: dict, is_claim: bool = False) -> bool:
        """exactly one review tag per object; never touch a human-reviewed object; needs-human on a
        fact is sticky until a person clears it (another citation's 'supports' must not hide a conflict)"""
        current = list(obj.get("tag_list", []))
        if human and human in current:
            return False
        if is_claim and outcome == "ai-validated" and tags["needs-human"] in current:
            return False
        tl = [t for t in current if t not in review_handles]
        for t in add:
            if t not in tl:
                tl.append(t)
        obj["tag_list"] = tl
        return tl != current

    # the reasoning, as a Research note on the citation
    note_text = (f"Validator ({rec['model']}, {rec['checked'][:10]}): {rec['verdict']}, confidence {rec['confidence_suggested']}/4"
                 + (f", tier {rec['tier_suggested']}" if rec["tier_suggested"] else "") + ".\n" + rec["note"]
                 + ("\nMatches: " + "; ".join(rec["match_points"]) if rec["match_points"] else "")
                 + ("\nFlags: " + "; ".join(rec["flags"]) if rec["flags"] else ""))
    created = api.send("POST", "notes/", {"text": {"string": note_text, "tags": []}, "type": "Research", "format": 0,
                                          "private": False, "tag_list": [tags["agent-validator"]]})
    note_handle = None
    for tr in created or []:
        if tr.get("_class") == "Note" or tr.get("type") == "add" and tr.get("handle"):
            note_handle = tr.get("handle")
    if not note_handle and isinstance(created, list) and created:
        note_handle = created[0].get("handle")
    cit = api.get(f"citations/{c['handle']}")
    retag(cit)
    if note_handle:
        cit.setdefault("note_list", []).append(note_handle)
    has_tier = any(typestr(a.get("type")) == "Tier" for a in cit.get("attribute_list") or [])
    if rec["tier_suggested"] and not has_tier:
        cit.setdefault("attribute_list", []).append({"type": "Tier", "value": rec["tier_suggested"], "private": False})
    api.send("PUT", f"citations/{c['handle']}", cit)
    for cl in claims:
        if cl["kind"] == "event":
            obj = api.get(f"events/{cl['handle']}")
            if retag(obj, is_claim=True):
                api.send("PUT", f"events/{cl['handle']}", obj)
        elif cl["kind"] == "family":
            obj = api.get(f"families/{cl['handle']}")
            if retag(obj, is_claim=True):
                api.send("PUT", f"families/{cl['handle']}", obj)


if __name__ == "__main__":
    sys.exit(main())
