"""Découvrir (PC + Android), sans clé : tops, genres, nouveautés, playlists populaires
(Deezer), artistes similaires et mix d'un artiste. Les morceaux ont le format habituel (vidéo YouTube choisie au téléchargement)."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import artistes

_UA = {"User-Agent": "MusicFlow (https://github.com/captentv1/musicflow)"}


def top(limite: int = 100):
    return [artistes._piste(t) for t in artistes._tout("/chart/0/tracks", limite)]


def playlists_populaires(limite: int = 30):
    d = artistes._get(f"/chart/0/playlists?limit={limite}")
    return [{"id": p["id"], "titre": p.get("title") or "", "pochette": p.get("picture_medium") or "",
             "nb": p.get("nb_tracks") or 0, "url": f"https://www.deezer.com/playlist/{p['id']}"} for p in d.get("data") or []]


def genres():
    d = artistes._get("/genre")
    return [{"id": g["id"], "nom": g.get("name") or "", "image": g.get("picture_medium") or ""}
            for g in d.get("data") or [] if g.get("id")]


def genre(genre_id: int, limite: int = 100):
    """Titres les plus écoutés d'un genre."""
    return [artistes._piste(t) for t in artistes._tout(f"/chart/{int(genre_id)}/tracks", limite)]


PAYS = {"fr": "France", "tn": "Tunisie", "dz": "Algérie", "ma": "Maroc", "be": "Belgique", "ca": "Canada",
        "ch": "Suisse", "us": "États-Unis", "gb": "Royaume-Uni", "de": "Allemagne", "es": "Espagne",
        "it": "Italie", "sa": "Arabie saoudite", "ae": "Émirats", "eg": "Égypte", "br": "Brésil", "jp": "Japon"}


def top_pays(pays: str = "fr", limite: int = 100):
    """Top des titres les plus écoutés d'un pays (classement Apple Music, sans clé)."""
    pays = pays if pays in PAYS else "fr"
    res, derniere = None, None
    for n in (min(int(limite), 100), 50, 25):          # le flux Apple est capricieux : on réessaie plus court
        url = f"https://rss.marketingtools.apple.com/api/v2/{pays}/music/most-played/{n}/songs.json"
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                res = (json.loads(r.read().decode("utf-8")).get("feed") or {}).get("results") or []
            break
        except Exception as exc:
            derniere = exc
    if res is None:
        raise RuntimeError(f"classement indisponible pour le moment ({derniere})")
    out = []
    for x in res:
        img = (x.get("artworkUrl100") or "")
        out.append({"source": "apple", "id": None, "url": None, "title": x.get("name") or "", "uploader": x.get("artistName") or "",
                    "album": "", "duration": "", "thumbnail": img.replace("100x100", "300x300"),
                    "cover": img.replace("100x100", "1000x1000"), "query": f"{x.get('name')} {x.get('artistName')}".strip()})
    return out


def similaires(artiste_id: int):
    d = artistes._get(f"/artist/{artiste_id}/related?limit=20")
    return [{"id": a["id"], "nom": a.get("name") or "", "photo": a.get("picture_medium") or "",
             "fans": a.get("nb_fan") or 0, "albums": a.get("nb_album") or 0} for a in d.get("data") or []]


def mix(artiste_id: int):
    """« Radio » Deezer de l'artiste : ~25 titres de lui et d'artistes proches."""
    return [artistes._piste(t) for t in artistes._get(f"/artist/{artiste_id}/radio?limit=50").get("data") or []]
