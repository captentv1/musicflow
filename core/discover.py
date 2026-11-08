from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

import artists

_UA = {"User-Agent": "MusicFlow (https://github.com/captentv1/musicflow)", "Accept-Language": "en"}


def top(limit_n: int = 100):
    return [artists._track_item(t) for t in artists._everything("/chart/0/tracks", limit_n)]


def popular_playlists(limit_n: int = 30):
    d = artists._get(f"/chart/0/playlists?limit={limit_n}")
    return [{"id": p["id"], "titre": p.get("title") or "", "pochette": p.get("picture_medium") or "",
             "nb": p.get("nb_tracks") or 0, "url": f"https://www.deezer.com/playlist/{p['id']}"} for p in d.get("data") or []]


def genres():
    d = artists._get("/genre")
    return [{"id": g["id"], "nom": g.get("name") or "", "image": g.get("picture_medium") or ""}
            for g in d.get("data") or [] if g.get("id")]


def genre(genre_id: int, limit_n: int = 100):
    return [artists._track_item(t) for t in artists._everything(f"/chart/{int(genre_id)}/tracks", limit_n)]


COUNTRY = {"fr": "France", "tn": "Tunisia", "dz": "Algeria", "ma": "Morocco", "be": "Belgium", "ca": "Canada",
        "ch": "Switzerland", "us": "United States", "gb": "United Kingdom", "de": "Germany", "es": "Spain",
        "it": "Italy", "sa": "Saudi Arabia", "ae": "UAE", "eg": "Egypt", "br": "Brazil", "jp": "Japan"}


_COUNTRY_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 3600


def top_country(country: str = "fr", limit_n: int = 100):
    country = country if country in COUNTRY else "fr"
    cache = _COUNTRY_CACHE.get(country)
    if cache and time.time() - cache[0] < _CACHE_TTL:
        return cache[1]
    res, latest = None, None
    for n in (min(int(limit_n), 100), 50):
        url = f"https://rss.marketingtools.apple.com/api/v2/{country}/music/most-played/{n}/songs.json"
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=8) as r:
                res = (json.loads(r.read().decode("utf-8")).get("feed") or {}).get("results") or []
            break
        except Exception as exc:
            latest = exc
    if res is None:
        if cache:
            return cache[1]
        raise RuntimeError(f"chart unavailable for now ({latest})")
    out = []
    for x in res:
        img = (x.get("artworkUrl100") or "")
        out.append({"source": "apple", "id": None, "url": None, "title": x.get("name") or "", "uploader": x.get("artistName") or "",
                    "album": "", "duration": "", "thumbnail": img.replace("100x100", "300x300"),
                    "cover": img.replace("100x100", "1000x1000"), "query": f"{x.get('name')} {x.get('artistName')}".strip()})
    _COUNTRY_CACHE[country] = (time.time(), out)
    return out


def similar(artist_id: int):
    d = artists._get(f"/artist/{artist_id}/related?limit=20")
    return [{"id": a["id"], "nom": a.get("name") or "", "photo": a.get("picture_medium") or "",
             "fans": a.get("nb_fan") or 0, "albums": a.get("nb_album") or 0} for a in d.get("data") or []]


def mix(artist_id: int):
    return [artists._track_item(t) for t in artists._get(f"/artist/{artist_id}/radio?limit=50").get("data") or []]
