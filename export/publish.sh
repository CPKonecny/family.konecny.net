#!/usr/bin/env bash
# Nightly: export the public-safe bundle from Gramps and push it to the site repo.
# Runs on the server from cron. Needs: the repo cloned over SSH, a write deploy key,
# and the read-only export account's password in a chmod-600 file.
set -euo pipefail
cd "$(dirname "$0")/.."
export GIT_SSH_COMMAND='ssh -i ~/.ssh/family_site_deploy -o IdentitiesOnly=yes'
git pull -q --ff-only
GRAMPS_USER="${GRAMPS_USER:-site-export}" \
python3 export/gramps_export.py --password-file ~/.config/family-site/export.pw --out . > /tmp/family-export-report.md
git add -A data media
if git diff --cached --quiet; then
  echo "$(date -Is) export unchanged"
  exit 0
fi
git -c user.name=gramps-export -c user.email=export@konecny.net commit -q -m "data: export $(date -u +%F)"
git push -q
echo "$(date -Is) pushed $(git rev-parse --short HEAD)"
