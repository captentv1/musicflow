"""One copy of each song, whatever its file name or folder. Shared by PC / Android.

The duplicate check used to compare file names in the destination folder only.
The same song was therefore downloaded again when:
  - its name differed (raw YouTube title, “Official Video”, another artist spelling,
    “feat.”), each copy with a different cover art;
  - it was downloaded into another folder;
  - on Android, the file had been moved to the user's folder (the server only looked
    in the app's internal folder, always empty).

So every downloaded track is recorded under several identity keys:
  - t: essential title + main artist, normalized (no “(Official Video)”, “Lyrics”,
       “- 2011 Remaster”, “feat.”, accents, case; every script kept);
  - v: source video id (the same video under two names is the same song);
  - i: ISRC (the recording's official code, from Deezer).
Files already in a folder (downloaded before this index existed, or by hand)
are indexed from their tags, then from their “Title - Artist” name.
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path

_LOCK = threading.Lock()
_CHEMIN: Path | None = None
_INDEX: dict | None = None            # {"pistes": {id: entry}, "dossiers": {dir: {name: mtime}}}
# Android: names of the files in the user's chosen folder (sent by the interface),
# which the server cannot list itself.
_NOMS_EXTERNES: set[str] = set()

_EXT = (".mp3", ".flac", ".wav", ".m4a", ".opus", ".ogg", ".mp4", ".webm")
_BRUIT = re.compile(
    r"\b(official|officiel|officielle|music|video|vid[eé]o|audio|lyrics?|lyric|paroles?|clip|"
    r"visuali[sz]er|hd|hq|4k|mv|full|version|original|explicit|clean|topic)\b", re.I)
_SUFFIXE = re.compile(r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|live|from|score|soundtrack|mono|stereo|"
                      r"bonus|demo|radio|single|extended)\b.*$", re.I)


def configurer(chemin) -> None:
    global _CHEMIN, _INDEX
    with _LOCK:
        _CHEMIN = Path(chemin)
        _INDEX = None


def _charger() -> dict:
    global _INDEX
    if _INDEX is None:
        try:
            _INDEX = json.loads(_CHEMIN.read_text(encoding="utf-8")) if _CHEMIN and _CHEMIN.exists() else {}
        except Exception:
            _INDEX = {}
        _INDEX.setdefault("pistes", {})
        _INDEX.setdefault("dossiers", {})
    return _INDEX


def _sauver() -> None:
    if not _CHEMIN:
        return
    try:
        tmp = _CHEMIN.with_suffix(".tmp")
        tmp.write_text(json.dumps(_INDEX, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CHEMIN)
    except Exception:
        pass


def _norm(texte: str) -> str:
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", t))


def norm_artiste(artiste: str) -> str:
    a = (artiste or "").strip()
    for sep in (",", " feat. ", " feat ", " ft. ", " ft ", " featuring ", " & ", " x ", " et ", " and "):
        i = a.lower().find(sep)
        if i > 0:
            a = a[:i]
    a = re.sub(r"\s*(-\s*topic|vevo|official)\s*$", "", a, flags=re.I)
    a = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", a)
    return _norm(_BRUIT.sub(" ", a)) or _norm(a)


def norm_titre(titre: str, artiste: str = "") -> str:
    t = titre or ""
    # Raw YouTube title “Artist - Song”: keep the song side.
    if " - " in t and artiste:
        gauche, droite = t.split(" - ", 1)
        na = norm_artiste(artiste)
        if na and na in _norm(gauche):
            t = droite
        elif na and na in _norm(droite):
            t = gauche
    t = _SUFFIXE.sub("", t)
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t)                  # (Official Video), [Lyrics], (feat. X)
    t = re.sub(r"\s(feat|ft|featuring)\.?\s.*$", " ", t, flags=re.I)
    t = _BRUIT.sub(" ", t)
    return _norm(t) or _norm(titre)


def cles(titre: str, artiste: str, video: str = "", isrc: str = "") -> list[str]:
    out = []
    nt = norm_titre(titre, artiste)
    if nt:
        out.append(f"t:{nt}|{norm_artiste(artiste)}")
    if video:
        out.append(f"v:{video}")
    if isrc:
        out.append(f"i:{isrc.upper()}")
    return out


def cles_nom(nom: str) -> list[str]:
    """Keys of a raw name “A - B” whose order is unknown: a YouTube title is usually
    “Artist - Song”, a MusicFlow file name “Song - Artist”. Both are tried."""
    morceaux = (nom or "").rsplit(".", 1)[0] if (nom or "").lower().endswith(_EXT) else (nom or "")
    morceaux = morceaux.split(" - ", 1)
    if len(morceaux) != 2:
        return cles(morceaux[0], "")
    a, b = morceaux
    return list(dict.fromkeys(cles(a, b) + cles(b, a)))


def _existe(entree: dict) -> bool:
    chemin = Path(entree.get("dossier") or "") / (entree.get("nom") or "")
    try:
        if chemin.is_file():
            return True
    except Exception:
        pass
    return (entree.get("nom") or "") in _NOMS_EXTERNES


def _ajouter(idx: dict, dossier: str, nom: str, cles_piste: list[str]) -> None:
    ident = f"{dossier}|{nom}".lower()
    ancienne = idx["pistes"].get(ident, {})
    toutes = list(dict.fromkeys((ancienne.get("cles") or []) + cles_piste))
    idx["pistes"][ident] = {"dossier": dossier, "nom": nom, "cles": toutes}


def _lire_titre_artiste(chemin: Path):
    try:
        import qualite_tags
        tags = qualite_tags.lire(chemin)
        if tags.get("titre"):
            return tags["titre"], tags.get("artiste") or ""
    except Exception:
        pass
    morceaux = chemin.stem.rsplit(" - ", 1)
    return (morceaux[0], morceaux[1]) if len(morceaux) == 2 else (chemin.stem, "")


def indexer_dossier(dossier) -> None:
    """Indexes the audio files of a folder not indexed yet (or modified)."""
    dossier = str(dossier)
    try:
        fichiers = [f for f in Path(dossier).iterdir() if f.suffix.lower() in _EXT]
    except Exception:
        return
    with _LOCK:
        idx = _charger()
        vus = idx["dossiers"].setdefault(dossier, {})
        nouveau = False
        for f in fichiers:
            try:
                mtime = int(f.stat().st_mtime)
            except Exception:
                continue
            if vus.get(f.name) == mtime:
                continue
            titre, artiste = _lire_titre_artiste(f)
            _ajouter(idx, dossier, f.name, cles(titre, artiste) + cles_nom(f.name))
            vus[f.name] = mtime
            nouveau = True
        if nouveau:
            _sauver()


def definir_noms_externes(noms) -> None:
    """Android: files of the user's chosen folder, indexed from their names."""
    global _NOMS_EXTERNES
    noms = {n for n in (noms or []) if str(n).lower().endswith(_EXT)}
    with _LOCK:
        _NOMS_EXTERNES = noms
        idx = _charger()
        connus = {e.get("nom") for e in idx["pistes"].values()}
        nouveau = False
        for n in noms - connus:
            _ajouter(idx, "", n, cles_nom(n))
            nouveau = True
        if nouveau:
            _sauver()


