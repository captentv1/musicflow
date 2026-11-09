try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
import difflib
import json
import re
import unicodedata
import hashlib
import os
import sys
import hmac
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(ROOT / "core"))

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory
import imageio_ffmpeg
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.wave import WAVE

import secrets_store as store
import video_choice
import spotify_client
import spotify_scan
import youtube_client
import audio_check
import lyrics
import album_info
import other_links
import tag_quality
import artists
import library
import reliability
import discover
import fallback_sources
import source_info
import identity

BASE_DIR = Path(__file__).resolve().parent
_ONEDRIVE_DESKTOP = Path.home() / "OneDrive" / "Bureau"
DEFAULT_DEST = (_ONEDRIVE_DESKTOP / "MusicFlow" / "Downloads") if _ONEDRIVE_DESKTOP.exists()     else (Path.home() / "Music" / "MusicFlow")
DEFAULT_DEST.mkdir(parents=True, exist_ok=True)
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
identity.configure_index(store.DATA_DIR / "identites.json")
fallback_sources.enable_js_engines(yt_dlp)

app = Flask(__name__, static_folder=None)

API_TOKEN = secrets.token_urlsafe(32)
_TOKEN_IN_PAGE = True
_LOCAL_HOSTS = {"127.0.0.1:5090", "localhost:5090"}


# Security: every /api/ call needs the page token and a local Host (blocks other sites, other apps, DNS rebinding).
@app.before_request
def _protect_api():
    if request.host not in _LOCAL_HOSTS:
        return "Forbidden", 403
    if request.path.startswith("/api/") and not hmac.compare_digest(
            request.headers.get("X-MF-Token", ""), API_TOKEN):
        return jsonify({"error": "Forbidden"}), 403


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


