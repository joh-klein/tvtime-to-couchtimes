#!/usr/bin/env python3
"""Convert a TV Time GDPR export into a CouchTimes backup (.couchtimes) for restore.

CouchTimes has no import feature; its .couchtimes backup is raw-DEFLATE'd JSON keyed
by TMDB ids. TV Time exports TheTVDB ids. This bridges the two via the TMDB API,
merges watched history / follows / archived status into a COPY of your real backup,
and writes a new .couchtimes you restore in the app.

Usage:
  export TMDB_TOKEN=<v4 read-access-token OR v3 api key>
  python3 tvtime_to_couchtimes.py --test-tvdb 73255   # validate one show first
  python3 tvtime_to_couchtimes.py                      # full run

ponytail: stdlib only, disk-cached, resumable. No requests/pandas.
"""
import csv, io, json, os, re, sys, time, zipfile, zlib, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".tmdb-cache")

# The backup envelope. Reusing a real backup's values is the safest path, but these known-good
# values (from CouchTimes 2.1.0.366) let you skip that step. If a future app version bumps
# schemaVersion and rejects the result, pass --backup <your.couchtimes> to reuse its envelope.
DEFAULT_ENVELOPE = {"appVersion": "2.1.0.366", "schemaVersion": 1}


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
TOKEN = os.environ.get("TMDB_TOKEN", "").strip()
IS_V4 = TOKEN.startswith("eyJ")  # v4 bearer tokens are JWTs
csv.field_size_limit(10_000_000)


def die(msg):
    print("ERROR:", msg, file=sys.stderr); sys.exit(1)


def arg(flag, default=None):
    """--flag VALUE from argv, else default."""
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


