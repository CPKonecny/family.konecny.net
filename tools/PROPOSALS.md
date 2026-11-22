# Proposals: how research reaches the research database

Nothing is typed into Gramps by an agent. A researcher writes one JSON file per change into
`proposals/inbox/`; `tools/apply_proposals.py` (the applier) checks each file and either applies it,
rejects it, or holds it for Chris. This page is the reference for those files. The applier is the
judge: if this page and the applier disagree, tell Claude.

Version: applier 0.3.2, 22 November 2026.

## Where a file goes, and when

| Folder | Meaning |
|---|---|
| `inbox/` | filed, not yet looked at |
| `applied/` | done; change actions also leave `undo/NAME.undo.json` |
| `rejected/` | not done; `NAME.reason.txt` says why. Fix the file and file it again |
| `held/` | waiting for Chris; `held/_WAITING.txt` lists everything waiting, in the applier's own words |

The applier runs from cron every half hour. Within that time a file has left `inbox/` for one of the
other three folders; if it is still in `inbox/` after an hour, the applier is not running: tell Chris.
A file in `applied/` is in Gramps at once. The public site shows it after the nightly export (03:15).

A file filed again under the same name replaces the one in `held/` and has to be approved again.

## What waits for Chris

Applied without asking: additions about people who are long dead.

Held until Chris approves, however good the source:

1. every **change** to something already in the tree (`update_person`, `update_family`,
   `update_event`, `merge_family`, `merge_event`, `set_private`);
2. anything about a **private** person, or a person who is **living or was born in the last 100
   years**. With no birth date the applier estimates from the death date and from the dates of
   parents, partner, children and marriage; with nothing to go on, the person counts as recent;
3. anything that depends on a person whose own `add_person` is waiting.

Chris sees a description that the applier writes from the file and from Gramps, not the researcher's
summary. Never describe to Chris what a held proposal does from memory: quote `held/_WAITING.txt`.
Proposals that change the same record are marked there to be read together.

## If you are looking for…

| You want to | Use |
|---|---|
| put a person who is already in the tree into a family that exists (`link_child`) | `update_family` with `add_children` |
| the same, as an adopted, step or foster child | `update_family` with `add_children` and `rel` (or `father_rel` / `mother_rel`) |
| correct a name, add a suffix, a married name or another name (`rename_person`) | `update_person` |
| correct the sex recorded (`set_gender`) | `update_person`, `set.gender` |
| fill the empty father or mother of a family, or swap the two (`set_family_parents`) | `update_family`, `set.father` / `set.mother` |
| join two families that are really one (`merge_families`) | `merge_family` |
| get rid of a duplicate or empty event (`delete_event`) | `merge_event` |
| correct the date or place of an event that is there | `update_event` |
| keep a person, family, event or note off the public site (`set_private`) | `set_private`; for something new, `"private": true` in the `add_…` file |
| record a marriage | `family.marriage` in `add_family`, or `add_event` with type `Marriage` |

Two things cannot be asked for: taking a parent out of a family, and deleting a person. Write those to
Chris as a question.

## Every file

```json
{
  "proposal_version": 1,
  "agent": "karel",
  "action": "add_event",
  "ancestry_attached": true,
  "reasoning": "What the record says and why it is this person.",
  "conflicts": "Optional: what disagrees with it."
}
```

`ancestry_attached` must be `true`: the Ancestry tree already shows what the file asks for. For a
repair of something that is wrong only in Gramps, `true` means Ancestry already shows it correctly.
The one exception is a research note (below). Other fields (`created`, `review`, …) are ignored.

People are named by `{"ancestry_id": "212806949341"}` or `{"gramps_id": "I0238"}`. In `add_person`
and `add_family` the fields are Ancestry IDs as plain strings. Families, events and notes are named by
Gramps ID (`"F0183"`, `"E1555"`, `"N0012"`).

Dates are text in quotes: `"1898"`, `"1898-07"` or `"1898-07-04"`, with optional `"date_quality"`:
`exact`, `about`, `before`, `after`, `estimated`, `calculated`. `between` is not supported. A date
written any other way (`"abt 1880"`, `"4 July 1898"`, a bare number, a month 13, a day in the future)
rejects the file; nothing of it is written.