def _page():
    page = (BASE_DIR / "index.html" if getattr(sys, "frozen", False) else ROOT / "web" / "index.html").read_text(encoding="utf-8")
    if _TOKEN_IN_PAGE:
        page = page.replace("<head>", f"<head><script>window.MF_TOKEN='{API_TOKEN}'</script>", 1)
    resp = app.response_class(page, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


_OAUTH_STATES: dict[str, float] = {}
_OAUTH_STATES_LOCK = threading.Lock()


def _new_oauth_state() -> str:
    state = uuid.uuid4().hex
    with _OAUTH_STATES_LOCK:
        _OAUTH_STATES[state] = time.time()
    return state


def _consume_oauth_state(state: str) -> bool:
    with _OAUTH_STATES_LOCK:
        ts = _OAUTH_STATES.pop(state, None)
    return bool(ts and time.time() - ts < 600)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name or "")
    name = re.sub(r"[\U00010000-\U0010FFFF]", "", name)
    name = re.sub(r"[←-⇿⌀-⏿①-⓿■-➿"
                  r"⤀-⥿⬀-⯿︀-️​-‏]", "", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if not name:
        return "musique"
    try:
        os.fsencode(name)
    except Exception:
        fallback = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        fallback = re.sub(r"\s+", " ", fallback).strip()
        name = fallback or ("musique_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8])
    return name[:150]


def _log(job_id: str, message: str):
    with JOBS_LOCK:
        JOBS[job_id]["log"].append(message)


def format_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def search_videos(title: str, limit: int = 8):
    query = title
    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    results = []
    for e in info.get("entries") or []:
        thumbs = e.get("thumbnails") or []
        results.append(
            {
                "id": e.get("id"),
                "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                "title": e.get("title") or "",
                "uploader": e.get("uploader") or e.get("channel") or "",
                "duration": format_duration(e.get("duration")),
                "thumbnail": thumbs[-1]["url"] if thumbs else "",
            }
        )
    return results


def _http_get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


_SPOTIFY_NEXT_DATA_RE = re.compile(
    r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>', re.S
)


def _spotify_entity(kind: str, spotify_id: str) -> dict:
    html = _http_get_text(f"https://open.spotify.com/embed/{kind}/{spotify_id}")
    m = _SPOTIFY_NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError("Cannot read the data of this Spotify link.")
    data = json.loads(m.group(1))
    return data["props"]["pageProps"]["state"]["data"]["entity"]


def _spotify_image(entity: dict) -> str:
    imgs = ((entity.get("visualIdentity") or {}).get("image")) or []
    if imgs:
        return imgs[0].get("url", "")
    sources = ((entity.get("coverArt") or {}).get("sources")) or []
    if sources:
        return sources[0].get("url", "")
    return ""


def _parse_spotify_path(url: str):
    parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
    for i, p in enumerate(parts):
        if p in ("track", "playlist", "album", "artist") and i + 1 < len(parts):
            return p, parts[i + 1]
    return None, None


def resolve_link(url: str):
    other = other_links.resolve_target(url)
    if other is not None:
        return other
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()

    if "spotify.com" in host:
        kind, spotify_id = _parse_spotify_path(url)
        if not kind or not spotify_id:
            raise ValueError("Unrecognized Spotify link (expected a track, album or playlist).")
        entity = _spotify_entity(kind, spotify_id)

        if kind == "track":
            artists = ", ".join(a.get("name", "") for a in entity.get("artists") or [])
            title = entity.get("name") or entity.get("title") or ""
            return [
                {
                    "source": "spotify",
                    "id": None,
                    "url": None,
                    "title": title,
                    "uploader": artists,
                    "duration": format_duration(round((entity.get("duration") or 0) / 1000)),
                    "thumbnail": _spotify_image(entity),
                    "query": f"{title} {artists}".strip(),
                }
            ], None

        full = spotify_client.fetch_full_tracklist(kind, spotify_id)
        if full is not None:
            name, tracks = full
            items = []
            for t in tracks:
                title = t.get("name") or ""
                artists = ", ".join(a.get("name", "") for a in t.get("artists") or [])
                images = ((t.get("album") or {}).get("images")) or []
                items.append(
                    {
                        "source": "spotify",
                        "id": None,
                        "url": None,
                        "title": title,
                        "uploader": artists,
                        "duration": format_duration(round((t.get("duration_ms") or 0) / 1000)),
                        "thumbnail": images[-1]["url"] if images else _spotify_image(entity),
                        "query": f"{title} {artists}".strip(),
                    }
                )
            if not items:
                raise ValueError("No tracks found in this Spotify playlist.")
            return items, (name or entity.get("name") or "Playlist Spotify")

        playlist_thumb = _spotify_image(entity)
        items = []
        for t in entity.get("trackList") or []:
            title = t.get("title") or ""
            artists = (t.get("subtitle") or "").replace("\xa0", " ")
            items.append(
                {
                    "source": "spotify",
                    "id": None,
                    "url": None,
                    "title": title,
                    "uploader": artists,
                    "duration": format_duration(round((t.get("duration") or 0) / 1000)),
                    "thumbnail": playlist_thumb,
                    "uri": t.get("uri") or "",
                    "query": f"{title} {artists}".strip(),
                }
            )
        if not items:
            raise ValueError("No tracks found in this Spotify playlist.")
        name = entity.get("name") or entity.get("title") or "Playlist Spotify"
        if len(items) >= 100:
            name += " — limited list (first 100 tracks)"
        return items, name

    if "youtube.com" in host or "youtu.be" in host:
        qs = urllib.parse.parse_qs(parsed.query)
        is_playlist = parsed.path.rstrip("/").endswith("/playlist") and "list" in qs

        if is_playlist:
            playlist_url = f"https://www.youtube.com/playlist?list={qs['list'][0]}"
            ydl_opts = {
                "extract_flat": "in_playlist",
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(playlist_url, download=False)
            items = []
            for e in info.get("entries") or []:
                thumbs = e.get("thumbnails") or []
                items.append(
                    {
                        "source": "youtube",
                        "id": e.get("id"),
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                        "title": e.get("title") or "",
                        "uploader": e.get("uploader") or e.get("channel") or "",
                        "duration": format_duration(e.get("duration")),
                        "thumbnail": thumbs[-1]["url"] if thumbs else "",
                        "query": None,
                    }
                )
            if not items:
                raise ValueError("No videos found in this YouTube playlist.")
            return items, (info.get("title") or "Playlist YouTube")

        ydl_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        thumbs = info.get("thumbnails") or []
        return [
            {
                "source": "youtube",
                "id": info.get("id"),
                "url": info.get("webpage_url") or url,
                "title": info.get("title") or "",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "duration": format_duration(info.get("duration")),
                "thumbnail": thumbs[-1]["url"] if thumbs else (info.get("thumbnail") or ""),
                "query": None,
            }
        ], None

    raise ValueError("Unrecognized link — use a Spotify, Deezer, YouTube, YouTube Music or Apple Music link.")


VALID_QUALITIES = {"128", "192", "320", "flac", "wav", "opus", "aac"}
_LOSSLESS_FORMATS = {"flac", "wav"}


_MAX_RESUMES = 6
_RETRY_WAIT = (3, 5, 10, 15, 30, 45)


def _is_network_error(exc) -> bool:
    text_value = f"{type(exc).__name__}: {exc}".lower()
    network = (
        "timed out", "timeout", "connection", "connection", "unreachable", "reset by peer",
        "temporary failure", "name resolution", "getaddrinfo", "network", "réseau",
        "incomplete read", "incompleteread", "content too short", "remote end closed",
        "broken pipe", "chunked", "premature end",
        "ssl", "http error 5", "unable to download", "read operation",
    )
    final_value = (
        "video unavailable", "private video", "removed", "copyright", "age-restricted",
        "not available", "sign in to confirm", "members-only", "requested format",
    )
    if any(m in text_value for m in final_value):
        return False
    return any(m in text_value for m in network)


class _Interrupted(Exception):
    pass


def _control(job_id: str):
    with JOBS_LOCK:
        return (JOBS.get(job_id) or {}).get("control")


def _clean_partials(dest_path, safe_title: str):
    try:
        for f in dest_path.glob(safe_title + ".*"):
            if f.suffix in (".part", ".ytdl", ".webp", ".jpg", ".jpeg", ".png") or f.name.endswith(".part"):
                f.unlink(missing_ok=True)
    except Exception:
        pass


def _write_tags_file(file_path, title_text: str, artist_name: str) -> str:
    if str(file_path).lower().rsplit(".", 1)[-1] in ("m4a", "opus", "ogg"):
        return tag_quality.write_base(file_path, title_text, artist_name)
    return _write_tags_id3_flac(file_path, title_text, artist_name)


def _write_tags_id3_flac(file_path, title_text: str, artist_name: str) -> str:
    if not title_text and not artist_name:
        return "no metadata to write"
    ext = str(file_path).lower().rsplit(".", 1)[-1]
    try:
        if ext == "flac":
            audio = FLAC(str(file_path))
            if title_text:
                audio["title"] = title_text
            if artist_name:
                audio["artist"] = artist_name
                audio["albumartist"] = artist_name
            audio.save()
            return ""
        if ext == "wav":
            wav = WAVE(str(file_path))
            if wav.tags is None:
                wav.add_tags()
            if title_text:
                wav.tags.add(TIT2(encoding=3, text=title_text))
            if artist_name:
                wav.tags.add(TPE1(encoding=3, text=artist_name))
                wav.tags.add(TPE2(encoding=3, text=artist_name))
            wav.save()
            return ""
        try:
            id3 = ID3(str(file_path))
        except ID3NoHeaderError:
            id3 = ID3()
        if title_text:
            id3.add(TIT2(encoding=3, text=title_text))
        if artist_name:
            id3.add(TPE1(encoding=3, text=artist_name))
            id3.add(TPE2(encoding=3, text=artist_name))
        id3.save(str(file_path))
        return ""
    except Exception as exc:
        return f"writing metadata: {exc}"


def _download_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


_SPOTIFY_IMG_RE = re.compile(r"(i\.scdn\.co/image/)[0-9a-f]{16}([0-9a-f]{24})", re.IGNORECASE)


def _hires_cover(url: str) -> str:
    return _SPOTIFY_IMG_RE.sub(r"\1ab67616d0000b273\2", url)


def _embed_cover_spotify(file_path, cover_url: str) -> str:
    data = _download_image(_hires_cover(cover_url))
    if not data:
        return "Spotify cover art unavailable"
    mime = "image/png" if cover_url.split("?")[0].lower().endswith(".png") else "image/jpeg"
    ext = str(file_path).lower().rsplit(".", 1)[-1]
    if ext in ("m4a", "opus", "ogg"):
        return tag_quality.cover_art(file_path, data, mime)
    try:
        if ext == "flac":
            audio = FLAC(str(file_path))
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = data
            audio.add_picture(pic)
            audio.save()
            return ""
        if ext == "wav":
            wav = WAVE(str(file_path))
            if wav.tags is None:
                wav.add_tags()
            wav.tags.delall("APIC")
            wav.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            wav.save()
            return ""
        try:
            id3 = ID3(str(file_path))
        except ID3NoHeaderError:
            id3 = ID3()
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        id3.save(str(file_path))
        return ""
    except Exception as exc:
        return f"writing cover art: {exc}"


def first_artist(artist_names: str) -> str:
    a = (artist_names or "").strip()
    if not a:
        return ""
    for sep in (",", " feat. ", " feat ", " ft. ", " ft ", " featuring "):
        i = a.lower().find(sep.lower())
        if i > 0:
            a = a[:i]
    return a.strip(" ,&-")


def _guess_initial_title_artist(save_name: str, title_text: str, artist_name: str):
    title_text = (title_text or "").strip()
    artist_name = first_artist((artist_name or "").strip())
    if not title_text:
        track_items = save_name.rsplit(" - ", 1)
        if len(track_items) == 2:
            title_text = track_items[0].strip()
            artist_name = artist_name or first_artist(track_items[1].strip())
        else:
            title_text = save_name.strip()
    return title_text, artist_name


_JUNK_TITLE_RE = re.compile(
    r"[\(\[][^)\]]*\b(lyrics?|paroles?|clip\s*officiel|officiel|official|"
    r"music\s*video|lyric\s*video|video|audio|visuali[sz]er|hd|hq|4k|mv)\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def _comparison_key(text_value: str) -> str:
    t = unicodedata.normalize("NFKD", (text_value or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\((feat|ft|with)[^)]*\)|\[[^\]]*\]", " ", t)
    return " ".join(re.findall(r"\w+", t))


def _match_plausible(search_query: str, found_title: str, expected_artist: str = "", found_artist: str = "") -> bool:
    a, b = _comparison_key(search_query), _comparison_key(found_title)
    if not a or not b:
        return False
    if not (f" {b} " in f" {a} " or f" {a} " in f" {b} "
            or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6):
        return False
    x, y = _comparison_key(expected_artist), _comparison_key(found_artist)
    if x and y and not (set(x.split()) & set(y.split())) \
            and difflib.SequenceMatcher(None, x, y).ratio() < 0.5:
        return False
    return True


def _clean_search_title(title_text: str) -> str:
    cleaned = _JUNK_TITLE_RE.sub("", title_text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned or title_text.strip()


def run_download(job_id: str, video_url: str, save_name: str, dest_folder: str, quality: str = "flac",
                 title_text: str = "", artist_name: str = "", cover: str = "", expected_duration: str = ""):
    if quality not in VALID_QUALITIES:
        quality = "flac"
    with JOBS_LOCK:
        opts = dict(JOBS.get(job_id, {}).get("opts") or {})
    dest_path = Path(dest_folder) if dest_folder else DEFAULT_DEST
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Cannot create the destination folder: {exc}")
        return

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    _log(job_id, f"Preparing download: “{save_name}”…")

    guess_title, guess_artist = _guess_initial_title_artist(save_name, title_text, artist_name)
    cover_url = (cover or "").strip()
    tag_title, tag_artist = guess_title, guess_artist
    if not cover_url:
        title_query = _clean_search_title(guess_title)
        match = spotify_client.search_track(title_query, guess_artist) or spotify_scan.search_track_item(title_query, guess_artist)
        if match and match.get("title") and _match_plausible(title_query, match["title"], guess_artist, match.get("artist") or ""):
            tag_title = match["title"]
            tag_artist = first_artist(match.get("artist") or "") or guess_artist
            cover_url = match.get("cover") or ""
            _log(job_id, f"Spotify match found: “{tag_title}” — {tag_artist or 'unknown artist'}.")
        elif match:
            _log(job_id, f"Spotify result discarded (“{match.get('title', '')}” does not match “{title_query}”).")

    tag_title, tag_artist = tag_quality.clean(tag_title, tag_artist, opts.get("feat", False), opts.get("case_fix", False))
    safe_title = sanitize_filename(f"{tag_title} - {tag_artist}" if tag_artist else tag_title)
    extension = {"flac": "flac", "wav": "wav", "opus": "opus", "aac": "m4a"}.get(quality, "mp3")

    identity.index_folder(dest_path)
    track_keys = list(dict.fromkeys(identity.key_list(tag_title, tag_artist, identity.video_id_of(video_url))
                                    + identity.key_list(guess_title, guess_artist)
                                    + (identity.name_keys(save_name) if not (title_text or "").strip() else [])))
    if opts.get("version"):
        track_keys = [f"x:{identity._norm(tag_title)}|{identity.norm_artist(tag_artist)}"]
    existing = identity.find_existing(track_keys)
    if existing and opts.get("force"):
        _log(job_id, f"Replacing “{Path(existing).name}” with another version.")
        try:
            Path(existing).unlink(missing_ok=True)
            Path(existing).with_suffix(".lrc").unlink(missing_ok=True)
        except Exception:
            pass
        existing = None
    if existing:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = existing
            JOBS[job_id]["duplicate"] = True
        _log(job_id, f"Duplicate detected — this song is already in your library: “{existing}”. Download skipped.")
        return
    if not identity.reserve_keys(track_keys):
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["duplicate"] = True
        _log(job_id, "Duplicate detected — this song is already being downloaded under another name.")
        return
    try:
        _run_download_suite(job_id, video_url, dest_path, quality, opts, tag_title, tag_artist, cover_url,
                            expected_duration, safe_title, extension, track_keys)
    finally:
        identity.release(track_keys)


def _run_download_suite(job_id, video_url, dest_path, quality, opts, tag_title, tag_artist, cover_url,
                        expected_duration, safe_title, extension, track_keys):
    duplicate_path = dest_path / f"{safe_title}.{extension}"
    if not duplicate_path.exists():
        duplicate_path = audio_check.duplicate_in_folder(dest_path, safe_title) or duplicate_path
    if duplicate_path.exists() and opts.get("force"):
        _log(job_id, f"Replacing “{duplicate_path.name}” with another version.")
        try:
            duplicate_path.unlink()
            duplicate_path.with_suffix(".lrc").unlink(missing_ok=True)
        except Exception:
            pass
        duplicate_path = dest_path / f"{safe_title}.{extension}"
    if duplicate_path.exists():
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(duplicate_path)
            JOBS[job_id]["duplicate"] = True
        _log(job_id, f"Duplicate detected — “{duplicate_path.name}” is already in the folder, download skipped.")
        return
    if not audio_check.reserve_keys(duplicate_path):
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(duplicate_path)
            JOBS[job_id]["duplicate"] = True
        _log(job_id, f"Duplicate detected — “{duplicate_path.name}” is already being downloaded.")
        return

    out_template = str(dest_path / f"{safe_title}.%(ext)s")

    def progress_hook(d):
        ctl = _control(job_id)
        if ctl:
            raise _Interrupted(ctl)
        conv_args = ydl_opts["postprocessor_args"]["extractaudio"]
        video_duration = (d.get("info_dict") or {}).get("duration")
        if video_duration and "-af" in conv_args:
            conv_args[conv_args.index("-af") + 1] = audio_check.silence_filter(video_duration)
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            eta = d.get("eta")
            if pct:
                suffix_text = ""
                if isinstance(eta, (int, float)) and eta >= 0:
                    eta = int(eta)
                    suffix_text = f" ETA {eta // 60:02d}:{eta % 60:02d}"
                _log(job_id, f"Downloading… {pct} ({speed}){suffix_text}")
        elif d.get("status") == "finished":
            _log(job_id, f"Download finished, converting to {extension.upper()}…")

    ydl_opts = {
        "format": "bestaudio/worst",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": 10 * 1024 * 1024,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "ffmpeg_location": FFMPEG_EXE,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": extension,
                **({} if extension in _LOSSLESS_FORMATS
                   else {"preferredquality": {"opus": "160", "aac": "256"}.get(quality, quality)}),
            },
        ],
        "progress_hooks": [progress_hook],
        "postprocessor_args": {"extractaudio":
            (["-af", audio_check.SILENCE_FILTER] if opts.get("silences", True) else [])
            + ({"opus": ["-c:a", "libopus", "-b:a", "160k"], "aac": ["-c:a", "aac", "-b:a", "256k"]}.get(quality, [])
               if opts.get("silences", True) else [])
            + (["-sample_fmt", "s16", "-ar", "44100"] if quality in ("flac", "wav") else [])},
    }

    def _soundcloud_fallback():
        if "soundcloud.com" in video_url or not tag_title:
            return None
        try:
            search_query = fallback_sources.fallback_query(tag_title, tag_artist)
            candidates = fallback_sources.search_soundcloud(search_query, 6, audio_check.to_seconds(expected_duration))
            best, note = video_choice.choose(candidates, tag_title, tag_artist, expected_duration, search_query)
            if not video_choice.reliable(best, tag_title, note):
                return None
            _log(job_id, f"YouTube refused this download — taking it from SoundCloud: “{best.get('title')}”.")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(best["url"], download=True)
        except _Interrupted:
            raise
        except Exception as exc_sc:
            _log(job_id, f"SoundCloud fallback failed ({str(exc_sc)[:100]}).")
            return None

    def _download_with_retries():
        for attempt in range(1, _MAX_RESUMES + 2):
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(video_url, download=True)
            except _Interrupted:
                raise
            except Exception as exc:
                if "Postprocessing" in str(exc):
                    raise
                if not _is_network_error(exc):
                    for clients in (["android", "web"], ["tv", "web_safari"], ["ios", "mweb"]):
                        _log(job_id, f"YouTube refused ({str(exc)[:120]}) — retrying ({', '.join(clients)})…")
                        opts2 = dict(ydl_opts, extractor_args={"youtube": {"player_client": clients}})
                        try:
                            with yt_dlp.YoutubeDL(opts2) as ydl:
                                return ydl.extract_info(video_url, download=True)
                        except _Interrupted:
                            raise
                        except Exception as exc2:
                            exc = exc2
                            if _is_network_error(exc2):
                                break
                    if not _is_network_error(exc):
                        fallback = _soundcloud_fallback()
                        if fallback:
                            return fallback
                        raise exc
                if attempt > _MAX_RESUMES:
                    raise
                waiting = _RETRY_WAIT[min(attempt - 1, len(_RETRY_WAIT) - 1)]
                _log(job_id, f"Connection lost — retrying in {waiting} s "
                             f"(attempt {attempt}/{_MAX_RESUMES}).")
                for _ in range(waiting * 2):
                    if _control(job_id):
                        raise _Interrupted(_control(job_id))
                    time.sleep(0.5)
                _log(job_id, "Resuming download…")
        raise RuntimeError("Cannot resume.")

    from concurrent.futures import ThreadPoolExecutor
    searches = ThreadPoolExecutor(max_workers=2)
    duration_s = audio_check.to_seconds(expected_duration)
    fut_info = (searches.submit(album_info.find_items, tag_title, first_artist(tag_artist), duration_s,
                                   opts.get("album") or "")
                 if opts.get("info", True) and tag_artist else None)
    fut_lyrics = (searches.submit(lyrics.find_items, tag_title, first_artist(tag_artist), duration_s)
                   if opts.get("lyrics_data", True) and tag_artist else None)

    try:
        info = _download_with_retries()
        final_path = dest_path / f"{safe_title}.{extension}"
        source_info.reject_preview(final_path, expected_duration)
        if info.get("track") and not opts.get("version"):
            source_keys = identity.key_list(info["track"], source_info.source_artist(info))
            track_keys.extend(c for c in source_keys if c not in track_keys)
            other = None if opts.get("force") else identity.find_existing(source_keys, except_path=str(final_path))
            if other:
                final_path.unlink(missing_ok=True)
                raise _AlreadyThere(other)
        tag_title, tag_artist = source_info.complete_title_artist(info, tag_title, tag_artist)
        err_tags = _write_tags_file(final_path, tag_title, tag_artist)
        if err_tags:
            _log(job_id, f"Metadata not written ({err_tags}).")
        else:
            _log(job_id, f"Metadata: “{tag_title}” — {tag_artist or 'unknown artist'}.")

        cover_source = "Spotify"
        if not cover_url:
            cover_url = source_info.thumbnail_jpg(info)
            cover_source = "SoundCloud" if "soundcloud.com" in (info.get("webpage_url") or "") else "YouTube"

        if cover_url:
            err_cover = _embed_cover_spotify(final_path, cover_url)
            if err_cover:
                _log(job_id, f"Cover art not applied ({err_cover}).")
            else:
                _log(job_id, f"{cover_source} cover art applied.")

        info_data = None
        if opts.get("info", True):
            info_data = fut_info.result() if fut_info else album_info.find_items(
                tag_title, first_artist(tag_artist), duration_s, opts.get("album") or "")
            if info_data and info_data.get("isrc"):
                track_keys.append(f"i:{info_data['isrc'].upper()}")
                other = None if opts.get("force") else identity.find_existing([track_keys[-1]], except_path=str(final_path))
                if other:
                    final_path.unlink(missing_ok=True)
                    raise _AlreadyThere(other)
            if info_data and info_data.get("cover_art_hd") and (opts.get("cover_art") == "hd" or not cover_url
                                                       or cover_source != "Spotify"):
                if not _embed_cover_spotify(final_path, info_data["cover_art_hd"]):
                    _log(job_id, "HD cover art (1000 px) applied.")
        if opts.get("position"):
            info_data = dict(info_data or {}, album=opts.get("playlist") or "Playlist", artiste_album="Divers",
                         track_entry=int(opts["position"]), track_entries=int(opts.get("tracks_index") or 0), disc=1, disques=1)
            album_info.embed(final_path, info_data)
            _log(job_id, f"Playlist order: track {opts['position']}/{opts.get('tracks_index') or '?'}.")
        if opts.get("info", True) and not opts.get("position"):
            if info_data:
                err_i = album_info.embed(final_path, info_data)
                _log(job_id, (f"Album: “{info_data['album']}” — {info_data.get('genre') or 'unknown genre'}"
                              f", track {info_data.get('track_info') or '?'}"
                              + (f"/{info_data['tracks_index']}" if info_data.get('tracks_index') else "")
                              + (f", {info_data['year']}" if info_data.get('year') else "") + ".")
                     if not err_i else f"Album info not written ({err_i}).")
            else:
                album_info.embed(final_path, source_info.fallback_album_info(info, tag_title, tag_artist,
                                                                                   opts.get("album") or ""))
                _log(job_id, "No album info found online — album info taken from the source.")

        if not tag_quality.read_value(final_path).get("cover_art"):
            for url_img in ((info_data or {}).get("cover_art_hd"), source_info.thumbnail_jpg(info)):
                if url_img and not _embed_cover_spotify(final_path, url_img):
                    _log(job_id, "Fallback cover art applied.")
                    break

        if opts.get("lyrics_data", True):
            found_items = fut_lyrics.result() if fut_lyrics else lyrics.find_items(
                tag_title, first_artist(tag_artist), duration_s)
            if found_items:
                err_p = lyrics.embed(final_path, found_items, opts.get("lrc", True))
                _log(job_id, "Lyrics added" + (" (synced, .lrc)" if found_items.get("synced") else "")
                     + "." if not err_p else f"Lyrics not added ({err_p}).")
            else:
                _log(job_id, "No lyrics found for this track.")

        for img_ext in (".webp", ".jpg", ".png"):
            try:
                (dest_path / f"{safe_title}{img_ext}").unlink(missing_ok=True)
            except Exception:
                pass
        warning = (audio_check.check_duration(final_path, expected_duration)
                         if opts.get("check_length", True) else "")
        if warning:
            _log(job_id, f"To check: duration {warning} on Spotify — this may not be the right version.")
        if opts.get("saturation", True):
            try:
                import subprocess
                output = subprocess.run([FFMPEG_EXE, "-hide_banner", *audio_check.analysis_excerpt(final_path),
                                         "-i", str(final_path), "-af", "volumedetect",
                                         "-f", "null", "-"], capture_output=True, text=True, timeout=120).stderr
                sat = tag_quality.saturation(output)
                if sat:
                    _log(job_id, f"To check: {sat}.")
                    warning = ", ".join(x for x in (warning, sat) if x)
            except Exception:
                pass
        identity.record(final_path, track_keys)
        _log(job_id, f"Saved to: {final_path}")
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(final_path)
            JOBS[job_id]["warning"] = warning
    except _AlreadyThere as other:
        final_path.with_suffix(".lrc").unlink(missing_ok=True)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(other)
            JOBS[job_id]["duplicate"] = True
        _log(job_id, f"Duplicate detected — same song already in your library: “{other}”. Copy removed.")
    except _Interrupted as stop_flag:
        mode = str(stop_flag)
        if mode == "stop":
            _clean_partials(dest_path, safe_title)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "paused" if mode == "pause" else "cancelled"
        _log(job_id, "Download paused." if mode == "pause" else "Download stopped.")
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Error: {exc}")
    finally:
        searches.shutdown(wait=False)
        audio_check.release(duplicate_path)


class _AlreadyThere(Exception):
    pass


@app.route("/")
def index():
    return _page()


@app.route("/api/default-folder")
def default_folder():
    return jsonify({"folder": str(DEFAULT_DEST)})


@app.route("/api/choose-folder", methods=["POST"])
def choose_folder():
    result = {"folder": None}

    def _pick():
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Choose the download folder")
        root.destroy()
        result["folder"] = folder or None

    t = threading.Thread(target=_pick)
    t.start()
    t.join(timeout=120)
    return jsonify(result)


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()

    if not title:
        return jsonify({"error": "The song name is required."}), 400

    source = (data.get("source") or "spotify").lower()
    limit_n = max(1, min(40, int(data.get("limit") or 10)))
    def deezer():
        try:
            d = artists._get(f"/search?q={urllib.parse.quote(title)}&limit={limit_n}")
            return [artists._track_item(t) for t in d.get("data") or []]
        except Exception:
            return []

    results = []
    if source == "deezer":
        results = deezer()
    elif source != "youtube":
        results = spotify_search(title, limit_n)
        if not results:
            results = deezer()
    if not results:
        try:
            results = search_videos(title, limit=limit_n)
        except Exception as exc:
            return jsonify({"error": f"Search failed: {exc}"}), 500

    return jsonify({"results": results})


def spotify_search(search_query: str, limit_n: int = 10) -> list[dict]:
    try:
        lines = spotify_scan.search_track_items(search_query, limit_n)
    except Exception:
        return []
    items = []
    for r in lines:
        title_text, artist_names = r.get("title") or "", r.get("artist") or ""
        if not title_text:
            continue
        items.append({
            "source": "spotify",
            "id": None,
            "url": None,
            "title": title_text,
            "uploader": artist_names,
            "album": r.get("album") or "",
            "duration": r.get("duration") or "",
            "thumbnail": _hires_cover(r.get("cover") or ""),
            "spotify_url": r.get("href") or "",
            "query": f"{title_text} {artist_names}".strip(),
        })
    return items


def _prewarm_search():
    try:
        spotify_scan.search_track_items("a", 1, timeout=20)
    except Exception:
        pass


@app.route("/api/resolve-link", methods=["POST"])
def resolve_link_route():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Missing link."}), 400

    try:
        items, playlist_name = resolve_link(url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Cannot analyze this link: {exc}"}), 500

    return jsonify({"results": items, "playlist_name": playlist_name})


_CACHE_YT: dict[str, tuple[float, dict]] = {}
_CACHE_YT_LOCK = threading.Lock()
_CACHE_YT_TTL = 3600


def _cache_yt_read(key_name: str):
    with _CACHE_YT_LOCK:
        v = _CACHE_YT.get(key_name)
    return v[1] if v and time.time() - v[0] < _CACHE_YT_TTL else None


def _cache_yt_write(key_name: str, value: dict):
    with _CACHE_YT_LOCK:
        if len(_CACHE_YT) > 500:
            _CACHE_YT.clear()
        _CACHE_YT[key_name] = (time.time(), value)


@app.route("/api/resolve-track", methods=["POST"])
def resolve_track_route():
    data = request.get_json(force=True)
    cache_key = "resolve:" + json.dumps(data, sort_keys=True, ensure_ascii=False)
    already = _cache_yt_read(cache_key)
    if already:
        return jsonify(already)
    query = (data.get("query") or "").strip()
    title_text = (data.get("title") or "").strip()
    artist_name = (data.get("artist") or "").strip()
    duration = data.get("duration") or 0

    if not query:
        return jsonify({"error": "Missing query."}), 400

    exclude = set(data.get("exclude") or [])
    try:
        matches = search_videos(query, limit=8)
    except Exception:
        matches = []

    if not matches:
        m, n = _search_fallback(title_text or query, artist_name, duration, exclude)
        if m and (not title_text or video_choice.reliable(m, title_text, n)):
            return jsonify({"result": m, "score": round(n)})
        return jsonify({"error": "Not found on YouTube, YouTube Music or SoundCloud."}), 404

    if exclude:
        remaining = [m for m in matches if m.get("id") not in exclude]
        matches = remaining or matches
    best, note = video_choice.choose(matches, title_text or query, artist_name, duration, query)
    if title_text and not video_choice.reliable(best, title_text, note):
        q2 = f"{video_choice._core_title(title_text)} {artist_name}".strip()
        if q2.lower() != query.lower():
            try:
                others = [m for m in search_videos(q2, limit=8) if m.get("id") not in exclude]
            except Exception:
                others = []
            m2, n2 = video_choice.choose(others, title_text, artist_name, duration, q2)
            if m2 and n2 > note:
                best, note = m2, n2
        if not video_choice.reliable(best, title_text, note):
            m3, n3 = _search_fallback(title_text, artist_name, duration, exclude)
            if m3 and (n3 > note or not video_choice.reliable(best, title_text, note)):
                best, note = m3, n3
        if not video_choice.reliable(best, title_text, note):
            close_item = (best or {}).get("title") or ""
            return jsonify({"error": f"Not found on YouTube, YouTube Music or SoundCloud (closest: “{close_item}”). "
                                     "Use “Versions” to pick a video."}), 404
    answer = {"result": best or matches[0], "score": round(note)}
    _cache_yt_write(cache_key, answer)
    return jsonify(answer)


_search_fallback = fallback_sources.best_fallback


_cover_cache: dict[str, str] = {}
_cover_lock = threading.Lock()


def _track_cover(uri: str) -> str:
    with _cover_lock:
        if uri in _cover_cache:
            return _cover_cache[uri]
    url = ""
    try:
        req = urllib.request.Request(
            "https://open.spotify.com/oembed?url=" + urllib.parse.quote(uri),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            url = (json.load(r) or {}).get("thumbnail_url") or ""
    except Exception:
        url = ""
    with _cover_lock:
        _cover_cache[uri] = url
    return url


@app.route("/api/track-covers", methods=["POST"])
def api_track_covers():
    uris = [u for u in (request.get_json(silent=True) or {}).get("uris", []) if u][:300]
    if not uris:
        return jsonify({"covers": {}})
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=12) as pool:
        result_items = list(pool.map(_track_cover, uris))
    return jsonify({"covers": {u: c for u, c in zip(uris, result_items) if c}})


@app.route("/api/covers-by-name", methods=["POST"])
def api_covers_by_name():
    items = [i for i in (request.get_json(silent=True) or {}).get("items", []) if i.get("t")][:60]
    if not items:
        return jsonify({"covers": {}})
    from concurrent.futures import ThreadPoolExecutor

    def one(i):
        key_name = f"{i['t']}|{i.get('a') or ''}".lower()
        with _cover_lock:
            if key_name in _cover_cache:
                return _cover_cache[key_name]
        url = album_info.cover_art(i["t"], i.get("a") or "")
        if url:
            with _cover_lock:
                _cover_cache[key_name] = url
        return url

    with ThreadPoolExecutor(max_workers=8) as pool:
        result_items = list(pool.map(one, items))
    return jsonify({"covers": {i["k"]: u for i, u in zip(items, result_items) if u}})


@app.route("/api/stream-url", methods=["POST"])
def stream_url():
    data = request.get_json(force=True)
    video_url = (data.get("url") or "").strip()
    if not video_url:
        return jsonify({"error": "Missing URL."}), 400
    already = _cache_yt_read("stream:" + video_url)
    if already:
        return jsonify(already)

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
        stream = info.get("url")
        if not stream and info.get("requested_formats"):
            stream = info["requested_formats"][0].get("url")
        if not stream:
            return jsonify({"error": "No audio stream found for this video."}), 404
        _cache_yt_write("stream:" + video_url, {"stream_url": stream})
        return jsonify({"stream_url": stream})
    except Exception as exc:
        return jsonify({"error": f"Preview unavailable: {exc}"}), 500


def run_full_scan(job_id: str, url: str):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    _log(job_id, "Opening the browser…")

    def progress_value(read_items, total):
        with JOBS_LOCK:
            JOBS[job_id]["scan"] = {"read_count": read_items, "total": total}

    try:
        track_items, total, connected = spotify_scan.scanner(url, on_progress=progress_value)
        with JOBS_LOCK:
            JOBS[job_id]["tracks"] = track_items
            JOBS[job_id]["scan"] = {"read_count": len(track_items), "total": total}
            JOBS[job_id]["status"] = "done"
        if total and len(track_items) < total:
            _log(job_id, f"Incomplete scan: {len(track_items)} tracks out of {total}.")
        else:
            _log(job_id, f"Playlist read: {len(track_items)} tracks.")
    except spotify_scan.ScanUnavailable as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, str(exc))
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Scan failed: {str(exc).split('Stacktrace')[0][:200]}")


@app.route("/api/full-scan", methods=["POST"])
def start_full_scan():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if "open.spotify.com" not in url:
        return jsonify({"error": "A Spotify link is expected."}), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None, "control": None,
                        "tracks": None, "scan": {"read_count": 0, "total": 0}}
    threading.Thread(target=run_full_scan, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/full-scan/login", methods=["POST"])
def full_scan_login():
    try:
        ok = spotify_scan.open_connection()
        return jsonify({"logged_in": bool(ok)})
    except spotify_scan.ScanUnavailable as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/spotify-radio", methods=["POST"])
def spotify_radio():
    data = request.get_json(silent=True) or {}
    search_query = (data.get("query") or "").strip()
    genre = "artist" if data.get("kind") == "artist" else "track"
    if not search_query:
        return jsonify({"error": "Type a song or an artist."}), 400
    try:
        radio = spotify_scan.find_radio(search_query, genre)
    except spotify_scan.ScanUnavailable as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        return jsonify({"error": f"Radio search failed: {str(exc).split('Stacktrace')[0][:200]}"}), 500
    if not radio:
        return jsonify({"error": "No Spotify radio found for this search."}), 404
    return jsonify(radio)


@app.route("/api/control/<job_id>", methods=["POST"])
def control_job(job_id):
    action = ((request.get_json(silent=True) or {}).get("action") or "").strip()
    if action not in ("pause", "stop"):
        return jsonify({"error": "Action inconnue."}), 400
    with JOBS_LOCK:
        if job_id not in JOBS:
            return jsonify({"error": "Download not found."}), 404
        JOBS[job_id]["control"] = action
    return jsonify({"ok": True})


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    video_url = (data.get("url") or "").strip()
    save_name = (data.get("save_name") or "").strip()
    dest_folder = (data.get("folder") or "").strip()
    quality = (data.get("quality") or "flac").strip()

    if not video_url or not save_name:
        return jsonify({"error": "Missing video or file name."}), 400

    job_id = uuid.uuid4().hex
    opts = {
        "silences": data.get("trim_silence", True) is not False,
        "lyrics_data": data.get("lyrics", True) is not False,
            "lrc": data.get("lyrics_file", True) is not False,
        "check_length": data.get("check_duration", True) is not False,
            "info": data.get("album_info", True) is not False,
            "cover_art": data.get("cover_source") or "spotify",
            "feat": bool(data.get("feat_to_artist")), "case_fix": bool(data.get("fix_case")),
            "saturation": data.get("check_clipping", True) is not False,
            "force": bool(data.get("force")), "version": bool(data.get("version")),
            "album": (data.get("album") or "").strip(),
            "position": int(data.get("position") or 0), "tracks_index": int(data.get("positions") or 0),
            "playlist": (data.get("playlist") or "").strip(),
    }
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None, "control": None, "opts": opts}

    thread = threading.Thread(
        target=run_download,
        args=(job_id, video_url, save_name, dest_folder, quality,
              (data.get("track_title") or "").strip(), (data.get("artist") or "").strip(),
              (data.get("cover") or "").strip(), str(data.get("duration") or "").strip()),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/existing", methods=["POST"])
def api_existing():
    data = request.get_json(silent=True) or {}
    folder_path = Path((data.get("folder") or "").strip() or str(DEFAULT_DEST))
    try:
        names = [f.name for f in folder_path.iterdir() if f.suffix.lower() in audio_check.EXTENSIONS_AUDIO]
    except Exception:
        names = []
    return jsonify({"names": names})


@app.route("/api/candidates", methods=["POST"])
def api_candidates():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Missing query."}), 400
    try:
        matches = search_videos(query, limit=8)
    except Exception as exc:
        return jsonify({"error": f"Search failed: {exc}"}), 500
    title_text, artist_name = (data.get("title") or query), (data.get("artist") or "")
    duration = audio_check.to_seconds(data.get("duration"))
    notes = sorted(((video_choice.note_down(m, title_text, artist_name, duration, query), m) for m in matches),
                   key=lambda x: -x[0])
    if not notes or not video_choice.reliable(notes[0][1], title_text, notes[0][0]):
        search_query = fallback_sources.fallback_query(title_text, artist_name)
        others = fallback_sources.search_ytmusic(search_query) + fallback_sources.search_soundcloud(search_query, 4, duration)
        notes = sorted(notes + [(video_choice.note_down(m, title_text, artist_name, duration, query), m) for m in others],
                       key=lambda x: -x[0])
    return jsonify({"candidates": [dict(m, score=round(n)) for n, m in notes[:5]]})


@app.route("/api/artist/search", methods=["POST"])
def api_artist_search():
    q = ((request.get_json(silent=True) or {}).get("q") or "").strip()
    if not q:
        return jsonify({"error": "Missing artist name."}), 400
    try:
        return jsonify({"artist_list": artists.search_artists(q)})
    except Exception as exc:
        return jsonify({"error": f"Search failed: {exc}"}), 500


@app.route("/api/artist/<int:artist_id>")
def api_artist(artist_id):
    try:
        return jsonify(artists.discography(artist_id))
    except Exception as exc:
        return jsonify({"error": f"Discographie indisponible : {exc}"}), 500


@app.route("/api/artist/albums", methods=["POST"])
def api_artist_albums():
    ids = [int(i) for i in (request.get_json(silent=True) or {}).get("ids", []) if str(i).isdigit()][:300]
    try:
        by_album = artists.albums_titles(ids)
        return jsonify({"per_album": by_album, "song_titles": [t for i in ids for t in by_album.get(str(i), [])]})
    except Exception as exc:
        return jsonify({"error": f"Tracks unavailable: {exc}"}), 500


@app.route("/api/library", methods=["POST"])
def api_library():
    data = request.get_json(silent=True) or {}
    folder_path = (data.get("folder") or "").strip() or str(DEFAULT_DEST)
    return jsonify({"folder_path": folder_path, "tracks_list": library.list_files(folder_path, data.get("tags", True) is not False)})


@app.route("/api/tags", methods=["POST"])
def api_tags():
    data = request.get_json(silent=True) or {}
    file_path = Path((data.get("path") or "").strip())
    if not file_path.is_file() or file_path.suffix.lower() not in audio_check.EXTENSIONS_AUDIO:
        return jsonify({"error": "File not found."}), 404
    if isinstance(data.get("fields"), dict):
        err = tag_quality.write_value(file_path, data["fields"])
        if err:
            return jsonify({"error": err}), 500
    return jsonify({"tags": tag_quality.read_value(file_path)})


@app.route("/api/retag", methods=["POST"])
def api_retag():
    data = request.get_json(silent=True) or {}
    file_path = Path((data.get("path") or "").strip())
    if not file_path.is_file() or file_path.suffix.lower() not in audio_check.EXTENSIONS_AUDIO:
        return jsonify({"error": "File not found."}), 404
    try:
        done_items = library.retag(file_path, data.get("lyrics", True) is not False, bool(data.get("cover_hd")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"done_list": done_items, "tags": tag_quality.read_value(file_path)})


@app.route("/api/explain", methods=["POST"])
def api_explain():
    return jsonify(reliability.explain((request.get_json(silent=True) or {}).get("message") or ""))


@app.route("/api/clean", methods=["POST"])
def api_clean():
    folder_path = ((request.get_json(silent=True) or {}).get("folder") or "").strip() or str(DEFAULT_DEST)
    return jsonify(reliability.clean(folder_path))


@app.route("/api/diagnostic")
def api_diagnostic():
    return jsonify({"tests": reliability.diagnostic(search_videos, spotify_search, lambda: Path(FFMPEG_EXE).exists())})


@app.route("/api/version")
def api_version():
    import sys
    return jsonify({"ytdlp": yt_dlp.version.__version__, "python": sys.version.split()[0], "platform": "PC (exe)" if getattr(sys, "frozen", False) else "PC",
                    "can_update": not getattr(sys, "frozen", False)})


@app.route("/api/update-ytdlp", methods=["POST"])
def api_update_ytdlp():
    import subprocess, sys
    if getattr(sys, "frozen", False):
        return jsonify({"ok": False, "message": ".exe version: download the latest MusicFlow release to update yt-dlp."})
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], capture_output=True, text=True, timeout=300)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if r.returncode != 0:
        return jsonify({"error": (r.stderr or r.stdout)[-300:]}), 500
    already = "already satisfied" in (r.stdout or "").lower()
    return jsonify({"ok": True, "message": "yt-dlp is already up to date." if already
                    else "yt-dlp updated — restart MusicFlow to use it."})


@app.route("/api/discover/<what>")
def api_discover(what):
    try:
        if what == "top":
            return jsonify({"song_titles": discover.top(100)})
        if what == "genres":
            return jsonify({"genres": discover.genres()})
        if what == "playlists":
            return jsonify({"playlists": discover.popular_playlists()})
        if what.startswith("genre-") and what[6:].isdigit():
            return jsonify({"song_titles": discover.genre(int(what[6:]))})
    except Exception as exc:
        return jsonify({"error": f"Unavailable: {exc}"}), 500
    return jsonify({"error": "Inconnu."}), 404


@app.route("/api/artist/<int:artist_id>/<what>")
def api_artist_more(artist_id, what):
    try:
        if what == "similar":
            return jsonify({"artist_list": discover.similar(artist_id)})
        if what == "mix":
            return jsonify({"song_titles": discover.mix(artist_id)})
    except Exception as exc:
        return jsonify({"error": f"Unavailable: {exc}"}), 500
    return jsonify({"error": "Inconnu."}), 404


@app.route("/api/latest-version")
def api_latest_version():
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    location = ""
    try:
        req = urllib.request.Request("https://github.com/captentv1/musicflow/releases/latest",
                                     headers={"User-Agent": "MusicFlow"})
        urllib.request.build_opener(_NoRedirect).open(req, timeout=15).close()
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.code in (301, 302, 303, 307, 308) else ""
        if not location:
            return jsonify({"error": f"HTTP {exc.code}"}), 502
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502
    tag = location.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"v?\d+(\.\d+)*", tag):
        return jsonify({"error": "No release found."}), 502
    base = f"https://github.com/captentv1/musicflow/releases/download/{tag}/"
    return jsonify({"tag_name": tag, "body": "See “What’s new” after installing.",
                    "assets": [{"name": n, "browser_download_url": base + n} for n in ("MusicFlow.apk", "MusicFlow.exe")]})


