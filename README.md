# family.konecny.net

The public family-history site, generated from a public-safe export of the Gramps research database and published on GitHub Pages. The public internet never touches the server or the Gramps API: the server exports, pushes; GitHub builds, serves.

```
export/      gramps_export.py (runs on the server), overrides.json, publish.sh, README (runbook)
data/        bundle.json + report.md — written by the export, committed by the server, never by hand
media/       public media files + thumbnails — same
site/        build.py, config.json, templates/, static/ (site.css, site.js, tree.css, tree.js, fonts/) — the generator
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
- **The research database (Gramps) is changed only through proposal files.** A researcher files a proposal; `tools/apply_proposals.py` (the applier, installed on the server with `tools/install_applier.py`) checks and applies it. The file format is in `tools/PROPOSALS.md`. Changes to records already in the tree, and anything about living, recent or private people, wait in `held/` until Chris approves them from the applier's own description of what they do. Nobody edits Gramps by hand except Chris.

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

## The tree explorer

`/tree/` shows everyone in one picture, three ways. **Long view**: older is always up and younger always down. The person you are with is drawn largest, on a line across the picture. Their ancestors recede above it toward a horizon, each generation sharing the width of the view (father's side left, mother's side right); below it are younger people: the way back down to the person at the centre, and that person's descendants. The view narrows to the branch you walk into. **Tree**: the same people from above, ancestors up and descendants down, on a true time scale. **Fan**: ancestors in rings, each wedge sized by how much of that branch is known; it opens on the inner rings, where the names can be read, and Fit shows all of it. The lines from `config.json` can be followed like a flight, and the Evidence switch recolours everything by the review state of each person's facts.

Any person can be the centre: “Stand here”, a double-tap, Enter, or the button under the picture, which counts the family that would open (“40 descendants” beside an ancestor, “34 ancestors” beside a descendant). With someone at the centre, all their ancestors are above them and all their descendants below, so you can walk back a few generations, open that ancestor's family, and walk down another branch without the picture ever turning round. The way you came stays marked and is the route that way to begin with; the browser's Back button returns to the centre before. To travel, drag the picture (it follows the finger: pull down and older people come to you), scroll as a page scrolls, use the arrow keys or the year slider, or press Fly. A route never stops on a child under 18. On a short picture, such as a phone with the browser's bars showing, the people you are with are named rather than given cards, so both halves keep their room.

- `site/static/tree.js` draws it all on one canvas, with no library and no third-party request. `site/static/tree.css` holds the page chrome; every class starts with `tx-`. `site/templates/tree.html` is the page. The page asks for both by content (`/static/tree.js?v=…`, from `asset()` in `site/build.py`), so after an update a reload brings the page and its script together instead of pairing the new page with a script the browser kept.
- The data is `/data/tree.json` (about 100 KB), written by `tree_data()` in `site/build.py` from the same bundle as the rest of the site: names and family links for everyone; years, towns, first crossing, review counts and open research cards only for people shown in full. A living person is a name and family links, a child under 18 the “Living child” placeholder. The export test checks that. Names in the panel, the lists and the search are the ones the rest of the site shows (“First (Birth) Married”); the picture labels people by first name and birth surname, as a pedigree does.
- Living and undated people have no public year. They are placed by generation from their nearest dated relatives, drawn hollow, and no year line is drawn from the first of them onward.
- Links in: `/tree/#long-I0145` (also `tree-` and `fan-`) opens a view on a person; `/tree/#long-I0141-wood` also follows that line. Person pages, line pages and the home page link this way.
- In the explorer the Morris line has its own colour (`--t-morris` in `tree.css`), because a tree rooted on one of the children shows both sides at once. Elsewhere on the site it still borrows the Wood colour from `config.json`.

## Roadmap

- **v0** (this): lineage routes, person and family pages with evidence ladders, citation and source pages with transcriptions, places, family stories, the research board, about.
- **Tree explorer** (25 Nov 2026): long view, tree and fan on one engine; stand with anyone; follow a line; evidence colouring. The long view shows both ways at once: older up, younger down.
- **Next on the same engine**: how-are-we-related path finder; a “start from me” link for each relative; brick walls that open their research card from the picture.
- **v1**: migration map with a time slider and a lifespan timeline against world events, sharing the explorer's year control.
- **v2**: name and place evolution, period map overlays.
