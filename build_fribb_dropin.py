#!/usr/bin/env python3
"""
Fribb-compatible anime-list-full.json builder v11.1 strict Fribb-preserve resumable

NO Fribb generated dataset.
NO Manami anime-offline-database.

Sources:
  - anime-and-manga/lists anime-full.json  -> MAL/AniList/AniDB/type
  - MAL Official API v2                   -> live catalog/type validation
  - AniDB anime-titles.xml.gz             -> conservative fallback for new MAL entries
  - ScudLee anime-list-full.xml            -> TVDB/IMDb/season/offset/raw TMDB
  - AnimeAPI dump                          -> provider enrichment:
       ANN, Anime-Planet, AniSearch, Kitsu, LiveChart, Simkl,
       plus additional AniDB/AniList/TMDB/TVDB/IMDb coverage
  - TMDB API                               -> classify/verify TV vs movie mappings

Main output is restricted to Fribb's public field names/types.
"""

import copy
import gzip, json, os, re, sys, time, unicodedata
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
import requests

MAL_API = "https://api.myanimelist.net/v2"
BUILDER_VERSION = "v11.6-reconcile-validator-fixed"
ANILIST_API = "https://graphql.anilist.co"
TMDB_API = "https://api.themoviedb.org/3"

MAL_CLIENT_ID = os.getenv("MAL_CLIENT_ID", "").strip()
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

BASE_MAP_FILE = Path("anime-and-manga-full.json")
FRIBB_BASE_FILE = Path("fribb-anime-list-full.json")
ANIMEAPI_FILE = Path("animeapi.json")
ANIDB_TITLES_GZ = Path("anime-titles.xml.gz")
SCUDLEE_XML = Path("anime-list-full.xml")

OUT_FULL = Path("anime-list-full.json")
OUT_MINI = Path("anime-list-mini.json")
DIAG = Path("build-diagnostics.json")
META = Path("build-meta.json")
STATE = Path("build-state.json")
WORK_CACHE_DIR = Path(".build-cache")
MAL_CACHE = WORK_CACHE_DIR / "mal-season-cache.json"
ANILIST_CACHE = WORK_CACHE_DIR / "anilist-mal-cache.json"
TMDB_CACHE = WORK_CACHE_DIR / "tmdb-cache.json"
BUILD_MODE = os.getenv("BUILD_MODE", "incremental").strip().lower()
if BUILD_MODE not in {"incremental", "full"}:
    BUILD_MODE = "incremental"

START_YEAR = 1917
END_YEAR = date.today().year + 1
SEASONS = ("winter", "spring", "summer", "fall")
PAGE_LIMIT = 100

MAL_INTERVAL = float(os.getenv("MAL_INTERVAL", "1.0"))
TMDB_INTERVAL = float(os.getenv("TMDB_INTERVAL", "0.08"))
ANILIST_INTERVAL = float(os.getenv("ANILIST_INTERVAL", "0.75"))
ANILIST_BATCH = int(os.getenv("ANILIST_BATCH", "50"))
ANILIST_DAILY_VERIFY = int(os.getenv("ANILIST_DAILY_VERIFY", "1000"))
_last_mal = 0.0
_last_tmdb = 0.0
_last_anilist = 0.0

MAL_FIELDS = "id,title,alternative_titles,media_type,status,start_date,start_season"

ALLOWED_KEYS = {
    "type", "anidb_id", "anilist_id", "animecountdown_id",
    "animenewsnetwork_id", "anime-planet_id", "anisearch_id",
    "imdb_id", "kitsu_id", "livechart_id", "mal_id", "simkl_id",
    "themoviedb_id", "tvdb_id", "season", "episode_offset",
}
VALID_TYPES = {"TV","MOVIE","OVA","ONA","SPECIAL","MUSIC","UNKNOWN"}
MAL_TYPE_MAP = {
    "tv":"TV","movie":"MOVIE","ova":"OVA","ona":"ONA",
    "special":"SPECIAL","tv_special":"SPECIAL","music":"MUSIC","unknown":"UNKNOWN",
}

def log(x): print(x, flush=True)

def write_json(path, value, pretty=True):
    tmp = Path(str(path)+".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False,
                  indent=2 if pretty else None,
                  separators=None if pretty else (",",":"))
    os.replace(tmp, path)

def parse_int(v):
    try: return int(str(v).strip())
    except (TypeError, ValueError): return None

def norm_title(v):
    if not v: return ""
    v = unicodedata.normalize("NFKC", str(v)).casefold()
    v = v.replace("×","x").replace("’","'").replace("‐","-").replace("–","-").replace("—","-")
    v = re.sub(r"<[^>]+>", " ", v)
    v = re.sub(r"[\u3000\s]+", " ", v)
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", v)

def wait_slot(last, interval):
    d = time.monotonic()-last
    if d < interval: time.sleep(interval-d)


def load_cache(path, default):
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value
    except Exception as exc:
        log(f"Cache load warning for {path}: {exc}")
    return default

