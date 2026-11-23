# Family stories

Hand-maintained pages. The nightly Gramps export does not write or delete this folder.

`export/publish.sh` runs `gramps_export.py` and then commits with `git add -A data media`. The exporter writes `data/bundle.json` and the public files under `media/`, and it deletes any file left directly in `media/` or `media/thumbs/` that is no longer a public Gramps image. `content/stories/` is outside those paths, so the job cannot overwrite `stories.json`, the photographs, or this note. Do not move them into `data/` or `media/`.

`site/build.py` turns `stories.json` and `photos/` into `/stories/` when GitHub Pages builds the site. Every story in the file is published. `status` does not hide one.

Living people: `tools/check_stories.py` fails the build when a story names a living person in full (exceptions are listed in `living-ok.txt`). A child under 18 is never named, not even by first name. The build cannot check that, because the site does not know their names. The nightly export does know them, so it checks, and the site leaves out any section that names a child until the text is changed. `living-ok.txt` does not apply to children.

Each story has a status field at the top of its record:

```yaml
status: confirming
```

`confirming` prints this banner under the title: "In process of confirming — details are still being verified against records." `proven` prints no banner. Change that one field when the notes are confirmed; the story stays on the site either way.

The wording, citations, sources, and confidence labels (Proven, Possible, family claim, compiled source only) are the research notes, unchanged. Each village photograph is credited in its caption — Photo: author, license, via Wikimedia Commons — and linked to the file page. Local file names are not written in the story text.
