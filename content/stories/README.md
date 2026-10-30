# Family stories

Hand-maintained pages. The nightly Gramps export does not write or delete this folder.

`export/publish.sh` runs `gramps_export.py` and then commits with `git add -A data media`. The exporter writes `data/bundle.json` and the public files under `media/`, and it deletes any file left directly in `media/` or `media/thumbs/` that is no longer a public Gramps image. `content/stories/` is outside those paths, so the job cannot overwrite `stories.json`, the photographs, or this note. Do not move them into `data/` or `media/`.

`site/build.py` turns `stories.json` and `photos/` into `/stories/` when GitHub Pages builds the site.

The wording, citations, sources, and confidence labels (Proven, Possible, family claim, compiled source only) are the research notes, unchanged. The village caption in `stories.json` also names a local folder used while the notes were drafted (`/workspace/stories-photos`). That path is not a source. The public page leaves it off and keeps the credit that is: photos are from Wikimedia Commons, with the file name cited on each village.
