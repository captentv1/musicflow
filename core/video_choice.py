from __future__ import annotations

import re
import unicodedata

_INDESIRABLES = {
    "reaction": 60, "réaction": 60, "reacts": 60,
    "casting": 60, "audition": 60, "the voice": 55, "nouvelle star": 55,
    "interview": 55, "making of": 45, "behind the scenes": 45, "coulisses": 45,
    "tutorial": 50, "tuto": 50, "how to play": 50, "lesson": 45, "cours de": 45,
    "karaoke": 50, "karaoké": 50, "instrumental": 40, "backing track": 45,
    "cover": 40, "reprise": 35, "parodie": 50, "parody": 50,
    "live": 25, "concert": 30, "en direct": 25, "session": 20, "acoustic": 20,
    "nightcore": 45, "sped up": 40, "slowed": 40, "reverb": 30, "8d audio": 45,
    "mashup": 35, "medley": 35, "compilation": 40, "mix": 15, "megamix": 40,
    "full album": 45, "album complet": 45, "playlist": 35,
    "trailer": 40, "bande annonce": 40, "extrait": 25,
}

_BONUS = {
    "official audio": 30, "audio officiel": 30, "official music video": 18,
    "clip officiel": 18, "official video": 15, "audio": 10, "lyrics": 6,
    "paroles": 6, "hq": 4,
}


def _normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", (texte or "").lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", texte)


def _mots(texte: str) -> set[str]:
    return {m for m in _normaliser(texte).split() if len(m) > 2}


_SUFFIXE = re.compile(r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|live|from|score|soundtrack|"
                      r"mono|stereo|bonus|demo|acoustic|radio|single|extended|instrumental|"
                      r"theme|ost|bande originale)\b.*$", re.I)
_PARENTHESES = re.compile(r"[\(\[][^\)\]]*\b(feat|ft|with|from|remaster(ed)?|version|edit|mix|"
                          r"live|score|soundtrack|bonus|demo|radio|ost)\b[^\)\]]*[\)\]]", re.I)


def _titre_essentiel(titre: str) -> str:
    court = _PARENTHESES.sub(" ", _SUFFIXE.sub("", titre or "")).strip()
    return court or (titre or "")


def _duree_en_secondes(valeur) -> int:
    if isinstance(valeur, (int, float)):
        return int(valeur)
    txt = str(valeur or "").strip()
    if not txt:
        return 0
    if txt.isdigit():
        return int(txt)
    parts = txt.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _penalites(titre_video: str, requete: str) -> int:
    v = _normaliser(titre_video)
    q = _normaliser(requete)
    total = 0
    for terme, poids in _INDESIRABLES.items():
        t = _normaliser(terme)
        if t in v and t not in q:
            total += poids
    return total


def noter(candidat: dict, titre: str, artiste: str, duree_s: int, requete: str = "") -> float:
    titre_video = candidat.get("title") or ""
    chaine = candidat.get("uploader") or ""
    requete = requete or f"{titre} {artiste}"
    note = 0.0

    duree_video = _duree_en_secondes(candidat.get("duration"))
    if duree_s > 0 and duree_video > 0:
        ecart = abs(duree_video - duree_s)
        if ecart <= 3:
            note += 100
        elif ecart <= 8:
            note += 80
        elif ecart <= 20:
            note += 45
        elif ecart <= 45:
            note += 10
        else:
            note -= min(120, ecart)
    elif duree_video > 900:
        note -= 60

    mots_titre = _mots(_titre_essentiel(titre))
    if mots_titre:
        presents = len(mots_titre & _mots(titre_video))
        note += 45 * (presents / len(mots_titre))

    mots_artiste = _mots(artiste)
    if mots_artiste:
        cible = _mots(titre_video) | _mots(chaine)
        note += 35 * (len(mots_artiste & cible) / len(mots_artiste))

    if re.search(r"-\s*topic$", chaine.strip(), re.I):
        note += 40

    for terme, poids in _BONUS.items():
        if _normaliser(terme) in _normaliser(titre_video):
            note += poids

    note -= _penalites(titre_video, requete)
    return note


def choisir(candidats: list[dict], titre: str, artiste: str, duree, requete: str = ""):
    if not candidats:
        return None, 0.0
    duree_s = _duree_en_secondes(duree)
    notes = [(noter(c, titre, artiste, duree_s, requete), c) for c in candidats]
    notes.sort(key=lambda x: x[0], reverse=True)
    return notes[0][1], notes[0][0]


SEUIL_FIABLE = 20


def fiable(candidat, titre: str, note: float) -> bool:
    if not candidat:
        return False
    mots = _mots(_titre_essentiel(titre))
    if mots and len(mots & _mots(candidat.get("title") or "")) / len(mots) < 0.5:
        return note >= SEUIL_SANS_TITRE
    return note >= SEUIL_FIABLE


SEUIL_SANS_TITRE = 135
