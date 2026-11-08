from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_API = "https://lrclib.net/api"
_UA = "MusicFlow (https://github.com/captentv1/musicflow)"

_ESSAIS = 4


def _get(url: str):
    for essai in range(_ESSAIS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (429, 500, 502, 503, 504) or essai == _ESSAIS - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if essai == _ESSAIS - 1:
                raise
        time.sleep(0.8 * (essai + 1))


_PARENTHESES = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_SUFFIXE = re.compile(
    r"\s+[-–—]\s+.*\b(remaster(ed)?|radio edit|edit|version|live|mono|stereo|mix|"
    r"remix|acoustic|explicit|clean|bonus|single|deluxe|from\b|feat\.?|ft\.?)\b.*$", re.I)
_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring|with|&|x)\s+.*$", re.I)


def _cle(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", t).replace("_", " ").split())


def _titres(titre: str) -> list[str]:
    base = _SUFFIXE.sub("", titre).strip()
    sans = _PARENTHESES.sub("", base).strip()
    court = re.split(r"\s+[-–—]\s+", sans)[0].strip()
    return [t for t in dict.fromkeys([titre.strip(), base, sans, court]) if t]


def _artiste(artiste: str) -> str:
    a = re.split(r"\s*[,;/]\s*", artiste or "")[0]
    return _FEAT.sub("", a).strip()


def _score(r: dict, titres: list[str], artiste: str, duree_s: int) -> float:
    if r.get("instrumental") or not (r.get("plainLyrics") or r.get("syncedLyrics")):
        return -1
    nom = _cle(r.get("trackName"))
    cles = [_cle(t) for t in titres]
    if nom in cles:
        score = 3.0
    elif any(c and (c in nom or nom in c) for c in cles):
        score = 2.0
    else:
        return -1
    if artiste:
        a, ra = _cle(artiste), _cle(r.get("artistName"))
        if a and (a in ra or ra in a):
            score += 2
        elif a and a not in nom:
            score -= 1.5
    if duree_s and r.get("duration"):
        ecart = abs(float(r["duration"]) - duree_s)
        score += 1 if ecart <= 3 else (0 if ecart <= 15 else -1)
    if r.get("syncedLyrics"):
        score += 0.3
    return score


def _lyrics_ovh(titre: str, artiste: str) -> dict | None:
    if not artiste:
        return None
    url = "https://api.lyrics.ovh/v1/{}/{}".format(urllib.parse.quote(artiste, safe=""), urllib.parse.quote(titre, safe=""))
    try:
        data = _get(url) or {}
    except Exception:
        return None
    texte = (data.get("lyrics") or "").replace("\r\n", "\n").strip()
    texte = re.sub(r"^Paroles de la chanson .* par .*\n", "", texte)
    return {"trackName": titre, "artistName": artiste, "plainLyrics": texte} if len(texte) > 40 else None


def chercher(titre: str, artiste: str, duree_s: int = 0) -> dict | None:
    if not titre:
        return None
    titres = _titres(titre)
    art = _artiste(artiste)
    candidats: list[dict] = []

    def tenter(chemin: str, **params):
        try:
            res = _get(f"{_API}/{chemin}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v}))
        except Exception:
            return
        if isinstance(res, dict):
            candidats.append(res)
        elif isinstance(res, list):
            candidats.extend(res)

    def meilleur():
        notes = [(_score(r, titres, art, duree_s), r) for r in candidats]
        notes = [n for n in notes if n[0] >= 2]
        return max(notes, key=lambda n: n[0])[1] if notes else None

    etapes = []
    for t in titres[1:] or titres:
        etapes.append(("get", {"track_name": t, "artist_name": art, "duration": duree_s or None}))
    for t in titres[1:] or titres:
        etapes.append(("search", {"track_name": t, "artist_name": art}))
    etapes.append(("search", {"q": f"{titres[-1]} {art}".strip()}))
    etapes.append(("search", {"track_name": titres[-1]}))
    resultat = None
    for chemin, params in etapes:
        tenter(chemin, **params)
        resultat = meilleur()
        if resultat:
            break
    if not resultat:
        for t in titres[-1:] + titres[:1]:
            resultat = _lyrics_ovh(t, art)
            if resultat:
                break
    if not resultat:
        return None

    plain = resultat.get("plainLyrics") or ""
    synced = resultat.get("syncedLyrics") or ""
    if not plain and synced:
        plain = "\n".join(l.split("]", 1)[-1].strip() for l in synced.splitlines())
    if not (plain or synced):
        return None
    return {"plain": plain, "synced": synced}


def integrer(chemin, paroles: dict, lrc: bool = True) -> str:
    chemin = Path(chemin)
    texte = paroles.get("plain") or ""
    ext = chemin.suffix.lower()
    try:
        if ext in (".mp3", ".wav"):
            from mutagen.id3 import ID3, USLT, ID3NoHeaderError
            if ext == ".wav":
                from mutagen.wave import WAVE
                w = WAVE(str(chemin))
                if w.tags is None:
                    w.add_tags()
                w.tags.add(USLT(encoding=3, lang="und", desc="", text=texte))
                w.save()
            else:
                try:
                    id3 = ID3(str(chemin))
                except ID3NoHeaderError:
                    id3 = ID3()
                id3.add(USLT(encoding=3, lang="und", desc="", text=texte))
                id3.save(str(chemin))
        elif ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(str(chemin))
            f["LYRICS"] = [texte]
            f.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(chemin))
            f["\xa9lyr"] = [texte]
            f.save()
        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            f = OggOpus(str(chemin))
            f["LYRICS"] = [texte]
            f.save()
        else:
            return f"unsupported format: {ext}"
    except Exception as exc:
        return f"writing lyrics: {exc}"
    if lrc and paroles.get("synced"):
        try:
            chemin.with_suffix(".lrc").write_text(paroles["synced"], encoding="utf-8")
        except Exception:
            pass
    return ""
