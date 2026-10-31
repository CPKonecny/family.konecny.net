# family.konecny.net — server runbook (export, validator, geocoder)

`gramps_export.py` turns the Gramps master into the public-safe data bundle the site builds from. It runs on the server, talks only to the loopback API (`http://127.0.0.1:5055/api`), needs nothing beyond Python 3.8's standard library, and writes exactly three things into the site repo: `data/bundle.json`, `data/report.md`, `media/` (public files + thumbnails). Export 0.3.1 · validator 0.5 · geocoder 0.1.1 · bundle schema 0.3 · all tested against gramps-webapi 3.24 / Gramps 6.0.8. Three scripts live in `export/`; they share one API client and never store a password.

## 1. First dry run (done 27 Oct 2026; kept for a rebuild)

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
  "withhold":     {"E0450": "event text mentions a living person"},
  "public_text":  {"N0812": "names a living cousin by name and relationship only — fine"}
}
```

Precedence per person: `exclude` → private flag → `deceased` → `living` → Gramps' own probably-alive verdict (`GET /api/living/{handle}`, max age 110, overridable with `--max-age`). `public_media` forces a file public; `withhold` keeps any family, event, citation, note or media object off the site regardless of other rules. A note whose text names a living or excluded person (first name + surname, any of their names) is held back automatically and listed in the report's warnings; `public_text` releases one after you have read it.

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

## 6. Validator — `validator.py` (operating model §7)

The second agent. For each citation still tagged `unreviewed` or `agent-proposed` it collects the claims the citation is attached to (the events, the person, the name), the source and citation text, the transcription notes, and the record image through the API's thumbnail endpoint, and asks a Claude model one question: does this record support these claims? The model is a different family from Karel's, as the operating model asks. One verdict file per citation lands in `~/gramps-tools/validation/`; with `--apply` the verdict also goes into Gramps:

| verdict | tags written | also |
|---|---|---|
| `supports` or `consistent`, no flags | `ai-validated` on the citation and on each claim | reasoning as a Research note on the citation; `Tier` attribute added when the citation has none; `agent-validator` provenance tag |
| anything else (`partial`, `contradicts`, `unreadable`, `irrelevant`, or any flag) | `needs-human` | same |

Rules it keeps: exactly one review tag per object; a human `reviewed` tag is never touched; `needs-human` on a fact is sticky until a person clears it, so a second citation that "supports" cannot hide a conflict another citation raised. Direct lines first (ancestors of the root person in `site/config.json`), closest generations first.

```
install -d -m 700 ~/.config/family-site && (umask 077; read -rsp 'Anthropic API key: ' k; echo; printf '%s\n' "$k" > ~/.config/family-site/anthropic.key)
cd ~/gramps-tools/family-site/export && read -p 'Gramps user: ' u
python3 validator.py --dry-run --user "$u" | head -40        # queue, order, cost estimate; nothing called
python3 validator.py --limit 10 --apply --user "$u"           # first batch, written to Gramps — check one in Gramps Web
python3 validator.py --all --apply --user "$u"                # the rest (an evening; ~1,150 citations ≈ 15 USD on Sonnet)
```

`--apply` needs an Editor account (the `applier` account or yours); verdict files alone need only read access. The key comes from console.anthropic.com → API keys; the record images are sent to that API for reading, which is the same kind of exposure as Karel reading them on Grok. Useful flags: `--ids C0634,C0636` to re-check specific citations, `--redo` to re-run ones that have verdict files, `--only-direct`, `--model` to pick another Claude model (the script falls back to the newest Sonnet it can see), `--mock` to test the plumbing without a model.

**One writer at a time.** Gramps Web stores the tree in SQLite, which takes one writer at a time; two scripts writing at once (validator + geocoder, or either + Karel) produce `HTTP 500 … database is locked`. The scripts now wait and retry (2/5/15/30 s), and every verdict is saved to its file before the write, so nothing is lost: after a run with `apply failed` lines, `python3 validator.py --reapply --apply --user <you>` writes the saved verdicts without calling the model again. Running the queue again is also free for anything already decided — citations with a verdict file are skipped.

**Passwords.** Python's hidden prompt echoes on this server's terminal. Set the password once per session with `read -rsp 'Gramps password: ' GRAMPS_PASSWORD; echo; export GRAMPS_PASSWORD` — all three scripts read it — and `unset GRAMPS_PASSWORD` when done. The nightly export uses `~/.config/family-site/export.pw` (account `site-export`, role Guest). Gramps Web rate-limits logins (HTTP 429 when tools run back to back), so a fresh token is cached for 14 minutes in `~/.cache/family-site/` and reused; a 429 is retried after 20/40/60 s.

The site shows the result the next morning: `AI-checked` stamps, the validator's note on the citation page, and the tier instead of "est."

## 7. Geocoder — `geocode_backfill.py` (v1 of the site needs it)

Looks up every place without coordinates on Nominatim (OpenStreetMap, free, one request per second), writes `~/gramps-tools/geocode_review.csv`, and only on a second run with `--apply` writes latitude/longitude into Gramps. House numbers, "Ward 4" and the like are stripped before lookup; "USA", "Bohemia", "Moravia" are mapped to the modern country for the search only — nothing in the place name itself changes.

```
cd ~/gramps-tools/family-site/export && read -p 'Gramps user: ' u
python3 geocode_backfill.py --user "$u"            # ~6 minutes for 315 places; writes the review file
python3 geocode_backfill.py --apply --user "$u"    # after the review
```

In the CSV, review rows come first. `auto` = clean match, written as is. `review` = look once: the reason column says why (another place with the same name, only the district matched, name differs); fix lat/lon from mapy.cz or OpenStreetMap if needed and set status to `ok`, or leave it and it is skipped. `none` = nothing found; add coordinates by hand and set `ok`, or leave it. Re-running the lookup reuses the cache, so a second pass is quick. `--apply` skips places that already have coordinates and ends with `failed N` — if N > 0, run it again (see *one writer at a time* above). Splitting the flat Ancestry place strings into a Gramps hierarchy (town → county → state → country) is a separate, later step; the map does not need it.

## 8. Repairs and checks (`tools/`)

- `tools/check_stories.py` — runs in the GitHub build: fails it if a hand-written story names a living person (namesakes of deceased people are skipped and listed). Allow-list: `content/stories/living-ok.txt`.
- `tools/check_families.py` — read-only, no login: duplicate families (same partner twice, one side missing). Run after a batch of Karel's marriage proposals.
- `tools/backup_gramps.py` — full .gramps backup through the API to Z:\Backup\family-history. Run it before anything below that writes.
- `tools/merge_family.py --into F0072 --from F0172` — merges a duplicate family into the real one (partners, children, events folded by type+date, citations/notes/media); dry run by default, `--apply` writes. Refuses if the two families have different partners.
- `tools/set_note.py --person I0145 --starts "Death 28 Feb 1936" --file corrected.txt` — replaces one note's text with a file Karel sends; dry run by default.
- `tools/fix_tiers.py` — maps non-standard Tier values on citations (published, hint, newspaper …) to the six real ones; dry run by default.
- `tools/fix_dict_notes.py` — one-off from 31 Oct 2026: notes the applier stored as a Python dict (`{'type': 'Research', 'text': …}`); dry run by default, `--apply` writes.
- Notes are never published when their type is *GEDCOM import* or their text is an import report; attributes whose type starts with `_` (`_UID`, `_AMTID`) are dropped; literal HTML entities in note text (`&amp;`) are turned back into characters.

## 9. Mark a media object public (Gramps Web)

Open the media object → Tags → add `public`. It becomes a file on the site at the next export, provided it is not attached to a living person and its path is not under the Ancestry/FamilySearch/import folders. Our own scans belong under `photos/`, `documents/`, `letters/`; record images stay under `records/`.
