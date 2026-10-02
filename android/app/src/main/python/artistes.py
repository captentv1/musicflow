"""Catégorie « Artiste » : chercher un chanteur, voir toute sa discographie, choisir.

Source : API publique Deezer (sans clé ni compte) — artistes, albums/singles/EP,
titres de chaque album, titres populaires. Les morceaux rendus ont le format habituel
(le morceau YouTube correspondant est choisi au téléchargement).
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_API = "https://api.deezer.com"
_UA = {"User-Agent": "MusicFlow (https://github.com/captentv1/musicflow)"}


def _get(chemin: str):
    for essai in range(6):
        req = urllib.request.Request(_API + chemin, headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        err = d.get("error") if isinstance(d, dict) else None
        if not err:
            return d
        if (err or {}).get("code") == 4:      # quota Deezer (~50 requêtes / 5 s) : on patiente
            time.sleep(1.5 + essai)
            continue
        raise RuntimeError((err or {}).get("message") or "erreur Deezer")
    raise RuntimeError("Deezer est saturé, réessaie dans un instant.")


def _tout(chemin: str, maxi: int = 1000):
    """Suit la pagination Deezer (next) jusqu'à `maxi` éléments."""
    items, url = [], chemin + ("&" if "?" in chemin else "?") + "limit=100"
    while url and len(items) < maxi:
        page = _get(url)
        items += page.get("data") or []
        suivant = page.get("next")
        url = suivant.replace(_API, "") if suivant else None
    return items


def _duree(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}" if sec else ""


def _piste(t, album=None, artiste=""):
    alb = t.get("album") or album or {}
    titre = t.get("title") or ""
    art = (t.get("artist") or {}).get("name") or artiste
    return {
        "source": "deezer", "id": None, "url": None, "title": titre, "uploader": art,
        "album": alb.get("title") or "", "duration": _duree(t.get("duration")),
        "thumbnail": alb.get("cover_medium") or "", "cover": alb.get("cover_xl") or alb.get("cover_big") or "",
        "query": f"{titre} {art}".strip(), "rang": t.get("rank") or 0,
    }


def chercher_artistes(q: str, limite: int = 12):
    d = _get(f"/search/artist?q={urllib.parse.quote(q)}&limit={limite}")
    return [{"id": a["id"], "nom": a.get("name") or "", "photo": a.get("picture_medium") or "",
             "fans": a.get("nb_fan") or 0, "albums": a.get("nb_album") or 0} for a in d.get("data") or []]


def discographie(artiste_id: int):
    a = _get(f"/artist/{artiste_id}")
    albums = _tout(f"/artist/{artiste_id}/albums", 500)
    top = _get(f"/artist/{artiste_id}/top?limit=50").get("data") or []
    return {
        "artiste": {"id": a["id"], "nom": a.get("name") or "", "photo": a.get("picture_big") or a.get("picture_medium") or "",
                    "fans": a.get("nb_fan") or 0},
        "top": [_piste(t, artiste=a.get("name")) for t in top],
        "albums": [{"id": x["id"], "titre": x.get("title") or "", "type": x.get("record_type") or "album",
                    "date": x.get("release_date") or "", "pochette": x.get("cover_medium") or "",
                    "pochette_hd": x.get("cover_xl") or ""} for x in albums],
    }


def titres_album(album_id: int):
    alb = _get(f"/album/{album_id}")
    pistes = _tout(f"/album/{album_id}/tracks", 300)
    return [_piste(t, album=alb, artiste=(alb.get("artist") or {}).get("name") or "") for t in pistes]


def titres_albums(ids: list[int]) -> dict:
    """Titres de plusieurs albums (en parallèle), rangés par identifiant d'album."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        listes = list(pool.map(lambda i: _sur(titres_album, i), ids))
    return {str(i): l for i, l in zip(ids, listes)}


def _sur(f, *a):
    try:
        return f(*a)
    except Exception:
        return []
