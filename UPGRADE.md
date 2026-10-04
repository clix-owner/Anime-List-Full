# Mapping builder v12

Replace the private builder repository files with this project, then run its existing GitHub Actions workflow in incremental mode. Existing Actions secrets and upstream downloads are retained. No credentials are included in this archive.

Changes:
- Source-backed additive `mapping-overrides.json`, reapplied on every run. Conflicting existing values are preserved and reported. Identity IDs cannot be overwritten.
- Black Clover sequel 195604 / MAL 61967 receives the verified TVDB series 331753, season 2 mapping. No TMDB/IMDb coordinate was guessed.
- No automatic copying of TVDB seasons or offsets into TMDB. Old copied values already in the dataset are not deleted; review them separately.
- A bounded official AniList relation lookup (100 TV entries per run by default), with persistent cache, one-second pacing and bounded retries. It records PREQUEL/SEQUEL hints in `anime-relations.json`. New/stale/failed entries are revisited in oldest-attempt order. Relations never cause automatic provider ID inheritance.
- `mapping-quality-report.json` lists missing provider IDs/season numbers, override conflicts, relation failures and independent TMDB override verification. TVDB corrections require human verification against the linked provider season page. TMDB override verification reports unavailable when the API is unavailable; source-backed overrides are retained.
- The workflow runs regression tests and commits the two new reports privately. Its existing public publish step still publishes only `anime-list-full.json`. Applications must explicitly support the relation sidecar to use those hints.

The original generated JSON snapshots are included unchanged. The next workflow run applies the new override and generates reports. This is a builder upgrade, not a completed live catalog refresh. A full API-backed build was not run here because the required upstream inputs and API credentials are not supplied in the archive.

Run tests locally:

```
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

To add a correction, append an object with `anilist_id`, a `set` patch and `sources` URLs to `mapping-overrides.json`. Supported patch fields: `tvdb_id`, `imdb_id`, `themoviedb_id` (TV), `season`, `episode_offset`. Each provider's coordinates must be verified independently. Missing target records are reported rather than created with an invented identity.

The database stores mappings, not playable streams or an episode catalog. Cinemeta's absent episodes still require a separate episode metadata source in the consuming website.
