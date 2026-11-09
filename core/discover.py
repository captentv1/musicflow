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
    return [{"id": p["id"], "song_title": p.get("title") or "", "cover_art": p.get("picture_medium") or "",
             "nb": p.get("nb_tracks") or 0, "url": f"https://www.deezer.com/playlist/{p['id']}"} for p in d.get("data") or []]


def genres():
    d = artists._get("/genre")
    return [{"id": g["id"], "name": g.get("name") or "", "image": g.get("picture_medium") or ""}
            for g in d.get("data") or [] if g.get("id")]


def genre(genre_id: int, limit_n: int = 100):
    return [artists._track_item(t) for t in artists._everything(f"/chart/{int(genre_id)}/tracks", limit_n)]



def similar(artist_id: int):
    d = artists._get(f"/artist/{artist_id}/related?limit=20")
    return [{"id": a["id"], "name": a.get("name") or "", "photo": a.get("picture_medium") or "",
             "fans": a.get("nb_fan") or 0, "albums": a.get("nb_album") or 0} for a in d.get("data") or []]


def mix(artist_id: int):
    return [artists._track_item(t) for t in artists._get(f"/artist/{artist_id}/radio?limit=50").get("data") or []]
