"""Infos d'album pour les lecteurs (Samsung Music…) : album, genre, n° de piste, disque,
année, artiste de l'album — sinon Samsung Music affiche le nom du dossier comme album,
« Inconnu » comme genre et 0 comme numéro de piste.

Sources gratuites, sans clé : iTunes Search (une requête suffit), puis Deezer en repli.
On ne garde un résultat que si le titre ET l'artiste correspondent (une reprise du même
titre par un autre artiste ne doit pas donner son album).
"""
from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

_UA = {"User-Agent": "MusicFlow (https://github.com/captentv1/musicflow)"}


def _get(url: str):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]|\s-\s.*$", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _correspond(titre, artiste, t2, a2) -> bool:
    n1, n2 = _norm(titre), _norm(t2)
    if not n1 or not n2 or not (n1 in n2 or n2 in n1):
        return False
    a1 = _norm((artiste or "").split(",")[0])
    return not a1 or a1 in _norm(a2) or _norm(a2) in a1


def _album_propre(nom: str) -> str:
    return re.sub(r"\s-\s(Single|EP)$", "", nom or "").strip()


def _itunes(titre, artiste, duree_s):
    q = urllib.parse.urlencode({"term": f"{titre} {artiste}".strip(), "entity": "song", "limit": 10})
    res = (_get(f"https://itunes.apple.com/search?{q}") or {}).get("results") or []
    bons = [r for r in res if _correspond(titre, artiste, r.get("trackName"), r.get("artistName"))]
    if not bons:
        return None
    if duree_s:
        bons.sort(key=lambda r: abs((r.get("trackTimeMillis") or 0) / 1000 - duree_s))
    r = bons[0]
    return {
        "album": _album_propre(r.get("collectionName")),
        "genre": r.get("primaryGenreName") or "",
        "piste": r.get("trackNumber") or 0,
        "pistes": r.get("trackCount") or 0,
        "disque": r.get("discNumber") or 0,
        "disques": r.get("discCount") or 0,
        "annee": (r.get("releaseDate") or "")[:4],
        "artiste_album": r.get("collectionArtistName") or r.get("artistName") or "",
    }


def _deezer(titre, artiste, duree_s):
    bons = []
    for requete in (f'track:"{titre}" artist:"{artiste}"' if artiste else titre, f"{titre} {artiste}".strip()):
        q = urllib.parse.urlencode({"q": requete})
        data = (_get(f"https://api.deezer.com/search?{q}") or {}).get("data") or []
        bons = [d for d in data if _correspond(titre, artiste, d.get("title"), (d.get("artist") or {}).get("name"))]
        if bons:
            break
    if not bons:
        return None
    if duree_s:
        bons.sort(key=lambda d: abs((d.get("duration") or 0) - duree_s))
    piste = _get(f"https://api.deezer.com/track/{bons[0]['id']}")
    album = _get(f"https://api.deezer.com/album/{(piste.get('album') or {}).get('id')}") if piste.get("album") else {}
    genres = ((album.get("genres") or {}).get("data")) or []
    return {
        "album": _album_propre((piste.get("album") or {}).get("title")),
        "genre": genres[0]["name"] if genres else "",
        "piste": piste.get("track_position") or 0,
        "pistes": album.get("nb_tracks") or 0,
        "disque": piste.get("disk_number") or 0,
        "disques": 0,
        "annee": (piste.get("release_date") or album.get("release_date") or "")[:4],
        "artiste_album": (album.get("artist") or {}).get("name") or "",
    }


def chercher(titre: str, artiste: str, duree_s: int = 0) -> dict | None:
    for source in (_itunes, _deezer):
        try:
            infos = source(titre, artiste, duree_s)
        except Exception:
            infos = None
        if infos and infos.get("album"):
            return infos
    return None


def integrer(chemin, infos: dict) -> str:
    """Écrit album, genre, n° de piste/disque, année, artiste d'album. "" si OK."""
    chemin = Path(chemin)
    ext = chemin.suffix.lower()
    piste = f"{infos['piste']}/{infos['pistes']}" if infos.get("pistes") else str(infos.get("piste") or "")
    disque = f"{infos['disque']}/{infos['disques']}" if infos.get("disques") else str(infos.get("disque") or "")
    try:
        if ext in (".mp3", ".wav"):
            from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TCON, TRCK, TPOS, TDRC, TPE2
            if ext == ".wav":
                from mutagen.wave import WAVE
                f = WAVE(str(chemin))
                if f.tags is None:
                    f.add_tags()
                tags = f.tags
            else:
                try:
                    tags = ID3(str(chemin))
                except ID3NoHeaderError:
                    tags = ID3()
            for cadre, val in ((TALB, infos.get("album")), (TCON, infos.get("genre")), (TRCK, piste),
                               (TPOS, disque), (TDRC, infos.get("annee")), (TPE2, infos.get("artiste_album"))):
                if val and val != "0":
                    tags.add(cadre(encoding=3, text=str(val)))
            if ext == ".wav":
                f.save()
            else:
                tags.save(str(chemin))
        elif ext in (".flac", ".opus", ".ogg"):
            if ext == ".flac":
                from mutagen.flac import FLAC
                f = FLAC(str(chemin))
            else:
                from mutagen.oggopus import OggOpus
                f = OggOpus(str(chemin))
            champs = {"ALBUM": infos.get("album"), "GENRE": infos.get("genre"),
                      "TRACKNUMBER": infos.get("piste"), "TRACKTOTAL": infos.get("pistes"),
                      "DISCNUMBER": infos.get("disque"), "DISCTOTAL": infos.get("disques"),
                      "DATE": infos.get("annee"), "ALBUMARTIST": infos.get("artiste_album")}
            for cle, val in champs.items():
                if val and str(val) != "0":
                    f[cle] = [str(val)]
            f.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(chemin))
            if infos.get("album"):
                f["\xa9alb"] = [infos["album"]]
            if infos.get("genre"):
                f["\xa9gen"] = [infos["genre"]]
            if infos.get("annee"):
                f["\xa9day"] = [infos["annee"]]
            if infos.get("artiste_album"):
                f["aART"] = [infos["artiste_album"]]
            if infos.get("piste"):
                f["trkn"] = [(int(infos["piste"]), int(infos.get("pistes") or 0))]
            if infos.get("disque"):
                f["disk"] = [(int(infos["disque"]), int(infos.get("disques") or 0))]
            f.save()
        else:
            return f"format non pris en charge : {ext}"
    except Exception as exc:
        return f"écriture des infos d'album : {exc}"
    return ""
