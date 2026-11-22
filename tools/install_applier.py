#!/usr/bin/env python3
"""Install tools/apply_proposals.py from this repo as the applier that cron runs.

    cd ~/gramps-tools/family-site && python3 tools/install_applier.py          # test, check, back up, install, dry run
    python3 tools/install_applier.py --check                                   # say what would happen, change nothing

It refuses to install over an applier it does not know: the new one was built on a particular earlier
version, and anything else on the server means somebody changed it in between (the same rule as for
site updates). It also shows the cron line and the last lines of the cron log, and puts a copy of the
applier and of tools/PROPOSALS.md on the share where researchers can read them.
Exit 0 installed or already current; 1 refused; 2 the tests failed.
"""
import argparse
import datetime as dt
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEW = os.path.join(HERE, "apply_proposals.py")
TARGET = os.path.expanduser("~/gramps-tools/apply_proposals.py")
SHARE_COPY = "/mnt/bulk8/Archive/Backup/family-history/apply_proposals.py"     # the copy researchers can read
BUILT_ON = {
    "0af6dedeff525b171ef13774ced2d7ba359b559caeb6abf3aafab4906e953ff1": "0.2.3 with the 31 Oct 2026 note patches",
    "659667c58b36e768278fa12146d30a5966620f1c677660ebe248fc09c551633c": "0.3.0 of 11 Nov 2026",
    "9f67e121b46df49c3b420ac2117bb8ac19d274ba566eb07a4b6a468d54f6490e": "0.3.1 of 16 Nov 2026",
}
SECRET = re.compile(r"(?i)((?:pass|password|token|key|secret)\w*\s*[=:]\s*)\S+")


def show_last_runs(cron):
    """When cron last ran the applier and what it said, from the log the cron line writes to. Read-only."""
    logs = [os.path.expanduser(m.replace("$HOME", "~")) for m in re.findall(r">>?\s*([^\s;&|]+)", cron) if "/dev/null" not in m]
    logs = [l for l in logs if os.path.isfile(l)] or sorted(glob.glob(os.path.expanduser("~/logs/applier*.log")))
    if not logs:
        print("applier log: none found (the cron line does not write one)")
        return
    log = logs[0]
    age = (dt.datetime.now() - dt.datetime.fromtimestamp(os.path.getmtime(log))).total_seconds() / 60
    print(f"applier log: {log}, last written {dt.datetime.fromtimestamp(os.path.getmtime(log)):%Y-%m-%d %H:%M} "
          f"({age:.0f} min ago; now {dt.datetime.now():%H:%M})")
    with open(log, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()[-12:]
    for line in lines:
        print("    | " + SECRET.sub(r"\1***", line)[:220])


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=TARGET)
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--force", action="store_true", help="install even over an unknown version (Claude said so)")
    args = ap.parse_args()

    new = sha(NEW)
    have = sha(args.target) if os.path.exists(args.target) else None
    cron = subprocess.run("crontab -l 2>/dev/null | grep -n apply_proposals", shell=True, capture_output=True, text=True).stdout.strip()
    cron = SECRET.sub(r"\1***", cron)                                              # never echo a secret, should one be inline
    print("cron runs: " + (cron.replace("\n", "\n           ") if cron else "(no crontab line mentions apply_proposals)"))
    try:
        show_last_runs(cron)
    except Exception as ex:                                                         # never let the report stop the install
        print(f"applier log: could not be read ({type(ex).__name__}: {ex})")
    if have is None and not args.force:
        print(f"STOP: there is no applier at {args.target}, so cron must be running it from somewhere else.\n"
              f"Nothing was changed. Send Claude the 'cron runs' line above.", file=sys.stderr)
        return 1
    if have == new:
        print(f"{args.target} is already this version — nothing to install")
        return 0
    if have and have not in BUILT_ON and not args.force:
        print(f"STOP: {args.target} is not the applier this update was built on.\n"
              f"  its sha256 is {have}\n"
              f"Nothing was changed. Send Claude that file.", file=sys.stderr)
        return 1
    print(f"installed now: {BUILT_ON.get(have, 'none' if not have else 'unknown version (forced)')}")

    r = subprocess.run([sys.executable, os.path.join(HERE, "test_apply_proposals.py")], capture_output=True, text=True)
    tail = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    print(f"tests: {tail}")
    if r.returncode != 0:
        print(r.stdout[-3000:] + r.stderr[-2000:], file=sys.stderr)
        print("STOP: the applier's tests fail on this machine. Nothing was changed. Send Claude the lines above.", file=sys.stderr)
        return 2
    if args.check:
        print("(--check: nothing installed)")
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    for dest in (args.target, SHARE_COPY):
        if dest == SHARE_COPY and not os.path.isdir(os.path.dirname(dest)):
            continue
        if os.path.exists(dest):
            shutil.copy2(dest, f"{dest}.bak-{stamp}")
        tmp = dest + ".new"
        shutil.copy2(NEW, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)                       # in one step: a cron run sees the old file or the new, never half
        print(f"installed {dest}" + (f"  (old one kept as {os.path.basename(dest)}.bak-{stamp})" if os.path.exists(f"{dest}.bak-{stamp}") else ""))

    share = os.path.dirname(SHARE_COPY)
    if os.path.isdir(share):                        # the reference, beside the applier's copy, for whoever writes proposals
        shutil.copy2(os.path.join(HERE, "PROPOSALS.md"), os.path.join(share, "PROPOSALS.md.new"))
        os.replace(os.path.join(share, "PROPOSALS.md.new"), os.path.join(share, "PROPOSALS.md"))
        print(f"installed {os.path.join(share, 'PROPOSALS.md')}  (the reference for proposal files)")
    r = subprocess.run([sys.executable, args.target, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    print(f"the applier cron runs is now version {r.stdout.strip() or '?'}")
    print("\ndry run with the new applier (writes nothing):")
    r = subprocess.run([sys.executable, args.target, "--dry-run"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = (r.stdout + r.stderr).strip().splitlines()
    for line in out[-25:]:
        print("  " + line)
    if r.returncode != 0 or not any(line.startswith("tree:") for line in out):
        print("\nThe dry run did not get as far as reading the tree. If it says 'login failed' or stops at a prompt, the\n"
              "applier could not find its login: cron passes it in, so the half-hourly runs are not affected, but\n"
              "--held and --approve need GRAMPS_USER and GRAMPS_PASS in ~/gramps-tools/.applier.env. Tell Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
