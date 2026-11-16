#!/usr/bin/env python3
"""
plex_abs_diff.py — read-only report of where Plex and Audiobookshelf disagree about the same book.

Books are paired by folder: both servers see the same files under different mount points, so the
path relative to each library root is the key. For every pair it compares title, author,
narrators, series, ASIN and runtime, and it lists books only one side has. Nothing is written
to either server.

Needs PLEX_URL, PLEX_TOKEN, ABS_URL, ABS_TOKEN in the environment.

  python3 plex_abs_diff.py --plex-library "Audio Books" --abs-library "Audio Books"   # -> plex_abs_diff.csv

The `differs` column names the fields that disagree:
  title      different book name (subtitles, brackets and punctuation are ignored)
  author     no author name in common
  narrators  the two narrator lists share nobody, or one side has none
  series     the two series lists share nothing, or one side has none
  asin       matched to different Audible editions
  runtime    lengths differ by more than 2% (different files behind the same folder)
  unmatched  Plex has no Audnexus match, so no tags at all
  only-plex / only-abs   the folder exists on one server only
"""
import argparse, collections, csv, json, os, re, sys, unicodedata, urllib.parse, urllib.request


def get(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=180) as r:
        return json.load(r)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


def short_title(t):
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t or "")
    t = re.split(r":| - ", t)[0]
    return re.sub(r"^(the|a|an) ", "", norm(t))


def names(s):
    return {norm(x) for x in re.split(r",|;| & | and ", s or "") if norm(x)}


def plex_books(url, token, library):
    h = {"Accept": "application/json", "X-Plex-Token": token}
    sec = next(d["key"] for d in get(url + "/library/sections", h)["MediaContainer"]["Directory"] if d["title"] == library)
    tracks = get(f"{url}/library/sections/{sec}/all?type=10", h)["MediaContainer"].get("Metadata", [])
    files, dur = collections.defaultdict(list), collections.Counter()
    for t in tracks:
        dur[t["parentRatingKey"]] += t.get("duration", 0)
        for m in t.get("Media", []):
            files[t["parentRatingKey"]] += [p["file"] for p in m.get("Part", []) if p.get("file")]
    keys = list(files)
    root = os.path.commonpath([f for k in keys for f in files[k]])
    books = {}
    for i in range(0, len(keys), 40):       # the section listing is truncated; fetch full detail in batches
        for a in get(f"{url}/library/metadata/{','.join(keys[i:i + 40])}", h)["MediaContainer"].get("Metadata", []):
            k = a["ratingKey"]
            folder = os.path.commonpath([os.path.dirname(f) for f in files[k]])
            rel = os.path.relpath(folder, root)
            m = re.search(r"audnexus://([A-Z0-9]+)", a.get("guid") or "")
            books.setdefault(rel, []).append({
                "key": k, "title": a.get("title", ""), "author": a.get("parentTitle", ""),
                "narrators": [x["tag"] for x in a.get("Style", [])],
                "series": [x["tag"][8:] for x in a.get("Mood", []) if x["tag"].startswith("Series: ")],
                "genres": [x["tag"] for x in a.get("Genre", [])], "moods": [x["tag"] for x in a.get("Mood", [])],
                "summary": a.get("summary") or "", "studio": a.get("studio") or "", "year": str(a.get("year") or ""),
                "asin": m.group(1) if m else "", "min": round(dur[k] / 60000)})
    return books