A `citation` is:

```json
{
  "source_title": "Manitowoc Herald-Times",
  "page": "16 April 1979, p. 3",
  "tier": "index",
  "confidence": 2,
  "repository": "2manitowoc.com",
  "transcription": "the words as written",
  "image": "karl-obit.png",
  "url": "https://…", "date": "1979-04-16", "author": "", "pubinfo": "",
  "source_type": "", "information": "", "evidence": "", "ancestry_record_id": ""
}
```

The first four are required. The highest confidence each tier may claim: `record` 4 (and it needs an
`image`, a file put in the inbox beside the proposal), `compiled` 3, `index` 2, `oral` 2, `tree` 1,
`ai-draft` 0.

## Additions

### add_person

```json
{
  "action": "add_person",
  "person": {
    "ancestry_id": "212815086130", "first_name": "Caroline M.", "surname": "Karl", "gender": "F",
    "married_surname": "Langenkamp", "suffix": "", "call_name": "", "nickname": "",
    "birth": {"date": "1919-04-06", "place": "Kewaunee, Wisconsin", "description": ""},
    "death": {"date": "2006-12-13", "place": "Wauwatosa, Wisconsin"},
    "private": false
  },
  "relations": {
    "father_ancestry_id": "212814968024", "mother_ancestry_id": "212814977135",
    "spouse_ancestry_id": "",
    "father_rel": "Birth", "mother_rel": "Birth"
  },
  "citation": { }
}
```

* `surname` is the **birth** surname and may be empty if it is not known; then `married_surname` is
  required. A woman is entered under her birth name with the married name as `married_surname`,
  whatever she was known as.
* `suffix` is `"Jr."`, `"III"` and the like.
* **A new person must be linked to someone**: a father, mother or spouse in `relations`, or an
  `add_family` / `update_family` in the same batch that places them. A person on their own is
  rejected.
* Name **both** parents when both are known and in the tree. A child filed with the father only goes
  into a father-only family, apart from the couple's family; that is how split families were made.
* `father_rel` / `mother_rel`: `Birth` (default), `Adopted`, `Stepchild`, `Foster`, `Sponsored`,
  `Unknown`. A child adopted by a stepfather is `"father_rel": "Adopted", "mother_rel": "Birth"`.
* Parents and spouses may be new people in the same batch; the applier works out the order. If the
  proposal of someone they depend on is rejected, they are rejected too. (Two new spouses who name
  each other are the exception: if the second fails, the first is already in, and the log carries a
  WARNING to link them later with `add_family`.)
* `"private": true` marks the person and their birth and death events private. A family the applier
  makes with them in it (with a spouse, or with the other parent of a child) is private too.

### add_family

Links people who are already in the tree. At least one parent; children are linked by birth.

```json
{
  "action": "add_family",
  "family": {
    "father_ancestry_id": "212811655300", "mother_ancestry_id": "212811655323",
    "children_ancestry_ids": ["212815100001", "212815100002"],
    "marriage": {"date": "1947-12-27", "place": "Manitowoc, Wisconsin", "description": ""},
    "private": false
  },
  "citation": { }
}
```

If the couple already has a family the children are added to it; otherwise the family is made.
A family is private when `private` is true or when either partner is private; one made earlier in
the same batch (when the second of the couple was added) counts as new. On a family that was in the
tree before the batch, `"private": true` is not applied but rejected: use `set_private` for that. Every ID given must
be in the tree. A child who already has a father or mother by birth in another family is rejected:
use `update_family` with a relationship, or `merge_family`.

`marriage` is optional and needs both partners and a `citation`. If the family already has a marriage
of that date (or of that year, when only a year is given) the citation is added to it and no second
one is made. With no children listed, the file simply records the couple and their marriage.

### add_event

```json
{
  "action": "add_event",
  "subject": {"gramps_id": "I0393"},
  "event": {"type": "Death", "date": "1953-01-15", "place": "Manitowoc, Wisconsin",
            "description": "", "role": "Primary", "private": false},
  "citation": { }
}
```

* A citation is required.
* Marriage, divorce and the like go on the couple's family: add `"spouse": {"ancestry_id": "…"}` if
  the subject has more than one family.
