# Kometa audiobook collections (Genres, Series, Narrators)

Kometa configuration for a Plex audiobook library that uses the
[Audnexus metadata agent](https://github.com/djdembeck/Audnexus.bundle).
It turns the tags Audnexus writes on every book into browsable collections:

| Collection group | Built from | What you get |
|---|---|---|
| **Genres** | Audible's category tree (album *Genre* tags) | ~20 curated buckets: Fantasy, Science Fiction, Mystery, Thriller & Suspense, Horror, Romance, Kids & Teens, Biographies & Memoirs, Self-Help, ... Sub-genres like *Cyberpunk* or *Cozy* roll up into their parent. |
| **Series** | `Series: X` mood tags | One collection per series, sorted in reading order. Single-book series are hidden until a second book arrives. Audible's duplicate series names can be merged. |
| **Narrators** | album *Style* tags | "Narrated by X" for every narrator with 5+ books in your library. Fully automatic: crosses the line, gets a collection; drops below, it goes away. |
| **Featured** | ratings, added date | *Top Rated Audiobooks*, *Recently Added Audiobooks*. |
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

**Thresholds.** Series minimum (`minimum_items: 2`) and narrator minimum (`minimum_items: 5`)
live in `config.yml`. *Top Rated* uses `album_critic_rating.gte: 9.6` because Audible ratings
run hot (a quarter of a typical library sits above 9.4).

**Sort order.** Every template sets a `sort_title` prefix (`!01_`, `!10_`, `!20_`, `!30_`,
`!99_`) so the Collections tab groups Featured → Genres → Series → Narrators instead of
alphabetical soup. Change or remove the prefixes if you prefer.

## Gotchas learned the hard way

- **`any:` not `all:` for merged series.** The original series guide uses `all:`. Once you
  merge two series names with `addons`, `<<value>>` becomes a list and `all:` ANDs the two
  moods together, giving *"Plex Error: No items for smart filter"*. `any:` ORs them.
- **Style keys can be lowercased.** Plex exposes the narrator tag *Full cast* as `full cast`.
  `title_override` handles it (→ *Full Cast Productions*).
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

## Credits

- [Kometa](https://github.com/Kometa-Team/Kometa)
- [Audnexus.bundle](https://github.com/djdembeck/Audnexus.bundle) and the
  [audnex.us](https://github.com/djdembeck/audnexus) API
- [audnexus-kometa-series](https://github.com/book-tools/audnexus-kometa-series) for the
  original series setup
