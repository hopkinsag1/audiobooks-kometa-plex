# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`audiobooks-kometa-plex` (https://github.com/hopkinsag1/audiobooks-kometa-plex):
Kometa configuration (YAML) plus one stdlib-only Python tool for a Plex audiobook library that
uses the Audnexus metadata agent. There is no build, no test suite, no linter, and no
dependencies. Validation means running Kometa against a real Plex server. The README is the
primary documentation and is written for end users; keep it in sync when changing config
behaviour.

The series setup is derived from
[book-tools/audnexus-kometa-series](https://github.com/book-tools/audnexus-kometa-series).
Keep that credit in the README (intro and Credits section) when editing.

## Commands

```
# Run Kometa for each library entry (two passes over the same Plex library, see below)
kometa --run --run-libraries "Audiobooks"
kometa --run --run-libraries "Audiobooks Narrators"
# Docker: docker exec Kometa python kometa.py --run --run-libraries "Audiobooks"

# Bulk-match books that never hit Audnexus (needs PLEX_URL and PLEX_TOKEN in env)
python3 tools/match_audnexus.py --library "Audiobooks" propose            # read-only, writes pairings.csv
python3 tools/match_audnexus.py --library "Audiobooks" apply  pairings.csv
python3 tools/match_audnexus.py --library "Audiobooks" verify pairings.csv

# Build chaptered m4b files from a download and import them (plan is read-only)
python3 tools/build_m4b.py --ssh root@nas plan "<download folder>"        # writes m4b_plan.json
python3 tools/build_m4b.py build   m4b_plan.json --out <staging>
python3 tools/build_m4b.py publish m4b_plan.json --staging <staging> --library <library> --abs-library "Audio Books" --plex-library "Audio Books"

# Report where Plex and Audiobookshelf disagree (needs all four env vars; read-only)
python3 tools/plex_abs_diff.py --plex-library "Audio Books" --abs-library "Audio Books"   # writes plex_abs_diff.csv

# Mirror Audiobookshelf details into Plex for unmatched albums (plan is a dry run; apply writes to Plex only)
python3 tools/abs_to_plex.py --plex-library "Audio Books" --abs-library "Audio Books" plan    # writes abs_to_plex.csv
python3 tools/abs_to_plex.py --plex-library "Audio Books" --abs-library "Audio Books" apply abs_to_plex.csv

# Audit Audiobookshelf tags vs file names (needs ABS_URL and ABS_TOKEN in env; read-only)
python3 tools/abs_tag_audit.py --library "Audio Books"                    # writes tag_audit.csv
```

Note the argument order: `--library` is a top-level flag and must come before the subcommand.
`apply` is the only mutating command; it calls Plex's `/match` endpoint. `*.csv` and
`config.yml` are gitignored (the CSV holds library data, the config holds tokens).

## Architecture

### Two library entries for one Plex library

`config/config.yml.example` maps the same Plex library twice: `Audiobooks` loads
`config/audiobooks.yml` with `minimum_items: 2`, and `Audiobooks Narrators` (via
`library_name: Audiobooks`) loads `config/narrators.yml` with `minimum_items: 5`. This is
deliberate, not duplication: Kometa rejects `minimum_items` per-collection when
`builder_level: album` is set, so the only way to give series and narrators different minimums
is separate library entries with library-level settings. Do not merge the two files.

The first entry's `delete_collections` must keep `ignore_empty_smart_collections: false`.
Kometa's default skips the `less: 1` test for smart collections, so the narrator collections
(unconfigured from the first entry's point of view) would be deleted and rebuilt every run.
The cost: a non-empty collection that is no longer configured is not cleaned up automatically.

### How Audnexus tags drive the collections

Every collection is a smart filter over album-level tags Audnexus writes:

| Plex album field | Audnexus content | Kometa attribute | Used by |
|---|---|---|---|
| Genre | Audible category tree (parents + sub-genres) | `album_genre` | Genre buckets |
| Mood | `Series: <name>` | `album_mood` | Series collections |
| Style | narrator name(s) | `album_style` | Narrator collections |
| Rating | Audible rating × 2 | `album_critic_rating` | Top Rated |

Genres use a curated `include` list plus `addons` that fold Audible sub-genres into ~20
buckets, because a real library has ~300 distinct genre tags. Narrators have no include list;
the 5-book threshold does the filtering, plus an `exclude` list for full-cast actors. Series get one collection per mood tag with
`remove_prefix: 'Series: '`.

### Template rules that are easy to break

- **Use `any:` not `all:` in templates whose `<<value>>` can be a list.** When `addons`
  merges tags (genre sub-genres, duplicate series names), `<<value>>` becomes a list and
  `all:` ANDs them, producing "Plex Error: No items for smart filter".
- **`sort_title` prefixes define the Collections tab order**: `!01_`–`!03_` featured,
  `!10_` genres, `!20_` series, `!30_` narrators, `!99_` admin. Keep new collections in
  this scheme.
- **Genre names containing commas must be quoted** inside YAML flow lists.
- **Plex lowercases some style keys** (`full cast`); use that spelling in `exclude` and filters.
- **Editions are un-merged series tags**, renamed with `title_override` (keys include the
  `Series: ` prefix), e.g. Harry Potter (Jim Dale) / (Full Cast). Do not fold an edition's
  series tag into `addons`.
- **Dramatized editions share the narrated books' series tag**, so they are split on the album
  title: the series template has `album_title.not: "(Dramatized"` and the parallel
  `Audiobook Series Dramatized` set builds `<Series> (Dramatized)`. Both nest `any:` inside
  `all:` (Kometa accepts this) so addons-merged tags still OR together.
- **Ensemble albums don't count towards narrators**: the narrator template carries
  `album_title.not`, `album_style.not: full cast` and an `album_mood.not` list. The `full cast`
  tag feeds the `!03_` Full Cast & Dramatized collection instead.
- **Plex title filters match from a word's first character**, punctuation included:
  `Dramatized` finds nothing, `(Dramatized` does.
- The series `addons` block in `audiobooks.yml` contains example merges from one library
  and is meant to be replaced by users.

### tools/match_audnexus.py

Finds albums whose GUID is not `com.plexapp.agents.audnexus://...` (Plex's own "Unmatched"
filter misses albums bound to the Personal Media agent or `local://` stubs). Pipeline:
Plex album list → Audible catalog search by title + author → score candidates on title,
author and runtime (album track durations vs Audible `runtime_length_min`) → confirm the ASIN
resolves on Audnexus → CSV with HIGH/MED/LOW/NONE confidence. `propose` pre-sets `apply=yes`
only for HIGH rows. The match PUT requires a `name` parameter or Plex returns HTTP 400.
`AUDIBLE_REGION` (default `us`) is baked into the GUID written to Plex.

When reading album tags from Plex, note that the `/library/sections/<id>/all` listing is
truncated (2 genres, no moods or styles); the tool fetches `/library/metadata/<k1>,<k2>,...`
in batches of 40 to get full detail.

### tools/build_m4b.py

`plan` → `build` → `publish`, stdlib only. A `Runner` runs ffmpeg/ffprobe either locally or
over ssh inside a throwaway container (source mounted read-only as `/in`, output as `/out`),
as the output folder's owner. `plan` groups audio files into books by album tag / folder with
part markers stripped, and sums the Audible runtimes of every listing whose cleaned title
matches to get a confidence. `build` writes an FFMETADATA file with tags and chapters and
stream-copies AAC or re-encodes to AAC. Chapter names come from the sources and pass through
`tidy_chapter_titles` (players show them verbatim); coarse source marks are replaced by
Audnexus chapter marks when those fit the audio (`audible_chapters`). The output album tag and folder end in
`(Dramatized Adaptation)`; the Kometa templates depend on that marker. `publish` matches in
Audiobookshelf and Plex with the first part's ASIN, then resets the title to the merged
book's own. It refuses to run against a missing folder because docker would create it.

### tools/plex_abs_diff.py

Read-only. Pairs Plex albums with Audiobookshelf items by folder relative to each library
root (Plex track file paths vs ABS `relPath`), then compares title, author, narrators (Plex
Style vs ABS narrators), series (Plex `Series: ` moods vs ABS series), ASIN and runtime.
Comparisons are loose on purpose (subtitles, brackets, "no name in common") to surface wrong
editions, not spelling. An Audiobookshelf ASIN that is not 10 characters of `[A-Z0-9]` is read as
empty: books with no Audible listing carry the placeholder `NO-AUDIBLE-LISTING` so ReadMeABook
does not fuzzy-match them. Prologue shows a Plex album's title, author, Style as "Narrated by",
genres, year, studio, summary and the file's embedded chapter names; that is the field list a
future ABS-to-Plex mirror should write.

### tools/abs_to_plex.py

`plan` (dry run, CSV) → `apply` (Plex only). Imports `plex_abs_diff` for the folder pairing.
`fill` rows are Plex albums without an Audnexus GUID: Style, Mood (`Series: <name>`), Genre,
studio, year and summary are copied from Audiobookshelf with
`PUT /library/sections/<id>/all?type=9&id=<key>&style[0].tag.tag=..&style.locked=1&summary.value=..`.
`rematch` rows compare Audnexus `runtimeLengthMin` for both ASINs with the album's track
durations. Guards: no copy when title/author/runtime disagree (ABS itself is mismatched), no
narrators for ensemble titles with two or fewer ABS narrators. It must stay idempotent: a
second `plan` after `apply` proposes nothing. Setting `field[i].tag.tag` only adds tags, so
`apply` reads the album first and removes stale ones with `field[].tag.tag-=<names>`.

### tools/abs_tag_audit.py

Read-only audit of an Audiobookshelf library through its REST API (`/api/libraries/<id>/items`
paged, then `/api/items/<id>?expanded=1` per item for `media.audioFiles[].metaTags` and
`media.metadata`). The folder/file name is the reference; each file gets a `verdict`
(`abs-wrong`, `tag-wrong`, `abs+tag-wrong`, `all-differ`, `stray-file`, `no-tag`) saying which
side disagrees with it. Name comparison is deliberately loose (subtitles, brackets, apostrophes
and plurals are ignored, and Audiobookshelf's separate `subtitle` field is appended to the
title) because the failure mode being hunted is "a completely different book", not spelling.
The tool never writes to Audiobookshelf; repairs are done in its UI (Match / Embed Metadata).
