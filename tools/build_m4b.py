#!/usr/bin/env python3
"""
build_m4b.py — turn a downloaded audiobook series (GraphicAudio parts, loose mp3 tracks, or
finished m4b files) into one chaptered, tagged .m4b per book, ready to drop into the library.

Two steps, so the grouping can be reviewed before any audio is written:

  plan   read-only. Probes every audio file under SOURCE, groups files into books (multi-part
         releases are merged), looks each book up on Audible for cast / series / ASIN and
         compares runtimes. Writes a JSON plan you can edit.
  lookup redoes the Audible lookup for a plan whose titles or authors you corrected by hand.
  build  reads the plan and writes <OUT>/<Author>/<Title> <suffix>/<Title> <suffix>.m4b with
         chapters, tags and cover. Never writes inside SOURCE and never overwrites.
  publish  moves the built books from staging into the library (never over an existing folder),
         then, if asked, scans and matches them in Audiobookshelf and Plex to the first part's
         ASIN, keeping the merged book's own title.

AAC sources are stream-copied (no quality loss). Anything else (mp3, flac...) is re-encoded to
AAC, because .m4b cannot hold it. Chapters come from the sources' own chapter marks, or one
chapter per file when a file has none.

Where the audio lives decides how ffmpeg is run:
  default            local ffmpeg/ffprobe, SOURCE and OUT are local paths
  --ssh user@host    SOURCE and OUT are paths on that host; ffmpeg runs there in a throwaway
                     container (--image, default the Audiobookshelf image, which ships ffmpeg)

Usage (top-level flags come before the subcommand):
  python3 build_m4b.py --ssh root@nas plan  "/downloads/Series (GraphicAudio)"   # -> m4b_plan.json
  python3 build_m4b.py --ssh root@nas build m4b_plan.json --out /downloads/staging
  python3 build_m4b.py publish m4b_plan.json --staging /downloads/staging --library /audiobooks \
        --abs-library "Audio Books" --plex-library "Audio Books"

Stdlib only. AUDIBLE_REGION (default us) picks the Audible store for the lookup.
"""
import argparse, json, os, re, shlex, subprocess, sys, unicodedata, urllib.parse, urllib.request

AUDIBLE = "https://api.audible.com/1.0/catalog/products"
REGION = os.environ.get("AUDIBLE_REGION", "us")
AUDIO = (".m4b", ".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".wav")
IMAGES = (".jpg", ".jpeg", ".png")
PROBE = ["-v", "error", "-show_entries",
         "format=duration,bit_rate:format_tags:stream=codec_type,codec_name,sample_rate,channels",
         "-show_chapters", "-of", "json"]


# ---------- running things where the files are ----------

class Runner:
    """Local, or over ssh with ffmpeg in a container. src is mounted read-only as /in, out as /out."""

    def __init__(self, ssh, image, src=None, out=None):
        self.ssh, self.image, self.src, self.out = ssh, image, src, out
        for d in (src, out):                 # docker would silently create a missing bind-mount folder
            if d and self.sh(f"test -d {shlex.quote(d)}", check=False).returncode:
                sys.exit(f"folder does not exist: {d}")
        # ssh mode: run the container as whoever owns the output (or source) folder, and hand
        # everything we create to that owner, so files never end up root-owned on the share
        self.owner = self.sh(f"stat -c %u:%g {shlex.quote(out or src)}").stdout.strip() if ssh else None

    def own(self, rel):
        if self.owner:
            top = rel.split("/")[0]
            self.sh(f"chown -R {self.owner} {shlex.quote(self.real_out(top))}")

    def sh(self, cmd, stdin=None, check=True):
        argv = ["ssh", "-o", "BatchMode=yes", self.ssh, cmd] if self.ssh else ["sh", "-c", cmd]
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True)
        if check and p.returncode:
            sys.exit(f"command failed ({p.returncode}): {cmd}\n{p.stderr[-2000:]}")
        return p

    def inp(self, rel):                      # path of a source file as ffmpeg sees it
        return ("/in/" + rel) if self.ssh else os.path.join(self.src, rel)

    def outp(self, rel):                     # path of an output file as ffmpeg sees it
        return ("/out/" + rel) if self.ssh else os.path.join(self.out, rel)

    def real_out(self, rel):                 # path of an output file on the machine that holds it
        return os.path.join(self.out, rel)

    def tool(self, name, args, check=True):
        if not self.ssh:
            return self.sh(shlex.join([name] + args), check=check)
        mounts = ["-v", f"{self.src}:/in:ro"] + (["-v", f"{self.out}:/out"] if self.out else [])
        return self.sh(shlex.join(["docker", "run", "--rm", "--network", "none", "-u", self.owner] + mounts +
                                  ["--entrypoint", name, self.image] + args), check=check)

    def list_files(self):
        out = self.sh(f"cd {shlex.quote(self.src)} && find . -type f").stdout
        return sorted((l[2:] for l in out.splitlines() if l.startswith("./")), key=natural)

    def probe_all(self, rels):
        loop = 'for f; do printf "@@%s\\n" "$f"; ffprobe ' + shlex.join(PROBE) + ' "$f"; done'
        res = {}
        for i in range(0, len(rels), 200):
            chunk = [self.inp(r) for r in rels[i:i + 200]]
            out = self.tool("sh", ["-c", loop, "sh"] + chunk).stdout
            for block in out.split("@@")[1:]:
                path, _, body = block.partition("\n")
                rel = path[4:] if self.ssh else os.path.relpath(path, self.src)
                try:
                    res[rel] = json.loads(body)
                except ValueError:
                    res[rel] = {}
        return res

    def write(self, rel, text):
        self.sh(f"cat > {shlex.quote(self.real_out(rel))}", stdin=text)
        self.own(rel)

    def mkdir(self, rel):
        self.sh(f"mkdir -p {shlex.quote(self.real_out(rel))}")
        self.own(rel)

    def exists(self, rel):
        return self.sh(f"test -e {shlex.quote(self.real_out(rel))}", check=False).returncode == 0


