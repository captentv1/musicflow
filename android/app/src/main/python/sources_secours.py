"""Fallback sources when the YouTube search finds no reliable video.

Many tracks exist on Spotify but have no regular YouTube upload (or only a
blocked/removed one). They are usually still available:
  - on YouTube Music, as an official catalog track (auto-generated "Topic"
    upload, not listed by the normal YouTube search);
  - on SoundCloud, uploaded by the artist or the label.

Both are searched with yt-dlp, and the results are returned in the same format
as app.search_videos, so choix_video can score them alongside YouTube videos.
SoundCloud previews (30 s excerpts of Go+ tracks) are dropped.
"""
from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor


def _format_duree(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _yt_dlp():
    import yt_dlp
    return yt_dlp


_OPTS_FLAT = {"extract_flat": "in_playlist", "skip_download": True, "quiet": True, "no_warnings": True}


def _details_ytmusic(entree: dict) -> dict | None:
    """Full info of a YouTube Music track: the search page gives neither the
    duration nor the artist, both needed to score it."""
    vid = entree.get("id")
    if not vid:
        return None
    try:
        with _yt_dlp().YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True,
                                  "noplaylist": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
    except Exception:
        return None
    artistes = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    artiste = ", ".join(artistes) or info.get("channel") or info.get("uploader") or ""
    thumbs = info.get("thumbnails") or []
    return {
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": info.get("track") or info.get("title") or entree.get("title") or "",
        # " - Topic" marks official catalog audio for choix_video (same bonus).
        "uploader": f"{artiste} - Topic",
        "duration": _format_duree(info.get("duration")),
        "thumbnail": info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else ""),
        "source_audio": "YouTube Music",
    }


def chercher_ytmusic(requete: str, limite: int = 3) -> list[dict]:
    url = "https://music.youtube.com/search?q=" + urllib.parse.quote(requete) + "#songs"
    try:
        with _yt_dlp().YoutubeDL(dict(_OPTS_FLAT, playlistend=limite)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return []
    entrees = [e for e in (info.get("entries") or []) if e.get("id")][:limite]
    if not entrees:
        return []
    with ThreadPoolExecutor(max_workers=len(entrees)) as pool:
        return [r for r in pool.map(_details_ytmusic, entrees) if r]


def chercher_soundcloud(requete: str, limite: int = 6, duree_attendue_s: int = 0) -> list[dict]:
    try:
        with _yt_dlp().YoutubeDL(_OPTS_FLAT) as ydl:
            info = ydl.extract_info(f"scsearch{limite}:{requete}", download=False)
    except Exception:
        return []
    resultats = []
    for e in info.get("entries") or []:
        duree = e.get("duration") or 0
        # Go+ tracks: only a 30 s preview can be downloaded.
        if duree and duree <= 31 and (duree_attendue_s == 0 or duree_attendue_s > 45):
            continue
        thumbs = e.get("thumbnails") or []
        resultats.append({
            "id": "sc" + str(e.get("id") or ""),
            "url": e.get("webpage_url") or e.get("url") or "",
            "title": e.get("title") or "",
            "uploader": e.get("uploader") or ", ".join(e.get("artists") or []),
            "duration": _format_duree(duree),
            "thumbnail": thumbs[-1]["url"] if thumbs else "",
            "source_audio": "SoundCloud",
        })
    return [r for r in resultats if r["url"]]


def requete_secours(titre: str, artiste: str) -> str:
    import choix_video
    return f"{choix_video._titre_essentiel(titre)} {(artiste or '').split(',')[0].strip()}".strip()


def meilleur_secours(titre: str, artiste: str, duree, exclure=()):
    """(best candidate, score) over the fallback sources, scored like YouTube videos.
    YouTube Music first; SoundCloud only if it found nothing reliable."""
    import choix_video
    import verif_audio
    requete = requete_secours(titre, artiste)
    meilleur, note = None, float("-inf")
    for chercher in (lambda: chercher_ytmusic(requete),
                     lambda: chercher_soundcloud(requete, duree_attendue_s=verif_audio.secondes(duree))):
        candidats = [c for c in chercher() if c.get("id") not in exclure]
        m, n = choix_video.choisir(candidats, titre, artiste, duree, requete)
        if m and n > note:
            meilleur, note = m, n
        if choix_video.fiable(meilleur, titre, note):
            break
    return meilleur, note
