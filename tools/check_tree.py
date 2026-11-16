#!/usr/bin/env python3
"""Read-only: things in the published tree that usually mean a proposal went in half-finished.

    python3 tools/check_tree.py                 # reads data/bundle.json of this clone; no login, changes nothing
    python3 tools/check_tree.py path/to/bundle.json

It lists
  1. people linked to no one (no parents, no partner, no children);
  2. families with one parent whose parent also has another family (children filed with the father only
     end up apart from the couple's family), and families with exactly the same parents;
  3. people with more than one birth, death, burial or baptism, and what kind of double it is.

Each has a proposal that repairs it (tools/PROPOSALS.md): add_family or update_family for 1,
merge_family or update_family for 2, merge_event (or set.prefer_event, if both dates are evidence) for 3.
Living people appear in the bundle by name only, so their events are not seen here; a doubled event of
theirs can still be merged by naming its type instead of the two event IDs.
"""
import collections
import json
import os
import sys

ONCE = ("Birth", "Death", "Burial", "Baptism", "Christening", "Cremation")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "bundle.json")
    B = json.load(open(path, encoding="utf-8"))
    P, F, E = B["people"], B["families"], B["events"]
    name = lambda i: f"{i} {(P.get(i, {}).get('name') or {}).get('display') or '?'}" if i else "—"
    years = lambda i: "({}–{})".format((P[i].get("birth") or {}).get("year") or "", (P[i].get("death") or {}).get("year") or "") if P[i]["status"] == "full" else "(name only)"

    alone = sorted(i for i, p in P.items() if not p.get("parents") and not p.get("families"))
    print(f"1. People linked to no one: {len(alone)}")
    for i in alone:
        print(f"   {name(i)} {years(i)}")

    by_parent = collections.defaultdict(list)
    same = collections.defaultdict(list)
    for fid, f in F.items():
        same[(f.get("father"), f.get("mother"))].append(fid)
        for side in ("father", "mother"):
            if f.get(side):
                by_parent[f[side]].append(fid)
    split = {pid: fids for pid, fids in by_parent.items()
             if len(fids) > 1 and any(not (F[x].get("father") and F[x].get("mother")) for x in fids)}
    print(f"\n2. One-parent families beside another family of the same parent: {len(split)}")
    for pid, fids in sorted(split.items()):
        print(f"   {name(pid)}:")
        for x in sorted(fids):
            f = F[x]
            print(f"      {x}: {name(f.get('father'))} × {name(f.get('mother'))}; children: "
                  f"{', '.join(name(c['ref']) for c in f.get('children', [])) or 'none'}")
    twins = {k: v for k, v in same.items() if len(v) > 1}
    print(f"   Families with exactly the same parents: {len(twins)}" +
          "".join(f"\n      {', '.join(sorted(v))}: {name(k[0])} × {name(k[1])}" for k, v in sorted(twins.items(), key=str)))

    def empty(e):
        return not (e.get("date") or e.get("place") or (e.get("description") or "").strip() or e.get("citations") or e.get("notes") or e.get("media"))

    def ymd(e):
        d = e.get("date") or {}
        return (d.get("year") or 0, d.get("month") or 0, d.get("day") or 0)

    doubles = []
    for i, p in sorted(P.items()):
        if p["status"] != "full":
            continue
        mine = collections.defaultdict(list)
        for r in p.get("events") or []:
            e = E.get(r["ref"])
            if e and r.get("role", "Primary") == "Primary" and e["type"] in ONCE:
                mine[e["type"]].append(e)
        for etype, evs in mine.items():
            if len(evs) < 2:
                continue
            dated = [e for e in evs if ymd(e)[0]]
            if any(empty(e) for e in evs):
                kind = "an empty placeholder beside a real one: merge_event, keep the real one"
            elif len({ymd(e) for e in evs}) == 1:
                kind = "the same date twice: merge_event"
            elif len({ymd(e)[0] for e in dated}) == 1 and any(ymd(e)[1:] == (0, 0) for e in dated):
                kind = "a year and a full date in that year: merge_event, keep the full date"
            else:
                kind = "different dates: evidence to weigh, not a duplicate (set.prefer_event once one is established)"
            doubles.append((name(i), etype, kind, evs))
    print(f"\n3. People with more than one birth, death, burial or baptism: {len({d[0] for d in doubles})}")
    for who, etype, kind, evs in doubles:
        print(f"   {who}: {etype} — {kind}")
        for e in evs:
            print(f"      {e['id']}: {(e.get('date') or {}).get('text') or 'no date'}; {len(e.get('citations') or [])} citation(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