@app.route("/api/audio-file", methods=["POST"])
def api_audio_file():
    file_path = Path(((request.get_json(silent=True) or {}).get("path") or "").strip())
    if not file_path.is_file() or file_path.suffix.lower() not in audio_check.EXTENSIONS_AUDIO:
        return jsonify({"error": "File not found."}), 404
    return send_file(file_path)


@app.route("/api/delete-file", methods=["POST"])
def api_delete_file():
    file_path = Path(((request.get_json(silent=True) or {}).get("path") or "").strip())
    if not file_path.is_file() or file_path.suffix.lower() not in audio_check.EXTENSIONS_AUDIO:
        return jsonify({"error": "File not found."}), 404
    try:
        file_path.unlink()
        file_path.with_suffix(".lrc").unlink(missing_ok=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/file-lyrics", methods=["POST"])
def api_file_lyrics():
    file_path = Path(((request.get_json(silent=True) or {}).get("path") or "").strip())
    if not file_path.is_file() or file_path.suffix.lower() not in audio_check.EXTENSIONS_AUDIO:
        return jsonify({"error": "File not found."}), 404
    text_value = ""
    try:
        import mutagen
        f = mutagen.File(str(file_path))
        t = f.tags if f else None
        if t is not None:
            for k in list(t.keys()):
                if str(k).startswith("USLT"):
                    text_value = str(t[k].text); break
            if not text_value:
                for k in ("LYRICS", "©lyr"):
                    if k in t:
                        text_value = str(t[k][0]); break
    except Exception:
        pass
    lrc = file_path.with_suffix(".lrc")
    if not text_value and lrc.exists():
        text_value = "\n".join(l.split("]", 1)[-1] for l in lrc.read_text(encoding="utf-8", errors="replace").splitlines())
    return jsonify({"lyrics_data": text_value})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    import os
    folder_path = ((request.get_json(silent=True) or {}).get("folder") or "").strip() or str(DEFAULT_DEST)
    # Security: folders only, os.startfile on a file would run it.
    if folder_path.startswith(("\\\\", "//")) or not Path(folder_path).is_dir():
        return jsonify({"error": "Folder not found."}), 404
    try:
        os.startfile(folder_path)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def convert_audio(args):
    import subprocess
    try:
        return subprocess.run([FFMPEG_EXE] + args, capture_output=True, timeout=600).returncode == 0
    except Exception:
        return False


@app.route("/api/shrink", methods=["POST"])
def api_shrink():
    data = request.get_json(silent=True) or {}
    src = Path((data.get("path") or "").strip())
    fmt = (data.get("format") or "opus").strip()
    if not src.is_file() or src.suffix.lower() not in audio_check.EXTENSIONS_AUDIO:
        return jsonify({"error": "File not found."}), 404
    target_path = src.with_suffix(library.extension_for(fmt))
    if target_path == src:
        target_path = src.with_name(src.stem + ".reduit" + target_path.suffix)
    before = src.stat().st_size
    if not convert_audio(library.conversion_args(src, target_path, fmt)) or not target_path.exists() or target_path.stat().st_size == 0:
        target_path.unlink(missing_ok=True)
        return jsonify({"error": "Conversion failed."}), 500
    library.copy_info(src, target_path)
    after = target_path.stat().st_size
    if data.get("delete_original", True):
        src.unlink(missing_ok=True)
        if target_path.name.endswith(".reduit" + target_path.suffix):
            final = src.with_suffix(target_path.suffix); target_path.replace(final); target_path = final
    return jsonify({"path": str(target_path), "name": target_path.name, "before": before, "after": after})


@app.route("/api/connection")
def api_connection():
    for url in ("https://www.youtube.com/generate_204", "https://1.1.1.1/"):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4):
                return jsonify({"ok": True})
        except urllib.error.HTTPError:
            return jsonify({"ok": True})
        except Exception:
            continue
    return jsonify({"ok": False})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Job inconnu."}), 404
        return jsonify(job)


