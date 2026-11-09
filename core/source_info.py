from __future__ import annotations

import re

import audio_check

_CHANNEL_RE = re.compile(r"\s*(-\s*topic|vevo|official|officiel)\s*$", re.IGNORECASE)


def source_artist(info: dict) -> str:
    artist_names = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    if artist_names:
        return ", ".join(artist_names)
    raw_text = info.get("creator") or info.get("channel") or info.get("uploader") or ""
    return _CHANNEL_RE.sub("", raw_text).strip()


def complete_title_artist(info: dict, title_text: str, artist_name: str):
    info = info or {}
    if not artist_name:
        artist_name = source_artist(info)
    if not title_text:
        title_text = info.get("track") or info.get("title") or ""
    return title_text, artist_name


def fallback_album_info(info: dict, title_text: str, artist_name: str, album_hint: str = "") -> dict:
    info = info or {}
    year = str(info.get("release_year") or "") or (info.get("upload_date") or "")[:4]
    return {
        "album": info.get("album") or album_hint or title_text,
        "album_artist": (artist_name or "").split(",")[0].strip(),
        "year": year,
        "genre": (info.get("genres") or [""])[0] if isinstance(info.get("genres"), list) else "",
        "track_info": 1 if not (info.get("album") or album_hint) else 0,
        "tracks_index": 1 if not (info.get("album") or album_hint) else 0,
    }


def thumbnail_jpg(info: dict) -> str:
    info = info or {}
    vid = info.get("id") or ""
    if info.get("extractor_key", "").lower().startswith("youtube") and vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    for t in reversed(info.get("thumbnails") or []):
        url = t.get("url") or ""
        if url.split("?")[0].lower().endswith((".jpg", ".jpeg", ".png")):
            return url
    url = info.get("thumbnail") or ""
    return "" if ".webp" in url.lower() else url


def reject_preview(file_path, expected_duration) -> None:
    expected = audio_check.to_seconds(expected_duration)
    actual = audio_check.file_duration(file_path)
    if expected >= 60 and 0 < actual < expected * 0.5:
        try:
            from pathlib import Path
            Path(file_path).unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"only an excerpt was available ({int(actual)} s instead of {expected} s)")
