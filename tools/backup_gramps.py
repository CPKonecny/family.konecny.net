#!/usr/bin/env python3
"""Download a full Gramps XML backup (.gramps, gzip) through the API, before anything that rewrites the tree.

    python3 tools/backup_gramps.py --user chris            # -> /mnt/bulk8/Archive/Backup/family-history/gramps-backup-<date>.gramps
    python3 tools/backup_gramps.py --user chris --out /path/file.gramps

Exits 1 if the file is suspiciously small, so `backup_gramps.py && merge_family.py …` never merges without a backup.
"""
import argparse
import datetime as dt
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "export"))
from gramps_export import Api, ApiError  # noqa: E402

DEFAULT_DIR = "/mnt/bulk8/Archive/Backup/family-history"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("GRAMPS_API", "http://127.0.0.1:5055/api"))
    ap.add_argument("--user", default=os.environ.get("GRAMPS_USER"))
    ap.add_argument("--out")
    args = ap.parse_args()
    user = args.user or input("Gramps user: ")
    password = os.environ.get("GRAMPS_PASSWORD") or getpass.getpass("Gramps password: ")
    api = Api(args.api, timeout=300)
    try:
        api.login(user, password)
        data = api.get_bytes("exporters/gramps/file")
    except ApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    out = args.out or os.path.join(DEFAULT_DIR if os.path.isdir(DEFAULT_DIR) else os.path.expanduser("~/gramps-tools"),
                                   f"gramps-backup-{dt.datetime.now():%Y-%m-%d_%H%M}.gramps")
    with open(out, "wb") as f:
        f.write(data)
    print(f"backup: {len(data) // 1024:,} KB -> {out}")
    if len(data) < 100_000:
        print("ERROR: that is too small for this tree — not trusting it", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
