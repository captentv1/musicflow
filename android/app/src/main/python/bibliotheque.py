"""Bibliothèque : les morceaux déjà téléchargés dans un dossier (PC + Android).

- lister(dossier) : fichiers audio avec titre/artiste/album/durée/taille/pochette/paroles.
- retaguer(chemin, options) : complète album, genre, n° de piste, année, pochette HD et
  paroles d'un fichier existant (à partir de ses tags, sinon de son nom « Titre - Artiste »).
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import infos_album
import paroles
import qualite_tags
import verif_audio


def _titre_artiste(chemin: Path, tags: dict):
    titre, artiste = tags.get("titre") or "", tags.get("artiste") or ""
    if not titre:
        nom = chemin.stem
        if " - " in nom:
            titre, artiste = [x.strip() for x in nom.split(" - ", 1)]
        else:
            titre = nom
    return titre, artiste


def lister(dossier, avec_tags: bool = True, maxi: int = 5000):
    d = Path(dossier)
    items = []
    try:
        fichiers = sorted((f for f in d.iterdir() if f.suffix.lower() in verif_audio.EXTENSIONS_AUDIO),
                          key=lambda f: f.stat().st_mtime, reverse=True)[:maxi]
    except Exception:
        return []
    for f in fichiers:
        tags = qualite_tags.lire(f) if avec_tags else {}
        titre, artiste = _titre_artiste(f, tags)
        st = f.stat()
        items.append({"chemin": str(f), "nom": f.name, "titre": titre, "artiste": artiste,
                      "album": tags.get("album", ""), "genre": tags.get("genre", ""), "annee": tags.get("annee", ""),
                      "duree": tags.get("duree", 0), "taille": st.st_size, "date": int(st.st_mtime),
                      "pochette": tags.get("pochette", False), "paroles": tags.get("paroles", False),
                      "format": f.suffix.lower().lstrip(".")})
    return items


def retaguer(chemin, avec_paroles: bool = True, forcer_pochette: bool = False) -> list[str]:
    """Complète les infos d'un fichier. Retourne la liste de ce qui a été ajouté."""
    f = Path(chemin)
    tags = qualite_tags.lire(f)
    titre, artiste = _titre_artiste(f, tags)
    fait = []
    if not tags.get("titre") or not tags.get("artiste"):
        if not qualite_tags.ecrire(f, {"titre": titre, "artiste": artiste}):
            fait.append("titre/artiste")
    infos = infos_album.chercher(titre, (artiste or "").split(",")[0].strip(), tags.get("duree") or 0)
    if infos:
        if not infos_album.integrer(f, infos):
            fait.append("album/genre/piste/année")
        if infos.get("pochette_hd") and (forcer_pochette or not tags.get("pochette")):
            try:
                req = urllib.request.Request(infos["pochette_hd"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    if not qualite_tags.pochette(f, r.read()):
                        fait.append("pochette HD")
            except Exception:
                pass
    if avec_paroles and not tags.get("paroles"):
        p = paroles.chercher(titre, (artiste or "").split(",")[0].strip(), tags.get("duree") or 0)
        if p and not paroles.integrer(f, p):
            fait.append("paroles")
    return fait


def transferer_infos(source, cible) -> None:
    """Après une conversion : recopie tags, pochette et paroles de l'ancien fichier."""
    import mutagen
    src = Path(source)
    tags = qualite_tags.lire(src)
    qualite_tags.ecrire(cible, {k: tags.get(k, "") for k in ("titre", "artiste", "album", "genre", "annee", "piste", "artiste_album")})
    # pochette
    data = None
    try:
        f = mutagen.File(str(src))
        t = f.tags if f else None
        if getattr(f, "pictures", None):
            data = f.pictures[0].data
        elif t is not None:
            for k in list(t.keys()):
                if str(k).startswith("APIC"):
                    data = t[k].data; break
            if data is None and "covr" in t:
                data = bytes(t["covr"][0])
            if data is None and "METADATA_BLOCK_PICTURE" in t:
                import base64
                from mutagen.flac import Picture
                data = Picture(base64.b64decode(t["METADATA_BLOCK_PICTURE"][0])).data
    except Exception:
        data = None
    if data:
        qualite_tags.pochette(cible, data)
    # paroles
    try:
        f = mutagen.File(str(src)); t = f.tags if f else None
        texte = ""
        if t is not None:
            for k in list(t.keys()):
                if str(k).startswith("USLT"):
                    texte = str(t[k].text); break
            for k in ("LYRICS", "\xa9lyr"):
                if not texte and k in t:
                    texte = str(t[k][0])
        if texte:
            paroles.integrer(cible, {"plain": texte, "synced": ""}, False)
    except Exception:
        pass


def args_conversion(source, cible, fmt: str) -> list[str]:
    """Arguments ffmpeg pour réduire un fichier : Opus 160 kbps, AAC 256 kbps ou MP3 (192/320)."""
    codec = {"opus": ["-c:a", "libopus", "-b:a", "160k"], "aac": ["-c:a", "aac", "-b:a", "256k"],
             "192": ["-c:a", "libmp3lame", "-b:a", "192k"], "320": ["-c:a", "libmp3lame", "-b:a", "320k"],
             "128": ["-c:a", "libmp3lame", "-b:a", "128k"]}.get(fmt, ["-c:a", "libopus", "-b:a", "160k"])
    return ["-y", "-loglevel", "error", "-i", str(source), "-vn", "-map", "0:a:0"] + codec + [str(cible)]


def extension_pour(fmt: str) -> str:
    return {"opus": ".opus", "aac": ".m4a"}.get(fmt, ".mp3")
