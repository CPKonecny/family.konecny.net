#!/usr/bin/env python3
"""Merge a duplicate family into the real one, through the Gramps Web API.

Typical case (Ancestry GEDCOM import): F0172 is "father only, Marriage 22 Feb 1919", while F0072 is
the same couple with the same marriage and the children. Everything F0172 has moves to F0072:

  partners   a partner the real family lacks is set on it (a *different* partner aborts the merge)
  children   appended to the real family; each child's parent_family link is repointed
  events     an event of the same type and date as one the real family already has is folded into it
             (its citations, notes, media and attributes are appended); any other event is moved over
  the rest   family citations, notes, media, attributes and tags are appended (import-log notes are not)
  cleanup    the duplicate family is deleted, the partner's family_list is repaired if the API left the
             link behind, and events that nothing references any more are deleted

Dry run by default; it prints the plan. --apply writes. Take a Gramps backup first (runbook §8).

    python3 tools/merge_family.py --into F0072 --from F0172 --user chris
    python3 tools/merge_family.py --into F0072 --from F0172 --user chris --apply
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "export"))
from gramps_export import Api, ApiError, is_tool_note, plain_text, typestr  # noqa: E402

LIST_FIELDS = ("citation_list", "note_list", "media_list", "attribute_list", "tag_list")


def by_gid(api: Api, table: str, gid: str) -> dict:
    hits = api.get(f"{table}/", {"gramps_id": gid})
    if not hits:
        raise SystemExit(f"ERROR: {table[:-1]} {gid} not found")
    return hits[0]


def ref_key(x):
    """identity of a list entry: handle for refs, (type, value) for attributes, plain value otherwise"""
    if isinstance(x, dict):
        if "ref" in x:
            return ("ref", x["ref"])
        if "type" in x:
            return ("attr", typestr(x.get("type")), x.get("value"))
    return ("v", x if not isinstance(x, dict) else str(sorted(x.items())))


def append_missing(dst: dict, src: dict, field: str) -> int:
    have = {ref_key(x) for x in dst.get(field) or []}
    added = 0
    for x in src.get(field) or []:
        if field == "attribute_list" and typestr(x.get("type")).startswith("_"):
            continue                                   # _UID and friends: bookkeeping, not worth carrying over
        if ref_key(x) not in have:
            dst.setdefault(field, []).append(x)
            have.add(ref_key(x))
            added += 1
    return added


def date_sig(ev: dict):
    d = ev.get("date") or {}
    return (typestr(ev.get("type")), tuple(d.get("dateval") or ()), d.get("modifier", 0), (d.get("text") or "").strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"))
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"))
    ap.add_argument("--into", required=True, help="Gramps ID of the family that stays")
    ap.add_argument("--from", dest="dup", required=True, help="Gramps ID of the duplicate that goes")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    user = args.user or input("Gramps user: ")
    password = os.environ.get("GRAMPS_PASSWORD") or getpass.getpass("Gramps password: ")
    api = Api(args.api)
    try:
        api.login(user, password)
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    keep, dup = by_gid(api, "families", args.into), by_gid(api, "families", args.dup)
    if keep["handle"] == dup["handle"]:
        raise SystemExit("ERROR: same family twice")
    plan, puts, orig = [], {}, {}   # puts: (table, handle) -> object to PUT (only if it changed)

    def person(h):
        key = ("people", h)
        if key not in puts:
            puts[key] = api.get(f"people/{h}")
            orig[key] = json.dumps(puts[key], sort_keys=True)
        return puts[key]

    def label(h):
        if not h:
            return "—"
        p = person(h)
        n = p.get("primary_name") or {}
        sur = next((s.get("surname") for s in n.get("surname_list") or [] if s.get("primary")), "")
        return f"{p.get('gramps_id')} {n.get('first_name', '')} {sur}".strip()

    # partners
    shared = False
    for side in ("father_handle", "mother_handle"):
        k, d = keep.get(side), dup.get(side)
        if d and k and d != k:
            raise SystemExit(f"ERROR: different {side[:-7]}s ({label(k)} vs {label(d)}) — not a duplicate, not merging")
        if d and k:
            shared = True
        if d and not k:
            keep[side] = d
            plan.append(f"set {side[:-7]} {label(d)} on {args.into}")
            pp = person(d)
            if keep["handle"] not in pp.get("family_list", []):
                pp.setdefault("family_list", []).append(keep["handle"])
    if not shared and not any(dup.get(s) for s in ("father_handle", "mother_handle")):
        raise SystemExit("ERROR: the duplicate has no partners at all — merge by hand")
    if not shared:
        print("note: the two families share no partner; merging because one side is empty", file=sys.stderr)
    plan.insert(0, f"{args.into}: {label(keep.get('father_handle'))} × {label(keep.get('mother_handle'))}  ←  "
                   f"{args.dup}: {label(dup.get('father_handle'))} × {label(dup.get('mother_handle'))}")

    # children
    have_kids = {c["ref"] for c in keep.get("child_ref_list") or []}
    for cr in dup.get("child_ref_list") or []:
        ch = person(cr["ref"])
        pf = [h for h in ch.get("parent_family_list", []) if h != dup["handle"]]
        if cr["ref"] not in have_kids:
            keep.setdefault("child_ref_list", []).append(cr)
            have_kids.add(cr["ref"])
            plan.append(f"move child {label(cr['ref'])}")
            if keep["handle"] not in pf:
                pf.append(keep["handle"])
        else:
            plan.append(f"child {label(cr['ref'])} already in {args.into}")
        ch["parent_family_list"] = pf

    # events
    keep_events = {}
    for er in keep.get("event_ref_list") or []:
        ev = api.get(f"events/{er['ref']}")
        keep_events.setdefault(date_sig(ev), (er, ev))
    orphans = []
    for er in dup.get("event_ref_list") or []:
        ev = api.get(f"events/{er['ref']}")
        sig = date_sig(ev)
        if sig in keep_events:
            tgt = keep_events[sig][1]
            n = sum(append_missing(tgt, ev, f) for f in LIST_FIELDS)
            if ev.get("description") and not tgt.get("description"):
                tgt["description"] = ev["description"]
            if ev.get("place") and not tgt.get("place"):
                tgt["place"] = ev["place"]
            puts[("events", tgt["handle"])] = tgt
            orphans.append(ev)
            plan.append(f"fold {sig[0]} {ev.get('gramps_id')} into {tgt.get('gramps_id')} (+{n} citations/notes/media/attributes)")
        else:
            keep.setdefault("event_ref_list", []).append(er)
            plan.append(f"move event {sig[0]} {ev.get('gramps_id')}")

    # family-level lists
    for f in LIST_FIELDS:
        if f == "note_list":
            skip = [h for h in dup.get("note_list") or [] if is_tool_note(api.get(f"notes/{h}"))]
            src = {"note_list": [h for h in dup.get("note_list") or [] if h not in skip]}
            if skip:
                plan.append(f"drop {len(skip)} import-log note(s) with the duplicate")
            n = append_missing(keep, src, f)
        else:
            n = append_missing(keep, dup, f)
        if n:
            plan.append(f"append {n} to {f}")
    for side in ("father_handle", "mother_handle"):
        if dup.get(side):
            pp = person(dup[side])
            pp["family_list"] = [h for h in pp.get("family_list", []) if h != dup["handle"]]
    plan.append(f"delete {args.dup}" + (f" and {len(orphans)} folded event(s)" if orphans else ""))

    print("Plan:\n  " + "\n  ".join(plan))
    if not args.apply:
        print("\n(dry run — add --apply to write)")
        return 0

    puts[("families", keep["handle"])] = keep
    order = [k for k in puts if k[0] == "events"] + [("families", keep["handle"])] + [k for k in puts if k[0] == "people"]
    for table, h in order:
        if orig.get((table, h)) == json.dumps(puts[(table, h)], sort_keys=True):
            continue
        api.send("PUT", f"{table}/{h}", puts[(table, h)])
    api.send("DELETE", f"families/{dup['handle']}")
    # the API may or may not have cleaned the back-links; make sure
    for h in {dup.get("father_handle"), dup.get("mother_handle")} | {c["ref"] for c in dup.get("child_ref_list") or []}:
        if not h:
            continue
        p = api.get(f"people/{h}")
        fl = [x for x in p.get("family_list", []) if x != dup["handle"]]
        pl = [x for x in p.get("parent_family_list", []) if x != dup["handle"]]
        if fl != p.get("family_list", []) or pl != p.get("parent_family_list", []):
            p["family_list"], p["parent_family_list"] = fl, pl
            api.send("PUT", f"people/{h}", p)
    for ev in orphans:
        try:
            info = api.get(f"events/{ev['handle']}", {"backlinks": 1})
            links = info.get("backlinks") if isinstance(info, dict) else None
            if links is None or any(links.values()):
                print(f"  kept event {ev.get('gramps_id')}: still referenced or backlinks unknown")
                continue
            api.send("DELETE", f"events/{ev['handle']}")
        except ApiError as e:
            print(f"  event {ev.get('gramps_id')} not deleted: {e}")
    print(f"merged {args.dup} into {args.into}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
