"""Conservative mapping maintenance. Relations are hints, never season proofs."""
import copy
import json
import time
from pathlib import Path

RELATION_QUERY = '''query($ids:[Int]){Page(perPage:50){media(id_in:$ids,type:ANIME){id relations{edges{relationType node{id idMal type}}}}}}'''


def refresh_relations(rows, request, cache_path, limit=100, now=None, pause=time.sleep):
    now = time.time() if now is None else now
    path = Path(cache_path)
    try:
        cache = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    ids = sorted({r['anilist_id'] for r in rows if isinstance(r.get('anilist_id'), int) and r.get('type') == 'TV'})
    # Failed/incomplete/new entries remain eligible, with oldest attempts first.
    pending = [i for i in ids if now - cache.get(str(i), {}).get('checked_at', 0) >= 7 * 86400]
    pending.sort(key=lambda i: (cache.get(str(i), {}).get('attempted_at', 0), i))
    failures = []
    for start in range(0, min(max(0, limit), len(pending)), 50):
        batch = pending[start:min(start + 50, limit)]
        for i in batch:
            cache.setdefault(str(i), {})['attempted_at'] = now
        try:
            for attempt in range(3):
                response = request(json={'query': RELATION_QUERY, 'variables': {'ids': batch}})
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 2:
                        response.raise_for_status()
                    pause(min(60, max(1, int(response.headers.get('Retry-After', '5')))))
                    continue
                response.raise_for_status()
                payload = response.json()
                if payload.get('errors'):
                    raise ValueError('AniList returned GraphQL errors')
                media = payload.get('data', {}).get('Page', {}).get('media')
                if not isinstance(media, list):
                    raise ValueError('Missing AniList media response')
                for node in media:
                    if node.get('id') not in batch:
                        continue
                    edges = []
                    for edge in node.get('relations', {}).get('edges', []):
                        target = edge.get('node') or {}
                        if edge.get('relationType') in ('PREQUEL', 'SEQUEL') and target.get('type') == 'ANIME':
                            edges.append({'relation': edge['relationType'], 'anilist_id': target['id'], 'mal_id': target.get('idMal')})
                    cache[str(node['id'])] = {'checked_at': now, 'attempted_at': now, 'relations': edges}
                break
        except Exception as exc:
            failures.append({'anilist_ids': batch, 'reason': type(exc).__name__})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2), encoding='utf-8')
        pause(1)
    return cache, failures


def apply_overrides(rows, overrides, allowed_keys):
    """Fill only absent values; reject malformed patches and report conflicts."""
    result = copy.deepcopy(rows)
    index = {}
    for row in result:
        if row.get('anilist_id') is not None:
            index.setdefault(row['anilist_id'], []).append(row)
    events = []
    for entry in overrides:
        aid = entry.get('anilist_id')
        patch = entry.get('set')
        if not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0 or not isinstance(patch, dict):
            raise ValueError('Override requires positive anilist_id and set object')
        if not entry.get('sources') or not all(isinstance(s, str) and s.startswith('https://') for s in entry['sources']):
            raise ValueError('Override requires source URLs')
        if set(patch) - (allowed_keys - {'anilist_id', 'mal_id', 'anidb_id', 'type'}):
            raise ValueError('Override may not change identity or unknown fields')
        for field, value in patch.items():
            if field in ('season', 'episode_offset'):
                if not isinstance(value, dict) or not value or set(value) - {'tvdb', 'tmdb'}:
                    raise ValueError('Invalid season/offset shape')
                if any(type(v) is not int or (field == 'season' and v < 0) for v in value.values()):
                    raise ValueError('Invalid season/offset number')
            elif field == 'themoviedb_id':
                if not isinstance(value, dict) or set(value) != {'tv'} or type(value['tv']) is not int or value['tv'] <= 0:
                    raise ValueError('TV overrides require a positive TMDB TV ID')
            elif field == 'tvdb_id':
                if type(value) is not int or value <= 0:
                    raise ValueError('Invalid TVDB series ID')
            elif field == 'imdb_id':
                import re
                if not isinstance(value, list) or not value or any(not isinstance(v, str) or not re.fullmatch(r'tt\d+', v) for v in value):
                    raise ValueError('Invalid IMDb ID list')
            else:
                raise ValueError('Unsupported override field')
        matches = index.get(aid, [])
        if len(matches) != 1:
            events.append({'anilist_id': aid, 'status': 'missing_or_ambiguous_target'})
            continue
        row = matches[0]
        for field, value in patch.items():
            additions = value.items() if isinstance(value, dict) else [(None, value)]
            for key, item in additions:
                dest = row.setdefault(field, {}) if key is not None else row
                dest_key = key if key is not None else field
                existing = dest.get(dest_key)
                if existing is None:
                    dest[dest_key] = copy.deepcopy(item)
                    status = 'applied'
                else:
                    status = 'already_present' if existing == item else 'conflict_preserved'
                events.append({'anilist_id': aid, 'field': field, 'provider': key, 'status': status, 'existing': existing, 'proposed': item})
    return result, events


def quality_report(rows, relations, events, failures):
    issues = []
    for row in rows:
        if row.get('type') != 'TV' or not row.get('anilist_id'):
            continue
        missing = [k for k in ('tvdb_id', 'imdb_id', 'themoviedb_id') if not row.get(k)]
        seasons = row.get('season') or {}
        if row.get('tvdb_id') and 'tvdb' not in seasons:
            missing.append('season.tvdb')
        if (row.get('themoviedb_id') or {}).get('tv') and 'tmdb' not in seasons:
            missing.append('season.tmdb')
        if missing:
            issues.append({'anilist_id': row['anilist_id'], 'mal_id': row.get('mal_id'), 'missing': missing,
                           'relations': relations.get(str(row['anilist_id']), {}).get('relations', [])})
    return {'schema_version': 1, 'incomplete_tv_mappings': issues, 'override_events': events,
            'relation_fetch_failures': failures, 'note': 'Relations never imply shared provider IDs or season numbers. Old inferred mappings require review.'}


def verify_tmdb_overrides(rows, overrides, get):
    """Check explicit TMDB coordinates independently; never convert TVDB slots."""
    index = {r.get('anilist_id'): r for r in rows}
    results = []
    for entry in overrides:
        aid = entry['anilist_id']
        row = index.get(aid, {})
        patch = entry['set']
        if 'tmdb' not in patch.get('season', {}):
            continue
        season = patch['season']['tmdb']
        tv = (patch.get('themoviedb_id') or row.get('themoviedb_id') or {}).get('tv')
        if not tv:
            raise ValueError('TMDB season override requires a TV series ID')
        detail = get(f'/tv/{tv}/season/{season}')
        status = 'unavailable'
        if detail:
            if detail.get('season_number') != season or not isinstance(detail.get('episodes'), list):
                raise ValueError(f'TMDB returned incompatible season for AniList {aid}')
            status = 'verified'
        results.append({'anilist_id': aid, 'tv_id': tv, 'season': season, 'status': status})
    return results
