#!/usr/bin/env python3
"""Replace the text of one note in Gramps with the contents of a file (for corrected notes Karel sends).

Pick the note by its ID, or by the person it is attached to plus the first words of its current text:

    python3 tools/set_note.py --person I0145 --starts "Death 28 Feb 1936" --file /mnt/bulk8/Archive/Backup/peter-note-corrected.txt --user chris
    python3 tools/set_note.py --id N0612 --file … --user chris --apply

Dry run by default: shows the note it found and the first line before and after. Type, tags, privacy and
any style spans in the old text are dropped only for the text — the note keeps its type, tags and flag.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "export"))
from gramps_export import Api, ApiError, plain_text  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"))
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"))
    ap.add_argument("--id", help="note Gramps ID")
    ap.add_argument("--person", help="person Gramps ID the note is attached to")
    ap.add_argument("--starts", help="first words of the note's current text (with --person)")
    ap.add_argument("--file", required=True, help="UTF-8 text file with the new text")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not args.id and not (args.person and args.starts):
        ap.error("give --id, or --person together with --starts")
    with open(args.file, encoding="utf-8-sig") as f:
        new = f.read().replace("\r\n", "\n").strip() + "\n"
    if len(new) < 20:
        raise SystemExit("ERROR: the file is almost empty — wrong file?")

    user = args.user or input("Gramps user: ")
    password = os.environ.get("GRAMPS_PASSWORD") or getpass.getpass("Gramps password: ")
    api = Api(args.api)
    try:
        api.login(user, password)
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.id:
        hits = api.get("notes/", {"gramps_id": args.id})
        if not hits:
            raise SystemExit(f"ERROR: note {args.id} not found")
        note = hits[0]
    else:
        people = api.get("people/", {"gramps_id": args.person})
        if not people:
            raise SystemExit(f"ERROR: person {args.person} not found")
        want = " ".join(args.starts.split()).lower()
        found = []
        for nh in people[0].get("note_list") or []:
            n = api.get(f"notes/{nh}")
            if " ".join(plain_text(n.get("text")).split()).lower().startswith(want):
                found.append(n)
        if len(found) != 1:
            raise SystemExit(f"ERROR: {len(found)} notes on {args.person} start with {args.starts!r} — need exactly one")
        note = found[0]

    old = plain_text(note.get("text"))
    print(f"note {note.get('gramps_id')} ({note.get('type')}), {len(old):,} chars -> {len(new):,} chars")
    print(f"  before: {old.strip()[:110]!r}")
    print(f"  after:  {new.strip()[:110]!r}")
    if old.strip() == new.strip():
        print("  (identical — nothing to do)")
        return 0
    if not args.apply:
        print("(dry run — add --apply to write)")
        return 0
    note["text"] = {"_class": "StyledText", "string": new.rstrip("\n"), "tags": []}
    api.send("PUT", f"notes/{note['handle']}", note)
    print("written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
