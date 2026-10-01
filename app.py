"""MusicFlow — recherche un titre et télécharge l'audio en MP3."""
try:
    import truststore  # certificats Windows (évite CERTIFICATE_VERIFY_FAILED)
    truststore.inject_into_ssl()
except ImportError:
    pass
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory
import imageio_ffmpeg
import yt_dlp

import secrets_store as store
import spotify_client
import youtube_client

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DEST = Path.home() / "OneDrive" / "Bureau" / "MusicFlow" / "Téléchargements"
DEFAULT_DEST.mkdir(parents=True, exist_ok=True)
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

app = Flask(__name__, static_folder=None)

# CSRF protection for the OAuth flows: state -> provider, expires after use
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

# job_id -> {"status": queued|running|done|error, "log": [...], "file": str|None}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return name or "musique"


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


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com(?:/intl-[\w-]+)?/track/([A-Za-z0-9]{22})")


def _spotify_track_ids(query: str, limit: int) -> list[str]:
    """Trouve des morceaux Spotify SANS API : recherche web (DuckDuckGo) limitée à open.spotify.com."""
    q = urllib.parse.quote(f"site:open.spotify.com/track {query}")
    req = urllib.request.Request(f"https://html.duckduckgo.com/html/?q={q}", headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = urllib.parse.unquote(resp.read().decode("utf-8", errors="replace"))
    return list(dict.fromkeys(_SPOTIFY_TRACK_RE.findall(html)))[:limit]


def _spotify_track_item(track_id: str):
    """Infos d'un morceau depuis sa page d'aperçu publique Spotify (même méthode que les playlists)."""
    try:
        e = _spotify_entity("track", track_id)
    except Exception:
        return None
    name = e.get("name") or e.get("title") or ""
    artists = ", ".join(a.get("name", "") for a in e.get("artists") or [])
    imgs = ((e.get("visualIdentity") or {}).get("image")) or []
    cover = max(imgs, key=lambda i: i.get("maxWidth") or 0).get("url", "") if imgs else ""
    duration_s = round((e.get("duration") or 0) / 1000)
    return {
        "source": "spotify",
        "id": None,
        "url": None,
        "title": name,
        "uploader": artists,
        "album": "",
        "release_date": ((e.get("releaseDate") or {}).get("isoString") or "")[:10],
        "duration": format_duration(duration_s),
        "duration_s": duration_s,
        "thumbnail": cover,
        "spotify_url": f"https://open.spotify.com/track/{track_id}",
        "query": f"{name} {artists}".strip(),
    }


def _deezer_genre(album_id) -> str:
    try:
        with urllib.request.urlopen(f"https://api.deezer.com/album/{album_id}", timeout=10) as resp:
            genres = (json.loads(resp.read()).get("genres") or {}).get("data") or []
        return ", ".join(g.get("name", "") for g in genres if g.get("name"))
    except Exception:
        return ""


def search_deezer(title: str, limit: int = 8):
    """Recherche publique Deezer (sans clé ni compte) : titre, artiste, album, genre, pochette HD."""
    url = f"https://api.deezer.com/search?q={urllib.parse.quote(title)}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read()).get("data") or []
    album_ids = list(dict.fromkeys((t.get("album") or {}).get("id") for t in data if (t.get("album") or {}).get("id")))
    with ThreadPoolExecutor(max_workers=8) as pool:
        genres = dict(zip(album_ids, pool.map(_deezer_genre, album_ids)))
    results = []
    for t in data:
        album = t.get("album") or {}
        artist = (t.get("artist") or {}).get("name") or ""
        name = t.get("title") or ""
        duration_s = int(t.get("duration") or 0)
        results.append(
            {
                "source": "deezer",
                "id": None,
                "url": None,
                "title": name,
                "uploader": artist,
                "album": album.get("title") or "",
                "genre": genres.get(album.get("id"), ""),
                "release_date": "",
                "duration": format_duration(duration_s),
                "duration_s": duration_s,
                "thumbnail": album.get("cover_medium") or "",
                "cover": album.get("cover_xl") or album.get("cover_big") or "",
                "spotify_url": "",
                "query": f"{name} {artist}".strip(),
            }
        )
    return results


def search_music(title: str):
    """Spotify d'abord (méthode des liens : pages publiques), Deezer si Spotify bloque, puis YouTube."""
    for finder in (search_spotify, search_deezer):
        try:
            results = finder(title)
        except Exception:
            results = None
        if results:
            return results
    return search_videos(title)


def search_spotify(title: str, limit: int = 8):
    """Recherche sur Spotify sans API ni abonnement : nom, artistes et pochette viennent de Spotify.
    Le morceau YouTube correspondant est trouvé plus tard (écoute/téléchargement)."""
    ids = _spotify_track_ids(title, limit)
    with ThreadPoolExecutor(max_workers=8) as pool:
        items = list(pool.map(_spotify_track_item, ids))
    return [it for it in items if it and it["title"]]


def _best_youtube_match(query: str, duration_s: int = 0):
    """Meilleur match YouTube : parmi les premiers résultats, celui dont la durée
    est la plus proche de celle de Spotify (évite clips longs, versions live…)."""
    matches = search_videos(f"{query} audio", limit=5) if duration_s else search_videos(query, limit=1)
    if not matches or not duration_s:
        return matches[0] if matches else None

    def secs(m):
        parts = [int(p) for p in (m.get("duration") or "0").split(":") if p.isdigit()]
        total = 0
        for p in parts:
            total = total * 60 + p
        return total

    return min(matches, key=lambda m: abs(secs(m) - duration_s) if secs(m) else 10**6)


def _apply_tags(mp3_path: Path, meta: dict, job_id: str):
    """Écrit titre/artiste/album/pochette Spotify dans le MP3 (ID3)."""
    tags = {
        "title": meta.get("title") or "",
        "artist": meta.get("artist") or "",
        "album": meta.get("album") or "",
        "date": (meta.get("release_date") or "")[:4],
        "genre": meta.get("genre") or "",
        "comment": meta.get("spotify_url") or "",
    }
    cover_path = None
    cover_url = meta.get("cover") or ""
    if cover_url.startswith("https://"):
        try:
            req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                cover_path = mp3_path.with_suffix(".cover.jpg")
                cover_path.write_bytes(resp.read())
        except Exception:
            cover_path = None

    tmp = mp3_path.with_suffix(".tag.mp3")
    cmd = [FFMPEG_EXE, "-y", "-loglevel", "error", "-i", str(mp3_path)]
    if cover_path:
        cmd += ["-i", str(cover_path), "-map", "0:a", "-map", "1:v",
                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    cmd += ["-c", "copy", "-id3v2_version", "3"]
    for k, v in tags.items():
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    cmd.append(str(tmp))
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        tmp.replace(mp3_path)
        _log(job_id, "Infos Spotify ajoutées (titre, artiste, album, pochette).")
    except Exception as exc:
        _log(job_id, f"Tags non écrits : {exc}")
        tmp.unlink(missing_ok=True)
    finally:
        if cover_path:
            cover_path.unlink(missing_ok=True)


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
        raise RuntimeError("Impossible de lire les données de ce lien Spotify.")
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
        if p in ("track", "playlist", "album") and i + 1 < len(parts):
            return p, parts[i + 1]
    return None, None


def resolve_link(url: str):
    """Détecte un lien Spotify ou YouTube (morceau ou playlist) et liste les morceaux.
    Retourne (items, playlist_name) — playlist_name est None pour un morceau seul."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()

    if "spotify.com" in host:
        kind, spotify_id = _parse_spotify_path(url)
        if not kind or not spotify_id:
            raise ValueError("Lien Spotify non reconnu (morceau, album ou playlist attendu).")
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

        # L'API officielle (Client Credentials) donne la playlist/album COMPLET, sans la limite
        # de ~50-100 morceaux de la page d'aperçu publique — utilisée si un Client ID/Secret
        # Spotify est configuré dans Comptes (aucune connexion utilisateur requise).
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
                raise ValueError("Aucun morceau trouvé dans cette playlist Spotify.")
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
                    "query": f"{title} {artists}".strip(),
                }
            )
        if not items:
            raise ValueError("Aucun morceau trouvé dans cette playlist Spotify.")
        name = entity.get("name") or entity.get("title") or "Playlist Spotify"
        if len(items) >= 50:
            name += " ⚠️ liste limitée — connecte ton compte Spotify (playlist privée) ou configure au moins un Client ID/Secret (playlist publique) dans Comptes"
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
                raise ValueError("Aucune vidéo trouvée dans cette playlist YouTube.")
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

    raise ValueError("Lien non reconnu — utilise un lien Spotify ou YouTube.")


VALID_QUALITIES = {"128", "192", "320"}


def run_download(job_id: str, video_url: str, save_name: str, dest_folder: str, quality: str = "192", meta: dict | None = None):
    if quality not in VALID_QUALITIES:
        quality = "192"
    dest_path = Path(dest_folder) if dest_folder else DEFAULT_DEST
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Impossible de créer le dossier de destination : {exc}")
        return

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    _log(job_id, f"Préparation du téléchargement : « {save_name} »…")

    safe_title = sanitize_filename(save_name)
    out_template = str(dest_path / f"{safe_title}.%(ext)s")

    def progress_hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            if pct:
                _log(job_id, f"Téléchargement… {pct} ({speed})")
        elif d.get("status") == "finished":
            _log(job_id, "Téléchargement terminé, conversion en MP3…")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_EXE,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }
        ],
        "progress_hooks": [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(video_url, download=True)
        final_path = dest_path / f"{safe_title}.mp3"
        if meta and final_path.exists():
            _apply_tags(final_path, meta, job_id)
        _log(job_id, f"Enregistré dans : {final_path}")
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(final_path)
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Erreur : {exc}")


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/default-folder")
def default_folder():
    return jsonify({"folder": str(DEFAULT_DEST)})


@app.route("/api/choose-folder", methods=["POST"])
def choose_folder():
    """Ouvre le sélecteur de dossier natif Windows côté serveur (poste local uniquement)."""
    result = {"folder": None}

    def _pick():
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        folder = filedialog.askdirectory(title="Choisir le dossier de téléchargement")
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
        return jsonify({"error": "Le nom de la musique est requis."}), 400

    try:
        results = search_music(title)
    except Exception as exc:
        return jsonify({"error": f"Recherche impossible : {exc}"}), 500

    return jsonify({"results": results})


@app.route("/api/resolve-link", methods=["POST"])
def resolve_link_route():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Lien manquant."}), 400

    try:
        items, playlist_name = resolve_link(url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Impossible d'analyser ce lien : {exc}"}), 500

    return jsonify({"results": items, "playlist_name": playlist_name})


@app.route("/api/resolve-track", methods=["POST"])
def resolve_track_route():
    """Trouve le meilleur match YouTube pour un morceau venant de Spotify (recherche par titre+artiste)."""
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify({"error": "Requête manquante."}), 400

    try:
        match = _best_youtube_match(query, int(data.get("duration_s") or 0))
    except Exception as exc:
        return jsonify({"error": f"Recherche impossible : {exc}"}), 500

    if not match:
        return jsonify({"error": "Aucune correspondance YouTube trouvée."}), 404

    return jsonify({"result": match})


@app.route("/api/stream-url", methods=["POST"])
def stream_url():
    """Extrait une URL de flux audio direct pour l'écoute d'aperçu (contourne le blocage d'intégration YouTube)."""
    data = request.get_json(force=True)
    video_url = (data.get("url") or "").strip()
    if not video_url:
        return jsonify({"error": "URL manquante."}), 400

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
            return jsonify({"error": "Flux audio introuvable pour cette vidéo."}), 404
        return jsonify({"stream_url": stream})
    except Exception as exc:
        return jsonify({"error": f"Aperçu indisponible : {exc}"}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    video_url = (data.get("url") or "").strip()
    save_name = (data.get("save_name") or "").strip()
    dest_folder = (data.get("folder") or "").strip()
    quality = (data.get("quality") or "192").strip()
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else None

    if not video_url or not save_name:
        return jsonify({"error": "Vidéo ou nom de fichier manquant."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None}

    thread = threading.Thread(
        target=run_download, args=(job_id, video_url, save_name, dest_folder, quality, meta), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


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
                f"Compte {target.capitalize()} non connecté — va dans l'onglet Comptes pour te connecter."
            )

        if target == "spotify":
            _log(job_id, f"Création de la playlist Spotify « {name} »…")
            playlist = spotify_client.create_playlist(name)
            uris = []
            for i, t in enumerate(tracks, 1):
                title, artist = t.get("title", ""), t.get("uploader", "")
                _log(job_id, f"[{i}/{len(tracks)}] {title} — {artist}")
                try:
                    uri = spotify_client.search_track_uri(title, artist)
                except Exception as exc:
                    uri = None
                    _log(job_id, f"  ↳ erreur de recherche : {exc}")
                if uri:
                    uris.append(uri)
                else:
                    _log(job_id, "  ↳ introuvable sur Spotify, ignoré")
            if uris:
                spotify_client.add_tracks(playlist["id"], uris)
            _log(job_id, f"Terminé : {len(uris)}/{len(tracks)} morceaux ajoutés.")
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["file"] = playlist.get("external_urls", {}).get("spotify", "")

        elif target == "youtube":
            _log(job_id, f"Création de la playlist YouTube « {name} »…")
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
                        _log(job_id, "  ↳ introuvable sur YouTube, ignoré")
                except Exception as exc:
                    _log(job_id, f"  ↳ erreur : {exc}")
            _log(job_id, f"Terminé : {added}/{len(tracks)} vidéos ajoutées.")
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "done"
                JOBS[job_id]["file"] = f"https://www.youtube.com/playlist?list={playlist_id}"

        else:
            raise ValueError("Plateforme cible inconnue.")

    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Erreur : {exc}")


@app.route("/api/transfer/start", methods=["POST"])
def transfer_start():
    data = request.get_json(force=True)
    target = (data.get("target") or "").strip()
    name = (data.get("name") or "Playlist MusicFlow").strip()
    tracks = data.get("tracks") or []

    if target not in ("spotify", "youtube"):
        return jsonify({"error": "Cible invalide (spotify ou youtube)."}), 400
    if not tracks:
        return jsonify({"error": "Aucun morceau à transférer."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None}

    thread = threading.Thread(target=run_transfer, args=(job_id, target, name, tracks), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


# ---------------------------------------------------------------------------
# Comptes : configuration OAuth + connexion/déconnexion Spotify & YouTube
# La connexion se fait toujours sur accounts.spotify.com / accounts.google.com
# — MusicFlow ne voit et ne stocke jamais le mot de passe du compte.
# ---------------------------------------------------------------------------

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
        return jsonify({"error": "Client ID et Client Secret requis."}), 400
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
        return "Configure d'abord le Client ID / Client Secret Spotify dans l'onglet Comptes.", 400
    return redirect(spotify_client.build_authorize_url(_new_oauth_state()))


@app.route("/auth/spotify/callback")
def spotify_callback():
    error = request.args.get("error")
    if error:
        return f"Connexion Spotify annulée ou refusée ({error}). <a href='/'>Retour</a>", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "État OAuth invalide ou expiré, réessaie depuis l'app.", 400
    try:
        spotify_client.exchange_code(code)
    except Exception as exc:
        return f"Échec de connexion Spotify : {exc} <a href='/'>Retour</a>", 500
    return redirect("/?connected=spotify")


@app.route("/auth/youtube/login")
def youtube_login():
    if not store.has_app_credentials("youtube"):
        return "Configure d'abord le Client ID / Client Secret Google dans l'onglet Comptes.", 400
    return redirect(youtube_client.build_authorize_url(_new_oauth_state()))


@app.route("/auth/youtube/callback")
def youtube_callback():
    error = request.args.get("error")
    if error:
        return f"Connexion YouTube annulée ou refusée ({error}). <a href='/'>Retour</a>", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "État OAuth invalide ou expiré, réessaie depuis l'app.", 400
    try:
        youtube_client.exchange_code(code)
    except Exception as exc:
        return f"Échec de connexion YouTube : {exc} <a href='/'>Retour</a>", 500
    return redirect("/?connected=youtube")


if __name__ == "__main__":
    print(f"MusicFlow lancé sur http://127.0.0.1:5090  (dossier par défaut : {DEFAULT_DEST})")
    app.run(host="127.0.0.1", port=5090, debug=False, threaded=True)
