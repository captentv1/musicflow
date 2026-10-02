"""MusicFlow mobile — serveur Flask lancé dans le processus de l'app Android via Chaquopy.

Différences avec la version PC (app.py) :
  - Pas de sélecteur de dossier natif (tkinter indisponible sur Android) : téléchargement
    toujours dans le dossier privé de l'app (Android/data/com.musicflow.app/files/Music),
    visible depuis un gestionnaire de fichiers.
  - Pas de réencodage ffmpeg : le fichier est gardé dans son format audio natif (m4a/opus)
    au lieu d'être reconverti en MP3, pour éviter d'embarquer un binaire ffmpeg natif ARM.
"""
import base64
import difflib
import io
import json
import re
import unicodedata
import hashlib
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory
import certifi
# yt-dlp, Pillow et mutagen ne sont PAS importés ici : ensemble ils coûtent plusieurs
# secondes au démarrage, pendant lesquelles l'app ne peut afficher que sa roue d'attente
# — le serveur doit écouter avant que la page ne s'ouvre. Ils sont chargés à la première
# utilisation réelle (voir _yt_dlp() et _tags()), ce qui rend l'ouverture immédiate.

import secrets_store as store
import choix_video
import spotify_client
import youtube_client
import verif_audio
import paroles
import infos_album
import liens_autres
import qualite_tags
import artistes
import bibliotheque
import fiabilite
import decouvrir

# Android/Chaquopy n'a pas toujours accès au magasin de certificats système par défaut
# pour ssl.create_default_context() — on force l'usage du bundle certifi.
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

_MODULES = {}


def _yt_dlp():
    """yt-dlp, chargé au premier téléchargement plutôt qu'au démarrage."""
    if "yt_dlp" not in _MODULES:
        import yt_dlp
        _MODULES["yt_dlp"] = yt_dlp
    return _MODULES["yt_dlp"]


def _tags():
    """Pillow + mutagen, chargés à la première écriture de pochette ou d'étiquette."""
    if "tags" not in _MODULES:
        from PIL import Image
        from mutagen.mp4 import MP4, MP4Cover
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, TPE2, ID3NoHeaderError
        from mutagen.oggopus import OggOpus
        from mutagen.flac import Picture, FLAC
        from mutagen.wave import WAVE
        _MODULES["tags"] = dict(Image=Image, MP4=MP4, MP4Cover=MP4Cover, ID3=ID3, APIC=APIC,
                                TIT2=TIT2, TPE1=TPE1, TPE2=TPE2,
                                ID3NoHeaderError=ID3NoHeaderError, OggOpus=OggOpus,
                                Picture=Picture, FLAC=FLAC, WAVE=WAVE)
    return _MODULES["tags"]


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DEST: Path | None = None  # défini par configure()

app = Flask(__name__, static_folder=None)

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
    """Safe file name — the full title stays intact in the tags.

    YouTube titles often contain an emoji. Kept in the file name, it breaks saving
    to an SD card: emoji are outside the Basic Multilingual Plane (beyond U+FFFF)
    and Android storage rejects them. Non-Latin scripts (Arabic, Cyrillic…) are kept
    as they are: they work, this has been checked.
    """
    name = re.sub(r'[\\/:*?"<>|]', "", name or "")
    # Emoji and pictographs outside the Basic Plane…
    name = re.sub(r"[\U00010000-\U0010FFFF]", "", name)
    # …but also those INSIDE the Basic Plane (✨ ⚡ ★ ➜ ☆ ✚), which the previous
    # pattern let through, plus invisible variation selectors and direction marks.
    name = re.sub(r"[←-⇿⌀-⏿①-⓿■-➿"
                  r"⤀-⥿⬀-⯿︀-️​-‏]", "", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)                  # control characters
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if not name:
        return "musique"
    # Safety net: if the file system cannot encode this name, a fallback name
    # is better than a failed download.
    try:
        os.fsencode(name)
    except Exception:
        secours = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        secours = re.sub(r"\s+", " ", secours).strip()
        name = secours or ("musique_" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8])
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
    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    with _yt_dlp().YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{title}", download=False)
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


_SPOTIFY_NEXT_DATA_RE = re.compile(r'__NEXT_DATA__"\s*type="application/json">(.*?)</script>', re.S)


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
    autre = liens_autres.resoudre(url)   # Deezer, Apple Music
    if autre is not None:
        return autre
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

        # The official API (Client Credentials) gives the FULL playlist/album, without the
        # ~50-100 track limit of the public preview page — used if a Spotify Client ID/Secret
        # is set in Accounts (no user sign-in required).
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
                    # La page embed ne donne pas de pochette par morceau : sans l'appel
                    # /api/track-covers ci-dessous, tous les titres portaient la même image
                    # (celle de la playlist).
                    "thumbnail": playlist_thumb,
                    "uri": t.get("uri") or "",
                    "query": f"{title} {artists}".strip(),
                }
            )
        if not items:
            raise ValueError("No tracks found in this Spotify playlist.")
        name = entity.get("name") or entity.get("title") or "Playlist Spotify"
        # The public preview page caps at 100 tracks: below that, the list is complete.
        # (It used to be flagged "limited" from 50, which started a web player scan — which only
        # shows about forty rows without an account — and replaced 50 tracks with ~40.)
        if len(items) >= 100:
            name += " — limited list (first 100 tracks)"
        return items, name

    if "youtube.com" in host or "youtu.be" in host:
        qs = urllib.parse.parse_qs(parsed.query)
        is_playlist = parsed.path.rstrip("/").endswith("/playlist") and "list" in qs

        if is_playlist:
            playlist_url = f"https://www.youtube.com/playlist?list={qs['list'][0]}"
            ydl_opts = {"extract_flat": "in_playlist", "skip_download": True, "quiet": True, "no_warnings": True}
            with _yt_dlp().YoutubeDL(ydl_opts) as ydl:
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

        ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True, "noplaylist": True}
        with _yt_dlp().YoutubeDL(ydl_opts) as ydl:
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

    raise ValueError("Unrecognized link — use a Spotify or YouTube link.")