* If the person already has an event of that type and date, the citation is added to it and no second
  event is made. A year-only proposal joins any event of that type in that year.
* For birth, death, burial, cremation, baptism and christening the applier also fills in an empty
  placeholder left by the import, and sharpens a year-only event of the same year, instead of adding
  a second one. A different date is kept as a second event: that is evidence, not a duplicate. If the
  date in the tree is simply wrong and should be replaced, that is `update_event`.
* `"private": true` keeps the event off the public site whatever happens to the person.

### add_citation, add_note, add_media

```json
{"action": "add_citation", "subject": {"ancestry_id": "…"}, "event_gramps_id": "E1556", "citation": { }}
```

```json
{"action": "add_note", "subject": {"ancestry_id": "…"},
 "note": {"type": "Research", "text": "RESEARCH SUMMARY — …", "tier": "ai-draft", "private": false}}
```

```json
{"action": "add_media", "subject": {"ancestry_id": "…"}, "citation": {"image": "stone.png", "…": "…"}}
```

Without `event_gramps_id` the citation goes on the person. `note` may also be plain text. A research
note with `"tier": "ai-draft"` and no citation, event or media is the one kind of file that may have
`"ancestry_attached": false`.

## Changes (always held for Chris)

### update_person

```json
{
  "action": "update_person",
  "subject": {"gramps_id": "I0401"},
  "set": {
    "surname": "Suchomel", "married_surname": "Wacek",
    "first_name": "Anna", "suffix": "", "call_name": "", "nickname": "",
    "also_known_as": ["Annie Wacek", {"first_name": "Anna", "surname": "Sukomel"}],
    "gender": "F", "private": true, "prefer_event": "E0123"
  },
  "citation": { }
}
```

Give only what changes. `first_name`, `surname` and `suffix` correct the main (birth) name.
`married_surname` and `also_known_as` add alternate names if they are not there yet. `prefer_event`
makes one of several births or deaths the main one shown. `citation` is optional and goes on the
person. Filing the same change twice does nothing the second time.

### update_family

```json
{
  "action": "update_family",
  "family": {"gramps_id": "F0183"},
  "set": {"mother": {"gramps_id": "I0401"}, "private": false, "type": "Married"},
  "add_children": [
    {"gramps_id": "I0407"},
    {"ancestry_id": "…", "father_rel": "Adopted", "mother_rel": "Birth"},
    {"ancestry_id": "…", "rel": "Adopted"}
  ],
  "citation": { }
}
```

* `family` may also be `{"father": {…}, "mother": {…}}` when exactly one family has those parents.
* `add_children` links people already in the tree. For a child already in the family it corrects the
  relationship; if the relationship is already as given, nothing happens. `rel` sets both sides.
* `set.father` / `set.mother` fill an empty place. Giving both, the other way round, swaps them.
  **Nobody is ever replaced**: taking a parent out is done by Chris. A parent who is filled in becomes
  the parent of every child already in the family, so the source has to say so for each of them.
* A person has one father and one mother by birth. Linking a child by birth to a second set of
  parents is rejected; so is linking a child into two families that share a parent (merge them).
* If the couple already has another family, the applier says to merge instead.
* `type`: `Married`, `Unmarried`, `Civil Union`, `Unknown`.

### update_event

Corrects an event that is already there: its date, place or description, or whether it is private.

```json
{
  "action": "update_event",
  "subject": {"gramps_id": "I0393"},
  "event": {"type": "Birth"},
  "set": {"date": "1866-05-30", "date_quality": "exact", "place": "Bohemia"},
  "citation": { }
}
```

* The event is named by Gramps ID (`"event": "E1601"`) or, when the ID cannot be seen, by its type:
  `{"type": "Birth"}` if the person has one birth, `{"type": "Residence", "date": "1910"}` (the date it
  has **now**) if there are several. For a family's event give `"family": {"gramps_id": "F0032"}` in
  place of `subject`.
* `set` may hold `date` (with `date_quality`), `place`, `description`, `private`. Give only what
  changes. A date or place cannot be taken away, only replaced.
