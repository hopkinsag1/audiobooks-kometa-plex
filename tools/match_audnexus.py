#!/usr/bin/env python3
"""
match_audnexus.py — find Plex audiobooks that never matched the Audnexus agent,
look them up on Audible, and (after you review the list) match them in bulk.

Why: Plex's "Unmatched" filter only shows albums with NO agent. Albums that Plex bound
to the Personal Media agent or a local stub years ago look "matched" but carry no
genres, series or narrator, so they never appear in any Kometa collection. This tool
finds all of them by looking at the album GUID instead.

Stdlib only (xml.etree parses responses from your own Plex server). Two steps:

  1. propose  -> writes pairings.csv (one row per unmatched book, best Audible ASIN,
                 confidence, runtimes). Read-only. Review/edit the CSV.
  2. apply    -> matches every row in the CSV whose `apply` column is "yes"
                 (propose pre-fills "yes" for HIGH confidence rows only).

Usage:
  export PLEX_URL=http://192.168.1.2:32400
  export PLEX_TOKEN=xxxxxxxxxxxx
  python3 match_audnexus.py propose --library "Audiobooks"            # -> pairings.csv
  python3 match_audnexus.py apply   --library "Audiobooks" pairings.csv
  python3 match_audnexus.py verify  --library "Audiobooks" pairings.csv

Confidence:
  HIGH = title + author match and runtime within ~15 min  -> safe to apply blind
  MED  = same book, but runtime differs (different edition / narrator)
  LOW  = weak match, look at it yourself
  NONE = nothing on Audible (Spotify/Max originals, out-of-print recordings, ...)
"""
import argparse, csv, difflib, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

AUDIBLE = "https://api.audible.com/1.0/catalog/products"
AUDNEXUS = "https://api.audnex.us/books/"
REGION = os.environ.get("AUDIBLE_REGION", "us")


# ---------------------------------------------------------------- Plex helpers
def plex():
    url = os.environ.get("PLEX_URL", "").rstrip("/")
    tok = os.environ.get("PLEX_TOKEN", "")
    if not url or not tok:
        sys.exit("set PLEX_URL and PLEX_TOKEN")
    return url, tok


def plex_get(path, **params):
    url, tok = plex()
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}{path}{'?' + q if q else ''}", headers={"X-Plex-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r:
        return ET.fromstring(r.read())


def plex_put(path, **params):
    url, tok = plex()
    req = urllib.request.Request(f"{url}{path}?{urllib.parse.urlencode(params)}", method="PUT",
                                 headers={"X-Plex-Token": tok})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def section_id(name):
    for d in plex_get("/library/sections").findall("Directory"):
        if d.get("title") == name:
            return d.get("key")
    sys.exit(f"library '{name}' not found")


def albums(sec):
    return plex_get(f"/library/sections/{sec}/all", type=9, **{"X-Plex-Container-Start": 0,
                                                             "X-Plex-Container-Size": 10000}).findall("Directory")


def album_minutes(key):
    ms = sum(int(t.get("duration") or 0) for t in plex_get(f"/library/metadata/{key}/children").findall("Track"))
    return ms // 60000


def album_detail(keys):
    out = []
    for i in range(0, len(keys), 40):
        out += plex_get("/library/metadata/" + ",".join(keys[i:i + 40])).findall("Directory")
    return out


# ------------------------------------------------------------- Audible lookup
def audible_search(title, author):
    params = {"num_results": 8, "products_sort_by": "Relevance",
              "response_groups": "contributors,series,product_attrs,product_desc"}
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    try:
        with urllib.request.urlopen(AUDIBLE + "?" + urllib.parse.urlencode(params), timeout=30) as r:
            return json.load(r).get("products", [])
    except Exception:
        return []


