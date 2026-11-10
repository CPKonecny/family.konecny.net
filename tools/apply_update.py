#!/usr/bin/env python3
"""Pull a site update into this clone and push it.

    cd ~/gramps-tools/family-site
    python3 tools/apply_update.py site-update-10          # finds site-update-10.bundle on the Backup share
    python3 tools/apply_update.py /path/to/update.bundle
    python3 tools/apply_update.py site-update-10 --no-push

An update is a git bundle: one or more commits Claude made on top of `main` as it was at the time.
Git merges them into `main` as it is now, so other people's changes since then are kept. If the same
lines were changed on both sides the merge is undone, nothing is pushed, and the update has to be
redone on the current `main`.

Never unzip or copy site files over this folder instead: that is how the Family stories pages were
lost on 6 November 2026.

Exit 0 done (or already applied); 1 conflict, nothing changed; 2 bundle not found, uncommitted changes
here, or a git failure.
"""
import argparse
import os
import subprocess
import sys

SEARCH = ["/mnt/bulk8/Archive/Backup", "/mnt/bulk8/Archive", os.path.expanduser("~")]
DEPLOY_KEY = os.path.expanduser("~/.ssh/family_site_deploy")


def run(*args, check=True):
    env = dict(os.environ)
    if os.path.exists(DEPLOY_KEY) and "GIT_SSH_COMMAND" not in env:
        env["GIT_SSH_COMMAND"] = f"ssh -i {DEPLOY_KEY} -o IdentitiesOnly=yes"
    r = subprocess.run(["git", *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        print(f"git {' '.join(args)} failed:\n{(r.stderr or r.stdout).strip()}", file=sys.stderr)
        raise SystemExit(2)
    return r


def out(*args) -> str:
    return run(*args).stdout.rstrip("\n")


def find_bundle(name: str) -> str:
    if os.path.isfile(name):
        return os.path.abspath(name)
    base = os.path.basename(name if name.endswith(".bundle") else name + ".bundle")
    for d in SEARCH:
        p = os.path.join(d, base)
        if os.path.isfile(p):
            return p
    print(f"ERROR: {base} not found in {', '.join(SEARCH)}", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("update", help="a name like site-update-10, or the path of a .bundle file")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-pull", action="store_true", help="do not fetch main first (testing)")
    args = ap.parse_args()

    os.chdir(out("rev-parse", "--show-toplevel"))
    if not os.path.exists("site/build.py"):
        print("ERROR: run this inside the family.konecny.net clone", file=sys.stderr)
        return 2
    bundle = find_bundle(args.update)
    dirty = [ln for ln in out("status", "--porcelain").splitlines() if ln and not ln.startswith("??")]
    if dirty:
        print("STOP: uncommitted changes here — commit or discard them first. Nothing changed.\n  " + "\n  ".join(dirty), file=sys.stderr)
        return 2

    if not args.no_pull:
        run("pull", "-q", "--rebase")
    run("bundle", "verify", "-q", bundle)                       # fails if this clone lacks the commit the update starts from
    heads = out("bundle", "list-heads", bundle).splitlines()
    ref = next((h.split()[1] for h in heads if h.split()[1].endswith("/main")), heads[0].split()[1])
    run("fetch", "-q", bundle, ref)
    if run("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD", check=False).returncode == 0:
        print(f"{os.path.basename(bundle)}: already applied — nothing to do")
        return 0
    commits = out("log", "--format=  %h %s", "HEAD..FETCH_HEAD")
    if run("merge", "-q", "--no-edit", "FETCH_HEAD", check=False).returncode != 0:
        clash = out("diff", "--name-only", "--diff-filter=U")
        run("merge", "--abort", check=False)
        print("STOP: this update and main changed the same lines in:\n  " + clash.replace("\n", "\n  ")
              + "\nThe merge was undone; nothing changed here and nothing was pushed. "
                "Tell Claude: the update has to be redone on the current main.", file=sys.stderr)
        return 1
    print(f"{os.path.basename(bundle)} merged:\n{commits}")
    if args.no_push:
        print("not pushed (--no-push)")
        return 0
    run("push", "-q")
    print("pushed " + out("rev-parse", "--short", "HEAD"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
