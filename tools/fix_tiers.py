#!/usr/bin/env python3
"""Normalise the Tier attribute on citations to the operating model's values.

record · index · compiled · tree · oral · ai-draft are the only tiers. Anything else that agents have
written is mapped (published → compiled, hint → tree, newspaper → record, …) or listed if unknown.

    python3 tools/fix_tiers.py --user chris            # list what would change
    python3 tools/fix_tiers.py --user chris --apply    # write
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "export"))
from gramps_export import Api, ApiError, typestr  # noqa: E402

TIERS = ("record", "index", "compiled", "tree", "oral", "ai-draft")
ALIASES = {"published": "compiled", "book": "compiled", "secondary": "compiled", "compiled genealogy": "compiled",
           "hint": "tree", "member tree": "tree", "family tree": "tree", "ancestry tree": "tree",
           "newspaper": "record", "original": "record", "primary": "record", "image": "record", "vital": "record",
           "index record": "index", "indexed": "index", "transcription": "index",
           "family memory": "oral", "interview": "oral", "letter": "oral",
           "ai": "ai-draft", "draft": "ai-draft", "ai draft": "ai-draft"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"))
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"))
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
    todo, unknown = [], []
    for c in api.paged("citations"):
        for a in c.get("attribute_list") or []:
            if typestr(a.get("type")) != "Tier":
                continue
            v = (a.get("value") or "").strip()
            if v in TIERS:
                continue
            new = ALIASES.get(v.lower())
            if new:
                todo.append((c, v, new))
            else:
                unknown.append(f"{c.get('gramps_id')} '{v}'")
    for c, v, new in todo:
        print(f"  {c.get('gramps_id')}: {v} -> {new}")
    print(f"{len(todo)} to change" + (f"; unknown, left alone: {', '.join(unknown)}" if unknown else ""))
    if not args.apply:
        return 0
    done = 0
    for c, v, new in todo:
        obj = api.get(f"citations/{c['handle']}")
        for a in obj.get("attribute_list") or []:
            if typestr(a.get("type")) == "Tier" and (a.get("value") or "").strip() == v:
                a["value"] = new
        api.send("PUT", f"citations/{c['handle']}", obj)
        done += 1
    print(f"changed {done}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
