from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor


def _format_duration(seconds) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _yt_dlp():
    import yt_dlp
    return yt_dlp


def enable_js_engines(yt_dlp) -> None:
    if getattr(yt_dlp.YoutubeDL, "_mf_js", False):
        return
    base = yt_dlp.YoutubeDL

    class YoutubeDL(base):
        _mf_js = True

        def __init__(self, params=None, *args, **kwargs):
            params = dict(params or {})
            params.setdefault("js_runtimes", {"deno": {}, "node": {}, "bun": {}, "quickjs": {}})
            super().__init__(params, *args, **kwargs)

    yt_dlp.YoutubeDL = YoutubeDL


_OPTS_FLAT = {"extract_flat": "in_playlist", "skip_download": True, "quiet": True, "no_warnings": True}


def _ytmusic_details(entry: dict) -> dict | None:
    vid = entry.get("id")
    if not vid:
        return None
    try:
        with _yt_dlp().YoutubeDL({"skip_download": True, "quiet": True, "no_warnings": True,
                                  "noplaylist": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
    except Exception:
        return None
    artist_names = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    artist_name = ", ".join(artist_names) or info.get("channel") or info.get("uploader") or ""
    thumbs = info.get("thumbnails") or []
    return {
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": info.get("track") or info.get("title") or entry.get("title") or "",
        "uploader": f"{artist_name} - Topic",
        "duration": _format_duration(info.get("duration")),
        "thumbnail": info.get("thumbnail") or (thumbs[-1]["url"] if thumbs else ""),
        "source_audio": "YouTube Music",
    }


def search_ytmusic(search_query: str, limit_n: int = 3) -> list[dict]:
    url = "https://music.youtube.com/search?q=" + urllib.parse.quote(search_query) + "#songs"
    try:
        with _yt_dlp().YoutubeDL(dict(_OPTS_FLAT, playlistend=limit_n)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return []
    entries = [e for e in (info.get("entries") or []) if e.get("id")][:limit_n]
    if not entries:
        return []
    with ThreadPoolExecutor(max_workers=len(entries)) as pool:
        return [r for r in pool.map(_ytmusic_details, entries) if r]


def search_soundcloud(search_query: str, limit_n: int = 6, expected_duration_s: int = 0) -> list[dict]:
    try:
        with _yt_dlp().YoutubeDL(_OPTS_FLAT) as ydl:
            info = ydl.extract_info(f"scsearch{limit_n}:{search_query}", download=False)
    except Exception:
        return []
    result_items = []
    for e in info.get("entries") or []:
        duration = e.get("duration") or 0
        if duration and duration <= 31 and (expected_duration_s == 0 or expected_duration_s > 45):
            continue
        thumbs = e.get("thumbnails") or []
        result_items.append({
            "id": "sc" + str(e.get("id") or ""),
            "url": e.get("webpage_url") or e.get("url") or "",
            "title": e.get("title") or "",
            "uploader": e.get("uploader") or ", ".join(e.get("artists") or []),
            "duration": _format_duration(duration),
            "thumbnail": thumbs[-1]["url"] if thumbs else "",
            "source_audio": "SoundCloud",
        })
    return [r for r in result_items if r["url"]]


def fallback_query(title_text: str, artist_name: str) -> str:
    import video_choice
    return f"{video_choice._core_title(title_text)} {(artist_name or '').split(',')[0].strip()}".strip()


def best_fallback(title_text: str, artist_name: str, duration, exclude=()):
    import video_choice
    import audio_check
    search_query = fallback_query(title_text, artist_name)
    best, note = None, float("-inf")
    for find_items in (lambda: search_ytmusic(search_query),
                     lambda: search_soundcloud(search_query, expected_duration_s=audio_check.to_seconds(duration))):
        candidates = [c for c in find_items() if c.get("id") not in exclude]
        m, n = video_choice.choose(candidates, title_text, artist_name, duration, search_query)
        if m and n > note:
            best, note = m, n
        if video_choice.reliable(best, title_text, note):
            break
    return best, note
