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

_ATTEMPTS = 4


def _get(url: str):
    for attempt in range(_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == _ATTEMPTS - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == _ATTEMPTS - 1:
                raise
        time.sleep(0.8 * (attempt + 1))


_BRACKETS = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_SUFFIX = re.compile(
    r"\s+[-–—]\s+.*\b(remaster(ed)?|radio edit|edit|version|live|mono|stereo|mix|"
    r"remix|acoustic|explicit|clean|bonus|single|deluxe|from\b|feat\.?|ft\.?)\b.*$", re.I)
_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring|with|&|x)\s+.*$", re.I)


def _key(text_value: str) -> str:
    t = unicodedata.normalize("NFKD", text_value or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", t).replace("_", " ").split())


def _titles(title_text: str) -> list[str]:
    base = _SUFFIX.sub("", title_text).strip()
    without = _BRACKETS.sub("", base).strip()
    short = re.split(r"\s+[-–—]\s+", without)[0].strip()
    return [t for t in dict.fromkeys([title_text.strip(), base, without, short]) if t]


def _artist(artist_name: str) -> str:
    a = re.split(r"\s*[,;/]\s*", artist_name or "")[0]
    return _FEAT.sub("", a).strip()


def _score(r: dict, titles: list[str], artist_name: str, duration_s: int) -> float:
    if r.get("instrumental") or not (r.get("plainLyrics") or r.get("syncedLyrics")):
        return -1
    name_ = _key(r.get("trackName"))
    key_list = [_key(t) for t in titles]
    if name_ in key_list:
        score = 3.0
    elif any(c and (c in name_ or name_ in c) for c in key_list):
        score = 2.0
    else:
        return -1
    if artist_name:
        a, ra = _key(artist_name), _key(r.get("artistName"))
        if a and (a in ra or ra in a):
            score += 2
        elif a and a not in name_:
            score -= 1.5
    if duration_s and r.get("duration"):
        gap = abs(float(r["duration"]) - duration_s)
        score += 1 if gap <= 3 else (0 if gap <= 15 else -1)
    if r.get("syncedLyrics"):
        score += 0.3
    return score


def _lyrics_ovh(title_text: str, artist_name: str) -> dict | None:
    if not artist_name:
        return None
    url = "https://api.lyrics.ovh/v1/{}/{}".format(urllib.parse.quote(artist_name, safe=""), urllib.parse.quote(title_text, safe=""))
    try:
        data = _get(url) or {}
    except Exception:
        return None
    text_value = (data.get("lyrics") or "").replace("\r\n", "\n").strip()
    text_value = re.sub(r"^Paroles de la chanson .* par .*\n", "", text_value)
    return {"trackName": title_text, "artistName": artist_name, "plainLyrics": text_value} if len(text_value) > 40 else None


def find_items(title_text: str, artist_name: str, duration_s: int = 0) -> dict | None:
    if not title_text:
        return None
    titles = _titles(title_text)
    art = _artist(artist_name)
    candidates: list[dict] = []

    def attempt_fn(file_path: str, **params):
        try:
            res = _get(f"{_API}/{file_path}?" + urllib.parse.urlencode({k: v for k, v in params.items() if v}))
        except Exception:
            return
        if isinstance(res, dict):
            candidates.append(res)
        elif isinstance(res, list):
            candidates.extend(res)

    def best():
        notes = [(_score(r, titles, art, duration_s), r) for r in candidates]
        notes = [n for n in notes if n[0] >= 2]
        return max(notes, key=lambda n: n[0])[1] if notes else None

    steps = []
    for t in titles[1:] or titles:
        steps.append(("get", {"track_name": t, "artist_name": art, "duration": duration_s or None}))
    for t in titles[1:] or titles:
        steps.append(("search", {"track_name": t, "artist_name": art}))
    steps.append(("search", {"q": f"{titles[-1]} {art}".strip()}))
    steps.append(("search", {"track_name": titles[-1]}))
    result_item = None
    for file_path, params in steps:
        attempt_fn(file_path, **params)
        result_item = best()
        if result_item:
            break
    if not result_item:
        for t in titles[-1:] + titles[:1]:
            result_item = _lyrics_ovh(t, art)
            if result_item:
                break
    if not result_item:
        return None

    plain = result_item.get("plainLyrics") or ""
    synced = result_item.get("syncedLyrics") or ""
    if not plain and synced:
        plain = "\n".join(l.split("]", 1)[-1].strip() for l in synced.splitlines())
    if not (plain or synced):
        return None
    return {"plain": plain, "synced": synced}


def embed(file_path, lyrics_data: dict, lrc: bool = True) -> str:
    file_path = Path(file_path)
    text_value = lyrics_data.get("plain") or ""
    ext = file_path.suffix.lower()
    try:
        if ext in (".mp3", ".wav"):
            from mutagen.id3 import ID3, USLT, ID3NoHeaderError
            if ext == ".wav":
                from mutagen.wave import WAVE
                w = WAVE(str(file_path))
                if w.tags is None:
                    w.add_tags()
                w.tags.add(USLT(encoding=3, lang="und", desc="", text=text_value))
                w.save()
            else:
                try:
                    id3 = ID3(str(file_path))
                except ID3NoHeaderError:
                    id3 = ID3()
                id3.add(USLT(encoding=3, lang="und", desc="", text=text_value))
                id3.save(str(file_path))
        elif ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(str(file_path))
            f["LYRICS"] = [text_value]
            f.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(file_path))
            f["\xa9lyr"] = [text_value]
            f.save()
        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            f = OggOpus(str(file_path))
            f["LYRICS"] = [text_value]
            f.save()
        else:
            return f"unsupported format: {ext}"
    except Exception as exc:
        return f"writing lyrics: {exc}"
    if lrc and lyrics_data.get("synced"):
        try:
            file_path.with_suffix(".lrc").write_text(lyrics_data["synced"], encoding="utf-8")
        except Exception:
            pass
    return ""