# ---------- names ----------

def natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


PART = re.compile(r"[\(\[]?\b(?:part|pt\.?|disc|cd)?\s*(\d+)\s*of\s*(\d+)\b[\)\]]?|[\(\[]?\b(?:part|pt\.?)\s*(\d+)\b[\)\]]?", re.I)


def part_no(s):
    m = PART.search(s or "")
    return int(m.group(1) or m.group(3)) if m else 0


def clean_title(s):
    s = PART.sub(" ", s or "")
    s = re.sub(r"[\(\[]\s*(dramati[sz]ed adaptation|gra\w{2,4}ic ?audio|unabridged|full[- ]cast[^\)\]]*)\s*[\)\]]", " ", s, flags=re.I)
    s = re.sub(r"\bdramati[sz]ed adaptation\b", " ", s, flags=re.I)
    s = re.sub(r"^\s*\d+\s*[-._]\s*", "", s)             # leading track/book number
    return re.sub(r"\s+", " ", s).strip(" -:_")


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "-", s).strip(" .")


# ---------- Audible ----------

def audible_parts(title, author, marker):
    """All Audible listings that are a part of this book's dramatized edition, in part order."""
    want, mk = norm(title), norm(marker)
    for who in (author, None):               # retry without the author: download tags misspell names
        params = {"title": f"{title} {marker}", "num_results": 25,
                  "response_groups": "product_desc,contributors,media,series", "products_sort_by": "Relevance"}
        if who:
            params["author"] = who
        try:
            with urllib.request.urlopen(f"{AUDIBLE.replace('.com', '.' + ('com' if REGION == 'us' else REGION))}"
                                        f"?{urllib.parse.urlencode(params)}", timeout=30) as r:
                prods = json.load(r).get("products", [])
        except Exception as e:
            print(f"  audible lookup failed: {e}", file=sys.stderr)
            return []
        hits = [p for p in prods if mk in norm(p.get("title", "")) and norm(clean_title(p.get("title", ""))) == want]
        if hits:
            return sorted(hits, key=lambda p: part_no(p.get("title", "")))
    return []


# ---------- plan ----------

