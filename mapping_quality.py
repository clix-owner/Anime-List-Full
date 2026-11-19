"""Conservative mapping maintenance, coverage enrichment, and watch-order builder."""
import copy
import json
import re
import time
import unicodedata
from pathlib import Path

RELATION_QUERY = '''query($ids:[Int]){Page(perPage:50){media(id_in:$ids,type:ANIME){id idMal format episodes startDate{year month day} title{english romaji} relations{edges{relationType node{id idMal type format startDate{year month day} title{english romaji}}}}}}}'''

CORE_OR_MEDIA_KEYS = ("mal_id", "anilist_id", "anidb_id", "tvdb_id", "imdb_id", "themoviedb_id")


def _norm_title(v):
    if not v:
        return ""
    v = unicodedata.normalize("NFKC", str(v)).casefold()
    v = v.replace("×", "x").replace("’", "'").replace("‐", "-").replace("–", "-").replace("—", "-")
    v = re.sub(r"[\u3000\s]+", " ", v)
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", v)


def prune_orphan_rows(rows, fribb_rows=None):
    """Remove unlinked secondary-ID-only bloat rows while preserving 100% of core/media & Fribb rows."""
    fribb_mal = {r.get("mal_id") for r in (fribb_rows or ()) if r.get("mal_id") is not None}
    fribb_al = {r.get("anilist_id") for r in (fribb_rows or ()) if r.get("anilist_id") is not None}
    fribb_adb = {r.get("anidb_id") for r in (fribb_rows or ()) if r.get("anidb_id") is not None}

    kept = []
    removed = 0
    for row in rows:
        has_core_or_media = any(row.get(k) is not None for k in CORE_OR_MEDIA_KEYS)
        in_fribb = (
            (row.get("mal_id") is not None and row.get("mal_id") in fribb_mal)
            or (row.get("anilist_id") is not None and row.get("anilist_id") in fribb_al)
            or (row.get("anidb_id") is not None and row.get("anidb_id") in fribb_adb)
        )
        if has_core_or_media or in_fribb:
            kept.append(row)
        else:
            removed += 1
    return kept, removed


def refresh_relations(rows, request, cache_path, limit=100, now=None, pause=time.sleep, include_all_types=False):
    now = time.time() if now is None else now
    path = Path(cache_path)
    try:
        cache = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    if include_all_types:
        ids = sorted({r['anilist_id'] for r in rows if isinstance(r.get('anilist_id'), int)})
    else:
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
                    ext_edges = []
                    for edge in node.get('relations', {}).get('edges', []):
                        target = edge.get('node') or {}
                        rel_type = edge.get('relationType')
                        if target.get('type') != 'ANIME':
                            continue
                        if rel_type in ('PREQUEL', 'SEQUEL'):
                            edges.append({'relation': rel_type, 'anilist_id': target['id'], 'mal_id': target.get('idMal')})
                        elif rel_type in ('SIDE_STORY', 'SPIN_OFF', 'ALTERNATIVE', 'PARENT'):
                            ext_edges.append({'relation': rel_type, 'anilist_id': target['id'], 'mal_id': target.get('idMal')})
                    entry = {'checked_at': now, 'attempted_at': now, 'relations': edges}
                    if ext_edges:
                        entry['extended_relations'] = ext_edges
                    if node.get('idMal') is not None:
                        entry['mal_id'] = node['idMal']
                    if node.get('format'):
                        entry['format'] = node['format']
                    if node.get('episodes') is not None:
                        entry['episodes'] = node['episodes']
                    if node.get('startDate'):
                        entry['startDate'] = node['startDate']
                    if node.get('title'):
                        entry['title'] = node['title']
                    cache[str(node['id'])] = entry
                break
        except Exception as exc:
            failures.append({'anilist_ids': batch, 'reason': type(exc).__name__})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2), encoding='utf-8')
        pause(1)
    return cache, failures


