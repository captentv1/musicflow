"""Metadata taken from the downloaded source itself (yt-dlp info), shared by PC / Android.

Used when the online lookups (Spotify, iTunes, Deezer) found nothing: without it,
the file was saved with no artist, no album and no cover art, and players showed
“Unknown” everywhere. YouTube Music uploads carry the real track, artists, album
and year; other videos at least give the channel and the upload year.
"""
from __future__ import annotations

import re

import verif_audio

_CHAINE_RE = re.compile(r"\s*(-\s*topic|vevo|official|officiel)\s*$", re.IGNORECASE)


def artiste_source(info: dict) -> str:
    artistes = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    if artistes:
        return ", ".join(artistes)
    brut = info.get("creator") or info.get("channel") or info.get("uploader") or ""
    return _CHAINE_RE.sub("", brut).strip()


def completer_titre_artiste(info: dict, titre: str, artiste: str):
    """Fills in a missing title/artist from the source."""
    info = info or {}
    if not artiste:
        artiste = artiste_source(info)
    if not titre:
        titre = info.get("track") or info.get("title") or ""
    return titre, artiste


def infos_album_secours(info: dict, titre: str, artiste: str, album_hint: str = "") -> dict:
    """Album info when iTunes/Deezer found nothing: the source's album if known,
    otherwise the track is tagged as a single (album = title), as streaming
    services do."""
    info = info or {}
    annee = str(info.get("release_year") or "") or (info.get("upload_date") or "")[:4]
    return {
        "album": info.get("album") or album_hint or titre,
        "artiste_album": (artiste or "").split(",")[0].strip(),
        "annee": annee,
        "genre": (info.get("genres") or [""])[0] if isinstance(info.get("genres"), list) else "",
        "piste": 1 if not (info.get("album") or album_hint) else 0,
        "pistes": 1 if not (info.get("album") or album_hint) else 0,
    }


def miniature_jpg(info: dict) -> str:
    """Best JPEG thumbnail of the source (players do not display WebP cover art)."""
    info = info or {}
    vid = info.get("id") or ""
    if info.get("extractor_key", "").lower().startswith("youtube") and vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    for t in reversed(info.get("thumbnails") or []):
        url = t.get("url") or ""
        if url.split("?")[0].lower().endswith((".jpg", ".jpeg", ".png")):
            return url
    url = info.get("thumbnail") or ""
    return "" if ".webp" in url.lower() else url


def refuser_extrait(chemin, duree_attendue) -> None:
    """Raises if the file is only an excerpt (SoundCloud Go+ 30 s preview, cut
    upload): better try another source than save half a song."""
    attendue = verif_audio.secondes(duree_attendue)
    reelle = verif_audio.duree_fichier(chemin)
    if attendue >= 60 and 0 < reelle < attendue * 0.5:
        try:
            from pathlib import Path
            Path(chemin).unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"only an excerpt was available ({int(reelle)} s instead of {attendue} s)")
