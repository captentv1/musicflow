from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MusicFlow"}


def _get_json(url: str):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _final_url(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.geturl()


def _duration(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def _item(source, title_text, artist_name, duration_s, cover_art, cover_hd="", album=""):
    return {
        "source": source, "id": None, "url": None,
        "title": title_text or "", "uploader": artist_name or "", "album": album or "",
        "duration": _duration(duration_s), "thumbnail": cover_art or "", "cover": cover_hd or cover_art or "",
        "query": f"{title_text} {artist_name}".strip(),
    }


def _deezer_track(t, album=None):
    alb = t.get("album") or album or {}
    return _item("deezer", t.get("title"), (t.get("artist") or {}).get("name"), t.get("duration"),
                 alb.get("cover_medium"), alb.get("cover_xl") or alb.get("cover_big"), alb.get("title"))


def deezer(url: str):
    host = urllib.parse.urlparse(url).netloc.lower()
    if "page.link" in host or host.startswith("link."):
        url = _final_url(url)
    m = re.search(r"/(track|album|playlist)/(\d+)", url)
    if not m:
        raise ValueError("Unrecognized Deezer link (expected a track, album or playlist).")
    genre, track_id = m.groups()
    if genre == "track":
        t = _get_json(f"https://api.deezer.com/track/{track_id}")
        if t.get("error"):
            raise ValueError("Deezer track not found.")
        return [_deezer_track(t)], None
    meta = _get_json(f"https://api.deezer.com/{genre}/{track_id}")
    if meta.get("error"):
        raise ValueError("Deezer album or playlist not found (private?).")
    track_entries, next_item = [], f"https://api.deezer.com/{genre}/{track_id}/tracks?limit=500"
    while next_item and len(track_entries) < 5000:
        page = _get_json(next_item)
        track_entries += page.get("data") or []
        next_item = page.get("next")
    album = meta if genre == "album" else None
    items = [_deezer_track(t, album) for t in track_entries if t.get("title")]
    if not items:
        raise ValueError("No tracks found in this Deezer link.")
    return items, meta.get("title") or "Deezer"


def _hd(url: str) -> str:
    return re.sub(r"/\d+x\d+bb\.(jpg|png)$", r"/1000x1000bb.\1", url or "")


def _apple_track(r):
    return _item("apple", r.get("trackName"), r.get("artistName"), (r.get("trackTimeMillis") or 0) / 1000,
                 (r.get("artworkUrl100") or "").replace("100x100", "300x300"), _hd(r.get("artworkUrl100")),
                 r.get("collectionName"))


def apple(url: str):
    p = urllib.parse.urlparse(url)
    country = (p.path.strip("/").split("/") or ["fr"])[0][:2] or "fr"
    qs = urllib.parse.parse_qs(p.query)
    if "/playlist/" in p.path:
        raise ValueError("Apple Music playlists can't be read without an account: "
                         "use an album or track link, or the same playlist on Spotify/Deezer.")
    if qs.get("i"):
        res = _get_json(f"https://itunes.apple.com/lookup?id={qs['i'][0]}&country={country}").get("results") or []
        if not res:
            raise ValueError("Apple Music track not found.")
        return [_apple_track(res[0])], None
    m = re.search(r"/(album|song)/[^/]*/?(\d+)", p.path) or re.search(r"/(album|song)/(\d+)", p.path)
    if not m:
        raise ValueError("Unrecognized Apple Music link (expected an album or track).")
    genre, track_id = m.groups()
    res = _get_json(f"https://itunes.apple.com/lookup?id={track_id}&entity=song&limit=200&country={country}").get("results") or []
    if genre == "song":
        songs = [r for r in res if r.get("wrapperType") == "track"]
        if not songs:
            raise ValueError("Apple Music track not found.")
        return [_apple_track(songs[0])], None
    name_ = next((r.get("collectionName") for r in res if r.get("wrapperType") == "collection"), "Apple Music")
    items = [_apple_track(r) for r in res if r.get("wrapperType") == "track"]
    if not items:
        raise ValueError("No tracks found in this Apple Music album.")
    return items, name_


def resolve_target(url: str):
    host = urllib.parse.urlparse(url).netloc.lower()
    if "deezer" in host:
        return deezer(url)
    if "music.apple.com" in host or "itunes.apple.com" in host:
        return apple(url)
    return None
