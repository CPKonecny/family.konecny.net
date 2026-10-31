# family.konecny.net

The public family-history site, generated from a public-safe export of the Gramps research database and published on GitHub Pages. The public internet never touches the server or the Gramps API: the server exports, pushes; GitHub builds, serves.

```
export/      gramps_export.py (runs on the server), overrides.json, publish.sh, README (runbook)
data/        bundle.json + report.md — written by the export, committed by the server, never by hand
media/       public media files + thumbnails — same
site/        build.py, config.json, templates/, static/ — the generator
content/     about.md, open-questions.md, stories/ — hand-written (the export never writes here; see content/stories/README.md)
tools/       log_to_open_questions.py — research log → content/open-questions.md
.github/     Pages workflow: on every push to main, build _site/ and deploy
CNAME        family.konecny.net
```

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

Everything on the person, family, place and source pages comes from `data/bundle.json` (schema in `Konecny_Site_Bundle_Schema`). Living people are name and relationships only; private objects never reach the bundle; record images from Ancestry/FamilySearch are linked to the family login, not copied; every fact shows its citations ranked by tier and its review state as tagged in Gramps. Family stories are the exception: hand-written pages in `content/stories/`, built with the rest of the site and never touched by the export.

## Roadmap

- **v0** (this): lineage routes, person and family pages with evidence ladders, citation and source pages with transcriptions, places, open questions, about.
- **v1**: migration map with a time slider (after the geocoding backfill into Gramps), lifespan timeline against world events.
- **v2**: how-are-we-related path finder in the browser, rooted-at-me entry links, name and place evolution, period map overlays.