* A new date or place needs a `citation`; it is added to the event beside the ones already there.
* What the event said before is kept in a note on it, with the name of the proposal file.
* If the event already says what is asked, nothing is done and no citation is added.
* Use this when the value in the tree is wrong or less exact than the record. When two records simply
  disagree, file the second one with `add_event`: both stay, as evidence.

### merge_family

```json
{"action": "merge_family", "keep": {"gramps_id": "F0183"}, "drop": {"gramps_id": "F0185"}}
```

Folds `drop` into `keep`: children, events, citations, notes and media move over and `drop` is
deleted. Allowed only when the two share a parent and no place holds two different people: each of
father and mother must be the same person in both, or missing in one (the family kept then gets that
parent). Two families with different fathers are not duplicates and are rejected.

### merge_event

```json
{"action": "merge_event", "subject": {"gramps_id": "I0059"}, "keep": "E1555", "drop": "E0240"}
```

```json
{"action": "merge_event", "family": {"gramps_id": "F0072"}, "keep": "E0700", "drop": "E0912"}
```

```json
{"action": "merge_event", "subject": {"gramps_id": "I0062"}, "type": "Birth"}
```

Folds a duplicate event into the one kept: its citations, notes and media move over, and what the
kept event lacks (date, place, description) is filled from it. This is also how an empty or duplicate
event is removed; nothing is ever simply deleted. Both must be events of the person or family named,
and of the same type. Keep the fuller one.

With `type` in place of `keep` and `drop`, the applier takes the two events of that type that the
person has (there must be exactly two) and keeps the fuller one. That is for people whose event IDs
are not in the published bundle. It is refused if the two have different dates: which date stands is
a research decision, so name `keep` and `drop`.

If the two have different dates or different place records the proposal is rejected unless it says
`"allow_different": true`; then a note on the kept event records what the dropped one said.

### set_private

```json
{"action": "set_private", "records": ["I0123", "F0045", "E0678", "N0012"], "private": true}
```

Marks people, families, events and notes private (`true`) or public again (`false`). `records` are
Gramps IDs; a person may also be given as `{"ancestry_id": "…"}`. Only what is listed changes.

* A private person is left off the public site altogether, and so is everything that hangs only on
  them, so their events need not be listed.
* A private **family** takes the link away as well: the partners no longer show as a couple and the
  children show no parents, while each of them is still listed by name. To keep a household off the
  site, list the people.
* `"private": false` puts things on the public site. Chris sees exactly what, and decides.

## Rules the applier cannot check

* Take relationships from the source itself, page open, never from a summary of it.
* From family papers (the Reunion Book, letters): names, relationships, and dates and places of
  birth, marriage and death only. No health, cause of death, character, work or addresses.
* Every living person goes in with `"private": true` unless Chris has said otherwise for that person.
  So does someone who died young and whose parents or brothers and sisters are living.
* One batch per household, so that Chris can read what he is approving.

## Checking the tree

`python3 tools/check_tree.py` reads the published `data/bundle.json` (no login, changes nothing) and
lists what usually means a proposal went in half-finished: people linked to no one, one-parent
families beside another family of the same parent, and people with two births or deaths, each with
the proposal that repairs it. Run it after a batch has been applied and exported.

## Chris's commands

On the server, in `~/gramps-tools`:

```
python3 apply_proposals.py --held                    # what is waiting, in plain words
python3 apply_proposals.py --approve all             # approve what --held just showed, and apply it
python3 apply_proposals.py --approve NAME            # or one by one (a unique beginning of the name is enough)
python3 apply_proposals.py --reject NAME --reason "why"
python3 apply_proposals.py --undo NAME               # put back what an applied change altered
```

`--approve all` covers only what the last `--held` listed, so nothing that arrived in between is
approved unseen. An approval is for one file with one content and is kept in
`~/.config/family-site/`, outside the proposals folder. `--undo` refuses if the same records have
been changed again since.

## For Claude

`tools/test_apply_proposals.py` runs the applier against an imitation of the Gramps Web API, and
against a real one when `APPLIER_TEST_API` is set (a scratch tree). Both must be green before a new
applier is installed. `tools/install_applier.py` runs the tests, checks that the applier on the
server is a version the new one was built on, keeps a copy of it, installs the new one, and shows
when cron last ran it.
