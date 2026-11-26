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

**How are we related?** The button under the picture names what the person you are with is to you (or, until you have said who you are, to the person the site starts from): “William Konecny is Greta’s 2× great-uncle. They both descend from Petr Pavel Konečný and Anna Tauber Konecny.” The picture centres on the ancestor the two share, with the way down to each of them lit, and the card lists the way from one to the other; “Compare with someone else” turns the search box into the second person. Blood relationships come first (the nearest shared ancestors, half relations where only one is shared); failing that one marriage or partnership, at either end or in the middle (“the niece of Lillian G. Wacek, the wife of Christopher’s 2× great-uncle”); two partners of one person are named through that person (“the wife of Marjorie’s husband, Frank Konecny”). A person's ancestry is the family they were born into, as everywhere in the tree; where a second, adoptive family is on record it is followed only when nothing else joins the two people, and the relationship is then said to be by adoption (“daughter by adoption”, “grandfather by adoption”). Husband and wife are said only where a marriage of two people both shown in full is on record; otherwise the word is partner. A child under 18 is never one of the two and can never be on the way. The link is `/tree/#kin-I0141-I0233` (what the second is to the first; `-tree` on the end for the tree view); `/tree/#kin-I0233`, which every person page carries, means “to me”.

**This is me.** Under More on anyone's panel. The choice is kept in this browser only (`localStorage`, key `tx-me`, like the light or dark theme) and sent nowhere. The tree then opens on you, “Start from me” returns to you, and relationships are named from you (“Your great-grandfather”, “To you: second cousin once removed”).

**Brick walls.** Where a line stops with no parents on record there is a short bar across its end, red and with a question mark where a research card is open on that person. A tap on it opens the panel at the question, with the link to the card.

- `site/static/tree.js` draws it all on one canvas, with no library and no third-party request. `site/static/tree.css` holds the page chrome; every class starts with `tx-`. `site/templates/tree.html` is the page. The page asks for both by content (`/static/tree.js?v=…`, from `asset()` in `site/build.py`), so after an update a reload brings the page and its script together instead of pairing the new page with a script the browser kept.
- The data is `/data/tree.json` (about 100 KB), written by `tree_data()` in `site/build.py` from the same bundle as the rest of the site: names and family links for everyone; years, towns, first crossing, review counts and open research cards only for people shown in full. A family carries its two partners, its children, and a married flag only where both partners are shown in full. A living person is a name and family links, a child under 18 the “Living child” placeholder. The export test checks that. Names in the panel, the lists and the search are the ones the rest of the site shows (“First (Birth) Married”); the picture labels people by first name and birth surname, as a pedigree does.
- Living and undated people have no public year. They are placed by generation from their nearest dated relatives, drawn hollow, and no year line is drawn from the first of them onward.
- Links in: `/tree/#long-I0145` (also `tree-` and `fan-`) opens a view on a person; `/tree/#long-I0141-wood` also follows that line. Person pages, line pages and the home page link this way.
- In the explorer the Morris line has its own colour (`--t-morris` in `tree.css`), because a tree rooted on one of the children shows both sides at once. Elsewhere on the site it still borrows the Wood colour from `config.json`.

## The map and the lifelines

`/map/` is one page with two views of the same years and one year control (slider, Play, arrow keys; the year is in the address, `/map/#map-1913`, `/map/#lives-1913`).

- **Map**: where everyone was in that year. A person is at the place of their latest dated record, and before their first located record at that first place; a long move leaves an arc for a few years. Marks are sized by how many people are there and coloured by line; a tap lists who was there, with links. “Go to” has the regions; drag and pinch or `+`/`−` for anywhere else. The database's several records of one town (a ward, a house, a street) are gathered under one mark and named by `map_town()` in `site/build.py`.
- **Lifelines**: every life with a date on record as a bar from birth to death, grouped by line, the oldest line first, world events ruled across. A bar that fades at one end has no date there. A tap on a life gives the person; a tap on the scale moves the year.
- The data is `/data/lives.json` (about 45 KB), written by `lives_data()` in `site/build.py` from the facts the person pages show: years of birth and death, and dated events at a place that has coordinates. **Only people shown in full are in it.** The living and children under 18 are not on the map or among the lifelines at all, because the site shows no dates or places for them. The export test checks that.
- The outlines are `site/static/atlas.json` (68 KB): Natural Earth 1:50m countries and lakes and the US Census Bureau's state borders, all public domain, clipped and simplified once by `tools/make_atlas.py` (the commands are at its head). The site still makes no third-party requests: there is no map service. Borders and coastlines are today's, and the page says so.
- `site/static/map.js` draws both views on one canvas; the page shares its frame and styles with the tree explorer (`tree.css`). The projection is an equal-area cone over the North Atlantic, turned so that north is up wherever you look.

## Roadmap

- **v0** (this): lineage routes, person and family pages with evidence ladders, citation and source pages with transcriptions, places, family stories, the research board, about.
- **Tree explorer** (25 Nov 2026): long view, tree and fan on one engine; stand with anyone; follow a line; evidence colouring. The long view shows both ways at once: older up, younger down.
- **On the same engine** (26 Nov 2026): how are we related, with the relationship in words and lit in the picture; this is me; brick walls that open their research card.
- **Map and lifelines** (26 Nov 2026): where the family was year by year, and every dated life on one time scale, sharing one year control.
- **Next**: the tree's year and the map's year linked both ways; story chapters that fly a line with its story alongside.
- **v2**: name and place evolution, period map overlays.
