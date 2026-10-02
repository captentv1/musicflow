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
