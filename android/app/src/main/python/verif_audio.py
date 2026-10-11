"""Quality checks shared by PC / Android: silences, duration, duplicates.

- Silences: many YouTube videos start (or end) with several seconds of silence
  or a black screen. The ffmpeg filter FILTRE_SILENCE removes them during
  conversion, without an extra pass.
- Duration: the resulting file's duration is compared with Spotify's; a large
  gap reveals a wrong video (music video with intro, live version, excerpt…).
- Duplicates: several downloads run in parallel on a playlist; two of them
  targeting the same file used to write at the same time (duplicated or
  damaged file). reserver()/liberer() guarantee one download per file.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from pathlib import Path

# Removes silence at the START and the END (keeps 0.2 s): the end is handled by reversing
# the audio. Gaps in the middle of the track are kept (pauses intended by the artist).
_DEBUT = "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:start_silence=0.2"
FILTRE_SILENCE = f"{_DEBUT},areverse,{_DEBUT},areverse"


def filtre_silence(duree_s) -> str:
    """Same result as FILTRE_SILENCE, much faster: only the last 30 s are reversed to trim
    the trailing silence (reversing the whole track buffered it all in memory and cost
    as much again as the conversion itself)."""
    try:
        duree_s = float(duree_s or 0)
    except (TypeError, ValueError):
        duree_s = 0
    if duree_s <= 60:
        return FILTRE_SILENCE
    coupe = int(duree_s - 30)
    return (f"asplit=2[a][b];[a]atrim=end={coupe},asetpts=PTS-STARTPTS,{_DEBUT}[a1];"
            f"[b]atrim=start={coupe},asetpts=PTS-STARTPTS,areverse,{_DEBUT},areverse[b1];"
            f"[a1][b1]concat=n=2:v=0:a=1")

# Beyond this gap with the Spotify duration, the track is flagged "to check".
ECART_MAX_S = 15

_EN_COURS: set[str] = set()
_EN_COURS_LOCK = threading.Lock()

EXTENSIONS_AUDIO = (".mp3", ".flac", ".wav", ".m4a", ".opus", ".ogg", ".mp4", ".webm")


def secondes(valeur) -> int:
    """"3:20", "1:02:03", 200 or "200" -> seconds (0 if unknown)."""
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


def extrait_analyse(chemin) -> list[str]:
    """ffmpeg input options limiting an analysis to 60 s from the middle of the track."""
    duree = duree_fichier(chemin)
    if duree <= 90:
        return []
    return ["-ss", str(int(duree / 2 - 30)), "-t", "60"]


def verifier_duree(chemin, attendue) -> str:
    """"" if the duration matches Spotify's, otherwise a short warning message."""
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
    return f"{fmt(reelle)} instead of {fmt(attendue_s)}"


def _normaliser(texte: str) -> str:
    t = unicodedata.normalize("NFD", texte or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"\((feat|ft|with)[^)]*\)|\[[^\]]*\]|\s-\s.*(remaster|version|edit|mix|live).*$", "", t)
    # Every script kept: limited to a-z, all Arabic/Cyrillic titles got the same empty
    # key, so reserver() saw them as “already being downloaded” and skipped them.
    return re.sub(r"[\W_]+", " ", t).strip()


def doublon_dans_dossier(dossier: Path, nom_fichier_sans_ext: str):
    """Audio file already present under an equivalent name (case, accents, punctuation,
    "(feat. …)", "- Remastered"…), whatever its extension."""
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
    """True if this file is free (and reserves it); False if it is already in progress."""
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
