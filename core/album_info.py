from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

_UA = {"User-Agent": "MusicFlow (https://github.com/captentv1/musicflow)"}


def _get(url: str):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]|\s-\s.*$", " ", t)
    return re.sub(r"[\W_]+", " ", t).strip()


def _matches(title_text, artist_name, t2, a2) -> bool:
    n1, n2 = _norm(title_text), _norm(t2)
    if not n1 or not n2 or not (n1 in n2 or n2 in n1):
        return False
    a1 = _norm((artist_name or "").split(",")[0])
    return not a1 or a1 in _norm(a2) or _norm(a2) in a1


_OTHER_VERSION = re.compile(r"\b(live|on stage|concert|unplugged|remix|mix|karaoke|instrumental|acoustic|"
                            r"sped up|slowed|nightcore|cover|tribute|edit|version|remaster(ed)?|demo|session)\b", re.I)


def _other_version(requested_title: str, *names: str) -> bool:
    requested = set(m.group(0).lower() for m in _OTHER_VERSION.finditer(requested_title or ""))
    return any(m.group(0).lower() not in requested for n in names for m in _OTHER_VERSION.finditer(n or ""))


def _clean_album(name_: str) -> str:
    return re.sub(r"\s-\s(Single|EP)$", "", name_ or "").strip()


def _doubtful_itunes(title_text, r) -> bool:
    return (_other_version(title_text, r.get("trackName"), r.get("collectionName"))
            or _norm(r.get("collectionArtistName") or "") in ("various artists", "artistes divers"))


def _itunes(title_text, artist_name, duration_s, album_hint=""):
    q = urllib.parse.urlencode({"term": f"{title_text} {artist_name}".strip(), "entity": "song", "limit": 10})
    res = (_get(f"https://itunes.apple.com/search?{q}") or {}).get("results") or []
    good = [r for r in res if _matches(title_text, artist_name, r.get("trackName"), r.get("artistName"))]
    if not good:
        return None
    hint = _norm(album_hint)
    def key_name(r):
        gap = abs((r.get("trackTimeMillis") or 0) / 1000 - duration_s) if duration_s else 0
        name_ = r.get("collectionName") or ""
        single = name_.endswith(" - Single") or name_.endswith(" - EP") or (r.get("trackCount") or 0) <= 3
        return (0 if hint and _norm(name_) == hint else 1, _doubtful_itunes(title_text, r), gap > 6, single, gap)
    good.sort(key=key_name)
    r = good[0]
    return {
        "album": _clean_album(r.get("collectionName")),
        "genre": r.get("primaryGenreName") or "",
        "piste": r.get("trackNumber") or 0,
        "pistes": r.get("trackCount") or 0,
        "disque": r.get("discNumber") or 0,
        "disques": r.get("discCount") or 0,
        "annee": (r.get("releaseDate") or "")[:4],
        "artiste_album": r.get("collectionArtistName") or r.get("artistName") or "",
        "pochette_hd": re.sub(r"/\d+x\d+bb\.(jpg|png)$", r"/1000x1000bb.\1", r.get("artworkUrl100") or ""),
        "_douteux": _doubtful_itunes(title_text, r),
    }


def _deezer(title_text, artist_name, duration_s):
    good = []
    for search_query in (f'track:"{title_text}" artist:"{artist_name}"' if artist_name else title_text, f"{title_text} {artist_name}".strip()):
        q = urllib.parse.urlencode({"q": search_query})
        data = (_get(f"https://api.deezer.com/search?{q}") or {}).get("data") or []
        good = [d for d in data if _matches(title_text, artist_name, d.get("title"), (d.get("artist") or {}).get("name"))]
        if good:
            break
    if not good:
        return None
    good.sort(key=lambda d: (_other_version(title_text, d.get("title"), (d.get("album") or {}).get("title")),
                             abs((d.get("duration") or 0) - duration_s) if duration_s else 0))
    track_entry = _get(f"https://api.deezer.com/track/{good[0]['id']}")
    album = _get(f"https://api.deezer.com/album/{(track_entry.get('album') or {}).get('id')}") if track_entry.get("album") else {}
    genres = ((album.get("genres") or {}).get("data")) or []
    return {
        "album": _clean_album((track_entry.get("album") or {}).get("title")),
        "genre": genres[0]["name"] if genres else "",
        "piste": track_entry.get("track_position") or 0,
        "pistes": album.get("nb_tracks") or 0,
        "disque": track_entry.get("disk_number") or 0,
        "disques": 0,
        "annee": (track_entry.get("release_date") or album.get("release_date") or "")[:4],
        "artiste_album": (album.get("artist") or {}).get("name") or "",
        "isrc": track_entry.get("isrc") or "",
        "bpm": int(track_entry.get("bpm") or 0),
        "label": album.get("label") or "",
        "pochette_hd": (track_entry.get("album") or {}).get("cover_xl") or "",
        "_douteux": (_other_version(title_text, track_entry.get("title"), (track_entry.get("album") or {}).get("title"))
                     or _norm((album.get("artist") or {}).get("name") or "") in ("various artists", "artistes divers")),
    }


