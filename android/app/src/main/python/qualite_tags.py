"""Tags et contrôles complémentaires, communs PC / Android.

- Titre/artiste et pochette pour M4A (AAC) et Opus, en plus de MP3/FLAC/WAV.
- Nettoyage optionnel des titres : « (feat. X) » déplacé dans l'artiste, MAJUSCULES
  ou minuscules remises en casse normale.
- Saturation : analyse de la sortie ffmpeg « volumedetect ».
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]", re.IGNORECASE)
_PETITS = {"a", "an", "and", "the", "of", "in", "on", "at", "to", "for", "de", "du", "des", "la", "le",
           "les", "et", "en", "un", "une", "y", "el", "los", "las", "di", "da"}


def nettoyer(titre: str, artiste: str, feat: bool = False, casse: bool = False):
    """Retourne (titre, artiste) nettoyés selon les options."""
    titre, artiste = titre or "", artiste or ""
    if feat:
        m = _FEAT.search(titre)
        if m:
            invites = [x.strip() for x in re.split(r",|&| x | and ", m.group(1)) if x.strip()]
            titre = _FEAT.sub("", titre).strip()
            deja = {a.strip().lower() for a in artiste.split(",")}
            ajout = [i for i in invites if i.lower() not in deja]
            if ajout:
                artiste = ", ".join([artiste] + ajout) if artiste else ", ".join(ajout)
    if casse:
        def recasser(t):
            lettres = [c for c in t if c.isalpha()]
            if len(lettres) < 4 or not (t.isupper() or t.islower()):
                return t
            mots = t.lower().split(" ")
            return " ".join(m if (i and m in _PETITS) else m[:1].upper() + m[1:] for i, m in enumerate(mots))
        titre, artiste = recasser(titre), recasser(artiste)
    return titre, artiste


def ecrire_base(chemin, titre: str, artiste: str) -> str:
    """Titre/artiste pour .m4a et .opus/.ogg. "" si OK."""
    ext = Path(chemin).suffix.lower()
    try:
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(chemin))
            if titre:
                f["\xa9nam"] = [titre]
            if artiste:
                f["\xa9ART"] = [artiste]
                f["aART"] = [artiste]
            f.save()
        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            f = OggOpus(str(chemin))
            if titre:
                f["TITLE"] = [titre]
            if artiste:
                f["ARTIST"] = [artiste]
                f["ALBUMARTIST"] = [artiste]
            f.save()
        else:
            return f"format non pris en charge : {ext}"
    except Exception as exc:
        return f"écriture des métadonnées : {exc}"
    return ""


def pochette(chemin, data: bytes, mime: str = "image/jpeg") -> str:
    """Pochette pour .m4a et .opus/.ogg. "" si OK."""
    ext = Path(chemin).suffix.lower()
    try:
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(str(chemin))
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            f["covr"] = [MP4Cover(data, imageformat=fmt)]
            f.save()
        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            from mutagen.flac import Picture
            f = OggOpus(str(chemin))
            pic = Picture()
            pic.data, pic.type, pic.mime = data, 3, mime
            f["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
            f.save()
        else:
            return f"format non pris en charge : {ext}"
    except Exception as exc:
        return f"pochette : {exc}"
    return ""


def saturation(sortie_ffmpeg: str) -> str:
    """"" si le son est propre, sinon un avertissement (d'après ffmpeg volumedetect)."""
    m_max = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", sortie_ffmpeg or "")
    m_0db = re.search(r"histogram_0db:\s*(\d+)", sortie_ffmpeg or "")
    m_moy = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", sortie_ffmpeg or "")
    if m_moy and float(m_moy.group(1)) < -40:
        return "son presque inaudible"
    m_n = re.search(r"n_samples:\s*(\d+)", sortie_ffmpeg or "")
    if m_max and float(m_max.group(1)) >= 0 and m_0db and m_n and int(m_n.group(1)):
        # Les masters modernes touchent souvent 0 dB : on ne signale qu'un écrêtage massif.
        if int(m_0db.group(1)) / int(m_n.group(1)) > 0.01:
            return "son saturé"
    return ""