def audnexus_ok(asin):
    req = urllib.request.Request(f"{AUDNEXUS}{asin}?region={REGION}", headers={"User-Agent": "match_audnexus/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("title") is not None
    except Exception:
        return False


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower())


def short_title(t):
    return re.sub(r"\s*[:(\-–].*$", "", t).strip()


def first_author(a):
    return re.sub(r"\s*-\s*Editor.*", "", re.split(r",|\|", a)[0]).strip()


def score(book, cand):
    if cand.get("language") and cand["language"].lower() != "english":
        return -1
    ts = difflib.SequenceMatcher(None, norm(short_title(book["title"])), norm(cand.get("title"))).ratio()
    au = norm(first_author(book["author"]))
    asc = max([difflib.SequenceMatcher(None, au, norm(x["name"])).ratio() for x in cand.get("authors", [])] or [0])
    if cand.get("runtime_length_min") and book["plex_min"]:
        rs = max(0.0, 1 - abs(cand["runtime_length_min"] - book["plex_min"]) / max(book["plex_min"], 1) * 2)
    else:
        rs = 0.3
    return ts * 0.5 + asc * 0.2 + rs * 0.3


def confidence(book, cand, s):
    if not cand:
        return "NONE"
    d = abs((cand.get("runtime_length_min") or 0) - book["plex_min"])
    if s >= 0.85 and d <= 15:
        return "HIGH"
    return "MED" if s >= 0.7 else "LOW"


# ------------------------------------------------------------------- commands
FIELDS = ["apply", "confidence", "ratingKey", "author", "plex_title", "plex_min",
          "asin", "audible_title", "narrators", "audible_min", "series"]


def cmd_propose(args):
    sec = section_id(args.library)
    unmatched = [a for a in albums(sec) if "audnexus" not in (a.get("guid") or "")]
    print(f"{len(unmatched)} albums without an Audnexus GUID", file=sys.stderr)
    rows = []
    for a in unmatched:
        book = {"key": a.get("ratingKey"), "author": a.get("parentTitle") or "", "title": a.get("title") or "",
                "plex_min": album_minutes(a.get("ratingKey"))}
        t, au = short_title(book["title"]), first_author(book["author"])
        cands = audible_search(t, au) or audible_search(book["title"], au) or audible_search(t, "")
        best, bs = None, -9
        for c in cands:
            s = score(book, c)
            if s > bs:
                bs, best = s, c
        conf = confidence(book, best, bs)
        if best and conf in ("HIGH", "MED") and not audnexus_ok(best["asin"]):
            conf = "LOW"   # Audible has it but Audnexus refuses it (podcasts etc.)
        rows.append({"apply": "yes" if conf == "HIGH" else "no", "confidence": conf, "ratingKey": book["key"],
                     "author": book["author"], "plex_title": book["title"], "plex_min": book["plex_min"],
                     "asin": best["asin"] if best else "", "audible_title": best.get("title") if best else "",
                     "narrators": ", ".join(n["name"] for n in best.get("narrators", [])) if best else "",
                     "audible_min": best.get("runtime_length_min") if best else "",
                     "series": ", ".join(f"{s.get('title')} #{s.get('sequence')}" for s in best.get("series", [])) if best else ""})
        print(f"  {conf:4} {book['author'][:24]:24} / {book['title'][:45]:45} -> {rows[-1]['asin']:10} "
              f"{book['plex_min']}->{rows[-1]['audible_min']}", file=sys.stderr)
        time.sleep(0.3)
    order = {"HIGH": 0, "MED": 1, "LOW": 2, "NONE": 3}
    rows.sort(key=lambda r: (order[r["confidence"]], r["author"], r["plex_title"]))
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {args.out}: " + ", ".join(f"{k}={sum(1 for r in rows if r['confidence'] == k)}" for k in order),
          file=sys.stderr)


def cmd_apply(args):
    section_id(args.library)  # validates connection
    rows = [r for r in csv.DictReader(open(args.csv)) if r["apply"].strip().lower() == "yes" and r["asin"]]
    print(f"applying {len(rows)} matches", file=sys.stderr)
    ok = 0
    for r in rows:
        guid = f"com.plexapp.agents.audnexus://{r['asin']}_{REGION}?lang=en"
        try:  # `name` is required — Plex returns 400 without it
            st = plex_put(f"/library/metadata/{r['ratingKey']}/match", guid=guid, name=r["audible_title"] or r["plex_title"])
            ok += st == 200
            print(f"  {'ok ' if st == 200 else 'ERR'} {r['ratingKey']:7} {r['asin']:11} {r['plex_title'][:60]}", file=sys.stderr)
        except Exception as e:
            print(f"  ERR {r['ratingKey']:7} {r['asin']:11} {r['plex_title'][:60]}  {e}", file=sys.stderr)
        time.sleep(1)
    print(f"{ok}/{len(rows)} accepted by Plex. Metadata arrives within a few minutes; run `verify` then Kometa.",
          file=sys.stderr)


def cmd_verify(args):
    section_id(args.library)
    rows = [r for r in csv.DictReader(open(args.csv)) if r["apply"].strip().lower() == "yes" and r["asin"]]
    det = album_detail([r["ratingKey"] for r in rows])
    g = sum("audnexus" in (a.get("guid") or "") for a in det)
    ge = sum(bool(a.findall("Genre")) for a in det)
    se = sum(any((m.get("tag") or "").startswith("Series:") for m in a.findall("Mood")) for a in det)
    st = sum(bool(a.findall("Style")) for a in det)
    print(f"{len(det)} albums: audnexus guid={g} genres={ge} series={se} narrator={st}")
    for a in det:
        if not a.findall("Genre"):
            print(f"  still no genres: {a.get('ratingKey')} {a.get('parentTitle')} / {a.get('title')}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--library", required=True, help="Plex audiobook library name")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("propose"); a.add_argument("--out", default="pairings.csv")
    b = sub.add_parser("apply"); b.add_argument("csv")
    c = sub.add_parser("verify"); c.add_argument("csv")
    args = p.parse_args()
    {"propose": cmd_propose, "apply": cmd_apply, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
