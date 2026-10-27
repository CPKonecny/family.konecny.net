# family.konecny.net — export runbook

`gramps_export.py` turns the Gramps master into the public-safe data bundle the site builds from. It runs on the server, talks only to the loopback API (`http://127.0.0.1:5055/api`), needs nothing beyond Python 3.8's standard library, and writes exactly three things into the site repo: `data/bundle.json`, `data/report.md`, `media/` (public files + thumbnails). Version 0.3.0 · bundle schema 0.3 · tested against gramps-webapi 3.24 / Gramps 6.0.8.

## 1. First dry run (now)

From your workstation, after downloading the two files from the chat:

```
scp gramps_export.py overrides.json server:~/gramps-tools/family-site/export/
```

On the server (prompts for your Gramps user and password; stores nothing; writes nothing):

```
mkdir -p ~/gramps-tools/family-site/export && cd ~/gramps-tools/family-site/export
python3 gramps_export.py --dry-run | tee ~/gramps-tools/export-dryrun-$(date +%F).md
```

What to look at in the report: the **People** table (full / name-only / excluded), the list *name-only without dates of their own*, the **Media** reasons, and **Warnings**. Expected on the current tree: ~145 media all login-only (every imported image carries `import-ancestry-2026-10-25`), a handful of name-only people who are actually deceased (born 1900–1926, no death event) — those go into `overrides.json`.

Exit codes: 0 fine · 1 dry run found leak-audit problems · 2 login/API error · 3 full run refused to write because the leak audit failed.

## 2. Overrides

`overrides.json` is the only hand-maintained input. Keys are Gramps IDs (or handles); values are free-text reasons so the file explains itself.

```json
{
  "living":       {"I0210": "asked not to be shown beyond name"},
  "deceased":     {"I0144": "d. 2019 per obituary, death event not entered yet"},
  "exclude":      {"I0301": "opted out entirely"},
  "public_media": {"O0012": "our own scan, tagged public but path looks like a record image"},
  "withhold":     {"E0450": "event text mentions a living person"}
}
```

Precedence per person: `exclude` → private flag → `deceased` → `living` → Gramps' own probably-alive verdict (`GET /api/living/{handle}`, max age 110, overridable with `--max-age`). `public_media` forces a file public; `withhold` keeps any event, citation, note or media object off the site regardless of other rules.

## 3. What the rules do (short form)

| Object | Published when | Reduced to |
|---|---|---|
| Person | not private, not excluded | **living**: name, gender, parent/spouse family links only. **full**: everything |
| Family | not private; at least one publishable member | partner living → partners + children refs only, no events |
| Event | reached from a full person/family; not private; not referenced by any living/excluded person or family | — |
| Place | reached from a published event, plus every enclosing place; not private | — |
| Citation / Source / Repository | reached from a published object; not private | — |
| Note | reached from a published object; not private | plain text + minimal HTML |
| Media | reached from a published object; not private | `public` (file copied) only if tagged `public`, not attached to a living person, and not an Ancestry/FamilySearch record image by path or import tag; otherwise `login` (metadata + link to familytree.konecny.net) |

Review state per object from tags: `reviewed` → `human`, `ai-validated` → `ai`, anything else → `unreviewed`; `needs-human`, `agent-proposed`, `sync-to-ancestry` are carried as flags; provenance tags (`import-…`, `agent-…`, `chris`) are carried as `provenance`.

Before writing, the script audits its own output: no living/excluded person outside name-and-relationship fields, no `private` flag anywhere, every reference resolvable. A failed audit means no bundle is written.

## 4. Nightly run (after v0 is live)

Use a dedicated **Guest-role** Gramps Web account for the export. Guest never sees private objects, so the API strips them before this script even runs — the private flag is enforced twice. Create the user in Gramps Web (Settings → Users → `site-export`, role Guest), then on the server:

```
install -d -m 700 ~/.config/family-site && (umask 077; read -rsp 'site-export password: ' p; echo; printf '%s\n' "$p" > ~/.config/family-site/export.pw)
ssh-keygen -t ed25519 -N '' -C family-site-export -f ~/.ssh/family_site_deploy && cat ~/.ssh/family_site_deploy.pub
```

Add that public key as a **deploy key with write access** on the GitHub repo, then:

```
cd ~/gramps-tools && GIT_SSH_COMMAND='ssh -i ~/.ssh/family_site_deploy -o IdentitiesOnly=yes' git clone git@github.com:<github-user>/family.konecny.net.git family-site
cd family-site && GRAMPS_USER=site-export python3 export/gramps_export.py --password-file ~/.config/family-site/export.pw --dry-run | tail -40
```

If that dry run looks right, install the cron job (03:15 server time; the script pulls, exports, commits only `data/` and `media/`, pushes; GitHub Actions does the rest):

```
chmod +x ~/gramps-tools/family-site/export/publish.sh
( crontab -l 2>/dev/null; echo '15 3 * * * ~/gramps-tools/family-site/export/publish.sh >> ~/gramps-tools/family-site-export.log 2>&1' ) | crontab -
```

Check: `tail ~/gramps-tools/family-site-export.log` the next morning; `data/report.md` in the repo is the full report of the last export.

## 5. Options worth knowing

```
--dry-run                 report only, write nothing
--raw-dir DIR             also cache the raw API responses (debugging; keep private, it contains everything)
--from-raw DIR            rebuild from a raw cache without touching the API
--today YYYY-MM-DD        pin the date used by the living rule
--max-age 110             years after birth a person is assumed alive
--public-media-tag public the Gramps tag that marks a media object public
--private-site URL        base URL for login-only media links (default https://familytree.konecny.net)
--no-living-api           use the script's own simplified living rule instead of Gramps' (not recommended)
--thumb 600               thumbnail size for public media
```

## 6. Mark a media object public (Gramps Web)

Open the media object → Tags → add `public`. It becomes a file on the site at the next export, provided it is not attached to a living person and its path is not under the Ancestry/FamilySearch/import folders. Our own scans belong under `photos/`, `documents/`, `letters/`; record images stay under `records/`.
