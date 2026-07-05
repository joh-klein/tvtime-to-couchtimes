# The `.couchtimes` backup format (reverse-engineered)

CouchTimes has no import feature and publishes no spec. This is what the `.couchtimes`
backup file actually contains, reverse-engineered from real backups. Everything here is
inferred from observed files — treat it as a field guide, not an official contract.

## Container

A `.couchtimes` file is **raw DEFLATE** (zlib with `wbits=-15` — no zlib/gzip header) of a
single UTF-8 JSON document. It is **not encrypted**; the high entropy is just compression.

```python
import zlib, json
data = json.loads(zlib.decompress(open("x.couchtimes", "rb").read(), -15))
# write it back:
co = zlib.compressobj(9, zlib.DEFLATED, -15)
open("out.couchtimes", "wb").write(co.compress(payload) + co.flush())
```

## Envelope

```jsonc
{
  "appVersion":     "2.1.0.366",       // observed value; probably cosmetic
  "exportDate":     "2026-07-05T…Z",   // ISO-8601 UTC
  "schemaVersion":   1,                 // the field most likely to be validated on restore
  "shows":  [ … ],                      // keyed conceptually by TMDB show id
  "movies": [ … ]                       // keyed conceptually by TMDB movie id
}
```

Whether the app strictly validates `appVersion`/`schemaVersion` is **unconfirmed** — reusing a
real backup's envelope is simply the safe path. The known-good values above (CouchTimes 2.1.0.366)
work as of writing; if a future version bumps `schemaVersion`, export a fresh backup and reuse its
envelope instead.

Restore is a **full replace** — it wipes existing app data, it does not merge.

## Dates — the one rule that breaks everything

Every date field decodes as a **non-optional** `Date`. A single empty string (`""`) anywhere
makes the whole file "not a valid couchtimes backup file". So **no date is ever `""`**: it
cascades episode → season → show air date, and finally to the sentinel `1970-01-01T00:00:00Z`.
Format is ISO-8601 with a `Z`: date-only fields become `YYYY-MM-DDT00:00:00Z`.

## Show object

Ids come from TMDB (`tv/{id}` + `tv/{id}/season/{n}`). Watch state is layered:
per-episode flags, **plus** a show-level `lastEpisodeWatchedDate` that actually drives the
"watched" badge.

| Field | Type | Source / meaning |
|---|---|---|
| `tmdbId` | int | TMDB show id (the key) |
| `title`, `originalName`, `sortableTitle` | str | `sortableTitle` drops a leading the/a/an |
| `overview`, `homepage`, `status`, `type` | str | TMDB; `type` defaults `"Scripted"` |
| `originalLanguage`, `originCountry`, `network` | str | first network / country |
| `rating` | float | TMDB `vote_average` |
| `userRating` | int | 0 — not imported |
| `firstAirDate`, `lastAirDate` | date | never `""` (see Dates) |
| `airedEpisodes`, `runtime` | int | 0 (unused at show level) |
| `episodeCount` | int | **remaining *unwatched* episodes**, not total → a completed show is `0` |
| `isActive` | bool | TV Time follow `active=1` (default true) |
| `isAbandoned` | bool | TV Time `archived=1` |
| `isFavorite`, `isPinned` | bool | false — no TV Time source |
| `assets` | array | poster (`isPoster:true`) + backdrop, `{assetPath, aspectRatio, isPoster}` |
| `seasons` | array | see below |
| `lastEpisodeWatchedDate` | datetime | **present only if watched.** This — not the episode flags — flips the show to "watched". Omitted for never-watched shows |
| `updatedAt` | datetime | = envelope `exportDate` |

### Season object
`{ number, title, overview, firstAired (date), tmdbId, episodes[] }`

### Episode object
| Field | Type | Meaning |
|---|---|---|
| `seasonNumber`, `episodeNumber` | int | |
| `title`, `overview` | str | TMDB |
| `airstamp`, `firstAired` | date | never `""` |
| `runtime` | int | |
| `isWatchable` | bool | true |
| `tmdbId` | int | TMDB episode id |
| `watchedStatus` | bool | `watchCount > 0` |
| `watchCount` | int | number of TV Time watch rows for this episode |
| `episodeTypeRaw` | int | 0 |
| `updatedAt` | datetime | envelope `exportDate` |

## Movie object

Ids come from TMDB (`movie/{id}`).

| Field | Type | Source / meaning |
|---|---|---|
| `tmdbId` | int | TMDB movie id (the key) |
| `title`, `originalTitle`, `sortableTitle` | str | |
| `overview`, `tagline`, `homepage` | str | TMDB |
| `releaseDate` | date | never `""` |
| `runtime`, `budget`, `revenue`, `voteCount` | int | TMDB |
| `rating` | float | TMDB `vote_average` |
| `userRating` | int | 0 — not imported |
| `isActive` | bool | true |
| `isFavorite`, `isPinned` | bool | false |
| `assets` | array | poster + backdrop, as for shows |
| `watchedStatus` | bool | true if TV Time has a `watch`/`rewatch` record |
| `watchedDate` | datetime | **present only if watched** — real TV Time watch timestamp |
| `addedDate` | datetime | watch timestamp, else `exportDate` |
| `updatedAt` | datetime | envelope `exportDate` |

## The 6 problems that caused failed imports

1. **Empty date string → whole file rejected.** Dates are non-optional; one `""` kills the parse.
   Every date cascades to a parent date and finally the `1970-01-01T00:00:00Z` sentinel.
2. **Empty `seasons`/`episodes` arrays are degenerate.** Unaired future seasons (TMDB lists the
   season with zero episodes) must be dropped.
3. **`lastEpisodeWatchedDate` drives "watched", not the episode flags.** A show with every
   episode `watchedStatus=true` still reads as unwatched unless this show-level timestamp is set.
4. **`episodeCount` = remaining unwatched**, not total. A fully-watched show must be `0`.
5. **Unwatched specials (season 0) block completion.** They aren't watched in TV Time, so an
   unwatched special leaves the show forever "in progress". Drop unwatched specials; keep watched ones.
6. **Same-titled movies must key by (title, year).** "Moana" 2016 (watched) vs 2026 (watchlist)
   are different films — title alone conflates them. Year also disambiguates the TMDB match.

## What TV Time has but the format cannot hold

- **Episode-level watch dates** — episodes have no date field, only the show-level `lastEpisodeWatchedDate`.
- **Custom lists** — no `lists` field in the backup.
- **Favorites** — TV Time's GDPR export ships `is_favorited=0` for every show, so there's no source.
- **Ratings** — no clear score column in the export; skipped.