def trouver(cles_cherchees: list[str], sauf: str = "") -> str | None:
    """Path (or name) of an existing copy of this song, or None.
    `sauf`: path of the file being checked itself (not a duplicate of itself)."""
    if not cles_cherchees:
        return None
    voulu = set(cles_cherchees)
    # Artist unknown on one side (raw YouTube video): same title is enough.
    prefixes = [k for k in voulu if k.startswith("t:") and k.endswith("|")]
    voulu |= {k.split("|", 1)[0] + "|" for k in voulu if k.startswith("t:")}
    with _LOCK:
        idx = _charger()
        perimees = []
        trouve = None
        for ident, e in idx["pistes"].items():
            cles_e = e.get("cles") or []
            if not (voulu.intersection(cles_e) or any(c.startswith(p) for p in prefixes for c in cles_e)):
                continue
            chemin = str(Path(e.get("dossier") or "") / e.get("nom", "")) if e.get("dossier") else e.get("nom", "")
            if sauf and chemin.lower() == str(sauf).lower():
                continue
            if _existe(e):
                trouve = chemin
                break
            perimees.append(ident)            # deleted by the user: may be downloaded again
        for ident in perimees:
            idx["pistes"].pop(ident, None)
        if perimees:
            _sauver()
        return trouve


_EN_COURS: set[str] = set()


def reserver(cles_piste: list[str]) -> bool:
    """False if the same song (under any name) is already being downloaded."""
    with _LOCK:
        if _EN_COURS.intersection(cles_piste):
            return False
        _EN_COURS.update(cles_piste)
        return True


def liberer(cles_piste: list[str]) -> None:
    with _LOCK:
        _EN_COURS.difference_update(cles_piste)


def id_video(url: str) -> str:
    """Stable id of a source: YouTube video id, otherwise the URL without its query."""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", url or "")
    if m:
        return m.group(1)
    return (url or "").split("?")[0].rstrip("/").lower()


def enregistrer(chemin, cles_piste: list[str], nom_final: str = "") -> None:
    """Records a finished download. `nom_final`: Android name in the chosen folder."""
    chemin = Path(chemin)
    with _LOCK:
        idx = _charger()
        _ajouter(idx, str(chemin.parent), chemin.name, cles_piste)
        if nom_final:
            _ajouter(idx, "", nom_final, cles_piste)
            _NOMS_EXTERNES.add(nom_final)
        try:
            idx["dossiers"].setdefault(str(chemin.parent), {})[chemin.name] = int(chemin.stat().st_mtime)
        except Exception:
            pass
        _sauver()
