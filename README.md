# TV Time → CouchTimes importer

CouchTimes has **no import feature**. This converts a [TV Time](https://www.tvtime.com/)
GDPR data export into a CouchTimes **backup file** (`.couchtimes`) that you restore in the app.

Validated against a real library of ~400 shows + ~700 movies: watched episodes/counts, movie
watch dates, follows→active, archived→abandoned, and completed shows correctly reading as
"watched" (via `lastEpisodeWatchedDate`) all restore cleanly. A handful of items may not resolve
— titles with no TMDB entry (upcoming/unreleased), or shows TMDB reclassified as movies — these
are reported at the end of the run; watched items are essentially always resolvable.

- **Input:** a TV Time GDPR export — request it at
  [gdpr.tvtime.com/gdpr/self-service](https://gdpr.tvtime.com/gdpr/self-service). It's generated
  in the portal (a few minutes, depending on how much you've tracked) and downloaded there as a
  `.zip` of ~50 CSVs. Reset your password first if you've forgotten it. **All account data is
  deleted after 15 July 2026 — export before then.**
- **Output:** `couchtimes-import-tvtime.couchtimes`
- **Script:** `tvtime_to_couchtimes.py` — stdlib only, no dependencies

## How to run

```bash
export TMDB_TOKEN="<TMDB v4 Read Access Token>"   # free: themoviedb.org/settings/api

python3 tvtime_to_couchtimes.py --export tv-time-export.zip --test-tvdb 71814  # one show first
python3 tvtime_to_couchtimes.py --export tv-time-export.zip                     # full run
```

`--export` accepts the raw GDPR **zip** (CSVs are read straight out of it, nested folders and all)
or an unpacked directory. It defaults to a `tvtime-gdpr-data/` dir or a lone `*.zip` in the cwd.

The output uses a built-in backup envelope (CouchTimes 2.1.0.366) so **you don't need to export a
backup first**. If a future app version rejects it, export any backup from CouchTimes and pass
`--backup <your.couchtimes>` (or set `$COUCHTIMES_BACKUP`) to reuse its envelope instead.

Then in CouchTimes: **restore** `couchtimes-import-tvtime.couchtimes`.
⚠️ Restore is a **full replace** — it wipes existing app data.

TMDB responses are cached in `.tmdb-cache/` forever, so re-runs are seconds. Delete the
folder to force a refetch.

## The `.couchtimes` format

Reverse-engineered — the full field-by-field schema (container, envelope, show/season/episode,
movie) and the date rule that makes it work live in **[FORMAT.md](FORMAT.md)**. In short:
raw-DEFLATE'd (`wbits=-15`) UTF-8 JSON, `{ appVersion, exportDate, schemaVersion, shows[], movies[] }`,
shows keyed by TMDB show id, movies by TMDB movie id.

## What gets imported

| TV Time source | → CouchTimes |
|---|---|
| watched episodes (`tracking-prod-records-v2.csv`) | episode `watchedStatus` + `watchCount` |
| movie watches (`tracking-prod-records.csv`, type `watch`/`rewatch`) | movie `watchedStatus` + `watchedDate` (from `created_at`) |
| follows (`followed_tv_show.csv`) | `isActive` |
| archived shows | `isAbandoned` |
| latest watch timestamp per show | `lastEpisodeWatchedDate` (**required** — see gotchas) |

**ID mapping:** TV Time stores **TheTVDB** ids; CouchTimes needs **TMDB**. Shows resolve
exactly via `/find?external_source=tvdb_id`. Movies have **no id** in the export, so they
resolve by **title + release year** search.

### Movies TMDB can't match

A few movies won't resolve by title+year — usually non-English films TV Time stored under a
translated title (e.g. *Sonnenallee* exported as "Sun Alley"), or unreleased titles with no TMDB
entry yet. The run prints any unresolved movies at the end. To fix them, copy
`movie_aliases.example.json` to `movie_aliases.json` and map each title (**exactly as TV Time
exported it**) to its TMDB movie id — the number in `themoviedb.org/movie/<id>`:

```json
{ "Sun Alley": 2241, "Shark Alarm at Müggel Lake": 185562 }
```

Re-run; the cache makes it instant. The script auto-loads `movie_aliases.json` from the current
directory (or pass `--aliases <path>`). Watched movies almost always resolve; misses are typically
watchlist/upcoming titles, so nothing you've actually seen is lost.

**Not imported** (the format can't hold it): episode-level watch dates, custom lists, favorites
(TV Time exports `is_favorited=0` for everything), ratings. See [FORMAT.md](FORMAT.md).

## Gotchas

Six problems each caused a failed/incorrect import before being fixed — empty dates rejecting the
whole file, `lastEpisodeWatchedDate` (not episode flags) driving "watched" status, unwatched
specials blocking completion, and more. All are documented with the schema in **[FORMAT.md](FORMAT.md)**.

## Validate before a full import

Restore is a **full replace**. To sanity-check quickly, run `--test-tvdb <id>` to carve a
single-show `couchtimes-TEST.couchtimes`, restore that first and confirm it's accepted with the
right watched status, then restore the full file.
