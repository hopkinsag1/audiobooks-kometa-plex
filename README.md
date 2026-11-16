# audiobooks-kometa-plex

Genre, Series and Narrator collections for a Plex audiobook library, built with
[Kometa](https://github.com/Kometa-Team/Kometa) on top of the
[Audnexus metadata agent](https://github.com/djdembeck/Audnexus.bundle).
It turns the tags Audnexus writes on every book into browsable collections:

| Collection group | Built from | What you get |
|---|---|---|
| **Genres** | Audible's category tree (album *Genre* tags) | ~20 curated buckets: Fantasy, Science Fiction, Mystery, Thriller & Suspense, Horror, Romance, Kids & Teens, Biographies & Memoirs, Self-Help, ... Sub-genres like *Cyberpunk* or *Cozy* roll up into their parent. |
| **Series** | `Series: X` mood tags | One collection per series, sorted in reading order. Single-book series are hidden until a second book arrives. Audible's duplicate series names can be merged. |
| **Narrators** | album *Style* tags | "Narrated by X" for every narrator with 5+ books in your library. Automatic: crosses the line, gets a collection; drops below, it goes away. Full-cast and dramatized albums don't count, so ensemble actors never get one. |
| **Editions** | separate `Series: X` tags, or the *(Dramatized Adaptation)* title marker | Side-by-side collections when you own one series in two recordings, e.g. *Harry Potter (Jim Dale)* / *Harry Potter (Full Cast)*, or *A Court of Thorns and Roses* / *A Court of Thorns and Roses (Dramatized)*. |
| **Featured** | ratings, added date, cast | *Top Rated Audiobooks*, *Recently Added Audiobooks*, *Full Cast & Dramatized*. |
| **Admin** | Plex unmatched flag | A hidden *Needs Metadata* collection so you can find books that never matched Audnexus. |

The Series part is based on the excellent
[audnexus-kometa-series](https://github.com/book-tools/audnexus-kometa-series) guide.
This repo extends it with genres, narrators, featured collections and a tool for bulk-fixing
unmatched books.

Tested with Kometa 2.4.8 and a ~970-book library. A full run of the audiobook library
takes about 3 minutes.

## How Audnexus tags map to Plex

Knowing this is what makes the config click:

| Plex field (album) | Audnexus writes | Kometa attribute |
|---|---|---|
| Genre | Audible's full category tree: 2 parent categories + up to 4 sub-genres, e.g. *Science Fiction & Fantasy*, *Fantasy*, *Epic*, *Paranormal & Urban* | `album_genre` |
| Mood | `Series: <name>` for every series the book belongs to (and, optionally, authors; turn that off in the agent settings) | `album_mood` |
| Style | Narrator name(s) | `album_style` |
| Rating | Audible user rating × 2 (so 4.7 stars = 9.4) | `album_critic_rating` |

A library of ~1000 books will have ~300 distinct genre tags and ~700 narrators, which is
why the config uses curated buckets (`include` + `addons`) for genres and a minimum count
for narrators rather than one collection per tag.

## Install

1. Have Plex + Audnexus working, and Kometa installed
   ([Kometa install guide](https://kometa.wiki/en/latest/kometa/install/overview/)).
   In the Audnexus agent settings, **uncheck "Append authors as Mood tags"**, otherwise
   every author becomes a "series".
2. Copy `config/audiobooks.yml` and `config/narrators.yml` into your Kometa config folder.
3. Add the two library entries from `config/config.yml.example` to your `config.yml`,
   replacing `Audiobooks` with your Plex library name (in **both** places: the first key and
   `library_name`).
4. Run Kometa once for the audiobook library and check the summary:

   ```
   kometa --run --run-libraries "Audiobooks"
   kometa --run --run-libraries "Audiobooks Narrators"
   ```

   (Docker: `docker exec Kometa python kometa.py --run --run-libraries "Audiobooks"`.)

## Why two library entries?

Kometa can process the same Plex library twice under different names via `library_name`.
That is the trick that makes narrator collections automatic:

```yaml
libraries:
  Audiobooks:                    # series + genres, minimum 2 books
    settings: { minimum_items: 2, delete_below_minimum: true }
  Audiobooks Narrators:          # same library, narrators need 5 books
    library_name: Audiobooks
    settings: { minimum_items: 5, delete_below_minimum: true }
```

`minimum_items` is **rejected** per-collection when `builder_level: album` is set
(Kometa: *"attribute not allowed with collection level 'Album'"*), but the library-level
setting still applies to every collection in that entry, including smart collections,
because Kometa counts the smart filter's matches before creating anything. So the only way
to give series and narrators different minimums is two entries.

## Customising

**Genre buckets.** Edit the `include` list (which buckets exist) and `addons` (which Audible
sub-genres fold into each bucket) in `audiobooks.yml`. To see what tags your library actually
has, open Plex → your library → Filter → Genre, or query
`/library/sections/<id>/genre?type=9` on the Plex API. Genres with commas in the name must be
quoted inside a YAML flow list (`"Mystery, Thriller & Suspense"`).

**Series merges.** Audible sometimes has two entries for one series (typos like
*His Dark Materialsik*, "publication order" vs "chronological order", translated editions).
The `addons` block under *Audiobook Series* merges them. The entries shipped here are examples
from one library; replace them with yours or delete the block.

**Editions.** When you own a series in two recordings and Audible gives each its own series
tag (*Harry Potter* and *Harry Potter (Full-Cast Editions)*), do not merge them in `addons`.
Rename each with `title_override` under *Audiobook Series* and they sort next to each other.
`exclude` drops umbrella tags such as *Wizarding World Collection* that would only repeat one
of them. `title_override` and `exclude` keys are the full tag, including the `Series: ` prefix.

**Dramatized editions.** Audible files a GraphicAudio-style dramatized adaptation under the
*same* series tag as the narrated books, so those cannot be split by tag. Instead every series
collection leaves out albums whose title contains `(Dramatized`, and a second dynamic set,
*Audiobook Series Dramatized*, builds `<Series> (Dramatized)` from the same tags for any
series with two or more such albums. This relies on the album title carrying
*(Dramatized Adaptation)*; `tools/build_m4b.py` writes it that way.

**Full cast.** Audnexus has no single tag for ensemble productions, so *Full Cast & Dramatized*
ORs three things: the `full cast` narrator tag, title markers (*(Full-Cast*, *(Dramatized*,
*Original Drama*) and a short list of series tags. Plex matches title words from their first
character, punctuation included, so a marker that sits inside brackets must be written with
the bracket: `Dramatized` finds nothing, `(Dramatized` does. Add a series tag there when a full-cast
production has neither marker. The narrator template in `narrators.yml` leaves the same albums
out of every count (`album_title.not`, `album_style.not`, `album_mood.not`); otherwise every
actor in a 7-book cast gets a "Narrated by" collection. Add a full-cast series tag to its
`album_mood.not` list when the titles don't say *(Dramatized*.

**Thresholds.** Series minimum (`minimum_items: 2`) and narrator minimum (`minimum_items: 5`)
live in `config.yml`. *Top Rated* uses `album_critic_rating.gte: 9.6` because Audible ratings
run hot (a quarter of a typical library sits above 9.4).

**Sort order.** Every template sets a `sort_title` prefix (`!01_`, `!10_`, `!20_`, `!30_`,
`!99_`) so the Collections tab groups Featured → Genres → Series → Narrators instead of
alphabetical soup. Change or remove the prefixes if you prefer.

## Gotchas learned the hard way

- **`any:` not `all:` in every tag template.** The original series guide uses `all:`. Once you
  merge two tags with `addons` (series names, narrator name variants), `<<value>>` becomes a
  list and `all:` ANDs them together, giving *"Plex Error: No items for smart filter"*.
  `any:` ORs them.
- **Style keys can be lowercased.** Plex exposes the narrator tag *Full cast* as `full cast`;
  use that spelling in `exclude` and in smart filters.
- **Renamed collections leave the old one behind.** Kometa makes the new title and stops
  managing the old one, but `delete_collections` here only removes empty ones. Delete the old
  collection in Plex after renaming, un-merging or excluding a tag.
- **`delete_collections` needs `ignore_empty_smart_collections: false`.** By default Kometa
  skips the `less:` test for smart collections, and every collection here is smart. The first
  library entry does not know about the narrator collections (the second entry builds them), so
  without that line it deletes all of them on every run and the second entry rebuilds them
  with new Plex ids.
- **Narrator names can leak into genres.** A few Audnexus records put the narrator in the
  Genre list. The curated `include` list makes this harmless.
- **The `/all` list view is truncated.** Plex returns only 2 genres and no styles/moods in
  section listings. Fetch `/library/metadata/<k1>,<k2>,...` (batches of ~40) to see everything.
- **Plex's "Unmatched" filter lies.** Books bound to the Personal Media agent or a `local://`
  stub count as matched, yet have no Audnexus data and appear in no collection. See the tool
  below.

## Tool: bulk-match books that never hit Audnexus

`tools/match_audnexus.py` finds every album in the library whose GUID is not
`com.plexapp.agents.audnexus://...`, looks it up on Audible's catalog API (title + author),
scores candidates by title, author and **runtime** (your file's length vs Audible's), checks
the ASIN resolves in Audnexus, and writes a CSV for you to review. Then it applies the ones
you approve through the same endpoint Plex's *Fix Match* uses.

```
export PLEX_URL=http://192.168.1.2:32400
export PLEX_TOKEN=...
python3 tools/match_audnexus.py --library "Audiobooks" propose      # writes pairings.csv, read-only
#   open pairings.csv, set the `apply` column to yes/no (HIGH rows are pre-set to yes)
python3 tools/match_audnexus.py --library "Audiobooks" apply  pairings.csv
python3 tools/match_audnexus.py --library "Audiobooks" verify pairings.csv   # a few minutes later
```

Then run Kometa. In practice: 60 of 80 "invisible" books matched cleanly on the first pass,
which created five new series collections and pushed three narrators over the 5-book line.

Notes:
- Stdlib only, no dependencies. Set `AUDIBLE_REGION` (default `us`) for other stores.
- `HIGH` means title + author match and runtime within about 15 minutes. `MED` is the same
  book in a different edition (other narrator or abridgement). Judge those yourself.
- Things that will never match: Spotify/Max audio dramas, out-of-print recordings not on
  Audible, podcasts (Audnexus rejects them even when Audible lists them).
- The match call needs a `name` parameter or Plex answers HTTP 400.

## Tool: build one chaptered m4b per book from a download

`tools/build_m4b.py` turns a downloaded series (GraphicAudio parts, loose mp3 tracks, or
finished m4b files) into one tagged, chaptered `.m4b` per book and brings it into the library.
It works in steps so nothing is written before you have seen the grouping:

```
export ABS_URL=... ABS_TOKEN=... PLEX_URL=... PLEX_TOKEN=...       # only publish needs these
python3 tools/build_m4b.py --ssh root@nas plan "/downloads/Series (GraphicAudio)"   # read-only -> m4b_plan.json
#   check m4b_plan.json: titles, file order, build flags. After editing titles: lookup m4b_plan.json
python3 tools/build_m4b.py build   m4b_plan.json --out /downloads/staging
python3 tools/build_m4b.py publish m4b_plan.json --staging /downloads/staging --library /audiobooks \
        --abs-library "Audio Books" --plex-library "Audio Books"
```

- `plan` groups files into books (multi-part releases are merged), looks each book up on
  Audible for cast, series and ASIN, and compares the summed part runtimes with your files.
  `--title`/`--author` force a whole folder into one book when the tags are a mess.
- `build` stream-copies AAC (no quality loss) and re-encodes anything else to AAC. Chapters
  are the sources' own marks, shifted into place, or one per file when a file has none. When
  those are coarse (over 30 minutes each on average) and Audible's chapter marks for the same
  parts fit the audio's length, Audible's finer marks are used instead; set `chapters_from` to
  `source` or `audible` on a book in the plan to force either. Chapter names are what players
  such as Prologue show in the chapter list, so file codes and repeated book titles are cleaned
  up (`STORMLIGHT0301P04` and `The Boys [Volume 02] [04]` both become `Part 4`). It
  writes `<Author>/<Title> (Dramatized Adaptation)/` with that same album tag, which is what
  keeps Plex from merging the book into your narrated copy and what the collections key on.
- `publish` moves the folders into the library (never over an existing one), then scans and
  matches in Audiobookshelf and Plex using the first part's ASIN, keeping the merged title.
- With `--ssh user@host` the files stay on that host and ffmpeg runs there in a throwaway
  container; without it, local `ffmpeg`/`ffprobe` are used.

A single long file without chapter marks stays one chapter. Books Audible does not carry
(GraphicAudio sells some series only directly) get tags and chapters but no match.

## Tool: audit Audiobookshelf tags vs file names

If you organise the same files with [Audiobookshelf](https://www.audiobookshelf.org/), a
tagger that dies halfway can leave embedded tags that name a different book than the file,
and Plex (which matches from those tags) then matches the wrong Audnexus record.
`tools/abs_tag_audit.py` asks the Audiobookshelf API for every item's folder, files,
embedded tags and metadata, and writes one row per file that disagrees. It is read-only.

```
export ABS_URL=http://192.168.1.224:13378
export ABS_TOKEN=...                       # Audiobookshelf -> Settings -> Users -> API token
python3 tools/abs_tag_audit.py --library "Audio Books"        # writes tag_audit.csv
```

The folder/file name is taken as the reference, and the `verdict` column says which side
disagrees with it:

| verdict | meaning | fix in Audiobookshelf |
|---|---|---|
| `abs-wrong` | file and tag agree, Audiobookshelf shows another book | Edit → Match, pick the right book |
| `tag-wrong` | file and Audiobookshelf agree, the tag names another book | item → Manage → Embed Metadata |
| `abs+tag-wrong` | tag is wrong and Audiobookshelf copied it | Match, then Embed Metadata |
| `all-differ` | all three disagree (often a foreign-language file) | look yourself |
| `stray-file` | one folder holds files tagged as different books | move the stray file out |
| `no-tag` | file has no album/title tag | Embed Metadata |

`duplicate` in the `flags` column means another item shows the same title and author. Two
editions of one book are fine; five folders all called *The Two Towers* are not.

Rescan the Audiobookshelf library first if files were changed since its last scan: the
tags it reports are the ones it read at scan time. After repairing, rescan in Plex, refresh
metadata on the affected albums, and rerun `match_audnexus.py propose`.

## Tool: where Plex and Audiobookshelf disagree

If you run both servers over the same files, they drift: each is matched separately, so one
can hold a different edition, narrator or series for the same book. `tools/plex_abs_diff.py`
pairs books by folder (the path relative to each library root) and reports the differences.
It is read-only on both sides.

```bash
export PLEX_URL=http://192.168.1.2:32400 PLEX_TOKEN=xxxx
export ABS_URL=http://192.168.1.2:13378 ABS_TOKEN=xxxx
python3 tools/plex_abs_diff.py --plex-library "Audiobooks" --abs-library "Audiobooks"   # writes plex_abs_diff.csv
```

The `differs` column lists what disagrees: `title`, `author`, `narrators` (no narrator in
common, or one side has none), `series`, `asin` (matched to different Audible listings),
`runtime` (more than 2% apart), `unmatched` (no Audnexus match in Plex), `split-in-plex` (one
folder, several Plex albums) and `only-plex` / `only-abs`. A different ASIN alone is often the
same recording under two listings; a different ASIN with different narrators is a wrong edition.

## Tool: mirror Audiobookshelf into Plex

> If another tool auto-matches new Audiobookshelf items (ReadMeABook's recently-added check does,
> for any item without an ASIN), a dramatized edition with no Audible listing of its own gets
> overwritten with the narrated edition's title, narrators and cover a few minutes after import.
> Correct the item in Audiobookshelf and leave an ASIN on it so it is not matched again, then mirror.
> ReadMeABook has no setting to turn this off. For a book with no Audible listing at all, put a
> placeholder such as `NO-AUDIBLE-LISTING` in the item's ASIN field; any non-empty value makes it
> skip the item, and the tools here ignore values that are not a 10-character ASIN.

Books Audible does not carry (or that never matched) have no narrators, series, genres or
summary in Plex, so they sit outside every collection. If Audiobookshelf has those details,
`tools/abs_to_plex.py` copies them to the Plex album and locks them. It also proposes a
re-match where the two servers hold different Audible listings, but only when Audible's
runtime for Audiobookshelf's listing fits your files and Plex's does not.

```bash
python3 tools/abs_to_plex.py --plex-library "Audiobooks" --abs-library "Audiobooks" plan    # dry run, writes abs_to_plex.csv
#   review the `apply` column and the `note` column
python3 tools/abs_to_plex.py --plex-library "Audiobooks" --abs-library "Audiobooks" apply abs_to_plex.csv
```

`plan` changes nothing. `apply` writes to Plex only, and removes tags the album has that
Audiobookshelf no longer lists, so a second `plan` proposes nothing. Rows are held back (`apply=no`) when
Audiobookshelf's own match looks wrong (title, author or runtime disagree with Plex), and
narrators are skipped for a dramatized or full-cast album that Audiobookshelf lists with one
or two narrators, since that is the narrated edition's credit. Albums matched to the same
listing on both sides are left to Audnexus. Run `plan` again afterwards: it should propose
nothing. Audiobookshelf's genres are Audible's top-level categories (`Science Fiction &
Fantasy`), so add them to the genre `addons` if you want filled books in the genre buckets.

## Credits

- [Kometa](https://github.com/Kometa-Team/Kometa)
- [Audnexus.bundle](https://github.com/djdembeck/Audnexus.bundle) and the
  [audnex.us](https://github.com/djdembeck/audnexus) API
- [audnexus-kometa-series](https://github.com/book-tools/audnexus-kometa-series) for the
  original series setup