def fetch_wikidata_mappings(cache_path, request_get=None, now=None):
    """Fetch bulk MAL <-> AniList/AniDB/TMDB/TVDB/IMDb links from Wikidata SPARQL with 7-day cache."""
    now = time.time() if now is None else now
    path = Path(cache_path)
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(cached, dict) and now - cached.get('checked_at', 0) < 7 * 86400 and cached.get('by_mal'):
            return cached['by_mal']
    except (OSError, ValueError):
        pass

    if request_get is None:
        import requests
        request_get = lambda url, params, headers: requests.get(url, params=params, headers=headers, timeout=45)

    sparql = """
    SELECT ?mal ?anilist ?anidb ?tmdb_tv ?tmdb_movie ?tvdb ?imdb WHERE {
      ?item wdt:P4086 ?mal .
      OPTIONAL { ?item wdt:P8729 ?anilist . }
      OPTIONAL { ?item wdt:P5646 ?anidb . }
      OPTIONAL { ?item wdt:P4985 ?tmdb_tv . }
      OPTIONAL { ?item wdt:P4947 ?tmdb_movie . }
      OPTIONAL { ?item wdt:P4835 ?tvdb . }
      OPTIONAL { ?item wdt:P345 ?imdb . }
    }
    """
    by_mal = {}
    try:
        resp = request_get(
            "https://query.wikidata.org/sparql",
            params={"query": sparql, "format": "json"},
            headers={"Accept": "application/sparql-results+json", "User-Agent": "AnimeListBuilder/12.1"},
        )
        resp.raise_for_status()
        bindings = resp.json().get("results", {}).get("bindings", [])
        for b in bindings:
            mal_raw = b.get("mal", {}).get("value")
            if not mal_raw or not str(mal_raw).isdigit():
                continue
            mid = str(int(mal_raw))
            rec = by_mal.setdefault(mid, {})
            for src, key, is_int in (
                ("anilist", "anilist_id", True),
                ("anidb", "anidb_id", True),
                ("tvdb", "tvdb_id", True),
                ("tmdb_tv", "tmdb_tv", True),
                ("tmdb_movie", "tmdb_movie", True),
                ("imdb", "imdb_id", False),
            ):
                val = b.get(src, {}).get("value")
                if not val:
                    continue
                if is_int:
                    if str(val).isdigit() and key not in rec:
                        rec[key] = int(val)
                else:
                    if re.fullmatch(r"tt\d+", str(val)):
                        rec.setdefault("imdb_id", [])
                        if val not in rec["imdb_id"]:
                            rec["imdb_id"].append(val)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": now, "by_mal": by_mal}), encoding='utf-8')
    except Exception:
        pass
    return by_mal


def apply_wikidata_mappings(rows, wikidata_by_mal):
    """Additively fill missing IDs from Wikidata without overwriting existing values or colliding identities."""
    if not wikidata_by_mal:
        return 0
    used_anilist = {r["anilist_id"] for r in rows if r.get("anilist_id") is not None}
    used_anidb = {r["anidb_id"] for r in rows if r.get("anidb_id") is not None}
    filled = 0
    for row in rows:
        mid = row.get("mal_id")
        if mid is None:
            continue
        wd = wikidata_by_mal.get(str(mid))
        if not wd:
            continue
        changed = False
        if row.get("anilist_id") is None and wd.get("anilist_id") and wd["anilist_id"] not in used_anilist:
            row["anilist_id"] = wd["anilist_id"]
            used_anilist.add(wd["anilist_id"])
            changed = True
        if row.get("anidb_id") is None and wd.get("anidb_id") and wd["anidb_id"] not in used_anidb:
            row["anidb_id"] = wd["anidb_id"]
            used_anidb.add(wd["anidb_id"])
            changed = True
        if row.get("tvdb_id") is None and wd.get("tvdb_id"):
            row["tvdb_id"] = wd["tvdb_id"]
            changed = True
        if row.get("imdb_id") is None and wd.get("imdb_id"):
            row["imdb_id"] = list(wd["imdb_id"])
            changed = True
        row_type = row.get("type")
        if row_type == "MOVIE" and wd.get("tmdb_movie"):
            tmdb = row.setdefault("themoviedb_id", {})
            if "movie" not in tmdb:
                tmdb["movie"] = [wd["tmdb_movie"]]
                changed = True
        elif row_type in ("TV", "OVA", "ONA", "SPECIAL") and wd.get("tmdb_tv"):
            tmdb = row.setdefault("themoviedb_id", {})
            if "tv" not in tmdb:
                tmdb["tv"] = wd["tmdb_tv"]
                changed = True
        if changed:
            filled += 1
    return filled


