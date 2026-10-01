"""Choix du bon résultat YouTube pour un morceau venant de Spotify.

Avant ce module, l'app prenait le TOUT PREMIER résultat de recherche, sans le moindre
contrôle : selon ce que YouTube renvoyait ce jour-là, on téléchargeait une reprise, un
live, un karaoké, une réaction ou un passage de casting à la place de la chanson.

On note donc plusieurs candidats. Le signal le plus fiable est la durée : Spotify la
donne exactement, et un live ou une réaction s'en écarte presque toujours.
"""
from __future__ import annotations

import re
import unicodedata

# Termes qui trahissent une version qui n'est pas le morceau original. Le poids reflète
# la gravité : une « reaction » n'est jamais la chanson, un « remix » peut l'être si
# l'utilisateur l'a demandé (d'où l'annulation de la pénalité si le terme est dans la
# requête, voir _penalites).
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

# Termes qui signalent au contraire la bonne piste.
_BONUS = {
    "official audio": 30, "audio officiel": 30, "official music video": 18,
    "clip officiel": 18, "official video": 15, "audio": 10, "lyrics": 6,
    "paroles": 6, "hq": 4,
}


def _normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", (texte or "").lower())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", texte)


def _mots(texte: str) -> set[str]:
    # Les mots très courts n'apportent rien pour comparer deux titres.
    return {m for m in _normaliser(texte).split() if len(m) > 2}


def _duree_en_secondes(valeur) -> int:
    """Accepte 215, "215", "3:35" ou "1:02:03"."""
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
    """Somme des pénalités, en ignorant un terme que l'utilisateur a lui-même demandé
    (chercher « ... live » ne doit pas pénaliser les versions live)."""
    v = _normaliser(titre_video)
    q = _normaliser(requete)
    total = 0
    for terme, poids in _INDESIRABLES.items():
        t = _normaliser(terme)
        if t in v and t not in q:
            total += poids
    return total


def noter(candidat: dict, titre: str, artiste: str, duree_s: int, requete: str = "") -> float:
    """Note un résultat YouTube. Plus c'est haut, mieux c'est."""
    titre_video = candidat.get("title") or ""
    chaine = candidat.get("uploader") or ""
    requete = requete or f"{titre} {artiste}"
    note = 0.0

    # 1) Durée : le signal le plus fiable. Spotify donne la durée exacte du morceau ;
    #    un live, une réaction ou un album complet s'en écartent nettement.
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
            note -= min(120, ecart)  # très loin du compte : presque sûrement autre chose
    elif duree_video > 900:
        note -= 60  # plus de 15 min sans durée de référence : album ou compilation

    # 2) Le titre du morceau doit se retrouver dans le titre de la vidéo.
    mots_titre = _mots(titre)
    if mots_titre:
        presents = len(mots_titre & _mots(titre_video))
        note += 45 * (presents / len(mots_titre))

    # 3) L'artiste doit apparaître, dans le titre ou dans le nom de la chaîne.
    mots_artiste = _mots(artiste)
    if mots_artiste:
        cible = _mots(titre_video) | _mots(chaine)
        note += 35 * (len(mots_artiste & cible) / len(mots_artiste))

    # 4) Chaîne « - Topic » : chaîne auto-générée par YouTube pour un artiste, elle ne
    #    contient que l'audio officiel du catalogue — le meilleur cas possible.
    if re.search(r"-\s*topic$", chaine.strip(), re.I):
        note += 40

    for terme, poids in _BONUS.items():
        if _normaliser(terme) in _normaliser(titre_video):
            note += poids

    note -= _penalites(titre_video, requete)
    return note


def choisir(candidats: list[dict], titre: str, artiste: str, duree, requete: str = ""):
    """Retourne (meilleur_candidat, note) ou (None, 0) si la liste est vide."""
    if not candidats:
        return None, 0.0
    duree_s = _duree_en_secondes(duree)
    notes = [(noter(c, titre, artiste, duree_s, requete), c) for c in candidats]
    notes.sort(key=lambda x: x[0], reverse=True)
    return notes[0][1], notes[0][0]