def _best_jpg_thumbnail_url(info: dict) -> str | None:
    """YouTube fournit systématiquement une version .jpg de chaque miniature (en plus du
    .webp) — on la préfère explicitement : le Pillow embarqué via Chaquopy sur Android n'a
    pas forcément le support WEBP (libwebp non liée à la compilation), contrairement à la
    version PC. Le JPEG, lui, est toujours décodable."""
    thumbs = info.get("thumbnails") or []
    jpgs = [t for t in thumbs if (t.get("url") or "").split("?")[0].lower().endswith(".jpg")]
    pool = jpgs or thumbs
    if not pool:
        return None
    pool.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    return pool[0].get("url")


def _embed_cover_art(audio_path: Path, image_bytes: bytes) -> str:
    """Intègre une pochette (bytes JPEG déjà téléchargés) dans le fichier audio (m4a/mp3/opus)
    via mutagen, sans ffmpeg. Repasse par Pillow pour normaliser (mode RGB, ré-encodage propre).
    Retourne "" en cas de succès, ou un message d'erreur explicite sinon (jamais avalé
    silencieusement, pour pouvoir diagnostiquer un échec précis)."""
    t = _tags()
    Image = t["Image"]; MP4 = t["MP4"]; MP4Cover = t["MP4Cover"]
    ID3 = t["ID3"]; APIC = t["APIC"]; TIT2 = t["TIT2"]; TPE1 = t["TPE1"]; TPE2 = t["TPE2"]
    ID3NoHeaderError = t["ID3NoHeaderError"]; OggOpus = t["OggOpus"]; Picture = t["Picture"]
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            jpg_bytes = buf.getvalue()
    except Exception as exc:
        return f"lecture/conversion miniature : {exc}"

    ext = audio_path.suffix.lower()
    try:
        if ext in (".m4a", ".mp4"):
            mp4 = MP4(str(audio_path))
            mp4["covr"] = [MP4Cover(jpg_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            mp4.save()
        elif ext == ".mp3":
            try:
                id3 = ID3(str(audio_path))
            except ID3NoHeaderError:
                id3 = ID3()
            id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpg_bytes))
            id3.save(str(audio_path))
        elif ext in (".opus", ".ogg"):
            oa = OggOpus(str(audio_path))
            pic = Picture()
            pic.data = jpg_bytes
            pic.type = 3
            pic.mime = "image/jpeg"
            oa["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            oa.save()
        elif ext == ".flac":
            fl = t["FLAC"](str(audio_path))
            pic = Picture()
            pic.data = jpg_bytes
            pic.type = 3
            pic.mime = "image/jpeg"
            fl.clear_pictures()
            fl.add_picture(pic)
            fl.save()
        elif ext == ".wav":
            w = t["WAVE"](str(audio_path))
            if w.tags is None:
                w.add_tags()
            w.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpg_bytes))
            w.save()
        else:
            return f"format non pris en charge : {ext}"
        return ""
    except Exception as exc:
        return f"écriture tag ({ext}) : {exc}"


# Number of automatic retries after a network drop, and wait between attempts.
_REPRISES_MAX = 6
_ATTENTE_REPRISE = (3, 5, 10, 15, 30, 45)


def _est_erreur_reseau(exc) -> bool:
    """Tells a network drop (to retry) from a real error (deleted video,
    unavailable format…), which would be pointless to retry in a loop."""
    texte = f"{type(exc).__name__}: {exc}".lower()
    reseau = (
        "timed out", "timeout", "connection", "connexion", "unreachable", "reset by peer",
        "temporary failure", "name resolution", "getaddrinfo", "network", "réseau",
        "incomplete read", "incompleteread", "content too short", "remote end closed",
        "broken pipe", "chunked", "premature end",
        "ssl", "http error 5", "unable to download", "read operation",
    )
    definitif = (
        "video unavailable", "private video", "removed", "copyright", "age-restricted",
        "not available", "sign in to confirm", "members-only", "requested format",
    )
    if any(m in texte for m in definitif):
        return False
    return any(m in texte for m in reseau)


class _Interrompu(Exception):
    """Raised from the progress hook to stop yt-dlp midway."""


def _controle(job_id: str):
    with JOBS_LOCK:
        return (JOBS.get(job_id) or {}).get("control")


def _nettoyer_partiels(dest_path, safe_title: str):
    """Deletes the partial files (.part, .ytdl) left by a stop.

    On pause they are KEPT instead: yt-dlp automatically resumes where it
    stopped on the next download.
    """
    try:
        for f in dest_path.glob(safe_title + ".*"):
            # .part/.ytdl = partial download; .webp/.jpg = thumbnail left
            # by yt-dlp before the cover art is embedded.
            if f.suffix in (".part", ".ytdl", ".webp", ".jpg", ".jpeg", ".png") or f.name.endswith(".part"):
                f.unlink(missing_ok=True)
    except Exception:
        pass