def enrich_missing_tmdb_mappings(rows, title_year_lookup, tmdb_get, cache_path, limit=400, now=None):
    """Safely resolve missing TMDB + TVDB + IMDb IDs for TV and MOVIE entries via strict title+year match."""
    now = time.time() if now is None else now
    path = Path(cache_path)
    try:
        cache = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}

    enriched = 0
    processed = 0
    for row in rows:
        if processed >= limit:
            break
        rtype = row.get("type")
        if rtype not in ("TV", "MOVIE"):
            continue
        mid = row.get("mal_id")
        aid = row.get("anilist_id")
        if mid is None and aid is None:
            continue
        tmdb = row.get("themoviedb_id") or {}
        needs_tv = rtype == "TV" and "tv" not in tmdb
        needs_movie = rtype == "MOVIE" and "movie" not in tmdb
        needs_ext = rtype == "TV" and "tv" in tmdb and (row.get("tvdb_id") is None or row.get("imdb_id") is None)
        if not (needs_tv or needs_movie or needs_ext):
            continue

        cache_key = f"{rtype}:{mid or aid}"
        cached = cache.get(cache_key)
        if cached and now - cached.get("checked_at", 0) < 14 * 86400:
            res = cached.get("result")
        else:
            info = title_year_lookup(row) or {}
            titles = [t for t in info.get("titles", []) if t]
            year = info.get("year")
            if not titles and not needs_ext:
                continue
            processed += 1
            res = {}
            if needs_ext and not (needs_tv or needs_movie):
                ext = tmdb_get(f"/tv/{tmdb['tv']}/external_ids") or {}
                if isinstance(ext.get("tvdb_id"), int) and ext["tvdb_id"] > 0:
                    res["tvdb_id"] = ext["tvdb_id"]
                if isinstance(ext.get("imdb_id"), str) and re.fullmatch(r"tt\d+", ext["imdb_id"]):
                    res["imdb_id"] = [ext["imdb_id"]]
            elif titles:
                endpoint = "/search/movie" if rtype == "MOVIE" else "/search/tv"
                search_res = tmdb_get(endpoint, query=titles[0]) or {}
                norm_candidates = {_norm_title(t) for t in titles if _norm_title(t)}
                best = None
                for item in search_res.get("results", [])[:8]:
                    item_titles = [item.get("name"), item.get("original_name"), item.get("title"), item.get("original_title")]
                    if not any(_norm_title(it) in norm_candidates for it in item_titles if it):
                        continue
                    rel_date = item.get("first_air_date") or item.get("release_date") or ""
                    item_year = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else None
                    if year and item_year and abs(year - item_year) > 1:
                        continue
                    best = item
                    break
                if best and isinstance(best.get("id"), int):
                    if rtype == "MOVIE":
                        res["tmdb_movie"] = [best["id"]]
                        det = tmdb_get(f"/movie/{best['id']}") or {}
                        if isinstance(det.get("imdb_id"), str) and re.fullmatch(r"tt\d+", det["imdb_id"]):
                            res["imdb_id"] = [det["imdb_id"]]
                    else:
                        res["tmdb_tv"] = best["id"]
                        ext = tmdb_get(f"/tv/{best['id']}/external_ids") or {}
                        if isinstance(ext.get("tvdb_id"), int) and ext["tvdb_id"] > 0:
                            res["tvdb_id"] = ext["tvdb_id"]
                        if isinstance(ext.get("imdb_id"), str) and re.fullmatch(r"tt\d+", ext["imdb_id"]):
                            res["imdb_id"] = [ext["imdb_id"]]
            cache[cache_key] = {"checked_at": now, "result": res}

        if not res:
            continue
        changed = False
        if res.get("tmdb_movie") and "movie" not in row.get("themoviedb_id", {}):
            row.setdefault("themoviedb_id", {})["movie"] = list(res["tmdb_movie"])
            changed = True
        if res.get("tmdb_tv") and "tv" not in row.get("themoviedb_id", {}):
            row.setdefault("themoviedb_id", {})["tv"] = res["tmdb_tv"]
            changed = True
        if res.get("tvdb_id") and row.get("tvdb_id") is None:
            row["tvdb_id"] = res["tvdb_id"]
            changed = True
        if res.get("imdb_id") and row.get("imdb_id") is None:
            row["imdb_id"] = list(res["imdb_id"])
            changed = True
        if changed:
            enriched += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache), encoding='utf-8')
    return enriched