def fill_audible(book, marker):
    """Look the book up by its (possibly hand-edited) title and author; fill cast, ASINs, series."""
    parts = audible_parts(book["title"], book["author"], marker)
    if not parts:
        book.update(asins=[], narrators=[], audible_min=0, confidence="NONE")
        return
    narr = []
    for p in parts:
        narr += [n["name"] for n in p.get("narrators") or [] if n["name"] not in narr]
    amin = sum(p.get("runtime_length_min") or 0 for p in parts)
    series = next((x for p in parts for x in p.get("series") or [] if x.get("sequence")), None) \
        or next((x for p in parts for x in p.get("series") or []), None)
    if series:
        book.update(series=series["title"], sequence=series.get("sequence") or book.get("sequence", ""))
    if parts[0].get("authors"):                      # Audible's spelling beats the file tag's
        book["author"] = parts[0]["authors"][0]["name"]
    book.update(asins=[p["asin"] for p in parts], narrators=narr, audible_min=amin,
                year=(parts[0].get("release_date") or "")[:4] or book.get("year", ""),
                confidence="HIGH" if abs(amin - book["source_min"]) <= max(15, 0.03 * book["source_min"]) else "CHECK")


def show(book):
    print(f"  {book['confidence']:5} {book['title'][:44]:44} {len(book['files']):3} file(s) {book['source_min']:5} min "
          f"vs audible {book['audible_min']:5} ({len(book['asins'])} part(s))  {book['codec']}  "
          f"{book['chapters']} ch  cast {len(book['narrators'])}")


def cmd_lookup(args):
    """Redo the Audible lookup after titles or authors were edited in the plan."""
    with open(args.plan) as f:
        plan = json.load(f)
    for book in plan["books"]:
        fill_audible(book, plan.get("marker", "Dramatized Adaptation"))
        show(book)
    with open(args.plan, "w") as f:
        json.dump(plan, f, indent=1, ensure_ascii=False)

def tags_of(probe):
    return {k.lower(): v for k, v in (probe.get("format", {}).get("tags") or {}).items()}


def cmd_plan(args):
    r = Runner(args.ssh, args.image, src=args.source.rstrip("/"))
    files = r.list_files()
    audio = [f for f in files if f.lower().endswith(AUDIO)]
    if not audio:
        sys.exit("no audio files found")
    print(f"probing {len(audio)} audio files ...")
    probes = r.probe_all(audio)

    books = {}
    for f in audio:
        t = tags_of(probes[f])
        d = os.path.dirname(f)
        # the book's name: album tag, else the folder, else the file name
        raw = args.title or t.get("album") or (os.path.basename(d) if d else os.path.splitext(os.path.basename(f))[0])
        key = norm(clean_title(raw))
        b = books.setdefault(key, {"title": clean_title(raw), "files": [], "tags": t})
        order = (part_no(t.get("album", "")) or part_no(d) or part_no(os.path.basename(f)),
                 int((t.get("disc") or "0").split("/")[0] or 0) if (t.get("disc") or "0").split("/")[0].isdigit() else 0,
                 int((t.get("track") or "0").split("/")[0] or 0) if (t.get("track") or "0").split("/")[0].isdigit() else 0,
                 natural(f))
        b["files"].append((order, f))

    plan = {"source": r.src, "ssh": args.ssh, "suffix": args.suffix, "marker": args.marker, "books": []}
    for b in sorted(books.values(), key=lambda b: natural(sorted(f for _, f in b["files"])[0])):
        fl = [f for _, f in sorted(b["files"])]
        t = b["tags"]
        author = args.author or re.split(r"[,;/&]", t.get("album_artist") or t.get("artist") or "Unknown")[0].strip()
        mins = round(sum(float(probes[f].get("format", {}).get("duration", 0)) for f in fl) / 60)
        codecs = sorted({s["codec_name"] for f in fl for s in probes[f].get("streams", []) if s["codec_type"] == "audio"})
        chapters = sum(len(probes[f].get("chapters", [])) or 1 for f in fl)
        dirs = {os.path.dirname(f) for f in fl}
        own_dir = len(dirs) == 1 and all(os.path.dirname(a) != list(dirs)[0] or a in fl for a in audio)
        cover = next((f for f in files if own_dir and os.path.dirname(f) == list(dirs)[0] and f.lower().endswith(IMAGES)), None)
        if not cover and any(s["codec_type"] == "video" for s in probes[fl[0]].get("streams", [])):
            cover = "embedded"

        g = re.match(r"(.+?)\s*#\s*([\d.]+)\s*$", t.get("grouping", ""))
        book = {"build": True, "title": b["title"], "author": author,
                "series": g.group(1) if g else "", "sequence": g.group(2) if g else "",
                "year": (t.get("date") or "")[:4], "narrators": [], "asins": [],
                "source_min": mins, "audible_min": 0, "confidence": "NONE",
                "codec": "+".join(codecs), "chapters": chapters, "cover": cover, "files": fl}
        fill_audible(book, args.marker)
        plan["books"].append(book)
        show(book)
    with open(args.plan, "w") as f:
        json.dump(plan, f, indent=1, ensure_ascii=False)
    print(f"wrote {args.plan}: {len(plan['books'])} book(s). Review it (titles, file order, build flags), then run build.")


