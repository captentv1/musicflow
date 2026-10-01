"""Contrôles de qualité communs PC / Android : silences, durée, doublons.

- Silences : beaucoup de vidéos YouTube commencent (ou finissent) par plusieurs secondes
  de silence ou d'écran noir. Le filtre ffmpeg FILTRE_SILENCE les retire pendant la
  conversion, sans passe supplémentaire.
- Durée : on compare la durée du fichier obtenu à celle annoncée par Spotify ; un gros
  écart trahit une mauvaise vidéo (clip avec intro, version live, extrait…).
- Doublons : plusieurs téléchargements tournent en parallèle sur une playlist ; deux
  d'entre eux visant le même fichier écrivaient en même temps (fichier en double ou
  abîmé). reserver()/liberer() garantissent un seul téléchargement par fichier.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from pathlib import Path

# Retire le silence au DÉBUT et à la FIN (garde 0,2 s) : la fin est traitée en retournant
# l'audio. Les blancs au milieu du morceau sont conservés (pauses voulues par l'artiste).
_DEBUT = "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:start_silence=0.2"
FILTRE_SILENCE = f"{_DEBUT},areverse,{_DEBUT},areverse"

# Au-delà de cet écart avec la durée Spotify, le morceau est signalé « à vérifier ».
ECART_MAX_S = 15

_EN_COURS: set[str] = set()
_EN_COURS_LOCK = threading.Lock()

EXTENSIONS_AUDIO = (".mp3", ".flac", ".wav", ".m4a", ".opus", ".ogg", ".mp4", ".webm")


def secondes(valeur) -> int:
    """« 3:20 », « 1:02:03 », 200 ou « 200 » -> secondes (0 si inconnu)."""
    if isinstance(valeur, (int, float)):
        return int(valeur)
    texte = str(valeur or "").strip()
    if not texte:
        return 0
    if texte.isdigit():
        return int(texte)
    total = 0
    for morceau in texte.split(":"):
        if not morceau.isdigit():
            return 0
        total = total * 60 + int(morceau)
    return total


def duree_fichier(chemin) -> float:
    try:
        import mutagen
        f = mutagen.File(str(chemin))
        return float(f.info.length) if f and f.info else 0.0
    except Exception:
        return 0.0


def verifier_duree(chemin, attendue) -> str:
    """"" si la durée colle à celle de Spotify, sinon un court message d'avertissement."""
    attendue_s = secondes(attendue)
    if not attendue_s:
        return ""
    reelle = duree_fichier(chemin)
    if not reelle:
        return ""
    ecart = reelle - attendue_s
    if abs(ecart) <= ECART_MAX_S:
        return ""
    fmt = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
    return f"{fmt(reelle)} au lieu de {fmt(attendue_s)}"


def _normaliser(texte: str) -> str:
    t = unicodedata.normalize("NFD", texte or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"\((feat|ft|with)[^)]*\)|\[[^\]]*\]|\s-\s.*(remaster|version|edit|mix|live).*$", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def doublon_dans_dossier(dossier: Path, nom_fichier_sans_ext: str):
    """Fichier audio déjà présent sous un nom équivalent (casse, accents, ponctuation,
    « (feat. …) », « - Remastered »…), quelle que soit son extension."""
    cle = _normaliser(nom_fichier_sans_ext)
    if not cle:
        return None
    try:
        for f in dossier.iterdir():
            if f.suffix.lower() in EXTENSIONS_AUDIO and _normaliser(f.stem) == cle:
                return f
    except Exception:
        return None
    return None


def reserver(chemin) -> bool:
    """True si ce fichier est libre (et le réserve) ; False s'il est déjà en cours."""
    cle = _normaliser(Path(chemin).stem) + "|" + str(Path(chemin).parent).lower()
    with _EN_COURS_LOCK:
        if cle in _EN_COURS:
            return False
        _EN_COURS.add(cle)
        return True


def liberer(chemin) -> None:
    cle = _normaliser(Path(chemin).stem) + "|" + str(Path(chemin).parent).lower()
    with _EN_COURS_LOCK:
        _EN_COURS.discard(cle)