def _write_tags(audio_path: Path, titre: str, artiste: str) -> str:
    """Écrit le titre et l'artiste dans le fichier audio.

    Sans ffmpeg (absent sur Android), aucune métadonnée n'était écrite : les lecteurs
    comme Samsung Music affichaient donc « Inconnu » comme artiste. Les noms de champs
    diffèrent selon le conteneur, d'où les trois branches.
    Retourne "" si tout s'est bien passé, sinon un message d'erreur.
    """
    t = _tags()
    Image = t["Image"]; MP4 = t["MP4"]; MP4Cover = t["MP4Cover"]
    ID3 = t["ID3"]; APIC = t["APIC"]; TIT2 = t["TIT2"]; TPE1 = t["TPE1"]; TPE2 = t["TPE2"]
    ID3NoHeaderError = t["ID3NoHeaderError"]; OggOpus = t["OggOpus"]; Picture = t["Picture"]
    if not titre and not artiste:
        return "no metadata to write"
    ext = audio_path.suffix.lower()
    try:
        if ext in (".m4a", ".mp4"):
            mp4 = MP4(str(audio_path))
            if titre:
                mp4["\xa9nam"] = [titre]
            if artiste:
                mp4["\xa9ART"] = [artiste]
                mp4["aART"] = [artiste]  # artiste de l'album, utilisé par certains lecteurs
            mp4.save()
        elif ext == ".mp3":
            try:
                id3 = ID3(str(audio_path))
            except ID3NoHeaderError:
                id3 = ID3()
            if titre:
                id3.add(TIT2(encoding=3, text=titre))
            if artiste:
                id3.add(TPE1(encoding=3, text=artiste))
                id3.add(TPE2(encoding=3, text=artiste))
            id3.save(str(audio_path))
        elif ext in (".opus", ".ogg"):
            oa = OggOpus(str(audio_path))
            if titre:
                oa["TITLE"] = [titre]
            if artiste:
                oa["ARTIST"] = [artiste]
            oa.save()
        elif ext == ".flac":
            fl = t["FLAC"](str(audio_path))
            if titre:
                fl["TITLE"] = [titre]
            if artiste:
                fl["ARTIST"] = [artiste]
                fl["ALBUMARTIST"] = [artiste]
            fl.save()
        elif ext == ".wav":
            w = t["WAVE"](str(audio_path))
            if w.tags is None:
                w.add_tags()
            if titre:
                w.tags.add(TIT2(encoding=3, text=titre))
            if artiste:
                w.tags.add(TPE1(encoding=3, text=artiste))
                w.tags.add(TPE2(encoding=3, text=artiste))
            w.save()
        else:
            return f"format non pris en charge : {ext}"
        # Relecture : écrire sans vérifier laissait passer un titre transformé en
        # « ????? » sans le moindre signe dans le journal.
        relu = _relire_titre(audio_path)
        if titre and relu and relu != titre:
            return f"titre relu différent : {relu!r} au lieu de {titre!r}"
        return ""
    except Exception as exc:
        return f"écriture métadonnées ({ext}) : {exc}"


def premier_artiste(artistes: str) -> str:
    """Keeps only the main artist.

    Spotify often returns "Clean Bandit, Jess Glynne" or "Calvin Harris feat. Rihanna".
    Written as is into the file, this list creates a separate artist per
    combination in players: "Coldplay" and "Coldplay, BTS" end up
    apart, and tracks by the same artist are no longer grouped.
    """
    a = (artistes or "").strip()
    if not a:
        return ""
    # Only the separators Spotify really uses to tell TWO artists
    # apart: the comma and "feat." mentions. "&", "and", "x",
    # "et" are left out — they usually belong to a band name (Simon and Garfunkel,
    # Coldplay & BTS being returned as "Coldplay, BTS" by Spotify anyway).
    for sep in (",", " feat. ", " feat ", " ft. ", " ft ", " featuring "):
        i = a.lower().find(sep.lower())
        if i > 0:
            a = a[:i]
    return a.strip(" ,&-")


def _relire_titre(audio_path: Path) -> str:
    """Relit le titre réellement stocké dans le fichier, pour vérifier l'écriture."""
    t = _tags()
    ext = audio_path.suffix.lower()
    try:
        if ext in (".m4a", ".mp4"):
            return (t["MP4"](str(audio_path)).tags.get("©nam") or [""])[0]
        if ext == ".mp3":
            return str(t["ID3"](str(audio_path)).get("TIT2") or "")
        if ext in (".opus", ".ogg"):
            return (t["OggOpus"](str(audio_path)).get("TITLE") or [""])[0]
        if ext == ".flac":
            return (t["FLAC"](str(audio_path)).get("TITLE") or [""])[0]
        if ext == ".wav":
            tags = t["WAVE"](str(audio_path)).tags
            return str(tags.get("TIT2") or "") if tags else ""
    except Exception:
        return ""
    return ""


