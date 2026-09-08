#!/usr/bin/env python3
"""
abs_tag_audit.py — find audiobooks in Audiobookshelf whose folder/file names, embedded
tags and Audiobookshelf metadata disagree with each other.

Why: a tagger that dies halfway leaves files whose embedded tags name a different book
than the file they live in (e.g. a file called "Book 5 - The Return of the King.m4b"
tagged as "The Two Towers"). Audiobookshelf may then show the wrong title, and Plex
(which matches from embedded tags) matches the wrong Audnexus book. This tool asks
Audiobookshelf for every item's path, its audio files, the tags it read from those
files, and its own metadata, and flags every item where they don't agree.

Read-only, stdlib only. It never touches files; repairs are done in Audiobookshelf.

Usage:
  export ABS_URL=http://192.168.1.2:13378
  export ABS_TOKEN=xxxxxxxxxxxx
  python3 abs_tag_audit.py                       # all book libraries -> tag_audit.csv
  python3 abs_tag_audit.py --library Audiobooks  # one library
  python3 abs_tag_audit.py --all                 # include items with no problems

The folder/file name is treated as the reference (it is the one thing the failed tagger
did not touch), and each file gets a verdict saying which side disagrees with it:

  verdict
    abs-wrong       file and embedded tag agree; Audiobookshelf shows a different book.
                    Fix: re-match the item in Audiobookshelf (Edit -> Match).
    tag-wrong       file and Audiobookshelf agree; the embedded tag names another book.
                    Fix: Audiobookshelf -> item -> Manage -> Embed Metadata.
    abs+tag-wrong   tag names another book and Audiobookshelf copied it. Re-match, then embed.
    all-differ      all three disagree (often a translated/foreign-language file). Look yourself.
    stray-file      an item mixes files tagged as different books (a file in the wrong folder).
    no-tag          file carries no album/title tag; embed to fix.
  flags       the raw disagreements (abs!=file, tag!=file, tag-missing, mixed-tags) plus
              `duplicate` when another item has the same Audiobookshelf title+author
              (legitimate for two editions, a symptom when several folders show one title).
  abs_title / abs_author      what Audiobookshelf currently shows
  folder                      item folder name (relative to the library folder)
  file                        audio file name
  tag_album / tag_title / tag_artist   embedded tags as read by Audiobookshelf
"""
import argparse, csv, json, os, re, sys, urllib.parse, urllib.request


# ------------------------------------------------------- Audiobookshelf helpers
def abs_get(path, **params):
    url = os.environ.get("ABS_URL", "").rstrip("/")
    tok = os.environ.get("ABS_TOKEN", "")
    if not url or not tok:
        sys.exit("set ABS_URL and ABS_TOKEN")
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}{path}{'?' + q if q else ''}",
                                 headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def libraries(name=None):
    libs = [l for l in abs_get("/api/libraries")["libraries"] if l.get("mediaType") == "book"]
    if name:
        libs = [l for l in libs if l["name"] == name]
        if not libs:
            sys.exit(f"library '{name}' not found")
    return libs


def library_items(lib_id):
    page, limit, out = 0, 200, []
    while True:
        r = abs_get(f"/api/libraries/{lib_id}/items", limit=limit, page=page)
        out += r.get("results", [])
        if len(r.get("results", [])) < limit:
            return out
        page += 1


# --------------------------------------------------------------- name matching
NOISE = re.compile(
    r"^\s*(\d{4}\s*-\s*|book\s*\d+\s*-\s*)"      # "2019 - ", "Book 5 - " prefixes
    r"|\{.*?\}|\bB0[0-9A-Z]{8}\b"                   # {1.1gb}, ASINs
    r"|\b\d{2,3}k\b|\b\d{1,2}\.\d{2}\.\d{2}\b"     # 128k, 20.09.50
    r"|\.(m4b|m4a|mp3|flac|ogg|opus)$",            # extension
    re.I)
STOP = {"the", "a", "an", "of", "and", "or", "unabridged", "audiobook", "novel", "edition"}


def norm(s):
    s = NOISE.sub(" ", s or "")
    s = re.sub(r"['\u2019*]", "", s)             # Gerald's == Geralds, Unfu*k == Unfuk
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w   # minutes == minute
            for w in s.split() if w not in STOP]


def main_title(s):
    """'Mythos: The Greek Myths Retold' -> 'Mythos'; 'Deep Work. Rules for...' -> 'Deep Work'."""
    return re.split(r"\s*[:\u2013\u2014]\s+|\s+-\s+|\.\s+", s or "", 1)[0]