# ---------- build ----------

def esc(v):
    return re.sub(r"([=;#\\\n])", r"\\\1", str(v))


def cmd_build(args):
    with open(args.plan) as f:
        plan = json.load(f)
    r = Runner(plan.get("ssh"), args.image, src=plan["source"], out=args.out.rstrip("/"))
    todo = [b for b in plan["books"] if b.get("build")]
    r.mkdir(".work")
    probes = r.probe_all([f for b in todo for f in b["files"]])
    for n, b in enumerate(todo):
        display = f"{b['title']} {plan['suffix']}".strip()     # goes in the tags, colons and all
        name = safe_name(display)                             # goes on disk
        folder = os.path.join(safe_name(b["author"]), name)
        target = os.path.join(folder, name + ".m4b")
        if r.exists(target):
            print(f"  skip (exists)  {target}")
            continue
        r.mkdir(folder)

        # chapters: each source's own marks shifted to its place in the book, else one per file
        chapters, offset = [], 0.0
        for f in b["files"]:
            p = probes[f]
            dur = float(p["format"]["duration"])
            own = p.get("chapters") or []
            if own:
                for c in own:
                    chapters.append((offset + float(c["start_time"]), offset + float(c["end_time"]),
                                     (c.get("tags") or {}).get("title") or f"Chapter {len(chapters) + 1}"))
            else:
                t = tags_of(p).get("title") or os.path.splitext(os.path.basename(f))[0]
                chapters.append((offset, offset + dur, clean_title(t) if len(b["files"]) > 1 else b["title"]))
            offset += dur

        meta = [";FFMETADATA1", f"title={esc(display)}", f"album={esc(display)}", f"artist={esc(b['author'])}",
                f"album_artist={esc(b['author'])}", "genre=Audiobook"]
        if b.get("narrators"):
            meta.append(f"composer={esc(', '.join(b['narrators']))}")
        if b.get("year"):
            meta.append(f"date={esc(b['year'])}")
        if b.get("series"):
            meta += [f"show={esc(b['series'])}", f"grouping={esc(b['series'])} #{esc(b.get('sequence', ''))}"]
            if b.get("sequence"):
                meta.append(f"episode_id={esc(b['sequence'])}")
        if b.get("asins"):
            meta.append(f"comment={esc('ASIN ' + ' '.join(b['asins']))}")
        for s, e, t in chapters:
            meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={round(s * 1000)}", f"END={round(e * 1000)}", f"title={esc(t)}"]
        r.write(f".work/{n}.meta", "\n".join(meta) + "\n")

        cover = None
        if b.get("cover") == "embedded":
            cover = f".work/{n}.cover.jpg"
            r.tool("ffmpeg", ["-v", "error", "-y", "-i", r.inp(b["files"][0]), "-an", "-frames:v", "1", r.outp(cover)], check=False)
            cover = cover if r.exists(cover) else None
        elif b.get("cover"):
            cover = os.path.join(folder, "cover" + os.path.splitext(b["cover"])[1].lower())
            r.sh(f"cp -n {shlex.quote(os.path.join(plan['source'], b['cover']))} {shlex.quote(r.real_out(cover))}")
            r.own(cover)

        streams = [s for f in b["files"] for s in probes[f]["streams"] if s["codec_type"] == "audio"]
        same = len({(s["codec_name"], s.get("sample_rate"), s.get("channels")) for s in streams}) == 1
        if same and streams[0]["codec_name"] == "aac":
            codec, how = ["-c:a", "copy"], "copy"
        else:
            kbps = sum(int(probes[f]["format"].get("bit_rate", 96000)) for f in b["files"]) / len(b["files"]) / 1000
            rate = min((64, 96, 128), key=lambda k: abs(k - min(kbps, 128)))
            codec, how = ["-c:a", "aac", "-b:a", f"{rate}k"], f"re-encode to AAC {rate}k"

        if len(b["files"]) == 1:
            src = ["-i", r.inp(b["files"][0])]
        else:
            listing = "".join("file '" + r.inp(f).replace("'", "'\\''") + "'\n" for f in b["files"])
            r.write(f".work/{n}.list", listing)
            src = ["-f", "concat", "-safe", "0", "-i", r.outp(f".work/{n}.list")]
        cmd = ["-v", "error", "-n"] + src + ["-i", r.outp(f".work/{n}.meta")]
        maps = ["-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1"] + codec
        if cover:
            cmd += ["-i", r.outp(cover)]
            maps += ["-map", "2:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        print(f"  build {name}  [{len(b['files'])} file(s), {len(chapters)} chapters, {how}] ...", flush=True)
        p = r.tool("ffmpeg", cmd + maps + ["-movflags", "+faststart", r.outp(target)], check=False)
        if p.returncode:
            print(f"    FAILED: {p.stderr[-600:]}")
            continue
        if cover and cover.startswith(".work/"):
            r.sh(f"cp -n {shlex.quote(r.real_out(cover))} {shlex.quote(r.real_out(os.path.join(folder, 'cover.jpg')))}")
            r.own(folder)
        chk = r_out_probe(r, target)
        ok = abs(chk[0] - offset) < 5 and chk[1] == len(chapters)
        print(f"    {'ok ' if ok else 'CHECK'} {chk[0] / 3600:.2f} h (source {offset / 3600:.2f} h), {chk[1]} chapters -> {r.real_out(target)}")
    r.sh(f"rm -rf {shlex.quote(r.real_out('.work'))}")


def r_out_probe(r, rel):
    """Duration and chapter count of a finished file."""
    probe = Runner(r.ssh, r.image, src=r.out)
    d = probe.probe_all([rel]).get(rel) or {}
    return float(d.get("format", {}).get("duration", 0)), len(d.get("chapters", []))


# ---------- publish ----------

def http(method, url, headers, body=None):
    req = urllib.request.Request(url, method=method, headers=dict(headers, **({"Content-Type": "application/json"} if body is not None else {})),
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else None


def cmd_publish(args):
    import time
    with open(args.plan) as f:
        plan = json.load(f)
    r = Runner(plan.get("ssh"), args.image, src=args.staging.rstrip("/"), out=args.library.rstrip("/"))
    moved = []
    for b in plan["books"]:
        if not b.get("build"):
            continue
        display = f"{b['title']} {plan['suffix']}".strip()
        name = safe_name(display)
        rel = os.path.join(safe_name(b["author"]), name)
        if r.exists(rel):
            print(f"  skip (already in library)  {rel}")
            continue
        if r.sh(f"test -f {shlex.quote(os.path.join(r.src, rel, name + '.m4b'))}", check=False).returncode:
            print(f"  skip (not built)  {rel}")
            continue
        r.sh(f"mkdir -p {shlex.quote(r.real_out(os.path.dirname(rel)))} && "
             f"cp -a {shlex.quote(os.path.join(r.src, rel))} {shlex.quote(r.real_out(rel))} && "
             f"chown -R {r.owner or '$(id -u):$(id -g)'} {shlex.quote(r.real_out(rel))} && "
             f"rm -rf {shlex.quote(os.path.join(r.src, rel))}")
        print(f"  in library  {rel}")
        moved.append((b, display, rel))
    if not moved:
        return

    abs_url, abs_tok = os.environ.get("ABS_URL"), os.environ.get("ABS_TOKEN")
    if abs_url and abs_tok and args.abs_library:
        h = {"Authorization": "Bearer " + abs_tok}
        lib = next(l["id"] for l in http("GET", abs_url + "/api/libraries", h)["libraries"] if l["name"] == args.abs_library)
        http("POST", f"{abs_url}/api/libraries/{lib}/scan", h)
        todo, deadline = {rel: (b, name) for b, name, rel in moved}, time.time() + 300
        while todo and time.time() < deadline:
            time.sleep(10)
            items = http("GET", f"{abs_url}/api/libraries/{lib}/items?limit=0&minified=1&sort=addedAt&desc=1", h)["results"]
            for it in items:
                if it["relPath"] in todo:
                    b, name = todo.pop(it["relPath"])
                    if b.get("asins"):
                        http("POST", f"{abs_url}/api/items/{it['id']}/match", h,
                             {"provider": "audible", "asin": b["asins"][0], "overrideDetails": True, "overrideCover": True})
                    m = http("GET", f"{abs_url}/api/items/{it['id']}?expanded=1", h)["media"]["metadata"]
                    series = [dict(id=x["id"], name=x["name"], sequence=x.get("sequence")) for x in m.get("series", [])]
                    http("PATCH", f"{abs_url}/api/items/{it['id']}/media", h,   # one title for the merged book, not "Part 1 of 2"
                         {"metadata": {"title": name, "subtitle": None, "narrators": b.get("narrators") or m.get("narrators"), "series": series}})
                    print(f"  audiobookshelf ok  {name}")
        for rel in todo:
            print(f"  audiobookshelf: not seen after scan  {rel}")

    plex_url, plex_tok = os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN")
    if plex_url and plex_tok and args.plex_library:
        h = {"Accept": "application/json", "X-Plex-Token": plex_tok}
        sec = next(d["key"] for d in http("GET", plex_url + "/library/sections", h)["MediaContainer"]["Directory"] if d["title"] == args.plex_library)
        http("GET", f"{plex_url}/library/sections/{sec}/refresh", h)
        todo, deadline = {name: b for b, name, rel in moved}, time.time() + 600
        while todo and time.time() < deadline:
            time.sleep(15)
            recent = http("GET", f"{plex_url}/library/sections/{sec}/all?type=9&sort=addedAt:desc&X-Plex-Container-Start=0&X-Plex-Container-Size=60", h)
            for a in recent["MediaContainer"].get("Metadata", []):
                b = todo.get(a["title"])
                if b is None:
                    continue
                del todo[a["title"]]
                if b.get("asins"):                # the match call needs name= or Plex answers 400
                    q = urllib.parse.urlencode({"guid": f"com.plexapp.agents.audnexus://{b['asins'][0]}_{REGION}?lang=en", "name": a["title"]})
                    http("PUT", f"{plex_url}/library/metadata/{a['ratingKey']}/match?{q}", h)
                q = urllib.parse.urlencode({"type": 9, "title.value": a["title"], "title.locked": 1})
                http("PUT", f"{plex_url}/library/metadata/{a['ratingKey']}?{q}", h)   # keep the merged-book title
                print(f"  plex ok  {a['title']}  (ratingKey {a['ratingKey']})")
        for name in todo:
            print(f"  plex: not seen after scan  {name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssh", help="user@host that holds the files; ffmpeg then runs there in a container")
    ap.add_argument("--image", default="ghcr.io/advplyr/audiobookshelf", help="container image with ffmpeg (ssh mode)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("source")
    p.add_argument("--plan", default="m4b_plan.json")
    p.add_argument("--suffix", default="(Dramatized Adaptation)", help="appended to title, folder and album tag")
    p.add_argument("--marker", default="Dramatized Adaptation", help="words an Audible listing must carry to count")
    p.add_argument("--title", help="treat every audio file under SOURCE as one book with this title")
    p.add_argument("--author", help="use this author instead of the files' tags")
    p.set_defaults(fn=cmd_plan)
    k = sub.add_parser("lookup")
    k.add_argument("plan")
    k.set_defaults(fn=cmd_lookup)
    b = sub.add_parser("build")
    b.add_argument("plan")
    b.add_argument("--out", required=True, help="staging folder, outside the library")
    b.set_defaults(fn=cmd_build)
    u = sub.add_parser("publish")
    u.add_argument("plan")
    u.add_argument("--staging", required=True, help="the folder build wrote to")
    u.add_argument("--library", required=True, help="the audiobook library folder")
    u.add_argument("--abs-library", help="Audiobookshelf library name (needs ABS_URL, ABS_TOKEN)")
    u.add_argument("--plex-library", help="Plex library name (needs PLEX_URL, PLEX_TOKEN)")
    u.set_defaults(fn=cmd_publish)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
