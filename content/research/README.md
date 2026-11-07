# The research board (`board.json`)

What `/research/` shows. One entry per question or piece of work. Karel updates it by pull request as part of the daily routine; Chris edits it directly when he likes. Claude owns the page code.

Fields:

| field | required | meaning |
|---|---|---|
| `id` | yes | `R01`, `R02` … — stable; never reuse a number |
| `status` | yes | `now` (being worked on), `next` (queued), `open` (known, not started), `stuck` (tried, blocked), `done` |
| `line` | yes | `konecny`, `tauber`, `wood`, `toepel`, `hansen`, `morris`, or `all` |
| `title` | yes | one line, family-readable |
| `summary` | yes | two to four sentences in plain words: what the question is and why it matters |
| `detail` | no | the researcher’s detail — registers, signatures, search order; shown collapsed |
| `ask` | no | what a family member could provide; the page lists these first, with a mail link |
| `owner` | no | `Karel`, `Chris`, `family` … |
| `people` | no | Gramps IDs of published people to link (`I0145`) |
| `since`, `done` | no | ISO dates; `done` on finished items (the page keeps the last ten) |

Rules:

- No living person’s dates, places or details. First names and relationships are fine (“Jody’s parents”); the build drops any entry whose text contains a living person’s full name and says so in the log.
- Write for the family, not the archive: the `summary` should make sense to a cousin; put signatures and image numbers in `detail`.
- Mark items `done` rather than deleting them, and keep the summary describing what was found.
- The “Flagged by the checker” list and the activity counts on the page are generated from the database; they are not in this file.
