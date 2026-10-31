#!/usr/bin/env python3
"""One-off repair (31 Oct 2026): research notes that the applier stored as a Python dict.

Karel's proposals carried  "note": {"type": "Research", "text": "…", "tier": "ai-draft"}  and
apply_proposals.py wrote str() of that whole dict into the note, after its date/agent prefix:

    2026-10-31 (karel): {'type': 'Research', 'text': "RESEARCH SUMMARY — …", 'tier': 'ai-draft'}

This turns each such note into

    2026-10-31 (karel): RESEARCH SUMMARY — …

    Tier: ai-draft

Type, tags and privacy are left as they are. Nothing else is touched.

    python3 tools/fix_dict_notes.py --user chris            # list what would change, write nothing
    python3 tools/fix_dict_notes.py --user chris --apply    # write to Gramps

Password: GRAMPS_PASSWORD in the environment, else a prompt.
"""
import argparse
import ast
import getpass
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "export"))
from gramps_export import Api, ApiError, plain_text  # noqa: E402

RX = re.compile(r"^(?P<prefix>\d{4}-\d{2}-\d{2} \([^)]+\): )?(?P<dict>\{'type':.*\})\s*$", re.S)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"))
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"))
    ap.add_argument("--apply", action="store_true", help="write the repaired text to Gramps")
    args = ap.parse_args()
    user = args.user or input("Gramps user: ")
    password = os.environ.get("GRAMPS_PASSWORD") or getpass.getpass("Gramps password: ")
    api = Api(args.api)
    try:
        api.login(user, password)
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    todo = []
    for n in api.paged("notes"):
        m = RX.match(plain_text(n.get("text")).strip())
        if not m:
            continue
        try:
            d = ast.literal_eval(m.group("dict"))
        except (ValueError, SyntaxError) as e:
            print(f"  {n.get('gramps_id')}: looks like a dict but does not parse ({e}); skipped", file=sys.stderr)
            continue
        if not isinstance(d, dict) or not str(d.get("text") or "").strip():
            continue
        new = (m.group("prefix") or "") + str(d["text"]).strip()
        if d.get("tier"):
            new += f"\n\nTier: {d['tier']}"
        todo.append((n, new))

    print(f"{len(todo)} notes stored as a dict")
    for n, new in todo[:2]:
        print(f"\n{n.get('gramps_id')} before: {plain_text(n.get('text'))[:150]!r}")
        print(f"{n.get('gramps_id')} after:  {new[:150]!r}")
    if not args.apply:
        print("\n(dry run — add --apply to write)" if todo else "")
        return 0

    done = failed = 0
    for n, new in todo:
        try:
            obj = api.get(f"notes/{n['handle']}")
            obj["text"] = {"_class": "StyledText", "string": new, "tags": []}
            api.send("PUT", f"notes/{n['handle']}", obj)
            done += 1
        except ApiError as e:
            failed += 1
            print(f"  {n.get('gramps_id')}: {e}", file=sys.stderr)
    print(f"repaired {done}; failed {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
