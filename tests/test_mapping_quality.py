import json
import uuid
from contextlib import contextmanager

@contextmanager
def test_directory():
    path = Path.cwd() / ("test-cache-" + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        for f in path.iterdir():
            f.unlink()
        path.rmdir()

import unittest
from pathlib import Path
from unittest.mock import Mock
import mapping_quality as q
import build_fribb_dropin as b


class MappingTests(unittest.TestCase):
    def override(self, patch):
        return [{'anilist_id': 195604, 'set': patch, 'sources': ['https://example.org/verified']}]

    def test_black_clover_override_preserves_distinct_identity(self):
        rows = [{'anilist_id': 97940, 'mal_id': 34572, 'season': {'tvdb': 1}},
                {'anilist_id': 195604, 'mal_id': 61967}]
        corrected, events = q.apply_overrides(rows, self.override({'tvdb_id': 331753, 'season': {'tvdb': 2}}), b.ALLOWED_KEYS)
        self.assertEqual(corrected[0], rows[0])
        self.assertEqual(corrected[1]['mal_id'], 61967)
        self.assertNotIn('tmdb', corrected[1]['season'])
        self.assertNotIn('season', rows[1])
        self.assertEqual(len(events), 2)

    def test_conflicts_are_preserved_and_idempotent(self):
        rows = [{'anilist_id': 195604, 'season': {'tvdb': 3, 'tmdb': 1}}]
        corrected, events = q.apply_overrides(rows, self.override({'season': {'tvdb': 2}}), b.ALLOWED_KEYS)
        self.assertEqual(rows, corrected)
        self.assertEqual(events[0]['status'], 'conflict_preserved')
        once, _ = q.apply_overrides([{'anilist_id': 195604}], self.override({'season': {'tvdb': 2}}), b.ALLOWED_KEYS)
        twice, events = q.apply_overrides(once, self.override({'season': {'tvdb': 2}}), b.ALLOWED_KEYS)
        self.assertEqual(once, twice)
        self.assertEqual(events[0]['status'], 'already_present')

    def test_no_cross_provider_coordinate_copy(self):
        row = {'themoviedb_id': {'tv': 73223}, 'season': {'tvdb': 2}, 'episode_offset': {'tvdb': 170}}
        b.enrich_tmdb(row, [])
        self.assertNotIn('tmdb', row['season'])
        self.assertNotIn('tmdb', row['episode_offset'])
        b.selftest_preservation_logic()

    def test_invalid_and_identity_overrides_rejected(self):
        for patch in ({'mal_id': 34572}, {'season': {'tmdb': '2'}}, {'tvdb_id': -1}, {'themoviedb_id': {'tv': True}}):
            with self.assertRaises(ValueError):
                q.apply_overrides([], self.override(patch), b.ALLOWED_KEYS)

    def test_ambiguous_target_not_modified(self):
        rows = [{'anilist_id': 195604}, {'anilist_id': 195604}]
        result, events = q.apply_overrides(rows, self.override({'tvdb_id': 331753}), b.ALLOWED_KEYS)
        self.assertEqual(result, rows)
        self.assertEqual(events[0]['status'], 'missing_or_ambiguous_target')

    def test_relations_are_cached_without_inheriting_ids(self):
        response = Mock(status_code=200)
        response.json.return_value = {'data': {'Page': {'media': [{'id': 195604, 'relations': {'edges': [
            {'relationType': 'PREQUEL', 'node': {'id': 97940, 'idMal': 34572, 'type': 'ANIME'}}]}}]}}}
        request = Mock(return_value=response)
        rows = [{'type': 'TV', 'anilist_id': 195604}]
        with test_directory() as tmp:
            path = Path(tmp) / 'cache.json'
            cache, failures = q.refresh_relations(rows, request, path, now=1_800_000_000, pause=lambda _: None)
            self.assertFalse(failures)
            self.assertEqual(cache['195604']['relations'][0]['anilist_id'], 97940)
            self.assertNotIn('tvdb_id', rows[0])
            q.refresh_relations(rows, request, path, now=1_800_000_001, pause=lambda _: None)
            self.assertEqual(request.call_count, 1)

    def test_outage_keeps_relations_and_retries_failed_records(self):
        with test_directory() as tmp:
            path = Path(tmp) / 'cache.json'
            path.write_text(json.dumps({'195604': {'checked_at': 1, 'relations': [{'anilist_id': 97940}]}}))
            request = Mock(side_effect=RuntimeError('outage'))
            rows = [{'type': 'TV', 'anilist_id': 195604}]
            cache, failures = q.refresh_relations(rows, request, path, now=1_800_000_000, pause=lambda _: None)
            self.assertEqual(cache['195604']['relations'][0]['anilist_id'], 97940)
            self.assertTrue(failures)
            q.refresh_relations(rows, request, path, now=1_800_000_001, pause=lambda _: None)
            self.assertEqual(request.call_count, 2)

    def test_report_identifies_missing_provider_season(self):
        report = q.quality_report([{'type': 'TV', 'anilist_id': 195604, 'themoviedb_id': {'tv': 73223}}], {}, [], [])
        self.assertIn('season.tmdb', report['incomplete_tv_mappings'][0]['missing'])

    def test_tmdb_verifies_its_own_season(self):
        get = Mock(return_value={'season_number': 3, 'episodes': []})
        rows = [{'anilist_id': 195604, 'themoviedb_id': {'tv': 73223}, 'season': {'tvdb': 2}}]
        results = q.verify_tmdb_overrides(rows, self.override({'season': {'tmdb': 3}}), get)
        get.assert_called_once_with('/tv/73223/season/3')
        self.assertEqual(results[0]['status'], 'verified')

    def test_tmdb_outage_and_wrong_season(self):
        rows = [{'anilist_id': 195604, 'themoviedb_id': {'tv': 73223}}]
        patch = self.override({'season': {'tmdb': 2}})
        self.assertEqual(q.verify_tmdb_overrides(rows, patch, lambda _: None)[0]['status'], 'unavailable')
        with self.assertRaises(ValueError):
            q.verify_tmdb_overrides(rows, patch, lambda _: {'season_number': 1, 'episodes': []})



    def test_prune_orphan_rows_keeps_core_and_fribb_only(self):
        rows = [
            {"type": "TV", "mal_id": 21, "kitsu_id": 12},
            {"type": "SPECIAL", "anisearch_id": 18676},
            {"type": "ONA", "kitsu_id": 50383, "anime-planet_id": "test"},
            {"type": "MOVIE", "themoviedb_id": {"movie": [999]}},
        ]
        kept, removed = q.prune_orphan_rows(rows)
        self.assertEqual(removed, 2)
        self.assertEqual(len(kept), 2)

    def test_apply_wikidata_mappings_additive_no_collisions(self):
        rows = [
            {"type": "TV", "mal_id": 100, "anilist_id": 50},
            {"type": "MOVIE", "mal_id": 200},
        ]
        wd = {
            "100": {"anilist_id": 999, "tvdb_id": 1234, "imdb_id": ["tt1234567"]},
            "200": {"anilist_id": 50, "tmdb_movie": 777},
        }
        filled = q.apply_wikidata_mappings(rows, wd)
        self.assertEqual(filled, 2)
        self.assertEqual(rows[0]["anilist_id"], 50)
        self.assertEqual(rows[0]["tvdb_id"], 1234)
        self.assertNotIn("anilist_id", rows[1])
        self.assertEqual(rows[1]["themoviedb_id"], {"movie": [777]})

    def test_enrich_missing_tmdb_mappings_strict_match(self):
        rows = [{"type": "TV", "mal_id": 300, "anilist_id": 301}]
        def tmdb_get(path, **params):
            if path == "/search/tv":
                return {"results": [{"id": 555, "name": "Test Anime", "first_air_date": "2024-04-01"}]}
            if path == "/tv/555/external_ids":
                return {"tvdb_id": 888, "imdb_id": "tt7654321"}
            return {}
        with test_directory() as tmp:
            filled = q.enrich_missing_tmdb_mappings(
                rows,
                lambda r: {"titles": ["Test Anime"], "year": 2024},
                tmdb_get,
                Path(tmp) / "tmdb.json",
                limit=10,
            )
            self.assertEqual(filled, 1)
            self.assertEqual(rows[0]["themoviedb_id"], {"tv": 555})
            self.assertEqual(rows[0]["tvdb_id"], 888)
            self.assertEqual(rows[0]["imdb_id"], ["tt7654321"])

    def test_build_watch_orders_clusters_and_overrides(self):
        rows = [
            {"type": "TV", "mal_id": 1, "anilist_id": 10},
            {"type": "TV", "mal_id": 2, "anilist_id": 20},
        ]
        rel = {
            "10": {"mal_id": 1, "format": "TV", "startDate": {"year": 2020, "month": 1, "day": 1}, "title": {"english": "Show S1"}, "relations": [{"relation": "SEQUEL", "anilist_id": 20, "mal_id": 2}]},
            "20": {"mal_id": 2, "format": "TV", "startDate": {"year": 2021, "month": 1, "day": 1}, "title": {"english": "Show S2"}, "relations": [{"relation": "PREQUEL", "anilist_id": 10, "mal_id": 1}]},
        }
        wo = q.build_watch_orders(rows, rel, [{"franchise_id": "custom", "franchise_name": "Custom", "release_order": [{"mal_id": 99}], "chronological_order": []}])
        self.assertEqual(wo["franchise_count"], 2)
        self.assertEqual(wo["by_mal_id"]["1"], "franchise-1")
        self.assertEqual(wo["by_mal_id"]["99"], "custom")


if __name__ == '__main__':
    unittest.main()
