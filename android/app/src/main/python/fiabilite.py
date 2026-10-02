"""Fiabilité (PC + Android) : diagnostic, versions, nettoyage, explication des échecs."""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

_UA = {"User-Agent": "Mozilla/5.0 MusicFlow"}

# Explication d'un échec à partir du message d'erreur, avec un conseil.
_RAISONS = [
    (r"sign in to confirm|not a bot|429|too many requests", "YouTube limite les téléchargements depuis cette connexion.",
     "Attends quelques minutes ou change de réseau (Wi-Fi ↔ 4G), puis « Réessayer les échecs »."),
    (r"age|confirm your age|inappropriate", "Vidéo réservée aux adultes sur YouTube.",
     "« Mauvaise version ? En prendre une autre » ou « Versions » pour choisir une autre vidéo."),
    (r"private|unavailable|removed|not available|copyright|blocked", "Vidéo indisponible (supprimée, privée ou bloquée dans ton pays).",
     "Une autre vidéo est essayée automatiquement ; sinon utilise « Versions »."),
    (r"introuvable|no video|aucune correspondance|aucune autre", "Morceau introuvable sur YouTube.",
     "Vérifie le titre/l'artiste, ou cherche-le à la main dans Rechercher."),
    (r"connexion|network|timed out|timeout|getaddrinfo|unreachable|ssl", "Problème de connexion internet.",
     "Il sera refait automatiquement au retour de la connexion."),
    (r"postprocessing|conversion|ffmpeg", "La conversion du fichier a échoué.",
     "Essaie un autre format (MP3 au lieu de FLAC/Opus) dans les réglages."),
    (r"espace|no space|disk", "Plus assez d'espace de stockage.",
     "Libère de la place ou choisis un autre dossier."),
    (r"permission|denied|accès", "Le dossier de destination n'est pas accessible.",
     "Choisis à nouveau le dossier de téléchargement."),
]


def expliquer(message: str) -> dict:
    m = (message or "").lower()
    for motif, raison, conseil in _RAISONS:
        if re.search(motif, m):
            return {"raison": raison, "conseil": conseil}
    return {"raison": "Erreur inattendue.", "conseil": "Réessaie ; si ça recommence, exporte le journal d'erreurs (Paramètres → Diagnostic)."}


def _tester(nom, fonction):
    debut = time.time()
    try:
        detail = fonction() or "OK"
        return {"nom": nom, "ok": True, "detail": str(detail)[:160], "ms": int((time.time() - debut) * 1000)}
    except Exception as exc:
        return {"nom": nom, "ok": False, "detail": str(exc)[:160], "ms": int((time.time() - debut) * 1000)}


def _http_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def _au_moins_un(liste, si_vide="aucun résultat"):
    if not liste:
        raise RuntimeError(si_vide)
    return f"{len(liste)} résultat(s)"


def diagnostic(search_videos, recherche_spotify, ffmpeg_ok) -> list[dict]:
    """Teste chaque service utilisé par l'app. Les fonctions de recherche sont fournies
    par le serveur (PC ou Android) pour tester exactement ce que l'app utilise."""
    tests = [
        ("Internet", lambda: _http_json("https://api.deezer.com/infos") and "joignable"),
        ("YouTube (recherche)", lambda: _au_moins_un(search_videos("daft punk one more time", limit=2))),
        ("Spotify (recherche)", lambda: _au_moins_un(recherche_spotify("one more time daft punk", 3),
                                                     "aucun résultat (la recherche passera par YouTube)")),
        ("Deezer (artistes, albums)", lambda: _http_json("https://api.deezer.com/search?q=daft%20punk&limit=1")["data"][0]["title"]),
        ("iTunes (infos d'album)", lambda: _http_json("https://itunes.apple.com/search?term=daft+punk&entity=song&limit=1")["results"][0]["collectionName"]),
        ("LRCLIB (paroles)", lambda: "OK" if _http_json("https://lrclib.net/api/search?q=one%20more%20time%20daft%20punk") else "vide"),
        ("Conversion audio (ffmpeg)", lambda: "OK" if ffmpeg_ok() else (_ for _ in ()).throw(RuntimeError("ffmpeg indisponible"))),
    ]
    return [_tester(n, f) for n, f in tests]


def nettoyer(dossier) -> dict:
    """Supprime les restes de téléchargements interrompus (.part, .ytdl, .temp.*, miniatures)."""
    d = Path(dossier)
    n, octets = 0, 0
    try:
        for f in d.iterdir():
            nom = f.name.lower()
            if f.is_file() and (nom.endswith((".part", ".ytdl", ".webp")) or ".temp." in nom or ".conv." in nom
                                or ".cover." in nom or nom.endswith(".part-frag")):
                try:
                    octets += f.stat().st_size
                    f.unlink()
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return {"fichiers": n, "octets": octets}
