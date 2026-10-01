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
        from mutagen.flac import Picture
        _MODULES["tags"] = dict(Image=Image, MP4=MP4, MP4Cover=MP4Cover, ID3=ID3, APIC=APIC,
                                TIT2=TIT2, TPE1=TPE1, TPE2=TPE2,
                                ID3NoHeaderError=ID3NoHeaderError, OggOpus=OggOpus,
                                Picture=Picture)
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
    """Nom de fichier sûr — le titre complet, lui, reste intact dans les étiquettes.

    Les titres YouTube contiennent souvent un emoji. Gardé dans le nom de fichier, il
    pose problème à l'enregistrement sur une carte SD : les emoji sont hors du plan
    multilingue de base (au-delà de U+FFFF) et le passage par le stockage Android les
    rejette. Les écritures non latines (arabe, cyrillique…) sont en revanche conservées
    telles quelles : elles fonctionnent, c'est vérifié.
    """
    name = re.sub(r'[\\/:*?"<>|]', "", name or "")
    # Emoji et pictogrammes hors du plan de base…
    name = re.sub(r"[\U00010000-\U0010FFFF]", "", name)
    # …mais aussi ceux qui sont DANS le plan de base (✨ ⚡ ★ ➜ ☆ ✚), que le motif
    # précédent laissait passer, plus les sélecteurs et marques de sens invisibles.
    name = re.sub(r"[←-⇿⌀-⏿①-⓿■-➿"
                  r"⤀-⥿⬀-⯿︀-️​-‏]", "", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)                  # caractères de contrôle
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if not name:
        return "musique"
    # Garde-fou : si le système de fichiers ne sait pas encoder ce nom, mieux vaut un
    # nom de repli qu'un téléchargement qui échoue.
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
                    # La page embed ne donne pas de pochette par morceau : sans l'appel
                    # /api/track-covers ci-dessous, tous les titres portaient la même image
                    # (celle de la playlist).
                    "thumbnail": playlist_thumb,
                    "uri": t.get("uri") or "",
                    "query": f"{title} {artists}".strip(),
                }
            )
        if not items:
            raise ValueError("Aucun morceau trouvé dans cette playlist Spotify.")
        name = entity.get("name") or entity.get("title") or "Playlist Spotify"
        if len(items) >= 50:
            name += " — liste limitée — connecte ton compte Spotify (playlist privée) ou configure au moins un Client ID/Secret (playlist publique) dans Comptes"
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
                raise ValueError("Aucune vidéo trouvée dans cette playlist YouTube.")
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

    raise ValueError("Lien non reconnu — utilise un lien Spotify ou YouTube.")


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
        else:
            return f"format non pris en charge : {ext}"
        return ""
    except Exception as exc:
        return f"écriture tag ({ext}) : {exc}"


# Nombre de reprises automatiques après une coupure réseau, et attente entre deux essais.
_REPRISES_MAX = 6
_ATTENTE_REPRISE = (3, 5, 10, 15, 30, 45)


def _est_erreur_reseau(exc) -> bool:
    """Distingue une coupure réseau (à réessayer) d'une vraie erreur (vidéo supprimée,
    format indisponible…), qu'il serait inutile de retenter en boucle."""
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
    """Levée depuis le hook de progression pour arrêter yt-dlp en cours de route."""


def _controle(job_id: str):
    with JOBS_LOCK:
        return (JOBS.get(job_id) or {}).get("control")


def _nettoyer_partiels(dest_path, safe_title: str):
    """Supprime les fichiers partiels (.part, .ytdl) laissés par un arrêt.

    Sur pause on les GARDE au contraire : yt-dlp reprend automatiquement là où il
    s'était arrêté au téléchargement suivant.
    """
    try:
        for f in dest_path.glob(safe_title + ".*"):
            # .part/.ytdl = téléchargement partiel ; .webp/.jpg = miniature laissée
            # par yt-dlp avant l'intégration de la pochette.
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
        return "aucune métadonnée à écrire"
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
    """Ne garde que l'artiste principal.

    Spotify renvoie souvent « Clean Bandit, Jess Glynne » ou « Calvin Harris feat. Rihanna ».
    Écrite telle quelle dans le fichier, cette liste crée un artiste distinct par
    combinaison dans les lecteurs : « Coldplay » et « Coldplay, BTS » se retrouvent
    séparés, et les morceaux d'un même artiste ne sont plus regroupés.
    """
    a = (artistes or "").strip()
    if not a:
        return ""
    # Uniquement les séparateurs que Spotify emploie vraiment pour distinguer DEUX
    # artistes : la virgule et les mentions « feat. ». On écarte « & », « and », « x »,
    # « et » — ils appartiennent le plus souvent au nom d'un groupe (Simon and Garfunkel,
    # Coldplay & BTS étant de toute façon renvoyé « Coldplay, BTS » par Spotify).
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
    """Premier titre/artiste connu, avant même le téléchargement — sert de requête pour
    chercher le morceau sur Spotify. Repli sur le nom d'enregistrement « Titre - Artiste »
    quand rien n'a été transmis (recherche YouTube directe)."""
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


