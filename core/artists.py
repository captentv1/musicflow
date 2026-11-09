from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_API = "https://api.deezer.com"
_UA = {"User-Agent": "MusicFlow (https://github.com/captentv1/musicflow)", "Accept-Language": "en"}


def _get(file_path: str):
    for attempt in range(6):
        req = urllib.request.Request(_API + file_path, headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        err = d.get("error") if isinstance(d, dict) else None
        if not err:
            return d
        if (err or {}).get("code") == 4:
            time.sleep(1.5 + attempt)
            continue
        raise RuntimeError((err or {}).get("message") or "Deezer error")
    raise RuntimeError("Deezer is overloaded, try again in a moment.")


def _everything(file_path: str, max_items: int = 1000):
    items, url = [], file_path + ("&" if "?" in file_path else "?") + "limit=100"
    while url and len(items) < max_items:
        page = _get(url)
        items += page.get("data") or []
        next_item = page.get("next")
        url = next_item.replace(_API, "") if next_item else None
    return items


def _duration(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def _track_item(t, album=None, artist_name=""):
    alb = t.get("album") or album or {}
    title_text = t.get("title") or ""
    art = (t.get("artist") or {}).get("name") or artist_name
    return {
        "source": "deezer", "id": None, "url": None, "title": title_text, "uploader": art,
        "album": alb.get("title") or "", "duration": _duration(t.get("duration")),
        "thumbnail": alb.get("cover_medium") or "", "cover": alb.get("cover_xl") or alb.get("cover_big") or "",
        "query": f"{title_text} {art}".strip(), "rank_pos": t.get("rank") or 0,
    }


def search_artists(q: str, limit_n: int = 12):
    d = _get(f"/search/artist?q={urllib.parse.quote(q)}&limit={limit_n}")
    return [{"id": a["id"], "name": a.get("name") or "", "photo": a.get("picture_medium") or "",
             "fans": a.get("nb_fan") or 0, "albums": a.get("nb_album") or 0} for a in d.get("data") or []]


def discography(artist_id: int):
    a = _get(f"/artist/{artist_id}")
    albums = _everything(f"/artist/{artist_id}/albums", 500)
    top = _get(f"/artist/{artist_id}/top?limit=50").get("data") or []
    return {
        "artist_name": {"id": a["id"], "name": a.get("name") or "", "photo": a.get("picture_big") or a.get("picture_medium") or "",
                    "fans": a.get("nb_fan") or 0},
        "top": [_track_item(t, artist_name=a.get("name")) for t in top],
        "albums": [{"id": x["id"], "song_title": x.get("title") or "", "type": x.get("record_type") or "album",
                    "date": x.get("release_date") or "", "cover_art": x.get("cover_medium") or "",
                    "cover_art_hd": x.get("cover_xl") or ""} for x in albums],
    }


def album_titles(album_id: int):
    alb = _get(f"/album/{album_id}")
    track_entries = _everything(f"/album/{album_id}/tracks", 300)
    return [_track_item(t, album=alb, artist_name=(alb.get("artist") or {}).get("name") or "") for t in track_entries]


def albums_titles(ids: list[int]) -> dict:
    with ThreadPoolExecutor(max_workers=6) as pool:
        lists = list(pool.map(lambda i: _safe(album_titles, i), ids))
    return {str(i): l for i, l in zip(ids, lists)}


def _safe(f, *a):
    try:
        return f(*a)
    except Exception:
        return []
