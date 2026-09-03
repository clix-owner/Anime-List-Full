# Clix Anime Mappings

A comprehensive anime ID mapping dataset providing mappings between multiple anime and media databases.

The dataset is intended for applications that need to translate an anime identifier from one supported database to another.

## Downloads

| File | Description | Repository | Raw |
| --- | --- | --- | --- |
| `anime-list-full.json` | Complete mapping dataset | [View](./anime-list-full.json) | [Download / Raw](./anime-list-full.json?raw=1) |
| `anime-list-mini.json` | Minified mapping dataset | [View](./anime-list-mini.json) | [Download / Raw](./anime-list-mini.json?raw=1) |
| `build-meta.json` | Dataset metadata and statistics | [View](./build-meta.json) | [Download / Raw](./build-meta.json?raw=1) |

The relative links above work from the repository README without hard-coding the repository owner or repository name.

## Supported Sources

Mappings may include identifiers from:

- AniDB
- AniList
- MyAnimeList
- Kitsu
- Anime News Network
- Anime-Planet
- AniSearch
- LiveChart
- SIMKL
- AnimeCountdown
- TheTVDB
- TheMovieDB
- IMDb

Not every anime has an identifier for every supported source.

## anime-list-full

`anime-list-full.json` is the complete mapping dataset.

Example:

```json
[
  {
    "type": "TV",
    "anidb_id": 1,
    "anilist_id": 290,
    "mal_id": 290,
    "kitsu_id": 265,
    "tvdb_id": 72025,
    "themoviedb_id": {
      "tv": 26209
    },
    "season": {
      "tvdb": 1,
      "tmdb": 1
    }
  }
]
```

Fields are included only when a mapping is available.

## anime-list-mini

`anime-list-mini.json` contains the same mapping dataset in a compact/minified representation intended for applications that prefer a smaller transfer size.

## TheMovieDB IDs

TheMovieDB TV mappings use:

```json
{
  "themoviedb_id": {
    "tv": 37854
  }
}
```

Movie mappings may contain multiple IDs:

```json
{
  "themoviedb_id": {
    "movie": [128]
  }
}
```

TheMovieDB uses separate ID namespaces for TV shows and movies, so consumers should use the corresponding media type.

## Season Mapping

Some entries include database-specific season mappings:

```json
{
  "season": {
    "tvdb": 1,
    "tmdb": 1
  }
}
```

The TVDB and TMDB season values are independent and are not required to be identical.

## Episode Offsets

Some entries require episode-number offsets between databases:

```json
{
  "episode_offset": {
    "tvdb": 7,
    "tmdb": 2
  }
}
```

TVDB and TMDB offsets may intentionally differ.

## Using IDs

Identifiers can be used with the corresponding services:

- AniDB: `https://anidb.net/anime/{id}`
- AniList: `https://anilist.co/anime/{id}`
- MyAnimeList: `https://myanimelist.net/anime/{id}`
- Kitsu: `https://kitsu.io/anime/{id}`
- Anime-Planet: `https://www.anime-planet.com/anime/{id}`
- AniSearch: `https://www.anisearch.com/anime/{id}`
- TheMovieDB Movie: `https://www.themoviedb.org/movie/{id}`
- TheMovieDB TV: `https://www.themoviedb.org/tv/{id}`

Replace `{id}` with the corresponding identifier from the mapping record.

## Generation

The dataset combines mappings from established anime mapping datasets and supported database sources.

Existing baseline mappings are preserved while additional reliable mappings and newly available records may be incorporated. Generated data is validated for mapping consistency before publication.

Implementation details, build configuration, credentials, automation configuration, and internal maintenance procedures are intentionally not part of the public dataset documentation.

## Updates

The generated mapping files are maintained automatically and updated periodically as upstream mapping information changes.

Applications consuming this repository should use the latest published JSON files.

## Credits

This project builds upon data and mappings from multiple projects and services, including:

- Fribb/anime-lists
- anime-offline-database
- Anime-Lists/anime-lists
- anime-and-manga/lists
- AniDB
- AniList
- MyAnimeList
- TheMovieDB

All source projects and services remain subject to their respective licenses and terms.

## Disclaimer

Mappings are compiled from independent databases. Identifiers and metadata can change independently between services, so some mappings may occasionally be incomplete or require upstream corrections.


## Private → Public publishing

This private builder repository keeps its own generated database files updated and can also publish the distributable files to a separate public repository.

Create these **private repository Actions secrets**:

- `PUBLIC_DATA_REPO` — public repository in `OWNER/REPO` format, for example `myname/clix-anime-database`
- `PUBLIC_DATA_TOKEN` — a fine-grained GitHub personal access token with **Contents: Read and write** access only to that public data repository

The private repository continues to commit/update its own generated files first. After a successful build, the workflow copies only:

- `anime-list-full.json`
- `anime-list-mini.json`
- `build-meta.json`
- `PUBLIC_README.md` → public repo `README.md`

to the public repository.

Builder scripts, workflow files, cache/state files, API credentials, and internal documentation are not published to the public repository.


## v11.5 public publishing fix

The public-repository publish step no longer checks step-local environment
variables in its GitHub Actions `if:` expression. That condition could be
evaluated before the secret-backed step environment was available, causing the
publish step to be silently skipped.

The publish step now:
- runs after every successful build,
- validates `PUBLIC_DATA_REPO` and `PUBLIC_DATA_TOKEN` inside the shell,
- fails with a clear GitHub Actions error if either secret is missing,
- prints the target repository name (never the token),
- publishes only the generated public database files and public README.


## v11.6 surgical reconciliation validator fix

This release fixes only the incremental latest-Fribb representation check.

The old check could report thousands of newly reconciled Fribb rows as missing
after deduplication removed a conflicting secondary AniList/AniDB identity.
The new check accepts a Fribb row as represented when any stable source identity
for that row is still present in the final dataset.

No builder stages were removed. The patch is intentionally surgical and keeps
the v11.5 public publishing logic, dedup rules, caches, previous-OUR incremental
architecture, and 10-hour workflow timeout.

## GitHub Actions source downloads

The workflow uses only the configured primary/original sources. AniDB `anime-titles.xml.gz` is downloaded with Python `urllib` using browser-like request headers instead of `curl`. There are no CDN, mirror, or third-party fallback sources; if an official source fails, the workflow fails.

## Automatic merge trigger

When this source JSON changes, this workflow sends a GitHub
`repository_dispatch` event named `anime-sources-updated` to the merge repo.

Configure in this source repository:

- `MERGE_REPO` — repository Variable (or Secret), e.g. `owner/anime-merge-repo`
- `MERGE_DISPATCH_TOKEN` — repository Secret. Use a fine-grained PAT that can
  access the merge repository and has **Contents: Read and write** permission.

The dispatch is sent only when this workflow detects that its generated source
JSON differs from the current copy on `origin/main`.
