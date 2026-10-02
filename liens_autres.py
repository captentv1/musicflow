"""Deezer and Apple Music links (PC + Android), no key or account.

- Deezer: track, album, playlist (public API api.deezer.com), short links
  deezer.page.link / link.deezer.com included.
- Apple Music: track and album (iTunes Lookup API). Apple Music playlists can't
  be read without a developer account.
The returned tracks have the same format as those of a Spotify link: the matching
YouTube track is picked at download time.
"""
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


def _url_finale(url: str) -> str:
    """Follows redirects (Deezer short links)."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.geturl()


def _duree(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def _item(source, titre, artiste, duree_s, pochette, pochette_hd="", album=""):
    return {
        "source": source, "id": None, "url": None,
        "title": titre or "", "uploader": artiste or "", "album": album or "",
        "duration": _duree(duree_s), "thumbnail": pochette or "", "cover": pochette_hd or pochette or "",
        "query": f"{titre} {artiste}".strip(),
    }


# ---------------- Deezer ----------------
def _deezer_piste(t, album=None):
    alb = t.get("album") or album or {}
    return _item("deezer", t.get("title"), (t.get("artist") or {}).get("name"), t.get("duration"),
                 alb.get("cover_medium"), alb.get("cover_xl") or alb.get("cover_big"), alb.get("title"))


def deezer(url: str):
    host = urllib.parse.urlparse(url).netloc.lower()
    if "page.link" in host or host.startswith("link."):
        url = _url_finale(url)
    m = re.search(r"/(track|album|playlist)/(\d+)", url)
    if not m:
        raise ValueError("Unrecognized Deezer link (expected a track, album or playlist).")
    genre, ident = m.groups()
    if genre == "track":
        t = _get_json(f"https://api.deezer.com/track/{ident}")
        if t.get("error"):
            raise ValueError("Deezer track not found.")
        return [_deezer_piste(t)], None
    meta = _get_json(f"https://api.deezer.com/{genre}/{ident}")
    if meta.get("error"):
        raise ValueError("Deezer album or playlist not found (private?).")
    pistes, suivant = [], f"https://api.deezer.com/{genre}/{ident}/tracks?limit=500"
    while suivant and len(pistes) < 5000:
        page = _get_json(suivant)
        pistes += page.get("data") or []
        suivant = page.get("next")
    album = meta if genre == "album" else None
    items = [_deezer_piste(t, album) for t in pistes if t.get("title")]
    if not items:
        raise ValueError("No tracks found in this Deezer link.")
    return items, meta.get("title") or "Deezer"


# ---------------- Apple Music ----------------
def _hd(url: str) -> str:
    return re.sub(r"/\d+x\d+bb\.(jpg|png)$", r"/1000x1000bb.\1", url or "")


def _apple_piste(r):
    return _item("apple", r.get("trackName"), r.get("artistName"), (r.get("trackTimeMillis") or 0) / 1000,
                 (r.get("artworkUrl100") or "").replace("100x100", "300x300"), _hd(r.get("artworkUrl100")),
                 r.get("collectionName"))


def apple(url: str):
    p = urllib.parse.urlparse(url)
    pays = (p.path.strip("/").split("/") or ["fr"])[0][:2] or "fr"
    qs = urllib.parse.parse_qs(p.query)
    if "/playlist/" in p.path:
        raise ValueError("Apple Music playlists can't be read without an account: "
                         "use an album or track link, or the same playlist on Spotify/Deezer.")
    if qs.get("i"):                              # track inside an album
        res = _get_json(f"https://itunes.apple.com/lookup?id={qs['i'][0]}&country={pays}").get("results") or []
        if not res:
            raise ValueError("Apple Music track not found.")
        return [_apple_piste(res[0])], None
    m = re.search(r"/(album|song)/[^/]*/?(\d+)", p.path) or re.search(r"/(album|song)/(\d+)", p.path)
    if not m:
        raise ValueError("Unrecognized Apple Music link (expected an album or track).")
    genre, ident = m.groups()
    res = _get_json(f"https://itunes.apple.com/lookup?id={ident}&entity=song&limit=200&country={pays}").get("results") or []
    if genre == "song":
        chansons = [r for r in res if r.get("wrapperType") == "track"]
        if not chansons:
            raise ValueError("Apple Music track not found.")
        return [_apple_piste(chansons[0])], None
    nom = next((r.get("collectionName") for r in res if r.get("wrapperType") == "collection"), "Apple Music")
    items = [_apple_piste(r) for r in res if r.get("wrapperType") == "track"]
    if not items:
        raise ValueError("No tracks found in this Apple Music album.")
    return items, nom


def resoudre(url: str):
    """(items, name) for a Deezer/Apple Music link, or None if it isn't one."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if "deezer" in host:
        return deezer(url)
    if "music.apple.com" in host or "itunes.apple.com" in host:
        return apple(url)
    return None
