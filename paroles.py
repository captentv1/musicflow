"""Paroles des morceaux téléchargés (PC et Android), via LRCLIB — gratuit, sans clé.

Les paroles sont écrites DANS le fichier (USLT pour MP3/WAV, LYRICS pour FLAC/Opus,
©lyr pour M4A) et, quand LRCLIB a une version synchronisée, dans un .lrc à côté du
fichier : la plupart des lecteurs (Samsung Music, Poweramp, foobar2000…) l'affichent
en défilant au rythme de la musique.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

_API = "https://lrclib.net/api"
_UA = "MusicFlow (https://github.com/captentv1/musicflow)"


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def chercher(titre: str, artiste: str, duree_s: int = 0) -> dict | None:
    """{"plain": str, "synced": str} ou None si rien n'est trouvé."""
    if not titre:
        return None
    params = {"track_name": titre, "artist_name": artiste or ""}
    if duree_s:
        params["duration"] = str(int(duree_s))
    resultat = None
    try:
        resultat = _get(f"{_API}/get?" + urllib.parse.urlencode(params))
    except Exception:
        resultat = None
    if not resultat or not (resultat.get("plainLyrics") or resultat.get("syncedLyrics")):
        try:
            q = urllib.parse.urlencode({"q": f"{titre} {artiste}".strip()})
            liste = _get(f"{_API}/search?{q}") or []
            resultat = next((r for r in liste if r.get("plainLyrics") or r.get("syncedLyrics")), None)
        except Exception:
            resultat = None
    if not resultat:
        return None
    plain = resultat.get("plainLyrics") or ""
    synced = resultat.get("syncedLyrics") or ""
    if not plain and synced:
        # Version texte tirée de la version synchronisée (on retire les [mm:ss.xx]).
        plain = "\n".join(l.split("]", 1)[-1].strip() for l in synced.splitlines())
    if resultat.get("instrumental") or not (plain or synced):
        return None
    return {"plain": plain, "synced": synced}


def integrer(chemin, paroles: dict) -> str:
    """Écrit les paroles dans le fichier (+ .lrc). Retourne "" si tout va bien."""
    chemin = Path(chemin)
    texte = paroles.get("plain") or ""
    ext = chemin.suffix.lower()
    try:
        if ext in (".mp3", ".wav"):
            from mutagen.id3 import ID3, USLT, ID3NoHeaderError
            if ext == ".wav":
                from mutagen.wave import WAVE
                w = WAVE(str(chemin))
                if w.tags is None:
                    w.add_tags()
                w.tags.add(USLT(encoding=3, lang="und", desc="", text=texte))
                w.save()
            else:
                try:
                    id3 = ID3(str(chemin))
                except ID3NoHeaderError:
                    id3 = ID3()
                id3.add(USLT(encoding=3, lang="und", desc="", text=texte))
                id3.save(str(chemin))
        elif ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(str(chemin))
            f["LYRICS"] = [texte]
            f.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(chemin))
            f["\xa9lyr"] = [texte]
            f.save()
        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            f = OggOpus(str(chemin))
            f["LYRICS"] = [texte]
            f.save()
        else:
            return f"format non pris en charge : {ext}"
    except Exception as exc:
        return f"écriture des paroles : {exc}"
    if paroles.get("synced"):
        try:
            chemin.with_suffix(".lrc").write_text(paroles["synced"], encoding="utf-8")
        except Exception:
            pass
    return ""