def run_download(job_id: str, video_url: str, save_name: str, dest_folder: str, quality: str = "192",
                 titre: str = "", artiste: str = "", cover: str = ""):
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

    guess_titre, guess_artiste = _deviner_titre_artiste_initial(save_name, titre, artiste)
    cover_url = (cover or "").strip()
    tag_titre, tag_artiste = guess_titre, guess_artiste
    if not cover_url:
        # Pas de pochette Spotify déjà connue (venant d'un lien/playlist Spotify) : on
        # cherche le morceau sur Spotify pour récupérer son vrai titre/artiste et sa vraie
        # pochette, plutôt que de garder le titre brut de la vidéo YouTube.
        requete_titre = _nettoyer_titre_recherche(guess_titre)
        try:
            match = spotify_client.search_track(requete_titre, "")
        except Exception:
            match = None
        if match and match.get("title") and _match_plausible(requete_titre, match["title"]):
            tag_titre = match["title"]
            tag_artiste = premier_artiste(match.get("artist") or "") or guess_artiste
            cover_url = match.get("cover") or ""
            _log(job_id, f"Correspondance Spotify trouvée : « {tag_titre} » — {tag_artiste or 'artiste inconnu'}.")
        elif match:
            _log(job_id, f"Résultat Spotify écarté (« {match.get('title', '')} » ne correspond pas à « {requete_titre} »).")

    safe_title = sanitize_filename(f"{tag_titre} - {tag_artiste}" if tag_artiste else tag_titre)

    doublons = [f for f in dest_path.glob(safe_title + ".*")
                if f.suffix.lower() in (".m4a", ".mp3", ".opus", ".ogg", ".mp4", ".webm")]
    if doublons:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(doublons[0])
        _log(job_id, f"Doublon détecté — « {doublons[0].name} » est déjà dans le dossier, téléchargement ignoré.")
        return

    out_template = str(dest_path / f"{safe_title}.%(ext)s")

    def progress_hook(d):
        # Pause ou arrêt demandé depuis l'onglet Téléchargements : on interrompt yt-dlp
        # en levant une exception depuis le hook, seul moyen de l'arrêter en cours.
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
                _log(job_id, f"Téléchargement… {pct} ({speed}){suffixe}")
        elif d.get("status") == "finished":
            _log(job_id, "Téléchargement terminé.")

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Reprise d'un fichier partiel et réessais internes : première ligne de défense
        # contre une connexion instable, avant la boucle de reprise ci-dessous.
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
    }

    def _telecharger_avec_reprises():
        """Télécharge en reprenant automatiquement après une coupure réseau.

        Le fichier partiel est conservé entre deux essais (continuedl), donc chaque
        reprise repart d'où la connexion s'est interrompue au lieu de tout refaire.
        Une pause/un arrêt demandés par l'utilisateur ne sont PAS des erreurs : ils
        traversent la boucle sans déclencher de reprise.
        """
        for essai in range(1, _REPRISES_MAX + 2):
            try:
                with _yt_dlp().YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(video_url, download=True)
            except _Interrompu:
                raise
            except Exception as exc:
                if essai > _REPRISES_MAX or not _est_erreur_reseau(exc):
                    raise
                attente = _ATTENTE_REPRISE[min(essai - 1, len(_ATTENTE_REPRISE) - 1)]
                _log(job_id, f"Connexion perdue — reprise dans {attente} s "
                             f"(essai {essai}/{_REPRISES_MAX}).")
                # Attente fractionnée pour rester réactif à une pause/un arrêt.
                for _ in range(attente * 2):
                    if _controle(job_id):
                        raise _Interrompu(_controle(job_id))
                    time.sleep(0.5)
                _log(job_id, "Reprise du téléchargement…")
        raise RuntimeError("Reprise impossible.")

    try:
        info = _telecharger_avec_reprises()
        ext = info.get("ext", "m4a")
        final_path = dest_path / f"{safe_title}.{ext}"

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
            _log(job_id, f"Métadonnées non écrites ({err_tags}).")
        else:
            _log(job_id, f"Métadonnées : « {tag_titre} » — {tag_artiste or 'artiste inconnu'}.")

        _log(job_id, f"Enregistré dans : {final_path}")
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(final_path)
    except _Interrompu as arret:
        mode = str(arret)
        if mode == "stop":
            _nettoyer_partiels(dest_path, safe_title)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "paused" if mode == "pause" else "cancelled"
        _log(job_id, "Téléchargement mis en pause." if mode == "pause" else "Téléchargement arrêté.")
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
    # Pas de sélecteur natif sur mobile v1 : téléchargement toujours dans DEFAULT_DEST.
    return jsonify({"folder": None})


