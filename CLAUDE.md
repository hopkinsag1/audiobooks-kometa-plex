# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Kometa configuration (YAML) plus one stdlib-only Python tool for a Plex audiobook library that
uses the Audnexus metadata agent. There is no build, no test suite, no linter, and no
dependencies. Validation means running Kometa against a real Plex server. The README is the
primary documentation and is written for end users; keep it in sync when changing config
behaviour.

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
the 5-book threshold does the filtering. Series get one collection per mood tag with
`remove_prefix: 'Series: '`.

### Template rules that are easy to break

- **Use `any:` not `all:` in templates whose `<<value>>` can be a list.** When `addons`
  merges tags (genre sub-genres, duplicate series names), `<<value>>` becomes a list and
  `all:` ANDs them, producing "Plex Error: No items for smart filter".
- **`sort_title` prefixes define the Collections tab order**: `!01_`/`!02_` featured,
  `!10_` genres, `!20_` series, `!30_` narrators, `!99_` admin. Keep new collections in
  this scheme.
- **Genre names containing commas must be quoted** inside YAML flow lists.
- **Plex lowercases some style keys** (`full cast`); handle with `title_override`.
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