def run_transfer(job_id: str, target: str, name: str, tracks: list[dict]):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"

    try:
        if not store.is_connected(target):
            raise RuntimeError(
                f"{target.capitalize()} account not connected — go to the Accounts tab to sign in."
            )

        if target == "spotify":
            _log(job_id, f"Creating the Spotify playlist “{name}”…")
            playlist = spotify_client.create_playlist(name)
            uris = []
            for i, t in enumerate(tracks, 1):
                title, artist = t.get("title", ""), t.get("uploader", "")
                _log(job_id, f"[{i}/{len(tracks)}] {title} — {artist}")
                try:
                    uri = spotify_client.search_track_uri(title, artist)
                except Exception as exc:
                    uri = None
                    _log(job_id, f"  ↳ search error: {exc}")
                if uri:
                    uris.append(uri)
                else:
                    _log(job_id, "  ↳ not found on Spotify, skipped")
            if uris:
                spotify_client.add_tracks(playlist["id"], uris)
            _log(job_id, f"Done: {len(uris)}/{len(tracks)} tracks added.")
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["file"] = playlist.get("external_urls", {}).get("spotify", "")

        elif target == "youtube":
            _log(job_id, f"Creating the YouTube playlist “{name}”…")
            playlist = youtube_client.create_playlist(name)
            playlist_id = playlist["id"]
            added = 0
            for i, t in enumerate(tracks, 1):
                title, artist = t.get("title", ""), t.get("uploader", "")
                video_id = t.get("id")
                _log(job_id, f"[{i}/{len(tracks)}] {title} — {artist}")
                try:
                    if not video_id:
                        matches = search_videos(f"{title} {artist}".strip(), limit=1)
                        video_id = matches[0]["id"] if matches else None
                    if video_id:
                        youtube_client.add_video(playlist_id, video_id)
                        added += 1
                    else:
                        _log(job_id, "  ↳ not found on YouTube, skipped")
                except Exception as exc:
                    _log(job_id, f"  ↳ error: {exc}")
            _log(job_id, f"Done: {added}/{len(tracks)} videos added.")
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["file"] = f"https://www.youtube.com/playlist?list={playlist_id}"

        else:
            raise ValueError("Plateforme cible inconnue.")

    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Error: {exc}")


