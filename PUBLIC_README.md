# Clix Anime Mappings

A comprehensive anime ID mapping dataset for translating identifiers between multiple anime and media databases.

## Downloads

| File | Description |
| --- | --- |
| [anime-list-full.json](./anime-list-full.json?raw=1) | Complete mapping dataset |
| [anime-list-mini.json](./anime-list-mini.json?raw=1) | Compact/minified mapping dataset |
| [build-meta.json](./build-meta.json?raw=1) | Build metadata and dataset statistics |

## Supported Sources

Mappings may include identifiers from AniDB, AniList, MyAnimeList, Kitsu, Anime News Network, Anime-Planet, AniSearch, LiveChart, SIMKL, AnimeCountdown, TheTVDB, TheMovieDB, and IMDb.

Not every record contains an identifier for every service.

## Structure

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

Some records may also contain `episode_offset`. TVDB and TMDB season or episode-offset values may intentionally differ.

## Updates

The published database files are maintained automatically and updated as upstream mapping information changes.

## Credits

This dataset builds upon data and mappings from multiple projects and services, including Fribb/anime-lists, anime-offline-database, Anime-Lists/anime-lists, anime-and-manga/lists, AniDB, AniList, MyAnimeList, and TheMovieDB.

All source projects and services remain subject to their respective licenses and terms.

## Disclaimer

Mappings are compiled from independent databases. Identifiers and metadata can change independently between services, so some mappings may occasionally be incomplete or require upstream corrections.
