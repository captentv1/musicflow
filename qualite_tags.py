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
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(str(chemin))
            pic = Picture()
            pic.data, pic.type, pic.mime = data, 3, mime
            f.clear_pictures()
            f.add_picture(pic)
            f.save()
        elif ext in (".mp3", ".wav"):
            from mutagen.id3 import ID3, APIC, ID3NoHeaderError
            if ext == ".wav":
                from mutagen.wave import WAVE
                f = WAVE(str(chemin))
                if f.tags is None:
                    f.add_tags()
                f.tags.delall("APIC")
                f.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                f.save()
            else:
                try:
                    t = ID3(str(chemin))
                except ID3NoHeaderError:
                    t = ID3()
                t.delall("APIC")
                t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                t.save(str(chemin))
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


# ---------------- Lecture / écriture génériques (bibliothèque, éditeur de tags) ----------------
_CHAMPS_ID3 = {"titre": "TIT2", "artiste": "TPE1", "album": "TALB", "genre": "TCON", "annee": "TDRC",
               "piste": "TRCK", "artiste_album": "TPE2"}
_CHAMPS_VORBIS = {"titre": "TITLE", "artiste": "ARTIST", "album": "ALBUM", "genre": "GENRE", "annee": "DATE",
                  "piste": "TRACKNUMBER", "artiste_album": "ALBUMARTIST"}
_CHAMPS_MP4 = {"titre": "©nam", "artiste": "©ART", "album": "©alb", "genre": "©gen", "annee": "©day",
               "artiste_album": "aART"}


def lire(chemin) -> dict:
    """Tags principaux + durée, pochette et paroles présentes ?"""
    import mutagen
    ext = Path(chemin).suffix.lower()
    out = {"titre": "", "artiste": "", "album": "", "genre": "", "annee": "", "piste": "", "artiste_album": "",
           "duree": 0, "pochette": False, "paroles": False}
    try:
        f = mutagen.File(str(chemin))
    except Exception:
        return out
    if f is None:
        return out
    out["duree"] = int(getattr(f.info, "length", 0) or 0)
    t = f.tags
    if t is None:
        return out
    if ext in (".mp3", ".wav"):
        for cle, cadre in _CHAMPS_ID3.items():
            if cadre in t:
                out[cle] = str(t[cadre].text[0]) if t[cadre].text else ""
        out["pochette"] = any(k.startswith("APIC") for k in t.keys())
        out["paroles"] = any(k.startswith("USLT") for k in t.keys())
    elif ext in (".flac", ".opus", ".ogg"):
        for cle, nom in _CHAMPS_VORBIS.items():
            if nom in t:
                out[cle] = t[nom][0]
        out["pochette"] = bool(getattr(f, "pictures", None)) or "METADATA_BLOCK_PICTURE" in t
        out["paroles"] = "LYRICS" in t
    elif ext in (".m4a", ".mp4"):
        for cle, nom in _CHAMPS_MP4.items():
            if nom in t:
                out[cle] = str(t[nom][0])
        if "trkn" in t:
            out["piste"] = str(t["trkn"][0][0])
        out["pochette"] = "covr" in t
        out["paroles"] = "©lyr" in t
    return out


def ecrire(chemin, champs: dict) -> str:
    """Écrit les champs fournis (titre, artiste, album, genre, annee, piste, artiste_album)."""
    ext = Path(chemin).suffix.lower()
    champs = {k: str(v).strip() for k, v in champs.items() if k in _CHAMPS_ID3 and v is not None}
    try:
        if ext in (".mp3", ".wav"):
            from mutagen import id3
            if ext == ".wav":
                from mutagen.wave import WAVE
                f = WAVE(str(chemin))
                if f.tags is None:
                    f.add_tags()
                t = f.tags
            else:
                try:
                    t = id3.ID3(str(chemin))
                except id3.ID3NoHeaderError:
                    t = id3.ID3()
            for cle, val in champs.items():
                cadre = _CHAMPS_ID3[cle]
                t.delall(cadre)
                if val:
                    t.add(getattr(id3, cadre)(encoding=3, text=val))
            if ext == ".wav":
                f.save()
            else:
                t.save(str(chemin))
        elif ext in (".flac", ".opus", ".ogg"):
            import mutagen
            f = mutagen.File(str(chemin))
            for cle, val in champs.items():
                nom = _CHAMPS_VORBIS[cle]
                if val:
                    f[nom] = [val]
                elif nom in f:
                    del f[nom]
            f.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(chemin))
            for cle, val in champs.items():
                if cle == "piste":
                    if val.split("/")[0].isdigit():
                        f["trkn"] = [(int(val.split("/")[0]), 0)]
                    continue
                nom = _CHAMPS_MP4[cle]
                if val:
                    f[nom] = [val]
                elif nom in f:
                    del f[nom]
            f.save()
        else:
            return f"format non pris en charge : {ext}"
    except Exception as exc:
        return f"écriture : {exc}"
    return ""