_JUNK_TITRE_RE = re.compile(
    r"[\(\[][^)\]]*\b(lyrics?|paroles?|clip\s*officiel|officiel|official|"
    r"music\s*video|lyric\s*video|video|audio|visuali[sz]er|hd|hq|4k|mv)\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def _nettoyer_titre_recherche(titre: str) -> str:
    """Retire les mentions parasites d'un titre de vidéo YouTube (« Lyrics », « Official
    Video »…) avant de l'utiliser comme requête de recherche Spotify."""
    nettoye = _JUNK_TITRE_RE.sub("", titre)
    nettoye = re.sub(r"\s+", " ", nettoye).strip(" -")
    return nettoye or titre.strip()


def _match_plausible(requete: str, titre_trouve: str) -> bool:
    """Écarte un résultat Spotify sans rapport avec la requête (titre YouTube trop
    pollué) — mieux vaut garder le titre YouTube brut qu'un autre morceau."""
    a = unicodedata.normalize("NFKD", requete.lower()).encode("ascii", "ignore").decode()
    b = unicodedata.normalize("NFKD", titre_trouve.lower()).encode("ascii", "ignore").decode()
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.4 or b.strip() in a


_SPOTIFY_IMG_RE = re.compile(r"(i\.scdn\.co/image/)[0-9a-f]{16}([0-9a-f]{24})", re.IGNORECASE)


def _pochette_haute_resolution(url: str) -> str:
    """Force la pochette Spotify à la plus haute résolution connue (640×640) — les
    vignettes de recherche/oEmbed sont souvent bien plus petites."""
    return _SPOTIFY_IMG_RE.sub(r"\1ab67616d0000b273\2", url)


def _deviner_titre_artiste_initial(save_name: str, titre: str, artiste: str):
    """First known title/artist, even before the download — used as the query to
    look the track up on Spotify. Falls back to the "Title - Artist" save name
    when nothing was passed (direct YouTube search)."""
    titre = (titre or "").strip()
    artiste = premier_artiste((artiste or "").strip())
    if not titre:
        morceaux = save_name.rsplit(" - ", 1)
        if len(morceaux) == 2:
            titre = morceaux[0].strip()
            artiste = artiste or premier_artiste(morceaux[1].strip())
        else:
            titre = save_name.strip()
    return titre, artiste


_FORMATS_SANS_PERTE = ("flac", "wav")


def _ffmpeg(args: list[str]) -> bool:
    """Lance ffmpeg via FFmpegKit (ajouté à l'APK) ; False s'il est indisponible ou échoue."""
    try:
        from java import jarray, jclass
        FFmpegKit = jclass("com.arthenica.ffmpegkit.FFmpegKit")
        ReturnCode = jclass("com.arthenica.ffmpegkit.ReturnCode")
        session = FFmpegKit.executeWithArguments(jarray(jclass("java.lang.String"))(args))
        return bool(ReturnCode.isSuccess(session.getReturnCode()))
    except Exception:
        return False


def _ffmpeg_sortie(args: list[str]) -> str:
    try:
        from java import jarray, jclass
        FFmpegKit = jclass("com.arthenica.ffmpegkit.FFmpegKit")
        session = FFmpegKit.executeWithArguments(jarray(jclass("java.lang.String"))(args))
        return str(session.getOutput() or "")
    except Exception:
        return ""


def _convertir(src: Path, quality: str, silences: bool, job_id: str) -> Path | None:
    """Convertit le fichier YouTube (m4a/opus) au format choisi : MP3 128/192/320, FLAC, WAV."""
    ext = {"flac": "flac", "wav": "wav", "opus": "opus", "aac": "m4a"}.get(quality, "mp3")
    dst = src.with_suffix(f".{ext}")
    if dst == src:
        dst = src.with_name(src.stem + ".conv." + ext)
    args = ["-y", "-loglevel", "error", "-i", str(src), "-vn"]
    if silences:
        args += ["-af", verif_audio.FILTRE_SILENCE]
    if ext == "mp3":
        args += ["-c:a", "libmp3lame", "-b:a", f"{quality if quality in ('128', '192', '320') else '320'}k"]
    elif ext == "flac":
        args += ["-c:a", "flac", "-sample_fmt", "s16", "-ar", "44100"]  # qualité CD, pas de 24 bits inutile
    elif ext == "opus":
        args += ["-c:a", "libopus", "-b:a", "160k"]
    elif ext == "m4a":
        args += ["-c:a", "aac", "-b:a", "256k"]
    else:
        args += ["-c:a", "pcm_s16le", "-ar", "44100"]
    args.append(str(dst))
    _log(job_id, f"Conversion en {ext.upper()}{'' if ext != 'mp3' else ' ' + quality + ' kbps'}…")
    if _ffmpeg(args) and dst.exists() and dst.stat().st_size > 0:
        if dst.name.endswith(".conv." + ext):
            final = src.with_suffix(f".{ext}")
            dst.replace(final)
            dst = final
        return dst
    try:
        dst.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def run_download(job_id: str, video_url: str, save_name: str, dest_folder: str, quality: str = "flac",
                 titre: str = "", artiste: str = "", cover: str = "", duree_attendue: str = ""):
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
        opts = dict(JOBS[job_id].get("opts") or {})
    _log(job_id, f"Preparing download: “{save_name}”…")

    guess_titre, guess_artiste = _deviner_titre_artiste_initial(save_name, titre, artiste)
    cover_url = (cover or "").strip()
    tag_titre, tag_artiste = guess_titre, guess_artiste
    if not cover_url:
        # No Spotify cover art known yet (from a Spotify link/playlist): look
        # the track up on Spotify to get its real title/artist and its real
        # pochette, plutôt que de garder le titre brut de la vidéo YouTube.
        requete_titre = _nettoyer_titre_recherche(guess_titre)
        try:
            match = spotify_client.search_track(requete_titre, "")
        except Exception:
            match = None
        if not match:
            premier = _bot_spotify(requete_titre, 1, 12000)
            if premier:
                match = {"title": premier[0].get("title") or "", "artist": premier[0].get("artist") or "",
                         "cover": _pochette_haute_resolution(premier[0].get("cover") or "")}
        if match and match.get("title") and _match_plausible(requete_titre, match["title"]):
            tag_titre = match["title"]
            tag_artiste = premier_artiste(match.get("artist") or "") or guess_artiste
            cover_url = match.get("cover") or ""
            _log(job_id, f"Spotify match found: “{tag_titre}” — {tag_artiste or 'unknown artist'}.")
        elif match:
            _log(job_id, f"Spotify result discarded (“{match.get('title', '')}” does not match “{requete_titre}”).")

    tag_titre, tag_artiste = qualite_tags.nettoyer(tag_titre, tag_artiste, opts.get("feat", False), opts.get("casse", False))
    safe_title = sanitize_filename(f"{tag_titre} - {tag_artiste}" if tag_artiste else tag_titre)

    doublons = [f for f in dest_path.glob(safe_title + ".*")
                if f.suffix.lower() in verif_audio.EXTENSIONS_AUDIO]
    if not doublons:
        equivalent = verif_audio.doublon_dans_dossier(dest_path, safe_title)
        doublons = [equivalent] if equivalent else []
    if doublons and opts.get("force"):
        for f in doublons:
            try:
                f.unlink()
            except Exception:
                pass
        _log(job_id, "Remplacement par une autre version.")
        doublons = []
    if doublons:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(doublons[0])
            JOBS[job_id]["doublon"] = True
        _log(job_id, f"Doublon détecté — « {doublons[0].name} » est déjà dans le dossier, téléchargement ignoré.")
        return
    reserve = dest_path / f"{safe_title}.audio"
    if not verif_audio.reserver(reserve):
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["doublon"] = True
        _log(job_id, "Doublon détecté — ce morceau est déjà en cours de téléchargement.")
        return

    out_template = str(dest_path / f"{safe_title}.%(ext)s")

    def progress_hook(d):
        # Pause or stop requested from the Downloads tab: yt-dlp is interrupted
        # by raising an exception from the hook, the only way to stop it midway.
        ctl = _controle(job_id)
        if ctl:
            raise _Interrompu(ctl)
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            # Temps restant lu par l'onglet Téléchargements de l'interface (format « ETA 01:23 »).
            eta = d.get("eta")
            if pct:
                suffixe = ""
                if isinstance(eta, (int, float)) and eta >= 0:
                    eta = int(eta)
                    suffixe = f" ETA {eta // 60:02d}:{eta % 60:02d}"
                _log(job_id, f"Downloading… {pct} ({speed}){suffixe}")
        elif d.get("status") == "finished":
            _log(job_id, "Téléchargement terminé.")

    ydl_opts = {
        "format": "bestaudio/worst",  # never a big video: the smallest one, whose audio is extracted
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Resuming a partial file and internal retries: first line of defense
        # against an unstable connection, before the retry loop below.
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        # Faster: 10 MB chunks (YouTube throttles long requests) and parallel fragments
        "http_chunk_size": 10 * 1024 * 1024,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
    }

    def _telecharger_avec_reprises():
        """Downloads, resuming automatically after a network drop.

        The partial file is kept between attempts (continuedl), so each
        retry picks up where the connection dropped instead of starting over.
        A pause/stop requested by the user is NOT an error: it
        goes through the loop without triggering a retry.
        """
        for essai in range(1, _REPRISES_MAX + 2):
            try:
                with _yt_dlp().YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(video_url, download=True)
            except _Interrompu:
                raise
            except Exception as exc:
                if "Postprocessing" in str(exc):
                    raise  # conversion error, not a YouTube refusal
                if not _est_erreur_reseau(exc):
                    # YouTube refusal (bot check, unavailable format, blocked client…):
                    # retry with other clients before declaring failure.
                    for clients in (["android", "web"], ["tv", "web_safari"], ["ios", "mweb"]):
                        _log(job_id, f"YouTube refused ({str(exc)[:120]}) — retrying ({', '.join(clients)})…")
                        opts2 = dict(ydl_opts, extractor_args={"youtube": {"player_client": clients}})
                        try:
                            with _yt_dlp().YoutubeDL(opts2) as ydl:
                                return ydl.extract_info(video_url, download=True)
                        except _Interrompu:
                            raise
                        except Exception as exc2:
                            exc = exc2
                            if _est_erreur_reseau(exc2):
                                break
                    if not _est_erreur_reseau(exc):
                        raise exc
                if essai > _REPRISES_MAX:
                    raise
                attente = _ATTENTE_REPRISE[min(essai - 1, len(_ATTENTE_REPRISE) - 1)]
                _log(job_id, f"Connection lost — retrying in {attente} s "
                             f"(attempt {essai}/{_REPRISES_MAX}).")
                # Wait in small steps to stay responsive to a pause/stop.
                for _ in range(attente * 2):
                    if _controle(job_id):
                        raise _Interrompu(_controle(job_id))
                    time.sleep(0.5)
                _log(job_id, "Resuming download…")
        raise RuntimeError("Reprise impossible.")

    try:
        info = _telecharger_avec_reprises()
        ext = ((info.get("requested_downloads") or [{}])[0].get("ext")) or info.get("ext", "m4a")
        final_path = dest_path / f"{safe_title}.{ext}"
        converti = _convertir(final_path, quality, opts.get("silences", True), job_id)
        if not converti and opts.get("silences", True):
            _log(job_id, "Nouvel essai de conversion sans le filtre de silence…")
            converti = _convertir(final_path, quality, False, job_id)
        if converti:
            try:
                final_path.unlink(missing_ok=True)
            except Exception:
                pass
            final_path = converti
        else:
            # On ne garde pas un fichier brut (parfois une vidéo de plusieurs centaines de Mo) :
            # échec, le morceau sera retenté avec une autre vidéo.
            try:
                final_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError("Postprocessing: conversion impossible de ce fichier")

        # Pochette Spotify (haute résolution) en priorité ; à défaut, la vignette YouTube.
        image_bytes = None
        source_pochette = ""
        if cover_url:
            try:
                req = urllib.request.Request(_pochette_haute_resolution(cover_url), headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    image_bytes = resp.read()
                source_pochette = "Spotify"
            except Exception:
                image_bytes = None
        if image_bytes is None:
            thumb_url = _best_jpg_thumbnail_url(info)
            if thumb_url:
                try:
                    req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        image_bytes = resp.read()
                    source_pochette = "YouTube"
                except Exception:
                    image_bytes = None

        if image_bytes:
            error = _embed_cover_art(final_path, image_bytes)
            _log(job_id, f"Pochette {source_pochette} intégrée." if not error else f"Pochette non intégrée ({error}).")
        else:
            _log(job_id, "Pochette non intégrée (aucune image disponible).")

        # Trace de diagnostic : si le titre arrive déjà abîmé, ce n'est pas l'écriture
        # des étiquettes qu'il faut corriger mais ce qui se passe en amont.
        if tag_titre and "?" in tag_titre:
            _log(job_id, f"Titre reçu suspect (contient des « ? ») : {tag_titre!r}")
        err_tags = _write_tags(final_path, tag_titre, tag_artiste)
        if err_tags:
            _log(job_id, f"Metadata not written ({err_tags}).")
        else:
            _log(job_id, f"Metadata: “{tag_titre}” — {tag_artiste or 'unknown artist'}.")

        infos = None
        if opts.get("infos", True):
            infos = infos_album.chercher(tag_titre, premier_artiste(tag_artiste),
                                         verif_audio.secondes(duree_attendue), opts.get("album") or "")
            if infos and infos.get("pochette_hd") and (opts.get("pochette") == "hd" or not cover_url):
                try:
                    req = urllib.request.Request(infos["pochette_hd"], headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        if not _embed_cover_art(final_path, resp.read()):
                            _log(job_id, "HD cover art (1000 px) applied.")
                except Exception:
                    pass
        if opts.get("position"):
            infos = dict(infos or {}, album=opts.get("playlist") or "Playlist", artiste_album="Divers",
                         piste=int(opts["position"]), pistes=int(opts.get("pistes") or 0), disque=1, disques=1)
            infos_album.integrer(final_path, infos)
            _log(job_id, f"Playlist order: track {opts['position']}/{opts.get('pistes') or '?'}.")
        if opts.get("infos", True) and not opts.get("position"):
            if infos:
                err_i = infos_album.integrer(final_path, infos)
                _log(job_id, (f"Album: “{infos['album']}” — {infos.get('genre') or 'unknown genre'}"
                              f", track {infos.get('piste') or '?'}"
                              + (f"/{infos['pistes']}" if infos.get('pistes') else "")
                              + (f", {infos['annee']}" if infos.get('annee') else "") + ".")
                     if not err_i else f"Album info not written ({err_i}).")
            else:
                _log(job_id, "No album info found for this track.")

        if opts.get("paroles", True):
            trouvees = paroles.chercher(tag_titre, premier_artiste(tag_artiste),
                                        verif_audio.secondes(duree_attendue))
            if trouvees:
                err_p = paroles.integrer(final_path, trouvees, opts.get("lrc", True))
                _log(job_id, "Paroles ajoutées." if not err_p else f"Lyrics not added ({err_p}).")
            else:
                _log(job_id, "No lyrics found for this track.")
        avertissement = (verif_audio.verifier_duree(final_path, duree_attendue)
                         if opts.get("verif_duree", True) else "")
        if avertissement:
            _log(job_id, f"To check: duration {avertissement} on Spotify — this may not be the right version.")
        if opts.get("saturation", True):
            sat = qualite_tags.saturation(_ffmpeg_sortie(["-hide_banner", "-i", str(final_path),
                                                          "-af", "volumedetect", "-f", "null", "-"]))
            if sat:
                _log(job_id, f"To check: {sat}.")
                avertissement = ", ".join(x for x in (avertissement, sat) if x)

        _log(job_id, f"Saved to: {final_path}")
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(final_path)
            JOBS[job_id]["avertissement"] = avertissement
    except _Interrompu as arret:
        mode = str(arret)
        if mode == "stop":
            _nettoyer_partiels(dest_path, safe_title)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "paused" if mode == "pause" else "cancelled"
        _log(job_id, "Download paused." if mode == "pause" else "Download stopped.")
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Erreur : {exc}")
    finally:
        verif_audio.liberer(reserve)


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/default-folder")
def default_folder():
    return jsonify({"folder": str(DEFAULT_DEST)})


@app.route("/api/choose-folder", methods=["POST"])
def choose_folder():
    # Pas de sélecteur natif sur mobile v1 : téléchargement toujours dans DEFAULT_DEST.
    return jsonify({"folder": None})


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "The song name is required."}), 400
    # Search Spotify first (official name, artist, cover art); the matching
    # YouTube track is picked when playing/downloading (/api/resolve-track).
    source = (data.get("source") or "spotify").lower()
    limite = max(1, min(40, int(data.get("limit") or 10)))
    results = []
    if source == "deezer":
        try:
            d = artistes._get(f"/search?q={urllib.parse.quote(title)}&limit={limite}")
            results = [artistes._piste(t) for t in d.get("data") or []]
        except Exception:
            results = []
    elif source != "youtube":
        results = recherche_spotify(title, limite)
    if not results:
        try:
            results = search_videos(title, limit=limite)
        except Exception as exc:
            return jsonify({"error": f"Recherche impossible : {exc}"}), 500
    return jsonify({"results": results})


def _bot_spotify(requete: str, limite: int = 10, timeout_ms: int = 15000) -> list[dict]:
    """Recherche Spotify sans connexion ni API via la WebView invisible de l'app
    (SpotifyRecherche.kt) — l'équivalent du navigateur Edge piloté de la version PC."""
    try:
        from java import jclass
        brut = jclass("com.musicflow.app.SpotifyRecherche").chercher(requete, limite, timeout_ms)
        return json.loads(str(brut)) or []
    except Exception:
        return []


def recherche_spotify(requete: str, limite: int = 10) -> list[dict]:
    """Résultats de la recherche Spotify, au même format que les morceaux d'un lien Spotify."""
    items = []
    for r in _bot_spotify(requete, limite):
        titre, artistes = r.get("title") or "", r.get("artist") or ""
        if not titre:
            continue
        items.append({
            "source": "spotify",
            "id": None,
            "url": None,
            "title": titre,
            "uploader": artistes,
            "album": r.get("album") or "",
            "duration": r.get("duration") or "",
            "thumbnail": _pochette_haute_resolution(r.get("cover") or ""),
            "spotify_url": r.get("href") or "",
            "query": f"{titre} {artistes}".strip(),
        })
    return items


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
        return jsonify({"error": f"Cannot analyze this link: {exc}"}), 500
    return jsonify({"results": items, "playlist_name": playlist_name})


@app.route("/api/resolve-track", methods=["POST"])
def resolve_track_route():
    """Finds the best YouTube match for a track coming from Spotify.

    The very first search result used to be taken, unchecked: depending on YouTube's
    mood, a live version, a karaoke, a reaction or a talent-show audition was downloaded
    instead of the song. Several candidates are now scored (see choix_video),
    with the exact duration from Spotify as the main signal.
    """
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    titre = (data.get("title") or "").strip()
    artiste = (data.get("artist") or "").strip()
    duree = data.get("duration") or 0

    if not query:
        return jsonify({"error": "Missing query."}), 400

    try:
        matches = search_videos(query, limit=8)
    except Exception as exc:
        return jsonify({"error": f"Recherche impossible : {exc}"}), 500

    if not matches:
        return jsonify({"error": "No YouTube match found."}), 404

    exclure = set(data.get("exclude") or [])
    if exclure:
        matches = [m for m in matches if m.get("id") not in exclure] or matches
    meilleur, note = choix_video.choisir(matches, titre or query, artiste, duree, query)
    return jsonify({"result": meilleur or matches[0], "score": round(note)})


_cover_cache = {}
_cover_lock = threading.Lock()


def _track_cover(uri):
    """Pochette d'un morceau Spotify via oEmbed, mise en cache."""
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
    """Pochettes de plusieurs morceaux d'un coup (en parallele : ~30 s en serie pour 100)."""
    uris = [u for u in (request.get_json(silent=True) or {}).get("uris", []) if u][:300]
    if not uris:
        return jsonify({"covers": {}})
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        resultats = list(pool.map(_track_cover, uris))
    return jsonify({"covers": {u: c for u, c in zip(uris, resultats) if c}})


@app.route("/api/stream-url", methods=["POST"])
def stream_url():
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
        with _yt_dlp().YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
        stream = info.get("url")
        if not stream and info.get("requested_formats"):
            stream = info["requested_formats"][0].get("url")
        if not stream:
            return jsonify({"error": "No audio stream found for this video."}), 404
        return jsonify({"stream_url": stream})
    except Exception as exc:
        return jsonify({"error": f"Preview unavailable: {exc}"}), 500


@app.route("/api/control/<job_id>", methods=["POST"])
def control_job(job_id):
    """Pauses or stops a running download (Downloads tab)."""
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
    # Titre/artiste connus de l'interface (venant de Spotify) : plus fiables que ce que
    # yt-dlp déduit du titre d'une vidéo YouTube.
    titre = (data.get("track_title") or "").strip()
    artiste = (data.get("artist") or "").strip()
    cover = (data.get("cover") or "").strip()
    if not video_url or not save_name:
        return jsonify({"error": "Missing video or file name."}), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None, "control": None, "opts": {
            "silences": data.get("trim_silence", True) is not False,
            "paroles": data.get("lyrics", True) is not False,
            "lrc": data.get("lyrics_file", True) is not False,
            "verif_duree": data.get("check_duration", True) is not False,
            "infos": data.get("album_info", True) is not False,
            "pochette": data.get("cover_source") or "spotify",
            "feat": bool(data.get("feat_to_artist")), "casse": bool(data.get("fix_case")),
            "saturation": data.get("check_clipping", True) is not False,
            "force": bool(data.get("force")), "album": (data.get("album") or "").strip(),
            "position": int(data.get("position") or 0), "pistes": int(data.get("positions") or 0),
            "playlist": (data.get("playlist") or "").strip(),
        }}
    threading.Thread(
        target=run_download,
        args=(job_id, video_url, save_name, dest_folder, quality, titre, artiste, cover,
              str(data.get("duration") or "").strip()),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/existants", methods=["POST"])
def api_existants():
    """Names of the audio files already in the destination folder: the interface
    removes these tracks from a playlist BEFORE downloading it."""
    data = request.get_json(silent=True) or {}
    dossier = Path((data.get("folder") or "").strip() or str(DEFAULT_DEST))
    try:
        noms = [f.name for f in dossier.iterdir() if f.suffix.lower() in verif_audio.EXTENSIONS_AUDIO]
    except Exception:
        noms = []
    return jsonify({"noms": noms})


@app.route("/api/candidates", methods=["POST"])
def api_candidates():
    """The best YouTube videos for a track, scored: to compare and pick."""
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Missing query."}), 400
    try:
        matches = search_videos(query, limit=8)
    except Exception as exc:
        return jsonify({"error": f"Recherche impossible : {exc}"}), 500
    titre, artiste = (data.get("title") or query), (data.get("artist") or "")
    duree = verif_audio.secondes(data.get("duration"))
    notes = sorted(((choix_video.noter(m, titre, artiste, duree, query), m) for m in matches),
                   key=lambda x: -x[0])
    return jsonify({"candidates": [dict(m, score=round(n)) for n, m in notes[:5]]})


@app.route("/api/artiste/recherche", methods=["POST"])
def api_artiste_recherche():
    q = ((request.get_json(silent=True) or {}).get("q") or "").strip()
    if not q:
        return jsonify({"error": "Nom d'artiste manquant."}), 400
    try:
        return jsonify({"artistes": artistes.chercher_artistes(q)})
    except Exception as exc:
        return jsonify({"error": f"Recherche impossible : {exc}"}), 500


@app.route("/api/artiste/<int:artiste_id>")
def api_artiste(artiste_id):
    try:
        return jsonify(artistes.discographie(artiste_id))
    except Exception as exc:
        return jsonify({"error": f"Discographie indisponible : {exc}"}), 500


@app.route("/api/artiste/albums", methods=["POST"])
def api_artiste_albums():
    ids = [int(i) for i in (request.get_json(silent=True) or {}).get("ids", []) if str(i).isdigit()][:300]
    try:
        par_album = artistes.titres_albums(ids)
        return jsonify({"par_album": par_album, "titres": [t for i in ids for t in par_album.get(str(i), [])]})
    except Exception as exc:
        return jsonify({"error": f"Titres indisponibles : {exc}"}), 500


@app.route("/api/bibliotheque", methods=["POST"])
def api_bibliotheque():
    data = request.get_json(silent=True) or {}
    dossier = (data.get("folder") or "").strip() or str(DEFAULT_DEST)
    return jsonify({"dossier": dossier, "morceaux": bibliotheque.lister(dossier, data.get("tags", True) is not False)})


@app.route("/api/tags", methods=["POST"])
def api_tags():
    """Reads (without "champs") or edits (with "champs") a file's tags."""
    data = request.get_json(silent=True) or {}
    chemin = Path((data.get("chemin") or "").strip())
    if not chemin.is_file() or chemin.suffix.lower() not in verif_audio.EXTENSIONS_AUDIO:
        return jsonify({"error": "File not found."}), 404
    if isinstance(data.get("champs"), dict):
        err = qualite_tags.ecrire(chemin, data["champs"])
        if err:
            return jsonify({"error": err}), 500
    return jsonify({"tags": qualite_tags.lire(chemin)})


@app.route("/api/retaguer", methods=["POST"])
def api_retaguer():
    data = request.get_json(silent=True) or {}
    chemin = Path((data.get("chemin") or "").strip())
    if not chemin.is_file():
        return jsonify({"error": "File not found."}), 404
    try:
        fait = bibliotheque.retaguer(chemin, data.get("lyrics", True) is not False, bool(data.get("cover_hd")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"fait": fait, "tags": qualite_tags.lire(chemin)})


@app.route("/api/expliquer", methods=["POST"])
def api_expliquer():
    return jsonify(fiabilite.expliquer((request.get_json(silent=True) or {}).get("message") or ""))


@app.route("/api/nettoyer", methods=["POST"])
def api_nettoyer():
    dossier = ((request.get_json(silent=True) or {}).get("folder") or "").strip() or str(DEFAULT_DEST)
    return jsonify(fiabilite.nettoyer(dossier))


@app.route("/api/diagnostic")
def api_diagnostic():
    return jsonify({"tests": fiabilite.diagnostic(search_videos, recherche_spotify,
                                                  lambda: "version" in _ffmpeg_sortie(["-version"]).lower())})


@app.route("/api/version")
def api_version():
    import sys
    return jsonify({"ytdlp": _yt_dlp().version.__version__, "python": sys.version.split()[0], "plateforme": "Android",
                    "maj_possible": False})


@app.route("/api/decouvrir/<quoi>")
def api_decouvrir(quoi):
    try:
        if quoi == "top":
            return jsonify({"titres": decouvrir.top(100)})
        if quoi == "genres":
            return jsonify({"genres": decouvrir.genres()})
        if quoi == "playlists":
            return jsonify({"playlists": decouvrir.playlists_populaires()})
        if quoi == "pays":
            return jsonify({"pays": decouvrir.PAYS})
        if quoi.startswith("pays-"):
            return jsonify({"titres": decouvrir.top_pays(quoi[5:], 100)})
        if quoi.startswith("genre-") and quoi[6:].isdigit():
            return jsonify({"titres": decouvrir.genre(int(quoi[6:]))})
    except Exception as exc:
        return jsonify({"error": f"Indisponible : {exc}"}), 500
    return jsonify({"error": "Inconnu."}), 404


@app.route("/api/artiste/<int:artiste_id>/<quoi>")
def api_artiste_plus(artiste_id, quoi):
    try:
        if quoi == "similaires":
            return jsonify({"artistes": decouvrir.similaires(artiste_id)})
        if quoi == "mix":
            return jsonify({"titres": decouvrir.mix(artiste_id)})
    except Exception as exc:
        return jsonify({"error": f"Indisponible : {exc}"}), 500
    return jsonify({"error": "Inconnu."}), 404


@app.route("/api/paroles-fichier", methods=["POST"])
def api_paroles_fichier():
    """Lyrics stored in a file (or in the .lrc next to it)."""
    chemin = Path(((request.get_json(silent=True) or {}).get("chemin") or "").strip())
    if not chemin.is_file():
        return jsonify({"error": "File not found."}), 404
    texte = ""
    try:
        import mutagen
        f = mutagen.File(str(chemin))
        t = f.tags if f else None
        if t is not None:
            for k in list(t.keys()):
                if str(k).startswith("USLT"):
                    texte = str(t[k].text); break
            if not texte:
                for k in ("LYRICS", "©lyr"):
                    if k in t:
                        texte = str(t[k][0]); break
    except Exception:
        pass
    lrc = chemin.with_suffix(".lrc")
    if not texte and lrc.exists():
        texte = "\n".join(l.split("]", 1)[-1] for l in lrc.read_text(encoding="utf-8", errors="replace").splitlines())
    return jsonify({"paroles": texte})


def CONVERTIR(args):
    return _ffmpeg(args)


@app.route("/api/reduire", methods=["POST"])
def api_reduire():
    """Converts an existing file to a lighter format (tags, cover art, lyrics kept)."""
    data = request.get_json(silent=True) or {}
    src = Path((data.get("chemin") or "").strip())
    fmt = (data.get("format") or "opus").strip()
    if not src.is_file():
        return jsonify({"error": "File not found."}), 404
    cible = src.with_suffix(bibliotheque.extension_pour(fmt))
    if cible == src:
        cible = src.with_name(src.stem + ".reduit" + cible.suffix)
    avant = src.stat().st_size
    if not CONVERTIR(bibliotheque.args_conversion(src, cible, fmt)) or not cible.exists() or cible.stat().st_size == 0:
        cible.unlink(missing_ok=True)
        return jsonify({"error": "Conversion impossible."}), 500
    bibliotheque.transferer_infos(src, cible)
    apres = cible.stat().st_size
    if data.get("supprimer_original", True):
        src.unlink(missing_ok=True)
        if cible.name.endswith(".reduit" + cible.suffix):
            final = src.with_suffix(cible.suffix); cible.replace(final); cible = final
    return jsonify({"chemin": str(cible), "nom": cible.name, "avant": avant, "apres": apres})


@app.route("/api/connexion")
def api_connexion():
    """Is the internet reachable? (YouTube then Cloudflare, short timeouts)."""
    for url in ("https://www.youtube.com/generate_204", "https://1.1.1.1/"):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4):
                return jsonify({"ok": True})
        except urllib.error.HTTPError:
            return jsonify({"ok": True})  # the server answered: the connection works
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
                    _log(job_id, f"  ↳ erreur de recherche : {exc}")
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
                    _log(job_id, f"  ↳ erreur : {exc}")
            _log(job_id, f"Done: {added}/{len(tracks)} videos added.")
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
        return jsonify({"error": "Invalid target (spotify or youtube)."}), 400
    if not tracks:
        return jsonify({"error": "No tracks to transfer."}), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None, "control": None}
    threading.Thread(target=run_transfer, args=(job_id, target, name, tracks), daemon=True).start()
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
        return f"Connexion Spotify annulée ou refusée ({error}). Reviens dans l'app MusicFlow.", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "Invalid or expired OAuth state, try again from the app.", 400
    try:
        spotify_client.exchange_code(code)
    except Exception as exc:
        return f"Échec de connexion Spotify : {exc}", 500
    return "Connexion Spotify réussie — reviens dans l'app MusicFlow."


@app.route("/auth/youtube/login")
def youtube_login():
    if not store.has_app_credentials("youtube"):
        return "First set the Google Client ID / Client Secret in the Accounts tab.", 400
    return redirect(youtube_client.build_authorize_url(_new_oauth_state()))


@app.route("/auth/youtube/callback")
def youtube_callback():
    error = request.args.get("error")
    if error:
        return f"Connexion YouTube annulée ou refusée ({error}). Reviens dans l'app MusicFlow.", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "Invalid or expired OAuth state, try again from the app.", 400
    try:
        youtube_client.exchange_code(code)
    except Exception as exc:
        return f"Échec de connexion YouTube : {exc}", 500
    return "Connexion YouTube réussie — reviens dans l'app MusicFlow."


def configure(default_dest: str, config_path: str):
    """Appelé une fois par MainActivity avant start_server()."""
    global DEFAULT_DEST
    DEFAULT_DEST = Path(default_dest)
    DEFAULT_DEST.mkdir(parents=True, exist_ok=True)
    store.init(config_path)


def start_server():
    """Bloquant — à appeler depuis un thread d'arrière-plan Kotlin, jamais le thread UI."""
    app.run(host="127.0.0.1", port=5090, debug=False, threaded=True, use_reloader=False)