def find_items(title_text: str, artist_name: str, duration_s: int = 0, album_hint: str = "") -> dict | None:
    main_item = None
    for source in (_itunes, _deezer):
        try:
            info_data = source(title_text, artist_name, duration_s, album_hint) if source is _itunes else source(title_text, artist_name, duration_s)
        except Exception:
            info_data = None
        if info_data and info_data.get("album"):
            if main_item is None or (main_item["_douteux"] and not info_data["_douteux"]):
                main_item = info_data
            if not main_item["_douteux"]:
                break
    if main_item is None:
        return None
    main_item.pop("_douteux", None)
    if not main_item.get("isrc"):
        try:
            extra = _deezer(title_text, artist_name, duration_s) or {}
            for key_name in ("isrc", "bpm", "label"):
                if extra.get(key_name):
                    main_item[key_name] = extra[key_name]
        except Exception:
            pass
    return main_item


def cover_art(title_text: str, artist_name: str) -> str:
    artist_name = (artist_name or "").split(",")[0].strip()
    try:
        q = urllib.parse.urlencode({"q": f"{title_text} {artist_name}".strip(), "limit": 5})
        data = (_get(f"https://api.deezer.com/search?{q}") or {}).get("data") or []
        good = [d for d in data if _matches(title_text, artist_name, d.get("title"), (d.get("artist") or {}).get("name"))]
        for d in good:
            url = (d.get("album") or {}).get("cover_medium") or ""
            if url:
                return url
    except Exception:
        pass
    try:
        q = urllib.parse.urlencode({"term": f"{title_text} {artist_name}".strip(), "entity": "song", "limit": 5})
        res = (_get(f"https://itunes.apple.com/search?{q}") or {}).get("results") or []
        for r in res:
            if _matches(title_text, artist_name, r.get("trackName"), r.get("artistName")) and r.get("artworkUrl100"):
                return re.sub(r"/\d+x\d+bb\.(jpg|png)$", r"/300x300bb.\1", r["artworkUrl100"])
    except Exception:
        pass
    return ""


def embed(file_path, info_data: dict) -> str:
    file_path = Path(file_path)
    ext = file_path.suffix.lower()
    track_entry = f"{info_data['piste']}/{info_data['pistes']}" if info_data.get("pistes") else str(info_data.get("piste") or "")
    disc = f"{info_data['disque']}/{info_data['disques']}" if info_data.get("disques") else str(info_data.get("disque") or "")
    try:
        if ext in (".mp3", ".wav"):
            from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TCON, TRCK, TPOS, TDRC, TPE2, TSRC, TBPM, TPUB
            if ext == ".wav":
                from mutagen.wave import WAVE
                f = WAVE(str(file_path))
                if f.tags is None:
                    f.add_tags()
                tags = f.tags
            else:
                try:
                    tags = ID3(str(file_path))
                except ID3NoHeaderError:
                    tags = ID3()
            for frame, val in ((TALB, info_data.get("album")), (TCON, info_data.get("genre")), (TRCK, track_entry),
                               (TPOS, disc), (TDRC, info_data.get("annee")), (TPE2, info_data.get("artiste_album")),
                               (TSRC, info_data.get("isrc")), (TBPM, info_data.get("bpm")), (TPUB, info_data.get("label"))):
                if val and val != "0":
                    tags.add(frame(encoding=3, text=str(val)))
            if ext == ".wav":
                f.save()
            else:
                tags.save(str(file_path))
        elif ext in (".flac", ".opus", ".ogg"):
            if ext == ".flac":
                from mutagen.flac import FLAC
                f = FLAC(str(file_path))
            else:
                from mutagen.oggopus import OggOpus
                f = OggOpus(str(file_path))
            fields_in = {"ALBUM": info_data.get("album"), "GENRE": info_data.get("genre"),
                      "TRACKNUMBER": info_data.get("piste"), "TRACKTOTAL": info_data.get("pistes"),
                      "DISCNUMBER": info_data.get("disque"), "DISCTOTAL": info_data.get("disques"),
                      "DATE": info_data.get("annee"), "ALBUMARTIST": info_data.get("artiste_album"),
                      "ISRC": info_data.get("isrc"), "BPM": info_data.get("bpm"), "LABEL": info_data.get("label"),
                      "ORGANIZATION": info_data.get("label")}
            for key_name, val in fields_in.items():
                if val and str(val) != "0":
                    f[key_name] = [str(val)]
            f.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(file_path))
            if info_data.get("album"):
                f["\xa9alb"] = [info_data["album"]]
            if info_data.get("genre"):
                f["\xa9gen"] = [info_data["genre"]]
            if info_data.get("annee"):
                f["\xa9day"] = [info_data["annee"]]
            if info_data.get("artiste_album"):
                f["aART"] = [info_data["artiste_album"]]
            if info_data.get("piste"):
                f["trkn"] = [(int(info_data["piste"]), int(info_data.get("pistes") or 0))]
            if info_data.get("disque"):
                f["disk"] = [(int(info_data["disque"]), int(info_data.get("disques") or 0))]
            if info_data.get("bpm"):
                f["tmpo"] = [int(info_data["bpm"])]
            from mutagen.mp4 import MP4FreeForm
            for key_name, val in (("ISRC", info_data.get("isrc")), ("LABEL", info_data.get("label"))):
                if val:
                    f[f"----:com.apple.iTunes:{key_name}"] = [MP4FreeForm(str(val).encode("utf-8"))]
            f.save()
        else:
            return f"unsupported format: {ext}"
    except Exception as exc:
        return f"writing album info: {exc}"
    return ""