@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Le nom de la musique est requis."}), 400
    try:
        results = search_videos(title)
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
    """Trouve le meilleur match YouTube pour un morceau venant de Spotify.

    On prenait le tout premier résultat de recherche, sans contrôle : selon l'humeur de
    YouTube on téléchargeait un live, un karaoké, une réaction ou un passage de casting
    à la place de la chanson. On note maintenant plusieurs candidats (voir choix_video),
    la durée exacte fournie par Spotify servant de signal principal.
    """
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    titre = (data.get("title") or "").strip()
    artiste = (data.get("artist") or "").strip()
    duree = data.get("duration") or 0

    if not query:
        return jsonify({"error": "Requête manquante."}), 400

    try:
        matches = search_videos(query, limit=8)
    except Exception as exc:
        return jsonify({"error": f"Recherche impossible : {exc}"}), 500

    if not matches:
        return jsonify({"error": "Aucune correspondance YouTube trouvée."}), 404

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
            return jsonify({"error": "Flux audio introuvable pour cette vidéo."}), 404
        return jsonify({"stream_url": stream})
    except Exception as exc:
        return jsonify({"error": f"Aperçu indisponible : {exc}"}), 500


@app.route("/api/control/<job_id>", methods=["POST"])
def control_job(job_id):
    """Met en pause ou arrête un téléchargement en cours (onglet Téléchargements)."""
    action = ((request.get_json(silent=True) or {}).get("action") or "").strip()
    if action not in ("pause", "stop"):
        return jsonify({"error": "Action inconnue."}), 400
    with JOBS_LOCK:
        if job_id not in JOBS:
            return jsonify({"error": "Téléchargement introuvable."}), 404
        JOBS[job_id]["control"] = action
    return jsonify({"ok": True})


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    video_url = (data.get("url") or "").strip()
    save_name = (data.get("save_name") or "").strip()
    dest_folder = (data.get("folder") or "").strip()
    quality = (data.get("quality") or "192").strip()
    # Titre/artiste connus de l'interface (venant de Spotify) : plus fiables que ce que
    # yt-dlp déduit du titre d'une vidéo YouTube.
    titre = (data.get("track_title") or "").strip()
    artiste = (data.get("artist") or "").strip()
    cover = (data.get("cover") or "").strip()
    if not video_url or not save_name:
        return jsonify({"error": "Vidéo ou nom de fichier manquant."}), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None, "control": None}
    threading.Thread(
        target=run_download,
        args=(job_id, video_url, save_name, dest_folder, quality, titre, artiste, cover),
        daemon=True,
    ).start()
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
        return f"Connexion Spotify annulée ou refusée ({error}). Reviens dans l'app MusicFlow.", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "État OAuth invalide ou expiré, réessaie depuis l'app.", 400
    try:
        spotify_client.exchange_code(code)
    except Exception as exc:
        return f"Échec de connexion Spotify : {exc}", 500
    return "Connexion Spotify réussie — reviens dans l'app MusicFlow."


@app.route("/auth/youtube/login")
def youtube_login():
    if not store.has_app_credentials("youtube"):
        return "Configure d'abord le Client ID / Client Secret Google dans l'onglet Comptes.", 400
    return redirect(youtube_client.build_authorize_url(_new_oauth_state()))


@app.route("/auth/youtube/callback")
def youtube_callback():
    error = request.args.get("error")
    if error:
        return f"Connexion YouTube annulée ou refusée ({error}). Reviens dans l'app MusicFlow.", 400
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not _consume_oauth_state(state):
        return "État OAuth invalide ou expiré, réessaie depuis l'app.", 400
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
