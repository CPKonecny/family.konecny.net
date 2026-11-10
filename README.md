# family.konecny.net

The public family-history site, generated from a public-safe export of the Gramps research database and published on GitHub Pages. The public internet never touches the server or the Gramps API: the server exports, pushes; GitHub builds, serves.

```
export/      gramps_export.py (runs on the server), overrides.json, publish.sh, README (runbook)
data/        bundle.json + report.md — written by the export, committed by the server, never by hand
media/       public media files + thumbnails — same
site/        build.py, config.json, templates/, static/ (site.css, site.js, fonts/) — the generator
content/     about.md, research/board.json, stories/ — hand-written (the export never writes here; see the READMEs there)
tools/       checks and repairs run by hand or in the build (check_stories.py runs in both workflows; apply_update.py pulls a code update from a git bundle)
.github/     pages.yml: on every push to main, check the stories, build _site/ and deploy; check.yml: build every pull request
CNAME        family.konecny.net
```

## Who changes what

- **Claude** owns everything that is code: `site/`, `export/`, `tools/`, `.github/`, `site/config.json`. Changes arrive as commits: a git bundle Chris pulls on the server (`python3 tools/apply_update.py <name>`), or a PR Chris merges.
- **Karel** owns research content: `content/stories/` and `content/research/board.json`, by pull request to `main`, content files only. A PR that touches anything outside `content/` is sent back; if content needs a template change, the PR description says so and Claude makes it first.
- **The server** owns `data/` and `media/`: written by the nightly export, never by hand.
- **Chris** merges PRs and approves anything that changes what the site shows about a person.

`.github/CODEOWNERS` records the same split, and `.github/workflows/check.yml` enforces it: every pull request is built as it would be after merging and shows a tick or a cross; a pull request from a `cursor/` branch fails if it touches anything outside `content/`, and any pull request fails if a research card would be hidden for naming a living person.

Two working rules:

- **One content pull request at a time, against `main`.** No stacking. If the previous one is still open, push to its branch instead of opening another on top of it.
- **Code changes start from the current `main`.** Clone or fetch first, whoever you are — this thread, another Claude session, anyone — and deliver commits (a bundle or a pull request), never loose files copied over the server's clone. On 6 November 2026 an update unzipped over newer files took the Family stories pages down until they were restored the next day. `tools/apply_update.py` merges a bundle with git, so other people's changes are kept or the clash is reported, and nothing is overwritten silently.

## Build locally

```
pip install -r site/requirements.txt
python3 site/build.py            # -> _site/ ; open _site/index.html through any static server
python3 -m http.server -d _site 8000
```

Without `data/bundle.json` the build produces a placeholder site, so Pages and DNS can be set up before the first export lands.

## Configure

`site/config.json` names the site, the private site URL and the lineage routes. A route is a `from` and a `to` person; references can be a Gramps ID (`I0123`), `ancestry:<Ancestry person id>` (works for deceased people, who carry the Ancestry ID attribute) or `name:First Last` (for living people, who carry nothing else). `surnames` colour the people of that line throughout the site. `world_events` feeds the v1 timeline.

## What the site shows

Everything on the person, family, place and source pages comes from `data/bundle.json` (schema in `Konecny_Site_Bundle_Schema`). Living people are name and relationships only; private objects never reach the bundle; record images from Ancestry/FamilySearch are linked to the family login, not copied; every fact shows its citations ranked by tier and its review state as tagged in Gramps. Two sections are hand-written and never touched by the export: the family stories in `content/stories/` and the research board in `content/research/board.json` (the `/research/` page; the flagged-facts list and activity counts on that page are generated). The build drops any story or board text that names a living person in full.

The three typefaces (Bricolage Grotesque, Source Serif 4, IBM Plex Mono — all SIL Open Font License) are served from `site/static/fonts/`; the site makes no third-party requests.

## Roadmap

- **v0** (this): lineage routes, person and family pages with evidence ladders, citation and source pages with transcriptions, places, family stories, the research board, about.
- **v1**: migration map with a time slider (after the geocoding backfill into Gramps), lifespan timeline against world events.
- **v2**: how-are-we-related path finder in the browser, rooted-at-me entry links, name and place evolution, period map overlays.