def save_cache(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

def mal_get(path, params=None, allow_not_found=False):
    global _last_mal
    if not MAL_CLIENT_ID:
        sys.exit("MAL_CLIENT_ID is required")

    for attempt in range(8):
        wait_slot(_last_mal, MAL_INTERVAL)
        try:
            r = requests.get(
                MAL_API + path,
                params=params,
                headers={"X-MAL-CLIENT-ID": MAL_CLIENT_ID},
                timeout=45,
            )
            _last_mal = time.monotonic()

            # Future seasonal endpoints can legitimately be unpublished.
            if r.status_code == 404 and allow_not_found:
                return None

            if r.status_code == 429:
                time.sleep(max(1, int(float(r.headers.get("Retry-After", 15)))))
                continue

            if r.status_code in (401, 403):
                sys.exit("MAL auth failed")

            if r.status_code >= 500:
                time.sleep(min(10 * (attempt + 1), 60))
                continue

            r.raise_for_status()
            return r.json()

        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404 and allow_not_found:
                return None
            log(f"MAL HTTP error {attempt+1}/8 [{status}]: {exc}")
            time.sleep(min(5 * (attempt + 1), 40))

        except requests.RequestException as exc:
            log(f"MAL network error {attempt+1}/8: {exc}")
            time.sleep(min(5 * (attempt + 1), 40))

    raise RuntimeError("MAL request failed: " + path)


def fetch_mal_catalog():
    cache = load_cache(MAL_CACHE, {"version": 1, "seasons": {}})
    seasons_cache = cache.setdefault("seasons", {})
    out = {}

    now = datetime.now(timezone.utc)
    current_year = now.year

    for year in range(START_YEAR, END_YEAR + 1):
        stop_future_year = False

        for season in SEASONS:
            if stop_future_year:
                break

            key = f"{year}-{season}"
            cached = seasons_cache.get(key)

            if isinstance(cached, dict) and cached.get("status") == "complete":
                nodes = cached.get("nodes") or []
                for node in nodes:
                    if isinstance(node, dict) and isinstance(node.get("id"), int):
                        out.setdefault(node["id"], node)
                log(f"MAL {year} {season}: cached ({len(out)})")
                continue

            season_nodes = []
            offset = 0
            unavailable = False

            while True:
                data = mal_get(
                    f"/anime/season/{year}/{season}",
                    {
                        "limit": PAGE_LIMIT,
                        "offset": offset,
                        "fields": MAL_FIELDS,
                        "sort": "anime_score",
                    },
                    allow_not_found=True,
                )

                if data is None:
                    log(f"MAL {year} {season}: not available yet, skipping")
                    unavailable = True
                    break

                nodes = [x.get("node") for x in data.get("data", []) if x.get("node")]
                season_nodes.extend(nodes)

                if not nodes or not (data.get("paging") or {}).get("next"):
                    break

                offset += PAGE_LIMIT

            if unavailable:
                # Do not checkpoint unpublished future seasons.
                # For a future year, once one season is unavailable, the later
                # seasons will also be unavailable in normal MAL publication order.
                if year > current_year:
                    log(f"MAL {year}: stopping after first unavailable future season")
                    stop_future_year = True
                continue

            seasons_cache[key] = {
                "status": "complete",
                "nodes": season_nodes,
                "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
            save_cache(MAL_CACHE, cache)

            for node in season_nodes:
                if isinstance(node, dict) and isinstance(node.get("id"), int):
                    out.setdefault(node["id"], node)

            log(f"MAL {year} {season}: fetched+checkpointed ({len(out)})")

    return out


def load_fribb_baseline():
    raw = json.loads(FRIBB_BASE_FILE.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("Fribb baseline is not a JSON array")

    rows = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"Fribb baseline row {i} is not an object")
        bad = set(item) - ALLOWED_KEYS
        if bad:
            raise RuntimeError(f"Fribb baseline row {i} contains unsupported keys: {bad}")
        # Preserve the baseline object values. Enrichment is additive.
        rows.append(dict(item))

    log(f"Fribb baseline: {len(rows)} records")
    return rows


def merge_preserving_baseline(target, incoming):
    """Add information without deleting/overwriting existing Fribb values."""
    for k, v in incoming.items():
        if k not in ALLOWED_KEYS or v is None:
            continue

        if k not in target:
            target[k] = v
            continue

        if k == "imdb_id" and isinstance(target.get(k), list) and isinstance(v, list):
            target[k] = list(dict.fromkeys(target[k] + v))
            continue

        if k == "themoviedb_id" and isinstance(target.get(k), dict) and isinstance(v, dict):
            cur = dict(target[k])
            if "tv" not in cur and isinstance(v.get("tv"), int):
                cur["tv"] = v["tv"]
            if isinstance(v.get("movie"), list):
                old_movies = cur.get("movie") if isinstance(cur.get("movie"), list) else []
                merged = list(dict.fromkeys(old_movies + v["movie"]))
                if merged:
                    cur["movie"] = merged
            target[k] = cur
            continue

        if k in ("season", "episode_offset") and isinstance(target.get(k), dict) and isinstance(v, dict):
            cur = dict(target[k])
            for subkey in ("tvdb", "tmdb"):
                if subkey not in cur and isinstance(v.get(subkey), int):
                    cur[subkey] = v[subkey]
            target[k] = cur
            continue

        # Scalar or already-populated structured field: baseline wins.
    return target


def merge_latest_fribb_into_ours(target, latest):
    """Merge latest Fribb into the previous OUR record without dropping OUR-only data.

    Latest Fribb wins for scalar fields it explicitly supplies. Structured/list
    mappings are union-merged so mappings added by OUR previous builds survive.
    """
    for k, v in latest.items():
        if k not in ALLOWED_KEYS or v is None:
            continue
        if k not in target:
            target[k] = v
            continue
        if k == "imdb_id" and isinstance(target.get(k), list) and isinstance(v, list):
            target[k] = list(dict.fromkeys(v + target[k]))
            continue
        if k == "themoviedb_id" and isinstance(target.get(k), dict) and isinstance(v, dict):
            cur = dict(target[k])
            # Latest Fribb values win where present; OUR-only values remain.
            if isinstance(v.get("tv"), int):
                cur["tv"] = v["tv"]
            if isinstance(v.get("movie"), list):
                old = cur.get("movie") if isinstance(cur.get("movie"), list) else []
                cur["movie"] = list(dict.fromkeys(v["movie"] + old))
            target[k] = cur
            continue
        if k in ("season", "episode_offset") and isinstance(target.get(k), dict) and isinstance(v, dict):
            cur = dict(target[k])
            cur.update(v)
            target[k] = cur
            continue
        # Fribb is an upstream reference: an explicit current scalar value wins.
        target[k] = v
    return target


def reconcile_latest_fribb_into_previous(rows, fribb_rows):
    """Use previous OUR JSON as primary state, then merge/append latest Fribb."""
    by_mal, by_anilist, by_anidb = make_indexes(rows)
    merged = appended = 0
    for fr in fribb_rows:
        idx = find_union_match(fr, by_mal, by_anilist, by_anidb, rows)
        if idx is None:
            rows.append(dict(fr))
            appended += 1
            idx = len(rows) - 1
            mid = parse_int(fr.get("mal_id")); aid = parse_int(fr.get("anilist_id")); adb = parse_int(fr.get("anidb_id"))
            if mid is not None: by_mal[mid] = idx
            if aid is not None: by_anilist[aid] = idx
            if adb is not None: by_anidb[adb] = idx
        else:
            before = canonical_record_hash(rows[idx])
            merge_latest_fribb_into_ours(rows[idx], fr)
            if canonical_record_hash(rows[idx]) != before:
                merged += 1
    return rows, merged, appended


def value_contains(original, enriched):
    if isinstance(original, dict):
        return isinstance(enriched, dict) and all(
            k in enriched and value_contains(v, enriched[k]) for k, v in original.items()
        )
    if isinstance(original, list):
        return isinstance(enriched, list) and all(x in enriched for x in original)
    return original == enriched


def assert_fribb_prefix_preserved(fribb_rows, rows):
    if len(rows) < len(fribb_rows):
        raise RuntimeError(
            f"Fribb preservation failed: output {len(rows)} < baseline {len(fribb_rows)}"
        )

    # v10 keeps every Fribb record at the same array index and appends only new rows.
    for i, original in enumerate(fribb_rows):
        enriched = rows[i]
        for key, value in original.items():
            if key not in enriched or not value_contains(value, enriched[key]):
                raise RuntimeError(
                    "Fribb preservation failed at baseline row "
                    f"{i}, field {key}: expected={value!r}, actual={enriched.get(key)!r}"
                )


def selftest_preservation_logic():
    # Regression test: Fribb may intentionally map TVDB and TMDB differently.
    sample = {
        "type": "OVA",
        "mal_id": 1919,
        "anidb_id": 384,
        "tvdb_id": 75113,
        "themoviedb_id": {"tv": 34163},
        "season": {"tvdb": 0, "tmdb": 0},
        "episode_offset": {"tvdb": 7, "tmdb": 2},
    }
    before = copy.deepcopy(sample)
    candidate = copy.deepcopy(sample)
    enrich_tmdb(candidate, [])
    merge_preserving_baseline(sample, candidate)

    if sample["episode_offset"] != before["episode_offset"]:
        raise RuntimeError(
            "Preservation self-test failed: episode_offset was overwritten "
            f"{before['episode_offset']} -> {sample['episode_offset']}"
        )
    if sample["season"] != before["season"]:
        raise RuntimeError(
            "Preservation self-test failed: season was overwritten "
            f"{before['season']} -> {sample['season']}"
        )



IDENTITY_FIELDS = ("mal_id", "anilist_id", "anidb_id")

def baseline_identity_owners(fribb_rows):
    owners = {field: {} for field in IDENTITY_FIELDS}
    for idx, row in enumerate(fribb_rows):
        for field in IDENTITY_FIELDS:
            value = parse_int(row.get(field))
            if value is not None:
                owners[field][value] = idx
    return owners

def sanitize_identity_collisions(rows, fribb_rows):
    """Keep Fribb identity ownership immutable; remove only added collisions."""
    owners = baseline_identity_owners(fribb_rows)
    removed = {field: 0 for field in IDENTITY_FIELDS}
    for field in IDENTITY_FIELDS:
        claims = {}
        for idx, row in enumerate(rows):
            value = parse_int(row.get(field))
            if value is not None:
                claims.setdefault(value, []).append(idx)
        for value, idxs in claims.items():
            if len(idxs) <= 1:
                continue
            immutable_owner = owners[field].get(value)
            if immutable_owner is not None and immutable_owner in idxs:
                winner = immutable_owner
            else:
                baseline_claims = [i for i in idxs if i < len(fribb_rows)]
                winner = baseline_claims[0] if baseline_claims else idxs[0]
            for idx in idxs:
                if idx == winner:
                    continue
                if idx < len(fribb_rows) and parse_int(fribb_rows[idx].get(field)) == value:
                    raise RuntimeError(
                        f"Fribb baseline has duplicate immutable {field}={value}"
                    )
                rows[idx].pop(field, None)
                removed[field] += 1
    return removed

def strip_candidate_identity_conflicts(candidate, by_mal, by_anilist, by_anidb, rows):
    mid = parse_int(candidate.get("mal_id"))
    aid = parse_int(candidate.get("anilist_id"))
    if aid is not None and aid in by_anilist:
        owner_mid = parse_int(rows[by_anilist[aid]].get("mal_id"))
        if owner_mid is not None and owner_mid != mid:
            candidate.pop("anilist_id", None)
    adb = parse_int(candidate.get("anidb_id"))
    if adb is not None and adb in by_anidb:
        owner_mid = parse_int(rows[by_anidb[adb]].get("mal_id"))
        if owner_mid is not None and owner_mid != mid:
            candidate.pop("anidb_id", None)

def assert_unique_identity_ids(rows):
    for field in IDENTITY_FIELDS:
        seen = {}
        for idx, row in enumerate(rows):
            value = parse_int(row.get(field))
            if value is None:
                continue
            if value in seen:
                raise RuntimeError(
                    f"Identity uniqueness failed: {field}={value} at rows "
                    f"{seen[value]} and {idx}"
                )
            seen[value] = idx



def record_identity_keys(row):
    """Stable identity keys used only to test whether a Fribb row is represented."""
    keys = []

    mid = parse_int(row.get("mal_id"))
    if mid is not None:
        keys.append(("mal_id", mid))

    aid = parse_int(row.get("anilist_id"))
    if aid is not None:
        keys.append(("anilist_id", aid))

    adb = parse_int(row.get("anidb_id"))
    if adb is not None:
        keys.append(("anidb_id", adb))

    # Fallback identities for Fribb rows that do not have MAL/AniList/AniDB.
    for field in (
        "kitsu_id",
        "animenewsnetwork_id",
        "anisearch_id",
        "livechart_id",
        "simkl_id",
        "animecountdown_id",
    ):
        value = row.get(field)
        if value is not None:
            keys.append((field, str(value)))

    ap = row.get("anime-planet_id")
    if ap:
        keys.append(("anime-planet_id", str(ap)))

    return keys


def find_unrepresented_fribb_rows(fribb_rows, rows):
    """
    A Fribb row is represented when at least one stable identity is still present
    in the output. Dedup cleanup may intentionally remove a conflicting secondary
    AniList/AniDB ID, so the validator must not require an exact identity tuple.
    """
    present = set()
    for row in rows:
        present.update(record_identity_keys(row))

    missing = []
    for idx, row in enumerate(fribb_rows):
        keys = record_identity_keys(row)
        if not keys:
            # No stable ID to compare. Full-build prefix preservation still protects
            # these rows, and incremental reconciliation cannot safely identify them.
            continue
        if not any(key in present for key in keys):
            missing.append(idx)

    return missing


def make_indexes(rows):
    by_mal, by_anilist, by_anidb = {}, {}, {}
    for idx, row in enumerate(rows):
        mid = parse_int(row.get("mal_id"))
        aid = parse_int(row.get("anilist_id"))
        adb = parse_int(row.get("anidb_id"))
        if mid is not None:
            by_mal[mid] = idx
        if aid is not None:
            by_anilist[aid] = idx
        if adb is not None:
            by_anidb[adb] = idx
    return by_mal, by_anilist, by_anidb


def find_union_match(candidate, by_mal, by_anilist, by_anidb, rows):
    mid = parse_int(candidate.get("mal_id"))
    aid = parse_int(candidate.get("anilist_id"))
    adb = parse_int(candidate.get("anidb_id"))

    if mid is not None and mid in by_mal:
        return by_mal[mid]

    if aid is not None and aid in by_anilist:
        idx = by_anilist[aid]
        existing_mid = parse_int(rows[idx].get("mal_id"))
        # Safe merge only if it will not collapse two different MAL IDs.
        if existing_mid is None or existing_mid == mid:
            return idx

    if adb is not None and adb in by_anidb:
        idx = by_anidb[adb]
        existing_mid = parse_int(rows[idx].get("mal_id"))
        existing_aid = parse_int(rows[idx].get("anilist_id"))
        if (existing_mid is None or existing_mid == mid) and (
            existing_aid is None or aid is None or existing_aid == aid
        ):
            return idx

    return None


def load_base():
    raw=json.loads(BASE_MAP_FILE.read_text(encoding="utf-8"))
    out={}
    for x in raw:
        mal=parse_int(x.get("idMal")); adb=parse_int(x.get("idAniDB")); al=parse_int(x.get("idAL"))
        if not mal or not adb: continue
        typ=str(x.get("type") or "UNKNOWN").upper()
        if typ not in VALID_TYPES: typ="UNKNOWN"
        r={"type":typ,"anidb_id":adb,"mal_id":mal}
        if al: r["anilist_id"]=al
        out[mal]=r
    return out

def load_animeapi():
    raw=json.loads(ANIMEAPI_FILE.read_text(encoding="utf-8"))
    if isinstance(raw,dict):
        # tolerate wrappers if upstream changes representation
        raw=raw.get("data") or raw.get("anime") or raw.get("results") or []
    by_mal={}
    by_anidb={}
    for x in raw:
        mal=parse_int(x.get("myanimelist"))
        adb=parse_int(x.get("anidb"))
        if mal: by_mal[mal]=x
        if adb: by_anidb[adb]=x
    log(f"AnimeAPI: MAL={len(by_mal)}, AniDB={len(by_anidb)}")
    return by_mal, by_anidb

def animeapi_fields(x):
    if not x: return {}
    r={}
    mapping={
      "anidb":"anidb_id",
      "anilist":"anilist_id",
      "animenewsnetwork":"animenewsnetwork_id",
      "anisearch":"anisearch_id",
      "kitsu":"kitsu_id",
      "livechart":"livechart_id",
      "myanimelist":"mal_id",
      "simkl":"simkl_id",
      "thetvdb":"tvdb_id",
    }
    for src,dst in mapping.items():
        v=parse_int(x.get(src))
        if v is not None: r[dst]=v

    ap=x.get("animeplanet")
    if isinstance(ap,str) and ap.strip():
        r["anime-planet_id"]=ap.strip()

    # Fribb/Manami style uses same ID for AnimeCountdown and Simkl.
    if "simkl_id" in r:
        r["animecountdown_id"]=r["simkl_id"]

    imdb=x.get("imdb")
    if isinstance(imdb,str) and re.fullmatch(r"tt\d+",imdb,re.I):
        r["imdb_id"]=[imdb]

    tmdb=parse_int(x.get("themoviedb"))
    tmdb_type=str(x.get("themoviedb_type") or "").lower()
    if tmdb and tmdb_type in ("tv","movie"):
        r["themoviedb_id"]={tmdb_type: tmdb if tmdb_type=="tv" else [tmdb]}

    return r

def load_anidb_index():
    idx=defaultdict(set)
    with gzip.open(ANIDB_TITLES_GZ,"rb") as gz:
        root=ET.parse(gz).getroot()
    for a in root.findall(".//anime"):
        aid=parse_int(a.attrib.get("aid"))
        if not aid: continue
        for t in a.findall("./title"):
            n=norm_title((t.text or "").strip())
            if n: idx[n].add(aid)
    return idx

def fallback_anidb(node, idx):
    alt=node.get("alternative_titles") or {}
    vals=[node.get("title"),alt.get("en"),alt.get("ja"),*(alt.get("synonyms") or [])]
    c=set()
    for v in vals:
        n=norm_title(v)
        if n: c.update(idx.get(n,()))
    return next(iter(c)) if len(c)==1 else None

def load_scudlee():
    root=ET.parse(SCUDLEE_XML).getroot()
    out={}
    for a in root.findall(".//anime"):
        aid=parse_int(a.attrib.get("anidbid"))
        if not aid: continue
        r={}
        tv=parse_int(a.attrib.get("tvdbid"))
        if tv: r["tvdb_id"]=tv
        imdb=sorted(set(re.findall(r"tt\d+",str(a.attrib.get("imdbid") or ""),re.I)))
        if imdb: r["imdb_id"]=imdb
        raw=sorted(set(int(x) for x in re.findall(r"\d+",str(a.attrib.get("tmdbid") or ""))))
        if raw: r["_raw_tmdb_ids"]=raw
        s=parse_int(a.attrib.get("defaulttvdbseason"))
        if s is not None and tv: r["season"]={"tvdb":s}
        off=parse_int(a.attrib.get("episodeoffset"))
        if off is not None and tv: r["episode_offset"]={"tvdb":off}
        out[aid]=r
    return out


ANILIST_QUERY = """
query ($malIds: [Int]) {
  Page(perPage: 50) {
    media(idMal_in: $malIds, type: ANIME) {
      id
      idMal
    }
  }
}
"""

def anilist_lookup_batch(mal_ids):
    global _last_anilist
    if not mal_ids:
        return {}

    for attempt in range(7):
        wait_slot(_last_anilist, ANILIST_INTERVAL)
        try:
            r = requests.post(
                ANILIST_API,
                json={"query": ANILIST_QUERY, "variables": {"malIds": mal_ids}},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=45,
            )
            _last_anilist = time.monotonic()

            if r.status_code == 429:
                wait = max(1, int(float(r.headers.get("Retry-After", 60))))
                log(f"AniList 429; waiting {wait}s")
                time.sleep(wait)
                continue

            if r.status_code >= 500:
                time.sleep(min(10 * (attempt + 1), 60))
                continue

            r.raise_for_status()
            payload = r.json()

            if payload.get("errors"):
                log(f"AniList GraphQL errors: {payload['errors'][:1]}")
                time.sleep(min(5 * (attempt + 1), 30))
                continue

            out = {}
            media = (((payload.get("data") or {}).get("Page") or {}).get("media") or [])
            for item in media:
                mid = parse_int(item.get("idMal"))
                aid = parse_int(item.get("id"))
                if mid and aid:
                    out[mid] = aid
            return out

        except requests.RequestException as exc:
            log(f"AniList error {attempt+1}/7: {exc}")
            time.sleep(min(5 * (attempt + 1), 40))

    return {}

def anilist_lookup_many(mal_ids):
    ids = sorted(set(int(x) for x in mal_ids if parse_int(x)))
    cache = load_cache(ANILIST_CACHE, {"version": 1, "results": {}})
    results = cache.setdefault("results", {})

    out = {}
    pending = []

    for mid in ids:
        entry = results.get(str(mid))
        if isinstance(entry, dict) and entry.get("checked"):
            aid = parse_int(entry.get("anilist_id"))
            if aid:
                out[mid] = aid
        else:
            pending.append(mid)

    if ids:
        log(f"AniList cache hits: {len(ids)-len(pending)}/{len(ids)}")

    for i in range(0, len(pending), ANILIST_BATCH):
        batch = pending[i:i + ANILIST_BATCH]
        resolved = anilist_lookup_batch(batch)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        for mid in batch:
            aid = resolved.get(mid)
            results[str(mid)] = {
                "checked": True,
                "anilist_id": aid,
                "saved_at": now
            }
            if aid:
                out[mid] = aid

        # Persist every completed GraphQL batch.
        save_cache(ANILIST_CACHE, cache)
        log(f"AniList fetched+checkpointed: {min(i+len(batch), len(pending))}/{len(pending)}")

    return out

def tmdb_get(path,params=None):
    global _last_tmdb
    if not TMDB_API_KEY: return None

    public_params = dict(params or {})
    cache_key = path + "?" + "&".join(f"{k}={public_params[k]}" for k in sorted(public_params))
    tmdb_cache = load_cache(TMDB_CACHE, {"version": 1, "responses": {}})
    responses = tmdb_cache.setdefault("responses", {})

    if cache_key in responses:
        return responses[cache_key]

    params=dict(public_params); params["api_key"]=TMDB_API_KEY
    for attempt in range(6):
        wait_slot(_last_tmdb,TMDB_INTERVAL)
        try:
            r=requests.get(TMDB_API+path,params=params,timeout=30)
            _last_tmdb=time.monotonic()
            if r.status_code==404:
                responses[cache_key] = None
                save_cache(TMDB_CACHE, tmdb_cache)
                return None
            if r.status_code==429:
                time.sleep(max(1,int(float(r.headers.get("Retry-After",2)))));continue
            if r.status_code>=500:
                time.sleep(min(2**attempt,20));continue
            r.raise_for_status()
            payload = r.json()
            responses[cache_key] = payload
            save_cache(TMDB_CACHE, tmdb_cache)
            return payload
        except requests.RequestException:
            time.sleep(min(2**attempt,10))
    return None

def tmdb_exists(kind,i): return bool(tmdb_get(f"/{kind}/{i}"))

def tmdb_find(external,source):
    p=tmdb_get(f"/find/{external}",{"external_source":source})
    if not p:return {"tv":[],"movie":[]}
    return {
      "tv":[int(x["id"]) for x in p.get("tv_results",[]) if x.get("id")],
      "movie":[int(x["id"]) for x in p.get("movie_results",[]) if x.get("id")]
    }

def merge_tmdb(existing, incoming):
    out=dict(existing or {})
    if not incoming:return out
    if isinstance(incoming.get("tv"),int) and "tv" not in out:
        out["tv"]=incoming["tv"]
    movies=incoming.get("movie")
    if isinstance(movies,list):
        out["movie"]=sorted(set((out.get("movie") or [])+[x for x in movies if isinstance(x,int)]))
    return out

def classify_raw(ids,typ):
    if not ids:return {}
    tv=[]; movies=[]
    if TMDB_API_KEY:
        for i in ids:
            mo=tmdb_exists("movie",i); ts=tmdb_exists("tv",i)
            if mo and not ts:movies.append(i)
            elif ts and not mo:tv.append(i)
            elif mo and ts:
                (movies if typ=="MOVIE" else tv).append(i)
    elif typ=="MOVIE":
        movies.extend(ids)
    r={}
    if tv:r["tv"]=tv[0]
    if movies:r["movie"]=sorted(set(movies))
    return r

def enrich_tmdb(row, raw_ids):
    tmdb=dict(row.get("themoviedb_id") or {})
    tmdb=merge_tmdb(tmdb,classify_raw(raw_ids,row.get("type")))

    if "tv" not in tmdb and row.get("tvdb_id") and TMDB_API_KEY:
        f=tmdb_find(row["tvdb_id"],"tvdb_id")
        if f["tv"]:tmdb["tv"]=f["tv"][0]
        elif f["movie"]:tmdb["movie"]=sorted(set((tmdb.get("movie") or [])+f["movie"]))

    if not tmdb and row.get("imdb_id") and TMDB_API_KEY:
        for iid in row["imdb_id"]:
            f=tmdb_find(iid,"imdb_id")
            if f["tv"]:tmdb["tv"]=f["tv"][0];break
            if f["movie"]:tmdb["movie"]=f["movie"];break

    if tmdb:row["themoviedb_id"]=tmdb
    if isinstance(tmdb.get("tv"),int):
        # Never overwrite an existing mapping. Fribb can intentionally have
        # TVDB and TMDB season/episode offsets that differ.
        if (
            row.get("season",{}).get("tvdb") is not None
            and row.get("season",{}).get("tmdb") is None
        ):
            row.setdefault("season",{})["tmdb"]=row["season"]["tvdb"]
        if (
            row.get("episode_offset",{}).get("tvdb") is not None
            and row.get("episode_offset",{}).get("tmdb") is None
        ):
            row.setdefault("episode_offset",{})["tmdb"]=row["episode_offset"]["tvdb"]

def merge_record(base, extra, overwrite=False):
    for k,v in extra.items():
        if k not in ALLOWED_KEYS or v is None: continue
        if overwrite or k not in base:
            base[k]=v
        elif k=="imdb_id":
            base[k]=sorted(set((base.get(k) or [])+(v or [])))
        elif k=="themoviedb_id":
            base[k]=merge_tmdb(base.get(k),v)

def canonicalize(r):
    r={k:v for k,v in r.items() if k in ALLOWED_KEYS and v is not None}
    if r.get("type") not in VALID_TYPES:r["type"]="UNKNOWN"

    for k in ("anidb_id","anilist_id","animecountdown_id","animenewsnetwork_id",
              "anisearch_id","kitsu_id","livechart_id","mal_id","simkl_id","tvdb_id"):
        if k in r:
            v=parse_int(r[k])
            if v is None:r.pop(k,None)
            else:r[k]=v

    if "anime-planet_id" in r:
        if not isinstance(r["anime-planet_id"],str) or not r["anime-planet_id"].strip():
            r.pop("anime-planet_id",None)

    if "imdb_id" in r:
        vals=sorted(set(x for x in r["imdb_id"] if isinstance(x,str) and re.fullmatch(r"tt\d+",x,re.I)))
        if vals:r["imdb_id"]=vals
        else:r.pop("imdb_id",None)

    if "themoviedb_id" in r:
        t=r["themoviedb_id"]; c={}
        if isinstance(t.get("tv"),int):c["tv"]=t["tv"]
        if isinstance(t.get("movie"),list):
            m=sorted(set(x for x in t["movie"] if isinstance(x,int)))
            if m:c["movie"]=m
        if c:r["themoviedb_id"]=c
        else:r.pop("themoviedb_id",None)

    for n in ("season","episode_offset"):
        if n in r:
            c={k:v for k,v in r[n].items() if k in ("tvdb","tmdb") and isinstance(v,int)}
            if c:r[n]=c
            else:r.pop(n,None)
    return r

def validate(rows):
    for i,r in enumerate(rows):
        bad=set(r)-ALLOWED_KEYS
        if bad:raise RuntimeError(f"row {i} custom keys: {bad}")
        t=r.get("themoviedb_id")
        if t:
            if "tv" in t and not isinstance(t["tv"],int):raise RuntimeError("TMDB tv type")
            if "movie" in t and not (isinstance(t["movie"],list) and all(isinstance(x,int) for x in t["movie"])):
                raise RuntimeError("TMDB movie type")



def sha256_bytes(data):
    return __import__("hashlib").sha256(data).hexdigest()

def sha256_file(path):
    h = __import__("hashlib").sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def canonical_record_hash(record):
    # Hash only the public Fribb-compatible record shape.
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(payload)

def load_state():
    if not STATE.exists():
        return {
            "source_hashes": {},
            "record_hashes": {},
            "final_file_hash": None,
            "last_generated_at": None,
            "anilist_cursor": 0,
        }
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not object")
        state.setdefault("source_hashes", {})
        state.setdefault("record_hashes", {})
        state.setdefault("final_file_hash", None)
        state.setdefault("last_generated_at", None)
        state.setdefault("anilist_cursor", 0)
        return state
    except Exception as exc:
        log(f"State load failed; rebuilding state: {exc}")
        return {
            "source_hashes": {},
            "record_hashes": {},
            "final_file_hash": None,
            "last_generated_at": None,
            "anilist_cursor": 0,
        }

def current_source_hashes():
    hashes = {}
    for name, path in {
        "fribb_baseline": FRIBB_BASE_FILE,
        "base_mapping": BASE_MAP_FILE,
        "animeapi": ANIMEAPI_FILE,
        "anidb_titles": ANIDB_TITLES_GZ,
        "scudlee": SCUDLEE_XML,
    }.items():
        hashes[name] = sha256_file(path) if path.exists() else None
    return hashes

def source_change_flags(old_hashes, new_hashes):
    return {k: old_hashes.get(k) != new_hashes.get(k) for k in new_hashes}

def load_existing_dataset():
    if BUILD_MODE != "incremental" or not OUT_FULL.exists():
        return [], {}, set()

    try:
        rows = json.loads(OUT_FULL.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            return [], {}, set()
    except Exception as exc:
        log(f"Existing dataset could not be loaded; falling back to full build: {exc}")
        return [], {}, set()

    by_mal = {}
    mal_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = parse_int(row.get("mal_id"))
        if mid:
            by_mal[mid] = row
            mal_ids.add(mid)

    log(f"Existing dataset: {len(rows)} records, {len(mal_ids)} MAL IDs")
    return rows, by_mal, mal_ids


def main():
    log(f"Builder version: {BUILDER_VERSION}")
    selftest_preservation_logic()
    log("Preservation self-test: OK")

    for p in (FRIBB_BASE_FILE, BASE_MAP_FILE, ANIMEAPI_FILE, ANIDB_TITLES_GZ, SCUDLEE_XML):
        if not p.exists():
            sys.exit(f"Missing {p}")

    existing_rows, existing_by_mal, existing_mal_ids = load_existing_dataset()
    state = load_state()
    new_source_hashes = current_source_hashes()
    changed = source_change_flags(state.get("source_hashes", {}), new_source_hashes)

    log("[1/7] Loading Fribb baseline...")
    fribb_rows = load_fribb_baseline()
    fribb_by_mal, fribb_by_anilist, fribb_by_anidb = make_indexes(fribb_rows)

    log("[2/7] Loading mapping sources...")
    base = load_base()
    api_mal, api_adb = load_animeapi()

    log("[3/7] Loading MAL catalog...")
    mal = fetch_mal_catalog()

    log("[4/7] Parsing AniDB titles...")
    adbidx = load_anidb_index()

    log("[5/7] Loading ScudLee mappings...")
    scud = load_scudlee()

    diag = {
        "build_mode": BUILD_MODE,
        "existing_records": len(existing_rows),
        "fribb_baseline_records": len(fribb_rows),
        "source_changes": changed,
        "new_mal_ids": [],
        "updated_mal_ids": [],
        "unchanged_mal_ids": [],
        "anilist_verified_ids": [],
        "anilist_conflicts_preserved_from_fribb": [],
        "fallback_title_matches": [],
        "mal_without_anidb_mapping": [],
        "fribb_rows_enriched": 0,
        "new_records_appended": 0,
        "fribb_updates_merged_into_previous": 0,
        "fribb_new_records_merged_into_previous": 0,
    }

    # Verify MAL IDs from BOTH universes. Fribb has MAL records that MAL seasonal
    # endpoints do not enumerate, and those must not be ignored.
    fribb_mal_ids = set(fribb_by_mal)
    mal_catalog_ids = set(mal)

    # Once OUR dataset exists, it is the primary working state. A Fribb update
    # is merged into it; it does NOT reset the dataset back to Fribb.
    effective_full = (
        BUILD_MODE == "full"
        or not existing_rows
    )

    if effective_full:
        anilist_verify_ids = sorted(fribb_mal_ids | mal_catalog_ids)
        next_anilist_cursor = 0
    else:
        existing_mal_ids_all = {
            parse_int(r.get("mal_id"))
            for r in existing_rows
            if parse_int(r.get("mal_id")) is not None
        }
        new_ids = sorted(mal_catalog_ids - existing_mal_ids_all)
        missing_anilist = sorted(
            parse_int(r.get("mal_id"))
            for r in existing_rows
            if parse_int(r.get("mal_id")) is not None
            and not parse_int(r.get("anilist_id"))
        )

        rolling_source = sorted(existing_mal_ids_all)
        cursor = int(state.get("anilist_cursor") or 0)
        rolling = []
        if rolling_source and ANILIST_DAILY_VERIFY > 0:
            for n in range(min(ANILIST_DAILY_VERIFY, len(rolling_source))):
                rolling.append(rolling_source[(cursor + n) % len(rolling_source)])
            next_anilist_cursor = (cursor + len(rolling)) % len(rolling_source)
        else:
            next_anilist_cursor = 0

        anilist_verify_ids = sorted(set(new_ids) | set(missing_anilist) | set(rolling))

    log(f"[6/7] AniList verification targets: {len(anilist_verify_ids)}")
    official_anilist = anilist_lookup_many(anilist_verify_ids)
    diag["anilist_verified_ids"] = sorted(official_anilist.keys())

    def build_candidate(mid, node=None):
        node = node or {}
        typ = MAL_TYPE_MAP.get((node.get("media_type") or "unknown").lower(), "UNKNOWN")

        row = None
        if mid in base:
            row = dict(base[mid])
            if row.get("type") in (None, "UNKNOWN") and typ != "UNKNOWN":
                row["type"] = typ
        else:
            ax = api_mal.get(mid)
            af = animeapi_fields(ax)
            aid = af.get("anidb_id")

            if not aid and node:
                aid = fallback_anidb(node, adbidx)
                if aid:
                    diag["fallback_title_matches"].append({
                        "mal_id": mid,
                        "anidb_id": aid,
                        "title": node.get("title"),
                    })

            row = {"type": typ, "mal_id": mid}
            if aid:
                row["anidb_id"] = aid
            merge_record(row, af)

        row["mal_id"] = mid
        aid = parse_int(row.get("anidb_id"))

        provider = api_mal.get(mid)
        if provider is None and aid:
            provider = api_adb.get(aid)
        merge_record(row, animeapi_fields(provider))

        official_id = official_anilist.get(mid)
        if official_id:
            row["anilist_id"] = official_id

        sm = dict(scud.get(aid, {}) if aid else {})
        raw = sm.pop("_raw_tmdb_ids", [])
        merge_record(row, sm)
        enrich_tmdb(row, raw)

        if not aid:
            diag["mal_without_anidb_mapping"].append({
                "mal_id": mid,
                "title": node.get("title") if node else None,
            })

        return canonicalize(row)

    def enrich_fribb_row(row):
        before = canonical_record_hash(row)
        mid = parse_int(row.get("mal_id"))
        adb = parse_int(row.get("anidb_id"))

        provider = api_mal.get(mid) if mid else None
        if provider is None and adb:
            provider = api_adb.get(adb)
        if provider:
            merge_preserving_baseline(row, animeapi_fields(provider))

        if mid and mid in official_anilist:
            official_id = official_anilist[mid]
            existing_id = parse_int(row.get("anilist_id"))
            if existing_id is None:
                row["anilist_id"] = official_id
            elif existing_id != official_id:
                # Strict superset policy: never overwrite an existing Fribb scalar.
                diag["anilist_conflicts_preserved_from_fribb"].append({
                    "mal_id": mid,
                    "fribb_anilist_id": existing_id,
                    "official_anilist_id": official_id,
                })

        if adb:
            sm = dict(scud.get(adb, {}))
            raw = sm.pop("_raw_tmdb_ids", [])
            merge_preserving_baseline(row, sm)

            # Run TMDB enrichment on a candidate copy, then add only missing data.
            tmdb_candidate = copy.deepcopy(row)
            enrich_tmdb(tmdb_candidate, raw)
            merge_preserving_baseline(row, tmdb_candidate)

        return canonical_record_hash(row) != before

    if effective_full:
        log("[7/7] Building Fribb-superset dataset...")

        # Strict invariant: the first N rows are the Fribb baseline, same order.
        rows = [copy.deepcopy(r) for r in fribb_rows]

        for i, row in enumerate(rows):
            if enrich_fribb_row(row):
                diag["fribb_rows_enriched"] += 1
            if (i + 1) % 5000 == 0:
                log(f"Fribb enrichment: {i+1}/{len(rows)}")

        cleanup = sanitize_identity_collisions(rows, fribb_rows)
        log("Identity cleanup after enrichment: " + ", ".join(f"{k}={v}" for k,v in cleanup.items()))
        by_mal, by_anilist, by_anidb = make_indexes(rows)

        for pos, mid in enumerate(sorted(mal_catalog_ids), 1):
            candidate = build_candidate(mid, mal.get(mid))
            match_idx = find_union_match(candidate, by_mal, by_anilist, by_anidb, rows)

            if match_idx is not None:
                existing = rows[match_idx]

                # If this is one of the immutable Fribb baseline rows, add-only.
                if match_idx < len(fribb_rows):
                    merge_preserving_baseline(existing, candidate)
                else:
                    # Non-Fribb appended rows can be refreshed from authoritative sources.
                    rows[match_idx] = candidate
            else:
                strip_candidate_identity_conflicts(candidate, by_mal, by_anilist, by_anidb, rows)
                rows.append(candidate)
                diag["new_records_appended"] += 1
                idx = len(rows) - 1
                mid2 = parse_int(candidate.get("mal_id"))
                aid2 = parse_int(candidate.get("anilist_id"))
                adb2 = parse_int(candidate.get("anidb_id"))
                if mid2 is not None:
                    by_mal[mid2] = idx
                if aid2 is not None:
                    by_anilist[aid2] = idx
                if adb2 is not None:
                    by_anidb[adb2] = idx

            if pos % 2500 == 0:
                log(f"MAL union: {pos}/{len(mal_catalog_ids)} | total={len(rows)}")

    else:
        log("[7/7] Smart incremental update: previous OUR JSON is primary...")
        rows = [copy.deepcopy(r) for r in existing_rows]

        # Merge the latest Fribb snapshot INTO our previous output. This keeps
        # OUR prior additions/enrichments while accepting Fribb changes/new rows.
        rows, fribb_merged, fribb_appended = reconcile_latest_fribb_into_previous(rows, fribb_rows)
        diag["fribb_updates_merged_into_previous"] = fribb_merged
        diag["fribb_new_records_merged_into_previous"] = fribb_appended
        log(f"Latest Fribb -> previous OUR: updated={fribb_merged}, appended={fribb_appended}, total={len(rows)}")

        by_mal, by_anilist, by_anidb = make_indexes(rows)

        target_ids = set()
        new_ids = mal_catalog_ids - set(by_mal)
        target_ids.update(new_ids)
        diag["new_mal_ids"] = sorted(new_ids)

        # If a non-Fribb mapping source changes, reconcile MAL-addressable rows.
        if (
            changed.get("base_mapping")
            or changed.get("animeapi")
            or changed.get("scudlee")
            or changed.get("anidb_titles")
        ):
            target_ids.update(mal_catalog_ids)

        target_ids.update(anilist_verify_ids)

        for pos, mid in enumerate(sorted(target_ids), 1):
            node = mal.get(mid)
            if node is None:
                # Fribb-only MAL IDs can still receive official AniList/provider fills.
                node = {}

            candidate = build_candidate(mid, node)
            match_idx = find_union_match(candidate, by_mal, by_anilist, by_anidb, rows)

            if match_idx is None:
                rows.append(candidate)
                diag["new_records_appended"] += 1
                diag["updated_mal_ids"].append(mid)
                by_mal, by_anilist, by_anidb = make_indexes(rows)
                continue

            old_hash = canonical_record_hash(rows[match_idx])

            # Determine whether this output row corresponds to a Fribb baseline identity.
            is_fribb = (
                mid in fribb_by_mal
                or (
                    parse_int(rows[match_idx].get("anilist_id")) is not None
                    and parse_int(rows[match_idx].get("anilist_id")) in fribb_by_anilist
                )
                or (
                    parse_int(rows[match_idx].get("anidb_id")) is not None
                    and parse_int(rows[match_idx].get("anidb_id")) in fribb_by_anidb
                )
            )

            if is_fribb:
                merge_preserving_baseline(rows[match_idx], candidate)
            else:
                rows[match_idx] = candidate

            new_hash = canonical_record_hash(rows[match_idx])
            if new_hash != old_hash:
                diag["updated_mal_ids"].append(mid)
            else:
                diag["unchanged_mal_ids"].append(mid)

            if pos % 2500 == 0:
                log(f"Incremental reconcile: {pos}/{len(target_ids)}")

    final_cleanup = sanitize_identity_collisions(rows, fribb_rows)
    log("Final identity cleanup: " + ", ".join(f"{k}={v}" for k,v in final_cleanup.items()))
    assert_unique_identity_ids(rows)

    # Strict guarantee: every current Fribb baseline record and every original
    # field/value remains represented at the beginning of the output in full mode.
    if effective_full:
        assert_fribb_prefix_preserved(fribb_rows, rows)
    else:
        # Every latest Fribb identity must still be represented after incremental merge.
        chk_mal, chk_anilist, chk_anidb = make_indexes(rows)
        missing_fribb = find_unrepresented_fribb_rows(fribb_rows, rows)
        if missing_fribb:
            sample = ", ".join(str(i) for i in missing_fribb[:10])
            raise RuntimeError(
                f"Latest Fribb reconciliation failed: {len(missing_fribb)} records not represented; "
                f"sample baseline rows: {sample}"
            )
        log(f"Latest Fribb reconciliation: PASS ({len(fribb_rows)} baseline rows represented)")

    validate(rows)

    record_hashes = {}
    for row in rows:
        mid = parse_int(row.get("mal_id"))
        if mid is not None:
            record_hashes[str(mid)] = canonical_record_hash(row)

    write_json(OUT_FULL, rows, True)
    write_json(OUT_MINI, rows, False)
    write_json(DIAG, diag, True)

    final_hash = sha256_file(OUT_FULL)

    fields = [
        "mal_id", "anilist_id", "anidb_id", "kitsu_id",
        "animenewsnetwork_id", "anime-planet_id", "anisearch_id",
        "livechart_id", "simkl_id", "animecountdown_id", "tvdb_id",
        "imdb_id", "themoviedb_id", "season", "episode_offset",
    ]

    meta = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "builder_version": BUILDER_VERSION,
        "build_mode": BUILD_MODE,
        "effective_full_reconciliation": effective_full,
        "schema": "Fribb anime-list-full compatible superset",
        "main_file_has_custom_fields": False,
        "source_changes": changed,
        "final_file_sha256": final_hash,
        "fribb_baseline": {
            "records": len(fribb_rows),
            "with_mal_id": sum("mal_id" in r for r in fribb_rows),
            "with_anilist_id": sum("anilist_id" in r for r in fribb_rows),
            "with_anidb_id": sum("anidb_id" in r for r in fribb_rows),
            "preservation_check": "passed" if effective_full else "not-required-in-incremental",
        },
        "anilist_official_verification": {
            "targets": len(anilist_verify_ids),
            "resolved": len(official_anilist),
            "rolling_daily_batch": ANILIST_DAILY_VERIFY,
        },
        "sources": [
            "Fribb/anime-lists anime-list-full.json (baseline)",
            "anime-and-manga/lists",
            "MAL Official API v2",
            "AniList GraphQL API",
            "AniDB title dump",
            "ScudLee/anime-lists",
            "AnimeAPI",
            "TMDB API",
        ],
        "counts": {
            "records": len(rows),
            **{f"with_{k}": sum(k in x for x in rows) for k in fields},
            "records_above_fribb_baseline": len(rows) - len(fribb_rows),
            "new_records_appended_this_run": diag["new_records_appended"],
            "fribb_rows_enriched": diag["fribb_rows_enriched"],
            "fribb_updates_merged_into_previous": diag["fribb_updates_merged_into_previous"],
            "fribb_new_records_merged_into_previous": diag["fribb_new_records_merged_into_previous"],
            "new_mal_ids_detected": len(diag["new_mal_ids"]),
            "records_updated": len(diag["updated_mal_ids"]),
            "records_unchanged_after_recheck": len(diag["unchanged_mal_ids"]),
            "anilist_verified": len(official_anilist),
            "anilist_conflicts_preserved_from_fribb": len(
                diag["anilist_conflicts_preserved_from_fribb"]
            ),
            "fallback_title_matches": len(diag["fallback_title_matches"]),
            "mal_without_anidb_mapping": len(diag["mal_without_anidb_mapping"]),
        },
    }
    write_json(META, meta, True)

    state_out = {
        "source_hashes": new_source_hashes,
        "record_hashes": record_hashes,
        "final_file_hash": final_hash,
        "last_generated_at": meta["generated_at"],
        "anilist_cursor": next_anilist_cursor,
        "fribb_baseline_hash": new_source_hashes.get("fribb_baseline"),
    }
    write_json(STATE, state_out, True)

    log(json.dumps(meta, indent=2))


if __name__=="__main__":
    main()
