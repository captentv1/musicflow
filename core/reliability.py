from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

_UA = {"User-Agent": "Mozilla/5.0 MusicFlow"}

_REASONS = [
    (r"sign in to confirm|not a bot|429|too many requests", "YouTube is limiting downloads from this connection.",
     "Wait a few minutes or switch networks (Wi-Fi ↔ 4G), then “Retry failed”."),
    (r"age|confirm your age|inappropriate", "Age-restricted video on YouTube.",
     "“Wrong version? Pick another one” or “Versions” to choose another video."),
    (r"private|unavailable|removed|not available|copyright|blocked", "Video unavailable (removed, private or blocked in your country).",
     "Another video is tried automatically; otherwise use “Versions”."),
    (r"introuvable|not found|no video|no youtube match|no reliable|aucune correspondance|aucune autre", "Track not found on YouTube.",
     "Check the title/artist, or search for it manually in Search."),
    (r"connexion|connection|network|timed out|timeout|getaddrinfo|unreachable|ssl", "Internet connection problem.",
     "It will be retried automatically when the connection is back."),
    (r"postprocessing|conversion|ffmpeg", "File conversion failed.",
     "Try another format (MP3 instead of FLAC/Opus) in the settings."),
    (r"espace|storage|no space|disk", "Not enough storage space.",
     "Free up space or choose another folder."),
    (r"permission|denied|accès", "The destination folder is not accessible.",
     "Choose the download folder again."),
]


def explain(message: str) -> dict:
    m = (message or "").lower()
    for pattern, reason, advice in _REASONS:
        if re.search(pattern, m):
            return {"raison": reason, "conseil": advice}
    return {"raison": "Unexpected error.", "conseil": "Try again; if it happens again, export the error log (Settings → Diagnostics)."}


def _probe(name_, func):
    start_at = time.time()
    try:
        detail = func() or "OK"
        return {"nom": name_, "ok": True, "detail": str(detail)[:160], "ms": int((time.time() - start_at) * 1000)}
    except Exception as exc:
        return {"nom": name_, "ok": False, "detail": str(exc)[:160], "ms": int((time.time() - start_at) * 1000)}


def _http_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def _at_least_one(items_list, if_empty="no results"):
    if not items_list:
        raise RuntimeError(if_empty)
    return f"{len(items_list)} result(s)"


def diagnostic(search_videos, spotify_search, ffmpeg_ok) -> list[dict]:
    tests = [
        ("Internet", lambda: _http_json("https://api.deezer.com/infos") and "joignable"),
        ("YouTube (search)", lambda: _at_least_one(search_videos("daft punk one more time", limit=2))),
        ("Spotify (search)", lambda: _at_least_one(spotify_search("one more time daft punk", 3),
                                                     "no results (search will go through YouTube)")),
        ("Deezer (artistes, albums)", lambda: _http_json("https://api.deezer.com/search?q=daft%20punk&limit=1")["data"][0]["title"]),
        ("iTunes (infos d'album)", lambda: _http_json("https://itunes.apple.com/search?term=daft+punk&entity=song&limit=1")["results"][0]["collectionName"]),
        ("LRCLIB (lyrics)", lambda: "OK" if _http_json("https://lrclib.net/api/search?q=one%20more%20time%20daft%20punk") else "empty"),
        ("Conversion audio (ffmpeg)", lambda: "OK" if ffmpeg_ok() else (_ for _ in ()).throw(RuntimeError("ffmpeg indisponible"))),
    ]
    return [_probe(n, f) for n, f in tests]


def clean(folder_path) -> dict:
    d = Path(folder_path)
    n, byte_count = 0, 0
    try:
        for f in d.iterdir():
            name_ = f.name.lower()
            if f.is_file() and (name_.endswith((".part", ".ytdl", ".webp")) or ".temp." in name_ or ".conv." in name_
                                or ".cover." in name_ or name_.endswith(".part-frag")):
                try:
                    byte_count += f.stat().st_size
                    f.unlink()
                    n += 1
                except Exception:
                    pass
    except Exception:
        pass
    return {"fichiers": n, "octets": byte_count}