class Export:
    """Read named CSVs from a TV Time GDPR export — a .zip or an already-unpacked dir, same API."""
    def __init__(self, path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if os.path.isfile(path) and zipfile.is_zipfile(path) else None
        self.names = self.zip.namelist() if self.zip else None

    def open_csv(self, basename):
        if self.zip is not None:
            member = next((n for n in self.names if n.rsplit("/", 1)[-1] == basename), None)
            if member is None:
                die(f"{basename} not found in zip {self.path}")
            return io.TextIOWrapper(self.zip.open(member), encoding="utf-8")
        fp = os.path.join(self.path, basename)
        if not os.path.exists(fp):
            die(f"{basename} not found in {self.path}")
        return open(fp, encoding="utf-8")


def find_export():
    """Locate the GDPR export: --export/env, else a *.zip or tvtime-gdpr-data/ dir in cwd."""
    p = arg("--export") or os.environ.get("TVTIME_EXPORT")
    if p:
        return p
    if os.path.isdir(os.path.join(HERE, "tvtime-gdpr-data")):
        return os.path.join(HERE, "tvtime-gdpr-data")
    zips = [f for f in os.listdir(HERE) if f.lower().endswith(".zip")]
    if len(zips) == 1:
        return os.path.join(HERE, zips[0])
    die("no export found: pass --export <tv-time-export.zip|dir> or set TVTIME_EXPORT "
        + (f"(found {len(zips)} zips, be specific)" if zips else ""))


# ---------- TMDB (cached) ----------
def tmdb(path, **params):
    """GET https://api.themoviedb.org/3/{path}; cache raw JSON on disk forever."""
    key = re.sub(r"[^A-Za-z0-9]+", "_", path + "?" + urllib.parse.urlencode(sorted(params.items())))
    fp = os.path.join(CACHE, key + ".json")
    if os.path.exists(fp):
        with open(fp) as f:
            return json.load(f)
    q = dict(params)
    headers = {"Accept": "application/json"}
    if IS_V4:
        headers["Authorization"] = "Bearer " + TOKEN
    else:
        q["api_key"] = TOKEN
    url = "https://api.themoviedb.org/3/" + path + "?" + urllib.parse.urlencode(q)
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                data = json.load(r)
            os.makedirs(CACHE, exist_ok=True)
            with open(fp, "w") as f:
                json.dump(data, f)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited
                time.sleep(2 ** attempt); continue
            if e.code == 404:
                return None
            die(f"TMDB {e.code} on {path}: {e.read()[:200]!r}")
        except urllib.error.URLError as e:
            time.sleep(1 + attempt)
    die(f"TMDB unreachable for {path}")


def find_tmdb_show(tvdb_id, name):
    """TVDB id -> TMDB show id. Fall back to name search."""
    r = tmdb(f"find/{tvdb_id}", external_source="tvdb_id")
    if r and r.get("tv_results"):
        return r["tv_results"][0]["id"]
    clean = re.sub(r"\s*\(\d{4}\)\s*$", "", name or "").strip()  # drop "(2011)" disambiguators
    if clean:
        r = tmdb("search/tv", query=clean)
        if r and r.get("results"):
            return r["results"][0]["id"]
    return None


def load_aliases():
    """name -> TMDB movie id overrides, for titles TMDB can't match by title+year (common for
    non-English films stored under a translated title). Reads --aliases PATH, else a
    movie_aliases.json next to the export or the script. Empty if none. See movie_aliases.example.json."""
    path = arg("--aliases")
    if not path:
        path = next((c for c in ("movie_aliases.json", os.path.join(HERE, "movie_aliases.json"))
                     if os.path.isfile(c)), None)
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return {k: int(v) for k, v in json.load(f).items() if not k.startswith("_")}


def imdb_lookup(name, year):
    """Title -> IMDB tt id via IMDB's public suggestion endpoint (no key). IMDB indexes the
    English/alternate titles TMDB search misses (e.g. 'Sun Alley' -> Sonnenallee). Prefer an
    exact-year, non-series hit. Unofficial endpoint — best-effort, returns None on any failure."""
    slug = urllib.parse.quote(name.strip().lower())
    fp = os.path.join(CACHE, "imdb_" + re.sub(r"[^a-z0-9]+", "_", name.lower()) + ".json")
    if os.path.exists(fp):
        data = json.load(open(fp))
    else:
        try:
            with urllib.request.urlopen(f"https://v3.sg.media-imdb.com/suggestion/x/{slug}.json"
                                        "?includeVideos=0", timeout=20) as r:
                data = json.load(r)
        except Exception:
            return None
        os.makedirs(CACHE, exist_ok=True)
        json.dump(data, open(fp, "w"))
    cands = [it for it in data.get("d", [])
             if str(it.get("id", "")).startswith("tt") and "series" not in (it.get("q") or "")]
    if year:
        cands = [it for it in cands if str(it.get("y")) == str(year)] or cands
    return cands[0]["id"] if cands else None


def find_tmdb_movie(name, year, aliases):
    """Movie title (+ release year) -> TMDB movie id. Movies have no id in the export, so match by
    title + release year, then fall back to IMDB (which indexes alternate titles TMDB search can't)."""
    if name in aliases:
        return aliases[name]
    if year:
        r = tmdb("search/movie", query=name, year=year)
        if r and r.get("results"):
            exact = [m for m in r["results"] if (m.get("release_date") or "")[:4] == year]
            return (exact or r["results"])[0]["id"]
    r = tmdb("search/movie", query=name)
    if r and r.get("results"):
        return r["results"][0]["id"]
    imdb_id = imdb_lookup(name, year)  # TMDB search blank -> resolve via IMDB, then map id->id
    if imdb_id:
        r = tmdb(f"find/{imdb_id}", external_source="imdb_id")
        if r and r.get("movie_results"):
            return r["movie_results"][0]["id"]
    return None


# ---------- read TV Time export ----------
def read_watched(export):
    """tvdb_id -> {'name', 'eps': {(season,ep): watch_count}}."""
    shows = {}
    with export.open_csv("tracking-prod-records-v2.csv") as f:
        for row in csv.DictReader(f):
            sid = row.get("s_id") or ""
            sn, en = row.get("season_number", ""), row.get("episode_number", "")
            if not (sid.isdigit() and sn.isdigit() and en.isdigit()):
                continue
            s = shows.setdefault(sid, {"name": row.get("series_name"), "eps": {}, "last": ""})
            key = (int(sn), int(en))
            s["eps"][key] = s["eps"].get(key, 0) + 1  # count rows -> watchCount
            ts = (row.get("created_at") or row.get("updated_at") or "").strip()
            if ts > s["last"]:
                s["last"] = ts                        # latest watch -> lastEpisodeWatchedDate
    return shows


WATCHED_MOVIE_TYPES = {"watch", "rewatch", "rewatch_count"}


def read_movies(export):
    """Distinct film -> {'name','year','watched','watched_at'}. Keyed by (name, release-year)
    so same-titled films (e.g. Moana 2016 vs 2026) stay separate. v1 records only."""
    out = {}
    with export.open_csv("tracking-prod-records.csv") as f:
        for row in csv.DictReader(f):
            name = (row.get("movie_name") or "").strip()
            if not name:
                continue
            year = (row.get("release_date") or "")[:4]
            year = year if year.isdigit() else None
            m = out.setdefault((name, year), {"name": name, "year": year,
                                              "watched": False, "watched_at": None})
            if row.get("type") in WATCHED_MOVIE_TYPES:
                m["watched"] = True
                ca = (row.get("created_at") or "").strip()  # actual watch timestamp
                if ca and (m["watched_at"] is None or ca < m["watched_at"]):
                    m["watched_at"] = ca                     # earliest = first watched
    return out


def read_follows(export):
    """tvdb_id -> {'active': bool, 'abandoned': bool}. archived=1 => abandoned."""
    out = {}
    with export.open_csv("followed_tv_show.csv") as f:
        for row in csv.DictReader(f):
            sid = row.get("tv_show_id", "")
            if sid.isdigit():
                out[sid] = {"active": row.get("active") == "1",
                            "abandoned": row.get("archived") == "1",
                            "name": row.get("tv_show_name")}
    return out


# ---------- build CouchTimes objects ----------
def sortable(title):
    return re.sub(r"^(the|a|an)\s+", "", title or "", flags=re.I)


EPOCH = "1970-01-01T00:00:00Z"  # sentinel: app decodes dates as non-optional, "" breaks parse


def iso(date_str):
    return (date_str or "") + "T00:00:00Z" if date_str else ""


def iso_or(*cands):
    """First non-empty ISO date among candidates, else epoch sentinel (never '')."""
    for c in cands:
        if c:
            return c
    return EPOCH


def iso_dt(ts):
    """'2025-03-13 12:13:20' -> '2025-03-13T12:13:20Z'."""
    ts = (ts or "").strip()
    return ts.replace(" ", "T") + "Z" if ts else ""


def build_show(tvdb_id, tmdb_id, watched_eps, follow, now, last_watch=""):
    d = tmdb(f"tv/{tmdb_id}")
    if not d:
        return None
    assets = []
    if d.get("poster_path"):
        assets.append({"aspectRatio": 0, "assetPath": d["poster_path"], "isPoster": True})
    if d.get("backdrop_path"):
        assets.append({"aspectRatio": 0, "assetPath": d["backdrop_path"], "isPoster": False})

    show_date = iso_or(iso(d.get("first_air_date")), iso(d.get("last_air_date")))
    seasons = []
    for smeta in d.get("seasons", []):
        n = smeta.get("season_number")
        if n is None:
            continue
        sd = tmdb(f"tv/{tmdb_id}/season/{n}")
        if not sd or not sd.get("episodes"):
            continue                     # skip unaired/empty seasons (no watch data, breaks nothing)
        season_date = iso_or(iso(smeta.get("air_date")), show_date)
        eps = []
        for e in sd.get("episodes", []):
            en = e.get("episode_number")
            wc = watched_eps.get((n, en), 0)
            if n == 0 and wc == 0:
                continue  # drop unwatched specials: they block the app's "watched" status, weren't watched in TV Time
            ep_date = iso_or(iso(e.get("air_date")), season_date)
            eps.append({
                "airstamp": ep_date,
                "episodeNumber": en,
                "episodeTypeRaw": 0,
                "firstAired": ep_date,
                "isWatchable": True,
                "overview": e.get("overview") or "",
                "runtime": e.get("runtime") or 0,
                "seasonNumber": n,
                "title": e.get("name") or "",
                "tmdbId": e.get("id"),
                "updatedAt": now,
                "watchCount": wc,
                "watchedStatus": wc > 0,
            })
        if not eps:
            continue  # season empty after dropping unwatched specials
        seasons.append({
            "episodes": eps,
            "firstAired": season_date,
            "number": n,
            "overview": smeta.get("overview") or "",
            "title": smeta.get("name") or f"Season {n}",
            "tmdbId": smeta.get("id"),
        })

    follow = follow or {}
    unwatched = sum(1 for se in seasons for e in se["episodes"] if not e["watchedStatus"])
    show = {
        "airedEpisodes": 0,
        "assets": assets,
        "episodeCount": unwatched,   # app treats this as remaining-unwatched count, not total
        "firstAirDate": show_date,
        "homepage": d.get("homepage") or "",
        "isAbandoned": bool(follow.get("abandoned")),
        "isActive": bool(follow.get("active", True)),
        "isFavorite": False,   # TV Time has no favorite; only for_later (watchlist), no CT equivalent
        "isPinned": False,
        "lastAirDate": iso_or(iso(d.get("last_air_date")), show_date),
        "network": (d.get("networks") or [{}])[0].get("name", ""),
        "originCountry": (d.get("origin_country") or [""])[0],
        "originalLanguage": d.get("original_language") or "",
        "originalName": d.get("original_name") or "",
        "overview": d.get("overview") or "",
        "rating": float(d.get("vote_average") or 0),
        "runtime": 0,
        "seasons": seasons,
        "sortableTitle": sortable(d.get("name")),
        "status": d.get("status") or "",
        "title": d.get("name") or "",
        "tmdbId": tmdb_id,
        "type": d.get("type") or "Scripted",
        "updatedAt": now,
        "userRating": 0,
    }
    if last_watch:  # marks the show as watched/in-progress; without it the app ignores watched flags
        show["lastEpisodeWatchedDate"] = iso_dt(last_watch)
    return show


def movie_sortable(title):
    return re.sub(r"\bthe\s+", "", (title or "").lower()).strip()


def build_movie(tmdb_id, watched, watched_at, now):
    d = tmdb(f"movie/{tmdb_id}")
    if not d:
        return None
    assets = []
    if d.get("poster_path"):
        assets.append({"aspectRatio": 0, "assetPath": d["poster_path"], "isPoster": True})
    if d.get("backdrop_path"):
        assets.append({"aspectRatio": 0, "assetPath": d["backdrop_path"], "isPoster": False})
    m = {
        "addedDate": iso_dt(watched_at) or now,
        "assets": assets,
        "budget": d.get("budget") or 0,
        "homepage": d.get("homepage") or "",
        "isActive": True,
        "isFavorite": False,
        "isPinned": False,
        "originalTitle": d.get("original_title") or "",
        "overview": d.get("overview") or "",
        "rating": float(d.get("vote_average") or 0),
        "releaseDate": iso_or(iso(d.get("release_date"))),
        "revenue": d.get("revenue") or 0,
        "runtime": d.get("runtime") or 0,
        "sortableTitle": movie_sortable(d.get("title")),
        "tagline": d.get("tagline") or "",
        "title": d.get("title") or "",
        "tmdbId": tmdb_id,
        "updatedAt": now,
        "userRating": 0,
        "voteCount": d.get("vote_count") or 0,
        "watchedStatus": bool(watched),
    }
    if watched:
        m["watchedDate"] = iso_dt(watched_at) or now  # real TV Time watch date
    return m


def merge_into_existing(existing_show, new_show):
    """OR the watched state of new_show onto an already-present show (keep existing metadata)."""
    idx = {(e["seasonNumber"], e["episodeNumber"]): e
           for se in existing_show["seasons"] for e in se["episodes"]}
    for se in new_show["seasons"]:
        for e in se["episodes"]:
            cur = idx.get((e["seasonNumber"], e["episodeNumber"]))
            if cur and e["watchedStatus"]:
                cur["watchedStatus"] = True
                cur["watchCount"] = max(cur.get("watchCount", 0), e["watchCount"])
    if new_show["isAbandoned"]:
        existing_show["isAbandoned"] = True


def main():
    if not TOKEN:
        die("set TMDB_TOKEN env var (v4 read-access-token or v3 api key)")

    export = Export(find_export())
    print(f"reading export: {export.path}" + (" (zip)" if export.zip is not None else " (dir)"))

    backup_in = arg("--backup") or os.environ.get("COUCHTIMES_BACKUP")
    if backup_in:  # override: reuse an exported backup's envelope (existing shows/movies discarded)
        if not os.path.isfile(backup_in):
            die(f"--backup not found: {backup_in}")
        backup = json.loads(zlib.decompress(open(backup_in, "rb").read(), -15))
    else:
        backup = dict(DEFAULT_ENVELOPE)
    now = backup["exportDate"] = utcnow_iso()
    backup["shows"] = []
    backup["movies"] = []
    by_tmdb = {}                  # merge_into_existing still dedups TVDB ids sharing one TMDB show

    watched = read_watched(export)
    follows = read_follows(export)
    tvdb_ids = set(watched) | set(follows)

    only = None
    if "--test-tvdb" in sys.argv:
        only = sys.argv[sys.argv.index("--test-tvdb") + 1]
        tvdb_ids = {only}
        print(f"TEST MODE: single show tvdb={only}")

    print(f"resolving {len(tvdb_ids)} shows via TMDB (cached in {os.path.relpath(CACHE, HERE)}) ...")
    added = merged = unresolved = 0
    misses = []
    for i, tvdb_id in enumerate(sorted(tvdb_ids, key=int), 1):
        name = (watched.get(tvdb_id) or {}).get("name") \
            or (follows.get(tvdb_id) or {}).get("name") or ""
        tmdb_id = find_tmdb_show(tvdb_id, name)
        if not tmdb_id:
            unresolved += 1; misses.append((tvdb_id, name)); continue
        show = build_show(tvdb_id, tmdb_id,
                          (watched.get(tvdb_id) or {}).get("eps", {}),
                          follows.get(tvdb_id), now,
                          (watched.get(tvdb_id) or {}).get("last", ""))
        if not show:
            unresolved += 1; misses.append((tvdb_id, name)); continue
        if tmdb_id in by_tmdb:
            merge_into_existing(by_tmdb[tmdb_id], show); merged += 1
        else:
            backup["shows"].append(show); by_tmdb[tmdb_id] = show; added += 1
        if i % 25 == 0 or only:
            print(f"  [{i}/{len(tvdb_ids)}] {name[:40]} -> tmdb {tmdb_id}  (added {added}, merged {merged})")

    if not only and "--no-movies" not in sys.argv:
        aliases = load_aliases()
        movies = read_movies(export)
        print(f"\nresolving {len(movies)} movies via TMDB ...", f"({len(aliases)} aliases)" if aliases else "")
        m_added = m_miss = 0; m_misses = []; seen = set()
        for i, m in enumerate(movies.values(), 1):
            mid = find_tmdb_movie(m["name"], m["year"], aliases)
            if not mid or mid in seen:
                if not mid:
                    m_miss += 1; m_misses.append(m["name"])
                continue
            mo = build_movie(mid, m["watched"], m["watched_at"], now)
            if not mo:
                m_miss += 1; m_misses.append(m["name"]); continue
            seen.add(mid); backup["movies"].append(mo); m_added += 1
            if i % 50 == 0:
                print(f"  [{i}/{len(movies)}] movies added {m_added}")
        print(f"movies: {m_added} added, {m_miss} unresolved.")
        if m_misses:
            print("  unresolved movies: " + ", ".join(m_misses[:20]) +
                  (" ..." if len(m_misses) > 20 else ""))
            print("  -> to fix, add \"Title\": <tmdb-id> to movie_aliases.json "
                  "(look ids up at themoviedb.org). See movie_aliases.example.json.")

    out_name = ("couchtimes-TEST.couchtimes" if only
                else "couchtimes-import-tvtime.couchtimes")
    out_path = os.path.join(HERE, out_name)
    raw = json.dumps(backup, ensure_ascii=False).encode("utf-8")
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    open(out_path, "wb").write(co.compress(raw) + co.flush())

    print(f"\nDONE: {added} shows added, {merged} merged, {unresolved} unresolved.")
    print(f"  total shows in backup: {len(backup['shows'])}")
    print(f"  wrote: {out_name}  ({os.path.getsize(out_path)} bytes)")
    if misses:
        print(f"  unresolved ({len(misses)}): " +
              ", ".join(f"{n or t}({t})" for t, n in misses[:20]) +
              (" ..." if len(misses) > 20 else ""))


if __name__ == "__main__":
    main()
