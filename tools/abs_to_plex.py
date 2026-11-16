#!/usr/bin/env python3
"""
abs_to_plex.py — mirror book details from Audiobookshelf into Plex. Audiobookshelf is the source.

Books are paired by folder (see plex_abs_diff.py). Two kinds of change are proposed:

  fill     Plex has no Audnexus match for the album (Audible does not carry it, or it was never
           matched), so it has no narrators, series, genres or summary and is invisible to the
           Kometa collections. The fields are copied from Audiobookshelf and locked.
  rematch  Both sides are matched, but to different Audible listings with different narrators.
           Audible's runtime for each listing is compared with the files: the album is re-matched
           to Audiobookshelf's listing only when that one fits (within 2%) and Plex's does not.

Albums that are matched to the same listing on both sides are left to Audnexus.

  python3 abs_to_plex.py --plex-library "Audio Books" --abs-library "Audio Books" plan     # dry run -> abs_to_plex.csv
  python3 abs_to_plex.py --plex-library "Audio Books" --abs-library "Audio Books" apply abs_to_plex.csv

`plan` writes nothing to either server. `apply` writes to Plex only, and only rows whose `apply`
column is yes. Needs PLEX_URL, PLEX_TOKEN, ABS_URL, ABS_TOKEN in the environment.
"""
import argparse, csv, html, json, os, re, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_abs_diff as diff

REGION = os.environ.get("AUDIBLE_REGION", "us")
COLS = ["action", "apply", "folder", "plex_title", "set_narrators", "set_series", "joins", "set_genres", "set_year",
        "set_studio", "set_summary", "rematch_asin", "note", "plex_key", "abs_id"]
SEP = " | "