def abs_books(url, token, library):
    h = {"Authorization": "Bearer " + token}
    lib = next(l["id"] for l in get(url + "/api/libraries", h)["libraries"] if l["name"] == library)
    items = get(f"{url}/api/libraries/{lib}/items?limit=0", h)["results"]
    books = {}
    for it in items:
        m = it["media"]["metadata"]
        rel = it["relPath"] if not it.get("isFile") else os.path.dirname(it["relPath"]) or it["relPath"]
        series = [re.sub(r"\s+#[\w.\-]+$", "", s).strip() for s in (m.get("seriesName") or "").split(", ") if s.strip()]
        books[rel] = {"id": it["id"], "title": m.get("title") or "", "author": m.get("authorName") or "",
                      "narrators": [n.strip() for n in (m.get("narratorName") or "").split(", ") if n.strip()],
                      "genres": m.get("genres") or [], "summary": m.get("description") or "",
                      "studio": m.get("publisher") or "", "year": str(m.get("publishedYear") or ""),
                      "series": series, "asin": m.get("asin") or "", "min": round((it["media"].get("duration") or 0) / 60)}
    return books


def compare(p, a):
    d = []
    pt, at = short_title(p["title"]), short_title(a["title"])
    if pt != at and pt not in at and at not in pt:
        d.append("title")
    if not names(p["author"]) & names(a["author"]):
        d.append("author")
    pn, an = {norm(x) for x in p["narrators"]} - {"full cast"}, {norm(x) for x in a["narrators"]} - {"full cast"}
    if (pn or an) and not pn & an:
        d.append("narrators")
    ps, as_ = {norm(x) for x in p["series"]}, {norm(x) for x in a["series"]}
    if (ps or as_) and not ps & as_:
        d.append("series")
    if p["asin"] and a["asin"] and p["asin"] != a["asin"]:
        d.append("asin")
    if p["min"] and a["min"] and abs(p["min"] - a["min"]) > 0.02 * max(p["min"], a["min"]):
        d.append("runtime")
    if not p["asin"]:
        d.append("unmatched")
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plex-library", required=True)
    ap.add_argument("--abs-library", required=True)
    ap.add_argument("--out", default="plex_abs_diff.csv")
    args = ap.parse_args()
    env = {k: os.environ.get(k) for k in ("PLEX_URL", "PLEX_TOKEN", "ABS_URL", "ABS_TOKEN")}
    if not all(env.values()):
        sys.exit("set " + ", ".join(k for k, v in env.items() if not v))
    plex = plex_books(env["PLEX_URL"].rstrip("/"), env["PLEX_TOKEN"], args.plex_library)
    abs_ = abs_books(env["ABS_URL"].rstrip("/"), env["ABS_TOKEN"], args.abs_library)

    rows, counts = [], collections.Counter()
    cols = ["differs", "folder", "plex_title", "abs_title", "plex_author", "abs_author", "plex_narrators",
            "abs_narrators", "plex_series", "abs_series", "plex_asin", "abs_asin", "plex_min", "abs_min", "plex_key", "abs_id"]
    blank = {"title": "", "author": "", "narrators": [], "series": [], "asin": "", "min": "", "key": "", "id": ""}
    for rel in sorted(set(plex) | set(abs_), key=str.lower):
        a = abs_.get(rel)
        for p in plex.get(rel) or [None]:
            d = ["only-abs"] if p is None else ["only-plex"] if a is None else compare(p, a)
            if p is not None and a is not None and len(plex[rel]) > 1:
                d.append("split-in-plex")        # one folder, several Plex albums
            for x in d:
                counts[x] += 1
            if not d:
                counts["agree"] += 1
                continue
            p_, a_ = p or blank, a or blank
            rows.append(dict(zip(cols, [" ".join(d), rel, p_["title"], a_["title"], p_["author"], a_["author"],
                                        ", ".join(p_["narrators"]), ", ".join(a_["narrators"]), ", ".join(p_["series"]),
                                        ", ".join(a_["series"]), p_["asin"], a_["asin"], p_["min"], a_["min"],
                                        p_.get("key", ""), a_.get("id", "")])))
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"plex folders {len(plex)}, audiobookshelf items {len(abs_)}")
    for k, v in counts.most_common():
        print(f"  {v:5}  {k}")
    print(f"wrote {args.out}: {len(rows)} rows")


if __name__ == "__main__":
    main()