@app.route("/api/transfer/start", methods=["POST"])
def transfer_start():
    data = request.get_json(force=True)
    target = (data.get("target") or "").strip()
    name = (data.get("name") or "Playlist MusicFlow").strip()
    tracks = data.get("tracks") or []

    if target not in ("spotify", "youtube"):
        return jsonify({"error": "Invalid target (spotify or youtube)."}), 400
    if not tracks:
        return jsonify({"error": "No tracks to transfer."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None, "control": None}

    thread = threading.Thread(target=run_transfer, args=(job_id, target, name, tracks), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/accounts/status")
def accounts_status():
    out = {}
    for provider in ("spotify", "youtube"):
        p = store.get_provider(provider)
        out[provider] = {
            "configured": store.has_app_credentials(provider),
            "connected": store.is_connected(provider),
            "user": (p.get("user") or {}).get("name") if p.get("user") else None,
        }
    return jsonify(out)


@app.route("/api/accounts/<provider>/config", methods=["POST"])
def accounts_config(provider):
    if provider not in ("spotify", "youtube"):
        return jsonify({"error": "Plateforme inconnue."}), 404
    data = request.get_json(force=True)
    client_id = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return jsonify({"error": "Client ID and Client Secret required."}), 400
    store.set_provider_fields(provider, client_id=client_id, client_secret=client_secret)
    return jsonify({"ok": True})


@app.route("/api/accounts/<provider>/logout", methods=["POST"])
def accounts_logout(provider):
    if provider not in ("spotify", "youtube"):
        return jsonify({"error": "Plateforme inconnue."}), 404
    store.clear_tokens(provider)
    return jsonify({"ok": True})


@app.route("/auth/spotify/login")
def spotify_login():
    if not store.has_app_credentials("spotify"):
        return "First set the Spotify Client ID / Client Secret in the Accounts tab.", 400
    return redirect(spotify_client.build_authorize_url(_new_oauth_state()))


@app.route("/auth/spotify/callback")
def spotify_callback():
    error = request.args.get("error")
    if error:
        return f"Spotify sign-in cancelled or refused ({error}). <a href='/'>Back</a>", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "Invalid or expired OAuth state, try again from the app.", 400
    try:
        spotify_client.exchange_code(code)
    except Exception as exc:
        return f"Spotify sign-in failed: {exc} <a href='/'>Back</a>", 500
    return redirect("/?connected=spotify")


@app.route("/auth/youtube/login")
def youtube_login():
    if not store.has_app_credentials("youtube"):
        return "First set the Google Client ID / Client Secret in the Accounts tab.", 400
    return redirect(youtube_client.build_authorize_url(_new_oauth_state()))


@app.route("/auth/youtube/callback")
def youtube_callback():
    error = request.args.get("error")
    if error:
        return f"YouTube sign-in cancelled or refused ({error}). <a href='/'>Back</a>", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "Invalid or expired OAuth state, try again from the app.", 400
    try:
        youtube_client.exchange_code(code)
    except Exception as exc:
        return f"YouTube sign-in failed: {exc} <a href='/'>Back</a>", 500
    return redirect("/?connected=youtube")


if __name__ == "__main__":
    import sys as _sys, socket as _socket, webbrowser as _wb
    _url = "http://127.0.0.1:5090"
    with _socket.socket() as _s:
        if _s.connect_ex(("127.0.0.1", 5090)) == 0:
            _wb.open(_url); _sys.exit(0)
    if getattr(_sys, "frozen", False):
        threading.Timer(1.5, lambda: _wb.open(_url)).start()
    threading.Thread(target=_prewarm_search, daemon=True).start()
    print(f"MusicFlow running on http://127.0.0.1:5090  (default folder: {DEFAULT_DEST})")
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 5090, app, threaded=True)
    server.socket.set_inheritable(False)
    server.serve_forever()