def plain(text):
    """Audiobookshelf stores descriptions as HTML; Plex shows the summary as plain text."""
    text = re.sub(r"<\s*(br|/p|/li)\s*/?>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def audible_min(asin):
    try:
        req = urllib.request.Request(f"https://api.audnex.us/books/{asin}?region={REGION}", headers={"User-Agent": "abs_to_plex/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("runtimeLengthMin") or 0
    except Exception:
        return 0


def same(a, b):
    return {diff.norm(x) for x in a} == {diff.norm(x) for x in b}


def cmd_plan(args, env):
    plex = diff.plex_books(env["PLEX_URL"], env["PLEX_TOKEN"], args.plex_library)
    abs_ = diff.abs_books(env["ABS_URL"], env["ABS_TOKEN"], args.abs_library)
    known = {m for albums in plex.values() for p in albums for m in p["moods"]}     # series tags Plex already has
    rows = []
    for rel in sorted(set(plex) & set(abs_), key=str.lower):
        if len(plex[rel]) != 1:
            continue                                   # one folder, several albums: not a clean pair
        p, a = plex[rel][0], abs_[rel]
        row = dict.fromkeys(COLS, "")
        row.update(folder=rel, plex_title=p["title"], plex_key=p["key"], abs_id=a["id"])
        if not p["asin"]:
            row["action"] = "fill"
            notes = []
            # an ensemble recording with one or two "narrators" means Audiobookshelf matched the
            # narrated edition: copying those names would put the wrong people on the album
            ensemble = re.search(r"\((dramatized|full[- ]cast)", p["title"], re.I) and len(a["narrators"]) <= 2
            if ensemble and a["narrators"]:
                notes.append("narrators skipped: Audiobookshelf lists the narrated edition's, fix the cast there")
            elif a["narrators"] and not same(a["narrators"], p["narrators"]):
                row["set_narrators"] = SEP.join(a["narrators"])
            moods = ["Series: " + s for s in a["series"]]
            if moods and not same(moods, p["moods"]):
                row["set_series"] = SEP.join(moods)
                row["joins"] = SEP.join(m[8:] for m in moods if m in known)
            if a["genres"] and not same(a["genres"], p["genres"]):
                row["set_genres"] = SEP.join(a["genres"])
            year = re.search(r"\b(1[5-9]|20)\d\d\b", a["year"])
            if year and year.group(0) != p["year"]:
                row["set_year"] = year.group(0)
            if a["studio"] and a["studio"] != p["studio"]:
                row["set_studio"] = a["studio"]
            if plain(a["summary"]) and plain(a["summary"]) != p["summary"].strip():
                row["set_summary"] = plain(a["summary"])
            changes = [c for c in COLS if c.startswith("set_") and row[c]]
            wrong = [d for d in diff.compare(p, a) if d in ("title", "author", "runtime")]
            if wrong:
                notes.append(f"Audiobookshelf's own match looks wrong ({', '.join(wrong)} differ): fix it there first")
            if not changes:
                notes.append("nothing to copy" if not (a["narrators"] or a["series"]) else "already mirrored")
            row["note"] = "; ".join(notes)
            row["apply"] = "yes" if changes and not wrong else "no"
        elif a["asin"] and a["asin"] != p["asin"] and "narrators" in diff.compare(p, a):
            row["action"], row["rematch_asin"] = "rematch", a["asin"]
            pm, am, f = audible_min(p["asin"]), audible_min(a["asin"]), p["min"]
            tol = max(3, 0.02 * f)
            fit_a, fit_p = bool(am) and abs(am - f) <= tol, bool(pm) and abs(pm - f) <= tol
            row["set_narrators"] = SEP.join(a["narrators"])
            row["note"] = f"files {f} min; Audiobookshelf's listing {am or '?'}; Plex's listing {pm or '?'} ({', '.join(p['narrators']) or 'no narrator'})"
            row["apply"] = "yes" if fit_a and not fit_p else "no"
            if not row["apply"] == "yes":
                row["note"] += "; both fit, listen to tell" if fit_a and fit_p else "; Plex's fits, Audiobookshelf may be wrong" if fit_p else "; neither fits"
        else:
            continue
        rows.append(row)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    for act in ("fill", "rematch"):
        r = [x for x in rows if x["action"] == act]
        print(f"{act:8} {len(r):4} album(s), {sum(x['apply'] == 'yes' for x in r)} set to apply")
    print(f"wrote {args.out}. Nothing was changed. Review the apply column, then run apply.")


def cmd_apply(args, env):
    url, h = env["PLEX_URL"], {"Accept": "application/json", "X-Plex-Token": env["PLEX_TOKEN"]}
    sec = next(d["key"] for d in diff.get(url + "/library/sections", h)["MediaContainer"]["Directory"] if d["title"] == args.plex_library)

    def put(path):
        urllib.request.urlopen(urllib.request.Request(url + path, method="PUT", headers=h), timeout=120).read()

    with open(args.csv, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["apply"].strip().lower() == "yes"]
    for r in rows:
        try:
            if r["action"] == "rematch":
                guid = f"com.plexapp.agents.audnexus://{r['rematch_asin']}_{REGION}?lang=en"
                put(f"/library/metadata/{r['plex_key']}/match?guid={urllib.parse.quote(guid, safe='')}&name={urllib.parse.quote(r['plex_title'])}")
            else:
                q = [("type", "9"), ("id", r["plex_key"])]
                now = diff.get(f"{url}/library/metadata/{r['plex_key']}", h)["MediaContainer"]["Metadata"][0]
                for col, field in (("set_narrators", "style"), ("set_series", "mood"), ("set_genres", "genre")):
                    if r[col]:
                        new = r[col].split(SEP)
                        q += [(f"{field}[{i}].tag.tag", v) for i, v in enumerate(new)] + [(f"{field}.locked", "1")]
                        stale = [x["tag"] for x in now.get(field.capitalize(), []) if x["tag"] not in new]
                        if stale:               # setting tags only adds; the ones no longer wanted are removed by name
                            q.append((f"{field}[].tag.tag-", ",".join(urllib.parse.quote(t, safe="") for t in stale)))
                for col, field in (("set_studio", "studio"), ("set_summary", "summary"), ("set_year", "year")):
                    if r[col]:
                        q += [(f"{field}.value", r[col]), (f"{field}.locked", "1")]
                put(f"/library/sections/{sec}/all?" + urllib.parse.urlencode(q))
            print(f"  ok  {r['action']:8} {r['folder']}")
        except Exception as e:
            print(f"  FAILED  {r['folder']}: {e}")
        time.sleep(0.5)
    print(f"{len(rows)} row(s) sent. Run plan again to confirm nothing is left, then run Kometa.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plex-library", required=True)
    ap.add_argument("--abs-library", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan", help="dry run: write the proposed changes to a CSV")
    sp.add_argument("--out", default="abs_to_plex.csv")
    sa = sub.add_parser("apply", help="write the rows marked apply=yes to Plex")
    sa.add_argument("csv")
    args = ap.parse_args()
    env = {k: (os.environ.get(k) or "").rstrip("/") for k in ("PLEX_URL", "PLEX_TOKEN", "ABS_URL", "ABS_TOKEN")}
    if not all(env.values()):
        sys.exit("set " + ", ".join(k for k, v in env.items() if not v))
    (cmd_plan if args.cmd == "plan" else cmd_apply)(args, env)


if __name__ == "__main__":
    main()