def build_watch_orders(rows, relation_cache, overrides=None):
    """Build release & prequel/sequel watch-order franchise clusters + curated overrides."""
    by_al = {r["anilist_id"]: r for r in rows if isinstance(r.get("anilist_id"), int)}
    adj = {}
    for aid_str, entry in (relation_cache or {}).items():
        if not str(aid_str).isdigit():
            continue
        aid = int(aid_str)
        adj.setdefault(aid, set())
        for edge in (entry.get("relations") or []) + (entry.get("extended_relations") or []):
            tid = edge.get("anilist_id")
            if isinstance(tid, int) and tid in by_al:
                adj[aid].add(tid)
                adj.setdefault(tid, set()).add(aid)

    visited = set()
    franchises = {}
    lookup_by_mal = {}
    for aid in sorted(adj):
        if aid in visited or not adj[aid]:
            continue
        comp = []
        stack = [aid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            comp.append(cur)
            for nxt in adj.get(cur, ()):
                if nxt not in visited:
                    stack.append(nxt)
        if len(comp) < 2:
            continue

        entries = []
        for cid in comp:
            meta = relation_cache.get(str(cid), {})
            row = by_al.get(cid, {})
            sd = meta.get("startDate") or {}
            sort_key = (sd.get("year") or 9999, sd.get("month") or 12, sd.get("day") or 31, cid)
            title_obj = meta.get("title") or {}
            title = title_obj.get("english") or title_obj.get("romaji") or f"AniList-{cid}"
            entries.append({
                "anilist_id": cid,
                "mal_id": row.get("mal_id") or meta.get("mal_id"),
                "title": title,
                "type": row.get("type") or meta.get("format") or "UNKNOWN",
                "episodes": meta.get("episodes"),
                "start_date": f"{sd.get('year')}-{sd.get('month') or 1:02d}-{sd.get('day') or 1:02d}" if sd.get("year") else None,
                "_sort": sort_key,
            })

        entries.sort(key=lambda x: x["_sort"])
        for e in entries:
            e.pop("_sort", None)

        # Mark entries reachable via PREQUEL/SEQUEL from the earliest TV entry as essential
        essential_ids = {entries[0]["anilist_id"]}
        queue = [entries[0]["anilist_id"]]
        while queue:
            cur = queue.pop(0)
            for edge in relation_cache.get(str(cur), {}).get("relations", []):
                tid = edge.get("anilist_id")
                if tid in by_al and tid not in essential_ids:
                    essential_ids.add(tid)
                    queue.append(tid)
        for e in entries:
            e["essential"] = e["anilist_id"] in essential_ids

        fid = f"franchise-{entries[0]['mal_id'] or entries[0]['anilist_id']}"
        franchises[fid] = {
            "franchise_id": fid,
            "franchise_name": entries[0]["title"],
            "curated": False,
            "release_order": entries,
            "chronological_order": [e for e in entries if e["essential"]],
        }
        for e in entries:
            if e.get("mal_id") is not None:
                lookup_by_mal[str(e["mal_id"])] = fid

    # Apply curated watch-order overrides (Fate, Monogatari, Steins;Gate, DanMachi, AoT, etc.)
    for ov in (overrides or []):
        fid = ov.get("franchise_id")
        if not fid:
            continue
        franchises[fid] = {**ov, "curated": True}
        for item in (ov.get("release_order") or []) + (ov.get("chronological_order") or []):
            if item.get("mal_id") is not None:
                lookup_by_mal[str(item["mal_id"])] = fid

    return {"schema_version": 1, "franchise_count": len(franchises), "by_mal_id": lookup_by_mal, "franchises": franchises}


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
