# For any agent working in this repository

Read `README.md`, “Who changes what”, before changing anything. In short:

- **`content/`** (family stories, the research board) is research content. Research agents change it by pull request to `main`: one pull request at a time, never stacked, and nothing outside `content/`. The rules for each are in `content/stories/README.md` and `content/research/README.md`.
- **`site/`, `export/`, `tools/`, `.github/`, `site/config.json`** are code. Code is changed by Claude at Chris’s direction, not by research agents. If content needs a code change, say so in the pull request and stop; it gets made first.
- **`data/`, `media/`** are written by the nightly export on the server. Never edit them by hand.
- **Start from the current `main`.** Fetch it before you change anything; do not work from a copy made earlier. Two writers on stale copies is how pages get lost.
- **Privacy.** Living people appear by name and relationship only: no dates, places or details, in any file. Nothing marked private in the research database is published.
- **Check before you ask for a merge:** `python3 tools/check_stories.py && python3 site/build.py` (the data bundle is in the repo, so this is the real build). The same checks run on every pull request.