def variants(s):
    """Shorter forms of a name to try as the needle: without (edition) brackets, the
    main title before a subtitle, the subtitle alone, the leading two words of a long title."""
    bare = re.sub(r"\(.*?\)|\[.*?\]", " ", s or "")
    parts = re.split(r"\s*[:\u2013\u2014]\s+|\s+-\s+|\.\s+", bare, 1)
    out = [s, bare] + parts
    for v in list(out):
        n = norm(v)
        if len(n) >= 4:
            out.append(" ".join(n[:2]))
    return out


def agrees(a, b):
    """True if the two names describe the same book: most significant words of one
    (or of a shorter variant of it) occur in the other. Symmetric."""
    for x, y in ((a, b), (b, a)):
        h = set(norm(y))
        for needle in variants(x):
            n = norm(needle)
            if not n:
                return True               # nothing to compare against
            if sum(1 for w in n if w in h) / len(n) >= 0.6:
                return True
    return False


# ------------------------------------------------------------------------ main
def audit(lib, show_all):
    rows = []
    items = library_items(lib["id"])
    print(f"{lib['name']}: {len(items)} items", file=sys.stderr)

    # duplicate detection on Audiobookshelf title+author
    def dupkey(md):
        return (" ".join(norm(md.get("title"))), " ".join(norm(md.get("authorName"))))
    seen = {}
    for it in items:
        seen.setdefault(dupkey(it["media"]["metadata"]), []).append(it["id"])

    for n, it in enumerate(items, 1):
        if n % 50 == 0:
            print(f"  {n}/{len(items)}", file=sys.stderr)
        full = abs_get(f"/api/items/{it['id']}", expanded=1)
        md = full["media"]["metadata"]
        title, author = md.get("title") or "", md.get("authorName") or ""
        if md.get("subtitle"):
            title = f"{title}: {md['subtitle']}"
        folder = full.get("relPath") or os.path.basename(full.get("path", ""))
        dup = len(seen.get(dupkey(md), [])) > 1

        files = full["media"].get("audioFiles") or []
        tag_books = set()
        for f in files:
            tags = f.get("metaTags") or {}
            fname = f.get("metadata", {}).get("filename") or ""
            album, ttl, artist = tags.get("tagAlbum") or "", tags.get("tagTitle") or "", tags.get("tagArtist") or ""
            book_tag = album or ttl
            tag_books.add(" ".join(norm(main_title(book_tag))))
            name = f"{folder} {fname}"      # author/series/book folders + file name
            abs_ok = agrees(title, name)
            tag_ok = bool(book_tag) and agrees(book_tag, name)
            flags = []
            if not book_tag:
                verdict = "no-tag"
                flags.append("tag-missing")
            elif abs_ok and tag_ok:
                verdict = ""
            elif tag_ok:
                verdict = "abs-wrong"        # file + tag agree, Audiobookshelf shows another book
                flags.append("abs!=file")
            elif abs_ok:
                verdict = "tag-wrong"        # file + Audiobookshelf agree, embedded tag is another book
                flags.append("tag!=file")
            else:
                verdict = "abs+tag-wrong" if agrees(book_tag, title) else "all-differ"
                flags += ["abs!=file", "tag!=file"]
            if dup:
                flags.append("duplicate")
            rows.append(dict(library=lib["name"], item_id=it["id"], verdict=verdict,
                             flags=",".join(flags),
                             abs_title=title, abs_author=author, folder=folder, file=fname,
                             tag_album=album, tag_title=ttl, tag_artist=artist))
        if len(tag_books - {""}) > 1:
            for r in rows[-len(files):]:
                r["flags"] = ",".join(x for x in [r["flags"], "mixed-tags"] if x)
                r["verdict"] = r["verdict"] or "stray-file"
        if not files:
            rows.append(dict(library=lib["name"], item_id=it["id"], verdict="no-audio-files", flags="no-audio-files",
                             abs_title=title, abs_author=author, folder=folder, file="",
                             tag_album="", tag_title="", tag_artist=""))
    return rows if show_all else [r for r in rows if r["flags"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", help="Audiobookshelf library name (default: every book library)")
    ap.add_argument("--all", action="store_true", help="include items with no problems")
    ap.add_argument("-o", "--out", default="tag_audit.csv")
    a = ap.parse_args()

    rows = []
    for lib in libraries(a.library):
        rows += audit(lib, a.all)

    cols = ["library", "item_id", "verdict", "flags", "abs_title", "abs_author", "folder", "file",
            "tag_album", "tag_title", "tag_artist"]
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    flagged = sum(1 for r in rows if r["flags"])
    print(f"wrote {a.out}: {len(rows)} rows, {flagged} with problems", file=sys.stderr)


if __name__ == "__main__":
    main()
