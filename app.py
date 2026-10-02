"""MusicFlow — recherche un titre et télécharge l'audio en MP3."""
try:
    import truststore  # certificats Windows (évite CERTIFICATE_VERIFY_FAILED)
    truststore.inject_into_ssl()
except ImportError:
    pass
import difflib
import json
import re
import unicodedata
import hashlib
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory
import imageio_ffmpeg
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.wave import WAVE

import secrets_store as store
import choix_video
import spotify_client
import spotify_scan
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
        if p in ("track", "playlist", "album", "artist") and i + 1 < len(parts):
            return p, parts[i + 1]
    return None, None


def resolve_link(url: str):
    """Détecte un lien Spotify ou YouTube (morceau ou playlist) et liste les morceaux.
    Retourne (items, playlist_name) — playlist_name est None pour un morceau seul."""
    autre = liens_autres.resoudre(url)   # Deezer, Apple Music
    if autre is not None:
        return autre
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
                    # La page embed ne donne aucune pochette par morceau : seule celle de la
                    # playlist existe ici, et l'utiliser partout affichait la même image sur
                    # tous les titres. On la garde en attendant, et l'interface remplace
                    # ensuite chaque vignette par la vraie via /api/track-covers (l'uri du
                    # morceau, lui, est bien fourni).
                    "thumbnail": playlist_thumb,
                    "uri": t.get("uri") or "",
                    "query": f"{title} {artists}".strip(),
                }
            )
        if not items:
            raise ValueError("Aucun morceau trouvé dans cette playlist Spotify.")
        name = entity.get("name") or entity.get("title") or "Playlist Spotify"
        # La page d'aperçu publique plafonne à 100 morceaux : en dessous, la liste est complète.
        # (On marquait « limitée » dès 50, ce qui lançait un scan du lecteur web — qui ne
        # montre qu'une quarantaine de lignes sans compte — et remplaçait 50 titres par ~40.)
        if len(items) >= 100:
            name += " — liste limitée (100 premiers morceaux)"
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


VALID_QUALITIES = {"128", "192", "320", "flac", "wav", "opus", "aac"}
# Formats sans perte (aucune recompression après le décodage) : même fidélité que la
# source, WAV n'étant qu'un FLAC non compressé (fichier bien plus gros, sans avantage
# de qualité réel).
_FORMATS_SANS_PERTE = {"flac", "wav"}


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


def _ecrire_tags(chemin, titre: str, artiste: str) -> str:
    if str(chemin).lower().rsplit(".", 1)[-1] in ("m4a", "opus", "ogg"):
        return qualite_tags.ecrire_base(chemin, titre, artiste)
    return _ecrire_tags_id3_flac(chemin, titre, artiste)


def _ecrire_tags_id3_flac(chemin, titre: str, artiste: str) -> str:
    """Écrit titre/artiste dans le fichier final (MP3 : ID3, FLAC : Vorbis comments).

    FFmpegMetadata renseigne déjà des tags, mais à partir du titre de la vidéo YouTube
    (« Coldplay - Hymn for the Weekend (Official Video) ») : quand l'interface connaît
    les vraies valeurs via Spotify, elles sont plus justes et évitent un « Inconnu »
    ou un titre pollué dans les lecteurs.
    """
    if not titre and not artiste:
        return "aucune métadonnée à écrire"
    ext = str(chemin).lower().rsplit(".", 1)[-1]
    try:
        if ext == "flac":
            audio = FLAC(str(chemin))
            if titre:
                audio["title"] = titre
            if artiste:
                audio["artist"] = artiste
                audio["albumartist"] = artiste
            audio.save()
            return ""
        if ext == "wav":
            wav = WAVE(str(chemin))
            if wav.tags is None:
                wav.add_tags()
            if titre:
                wav.tags.add(TIT2(encoding=3, text=titre))
            if artiste:
                wav.tags.add(TPE1(encoding=3, text=artiste))
                wav.tags.add(TPE2(encoding=3, text=artiste))
            wav.save()
            return ""
        try:
            id3 = ID3(str(chemin))
        except ID3NoHeaderError:
            id3 = ID3()
        if titre:
            id3.add(TIT2(encoding=3, text=titre))
        if artiste:
            id3.add(TPE1(encoding=3, text=artiste))
            id3.add(TPE2(encoding=3, text=artiste))
        id3.save(str(chemin))
        return ""
    except Exception as exc:
        return f"écriture métadonnées : {exc}"


def _telecharger_image(url: str) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception:
        return None


_SPOTIFY_IMG_RE = re.compile(r"(i\.scdn\.co/image/)[0-9a-f]{16}([0-9a-f]{24})", re.IGNORECASE)


def _pochette_haute_resolution(url: str) -> str:
    """Les vignettes récupérées (ligne de résultat de recherche, oEmbed…) sont souvent de
    petits formats (64×64) : le préfixe d'ID d'image i.scdn.co encode la résolution, on le
    force à ab67616d0000b273 (640×640) pour avoir une vraie pochette d'album."""
    return _SPOTIFY_IMG_RE.sub(r"\1ab67616d0000b273\2", url)


def _embed_cover_spotify(chemin, cover_url: str) -> str:
    """Remplace la pochette embarquée (celle de la vignette YouTube, mise par
    EmbedThumbnail) par la vraie pochette Spotify du morceau."""
    data = _telecharger_image(_pochette_haute_resolution(cover_url))
    if not data:
        return "pochette Spotify indisponible"
    mime = "image/png" if cover_url.split("?")[0].lower().endswith(".png") else "image/jpeg"
    ext = str(chemin).lower().rsplit(".", 1)[-1]
    if ext in ("m4a", "opus", "ogg"):
        return qualite_tags.pochette(chemin, data, mime)
    try:
        if ext == "flac":
            audio = FLAC(str(chemin))
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
            wav = WAVE(str(chemin))
            if wav.tags is None:
                wav.add_tags()
            wav.tags.delall("APIC")
            wav.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            wav.save()
            return ""
        try:
            id3 = ID3(str(chemin))
        except ID3NoHeaderError:
            id3 = ID3()
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        id3.save(str(chemin))
        return ""
    except Exception as exc:
        return f"écriture pochette : {exc}"


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


_JUNK_TITRE_RE = re.compile(
    r"[\(\[][^)\]]*\b(lyrics?|paroles?|clip\s*officiel|officiel|official|"
    r"music\s*video|lyric\s*video|video|audio|visuali[sz]er|hd|hq|4k|mv)\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def _match_plausible(requete: str, titre_trouve: str) -> bool:
    """Écarte un résultat Spotify sans rapport avec la requête (arrive quand la recherche
    part d'un titre YouTube trop pollué) — mieux vaut garder le titre YouTube brut qu'un
    autre morceau."""
    a = unicodedata.normalize("NFKD", requete.lower()).encode("ascii", "ignore").decode()
    b = unicodedata.normalize("NFKD", titre_trouve.lower()).encode("ascii", "ignore").decode()
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.4 or b.strip() in a


def _nettoyer_titre_recherche(titre: str) -> str:
    """Retire les mentions parasites d'un titre de vidéo YouTube (« Lyrics », « Official
    Video »…) avant de l'utiliser comme requête de recherche Spotify — les garder pollue
    la recherche et lui fait manquer le vrai morceau (ou trouver un autre titre)."""
    nettoye = _JUNK_TITRE_RE.sub("", titre)
    nettoye = re.sub(r"\s+", " ", nettoye).strip(" -")
    return nettoye or titre.strip()


def run_download(job_id: str, video_url: str, save_name: str, dest_folder: str, quality: str = "flac",
                 titre: str = "", artiste: str = "", cover: str = "", duree_attendue: str = ""):
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
        # pochette, plutôt que de garder le titre brut de la vidéo YouTube (souvent pollué
        # par « Lyrics », « Official Video »…) et sa vignette.
        requete_titre = _nettoyer_titre_recherche(guess_titre)
        match = spotify_client.search_track(requete_titre, "") or spotify_scan.chercher_morceau(requete_titre, "")
        if match and match.get("title") and _match_plausible(requete_titre, match["title"]):
            tag_titre = match["title"]
            tag_artiste = premier_artiste(match.get("artist") or "") or guess_artiste
            cover_url = match.get("cover") or ""
            _log(job_id, f"Correspondance Spotify trouvée : « {tag_titre} » — {tag_artiste or 'artiste inconnu'}.")
        elif match:
            _log(job_id, f"Résultat Spotify écarté (« {match.get('title', '')} » ne correspond pas à « {requete_titre} »).")

    tag_titre, tag_artiste = qualite_tags.nettoyer(tag_titre, tag_artiste, opts.get("feat", False), opts.get("casse", False))
    safe_title = sanitize_filename(f"{tag_titre} - {tag_artiste}" if tag_artiste else tag_titre)
    extension = {"flac": "flac", "wav": "wav", "opus": "opus", "aac": "m4a"}.get(quality, "mp3")

    doublon_path = dest_path / f"{safe_title}.{extension}"
    if not doublon_path.exists():
        doublon_path = verif_audio.doublon_dans_dossier(dest_path, safe_title) or doublon_path
    if doublon_path.exists() and opts.get("force"):
        _log(job_id, f"Remplacement de « {doublon_path.name} » par une autre version.")
        try:
            doublon_path.unlink()
            doublon_path.with_suffix(".lrc").unlink(missing_ok=True)
        except Exception:
            pass
        doublon_path = dest_path / f"{safe_title}.{extension}"
    if doublon_path.exists():
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(doublon_path)
            JOBS[job_id]["doublon"] = True
        _log(job_id, f"Doublon détecté — « {doublon_path.name} » est déjà dans le dossier, téléchargement ignoré.")
        return
    if not verif_audio.reserver(doublon_path):
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["file"] = str(doublon_path)
            JOBS[job_id]["doublon"] = True
        _log(job_id, f"Doublon détecté — « {doublon_path.name} » est déjà en cours de téléchargement.")
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
            # Temps restant : l'interface l'affiche dans l'onglet Téléchargements, elle le
            # relit dans cette ligne (format « ETA 01:23 »). Sans lui, elle ne peut pas
            # l'estimer pour un morceau seul.
            eta = d.get("eta")
            if pct:
                suffixe = ""
                if isinstance(eta, (int, float)) and eta >= 0:
                    eta = int(eta)
                    suffixe = f" ETA {eta // 60:02d}:{eta % 60:02d}"
                _log(job_id, f"Téléchargement… {pct} ({speed}){suffixe}")
        elif d.get("status") == "finished":
            _log(job_id, f"Téléchargement terminé, conversion en {extension.upper()}…")

    ydl_opts = {
        "format": "bestaudio/best",
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
        "ffmpeg_location": FFMPEG_EXE,
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": extension,
                **({} if extension in _FORMATS_SANS_PERTE
                   else {"preferredquality": {"opus": "160", "aac": "256"}.get(quality, quality)}),
            },
            # yt-dlp ne sait pas intégrer de miniature dans un .wav (formats acceptés :
            # mp3, mkv/mka, ogg/opus/flac, m4a/mp4/m4v/mov) — la pochette WAV est plutôt
            # embarquée nous-mêmes après coup, via _embed_cover_spotify.
            *([] if extension in ("wav", "opus") else [{"key": "EmbedThumbnail"}]),
            {"key": "FFmpegMetadata"},  # titre/artiste dans les tags
        ],
        "progress_hooks": [progress_hook],
        # Silence/écran noir au début ou à la fin de la vidéo : retiré à la conversion.
        # Silence/écran noir retiré à la conversion. Opus/AAC : la source est souvent déjà dans
        # ce codec et yt-dlp la recopierait (incompatible avec un filtre) — on force le réencodage.
        **({"postprocessor_args": {"extractaudio": ["-af", verif_audio.FILTRE_SILENCE]
                                   + {"opus": ["-c:a", "libopus", "-b:a", "160k"],
                                      "aac": ["-c:a", "aac", "-b:a", "256k"]}.get(quality, [])}}
           if opts.get("silences", True) else {}),
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
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.extract_info(video_url, download=True)
            except _Interrompu:
                raise
            except Exception as exc:
                if "Postprocessing" in str(exc):
                    raise  # erreur de conversion, pas un refus de YouTube
                if not _est_erreur_reseau(exc):
                    # Refus de YouTube (robot, format indisponible, client bloqué…) : on
                    # réessaie avec d'autres clients avant de déclarer l'échec.
                    for clients in (["android", "web"], ["tv", "web_safari"], ["ios", "mweb"]):
                        _log(job_id, f"YouTube a refusé ({str(exc)[:120]}) — nouvel essai ({', '.join(clients)})…")
                        opts2 = dict(ydl_opts, extractor_args={"youtube": {"player_client": clients}})
                        try:
                            with yt_dlp.YoutubeDL(opts2) as ydl:
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
        final_path = dest_path / f"{safe_title}.{extension}"
        err_tags = _ecrire_tags(final_path, tag_titre, tag_artiste)
        if err_tags:
            _log(job_id, f"Métadonnées non écrites ({err_tags}).")
        else:
            _log(job_id, f"Métadonnées : « {tag_titre} » — {tag_artiste or 'artiste inconnu'}.")

        # WAV : yt-dlp ne peut pas y intégrer de miniature lui-même (voir ydl_opts) — sans
        # pochette Spotify, on embarque nous-mêmes celle de la vidéo YouTube en repli.
        source_pochette = "Spotify"
        if not cover_url and extension in ("wav", "opus"):
            cover_url = info.get("thumbnail") or ""
            source_pochette = "YouTube"

        if cover_url:
            err_cover = _embed_cover_spotify(final_path, cover_url)
            if err_cover:
                _log(job_id, f"Pochette non appliquée ({err_cover}).")
            else:
                _log(job_id, f"Pochette {source_pochette} appliquée.")

        infos = None
        if opts.get("infos", True):
            infos = infos_album.chercher(tag_titre, premier_artiste(tag_artiste),
                                         verif_audio.secondes(duree_attendue), opts.get("album") or "")
            if infos and infos.get("pochette_hd") and (opts.get("pochette") == "hd" or not cover_url
                                                       or source_pochette == "YouTube"):
                if not _embed_cover_spotify(final_path, infos["pochette_hd"]):
                    _log(job_id, "Pochette HD (1000 px) appliquée.")
        if opts.get("position"):
            # Mode playlist : album = nom de la playlist, n° de piste = place dans la playlist
            infos = dict(infos or {}, album=opts.get("playlist") or "Playlist", artiste_album="Divers",
                         piste=int(opts["position"]), pistes=int(opts.get("pistes") or 0), disque=1, disques=1)
            infos_album.integrer(final_path, infos)
            _log(job_id, f"Ordre de la playlist : piste {opts['position']}/{opts.get('pistes') or '?'}.")
        if opts.get("infos", True) and not opts.get("position"):
            if infos:
                err_i = infos_album.integrer(final_path, infos)
                _log(job_id, (f"Album : « {infos['album']} » — {infos.get('genre') or 'genre inconnu'}"
                              f", piste {infos.get('piste') or '?'}"
                              + (f"/{infos['pistes']}" if infos.get('pistes') else "")
                              + (f", {infos['annee']}" if infos.get('annee') else "") + ".")
                     if not err_i else f"Infos d'album non écrites ({err_i}).")
            else:
                _log(job_id, "Infos d'album introuvables pour ce morceau.")

        if opts.get("paroles", True):
            trouvees = paroles.chercher(tag_titre, premier_artiste(tag_artiste),
                                        verif_audio.secondes(duree_attendue))
            if trouvees:
                err_p = paroles.integrer(final_path, trouvees, opts.get("lrc", True))
                _log(job_id, "Paroles ajoutées" + (" (synchronisées, .lrc)" if trouvees.get("synced") else "")
                     + "." if not err_p else f"Paroles non ajoutées ({err_p}).")
            else:
                _log(job_id, "Pas de paroles trouvées pour ce morceau.")

        # Miniature YouTube laissée à côté par yt-dlp (WAV/Opus) : inutile une fois intégrée.
        for ext_img in (".webp", ".jpg", ".png"):
            try:
                (dest_path / f"{safe_title}{ext_img}").unlink(missing_ok=True)
            except Exception:
                pass
        avertissement = (verif_audio.verifier_duree(final_path, duree_attendue)
                         if opts.get("verif_duree", True) else "")
        if avertissement:
            _log(job_id, f"À vérifier : durée {avertissement} sur Spotify — ce n'est peut-être pas la bonne version.")
        if opts.get("saturation", True):
            try:
                import subprocess
                sortie = subprocess.run([FFMPEG_EXE, "-hide_banner", "-i", str(final_path), "-af", "volumedetect",
                                         "-f", "null", "-"], capture_output=True, text=True, timeout=120).stderr
                sat = qualite_tags.saturation(sortie)
                if sat:
                    _log(job_id, f"À vérifier : {sat}.")
                    avertissement = ", ".join(x for x in (avertissement, sat) if x)
            except Exception:
                pass
        _log(job_id, f"Enregistré dans : {final_path}")
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
        _log(job_id, "Téléchargement mis en pause." if mode == "pause" else "Téléchargement arrêté.")
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Erreur : {exc}")
    finally:
        verif_audio.liberer(doublon_path)


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

    # Recherche sur Spotify d'abord (nom, artiste, pochette officiels) ; le morceau
    # YouTube correspondant est choisi au moment d'écouter/télécharger (/api/resolve-track).
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


def recherche_spotify(requete: str, limite: int = 10) -> list[dict]:
    """Résultats de la recherche Spotify (navigateur piloté, sans connexion ni API),
    au même format que les morceaux d'un lien Spotify."""
    try:
        lignes = spotify_scan.chercher_morceaux(requete, limite)
    except Exception:
        return []
    items = []
    for r in lignes:
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


def _prechauffer_recherche():
    """Lance le navigateur de recherche en avance : la 1re recherche n'attend pas Edge."""
    try:
        spotify_scan.chercher_morceaux("a", 1, timeout=20)
    except Exception:
        pass


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

    exclure = set(data.get("exclude") or [])
    if exclure:
        restants = [m for m in matches if m.get("id") not in exclure]
        matches = restants or matches  # rien d'autre : le client détecte la vidéo en double
    meilleur, note = choix_video.choisir(matches, titre or query, artiste, duree, query)
    return jsonify({"result": meilleur or matches[0], "score": round(note)})


_cover_cache: dict[str, str] = {}
_cover_lock = threading.Lock()


def _track_cover(uri: str) -> str:
    """Pochette d'un morceau Spotify via oEmbed (~0,3 s), mise en cache."""
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
        url = ""  # pas de pochette : l'interface garde la vignette actuelle
    with _cover_lock:
        _cover_cache[uri] = url
    return url


@app.route("/api/track-covers", methods=["POST"])
def api_track_covers():
    """Pochettes de plusieurs morceaux d'un coup.

    Appelé par l'interface après l'affichage de la liste : en série, 100 morceaux
    prendraient ~30 s, d'où les requêtes menées en parallèle.
    """
    uris = [u for u in (request.get_json(silent=True) or {}).get("uris", []) if u][:300]
    if not uris:
        return jsonify({"covers": {}})
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=12) as pool:
        resultats = list(pool.map(_track_cover, uris))
    return jsonify({"covers": {u: c for u, c in zip(uris, resultats) if c}})


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


def run_full_scan(job_id: str, url: str):
    """Scan complet d'une playlist Spotify via navigateur piloté (voir spotify_scan)."""
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    _log(job_id, "Ouverture du navigateur…")

    def progres(lus, total):
        with JOBS_LOCK:
            JOBS[job_id]["scan"] = {"lus": lus, "total": total}

    try:
        morceaux, total, connecte = spotify_scan.scanner(url, on_progress=progres)
        with JOBS_LOCK:
            JOBS[job_id]["tracks"] = morceaux
            JOBS[job_id]["scan"] = {"lus": len(morceaux), "total": total}
            JOBS[job_id]["status"] = "done"
        if total and len(morceaux) < total:
            _log(job_id, f"Scan incomplet : {len(morceaux)} morceaux sur {total}.")
        else:
            _log(job_id, f"Playlist lue : {len(morceaux)} morceaux.")
    except spotify_scan.ScanIndisponible as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, str(exc))
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
        _log(job_id, f"Scan impossible : {str(exc).split('Stacktrace')[0][:200]}")


@app.route("/api/full-scan", methods=["POST"])
def start_full_scan():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if "open.spotify.com" not in url:
        return jsonify({"error": "Lien Spotify attendu."}), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "file": None, "control": None,
                        "tracks": None, "scan": {"lus": 0, "total": 0}}
    threading.Thread(target=run_full_scan, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/full-scan/login", methods=["POST"])
def full_scan_login():
    """Ouvre une fenêtre où l'utilisateur se connecte lui-même à Spotify.

    Utile si le scan revient incomplet : une session connectée donne accès à toute la
    playlist. MusicFlow ne saisit aucun identifiant — la saisie se fait dans la fenêtre.
    """
    try:
        ok = spotify_scan.ouvrir_connexion()
        return jsonify({"connecte": bool(ok)})
    except spotify_scan.ScanIndisponible as exc:
        return jsonify({"error": str(exc)}), 500


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
    quality = (data.get("quality") or "flac").strip()

    if not video_url or not save_name:
        return jsonify({"error": "Vidéo ou nom de fichier manquant."}), 400

    job_id = uuid.uuid4().hex
    opts = {
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


@app.route("/api/existants", methods=["POST"])
def api_existants():
    """Noms des fichiers audio déjà présents dans le dossier de destination : l'interface
    retire ces morceaux d'une playlist AVANT de la télécharger."""
    data = request.get_json(silent=True) or {}
    dossier = Path((data.get("folder") or "").strip() or str(DEFAULT_DEST))
    try:
        noms = [f.name for f in dossier.iterdir() if f.suffix.lower() in verif_audio.EXTENSIONS_AUDIO]
    except Exception:
        noms = []
    return jsonify({"noms": noms})


@app.route("/api/candidates", methods=["POST"])
def api_candidates():
    """Les meilleures vidéos YouTube pour un morceau, notées : pour comparer et choisir."""
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Requête manquante."}), 400
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
    """Lire (sans « champs ») ou modifier (avec « champs ») les tags d'un fichier."""
    data = request.get_json(silent=True) or {}
    chemin = Path((data.get("chemin") or "").strip())
    if not chemin.is_file() or chemin.suffix.lower() not in verif_audio.EXTENSIONS_AUDIO:
        return jsonify({"error": "Fichier introuvable."}), 404
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
        return jsonify({"error": "Fichier introuvable."}), 404
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
    return jsonify({"tests": fiabilite.diagnostic(search_videos, recherche_spotify, lambda: Path(FFMPEG_EXE).exists())})


@app.route("/api/version")
def api_version():
    import sys
    return jsonify({"ytdlp": yt_dlp.version.__version__, "python": sys.version.split()[0], "plateforme": "PC", "maj_possible": True})


@app.route("/api/maj-ytdlp", methods=["POST"])
def api_maj_ytdlp():
    """Met à jour yt-dlp (YouTube change souvent : une vieille version finit par échouer)."""
    import subprocess, sys
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], capture_output=True, text=True, timeout=300)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if r.returncode != 0:
        return jsonify({"error": (r.stderr or r.stdout)[-300:]}), 500
    deja = "already satisfied" in (r.stdout or "").lower()
    return jsonify({"ok": True, "message": "yt-dlp est déjà à jour." if deja
                    else "yt-dlp mis à jour — redémarre MusicFlow pour l'utiliser."})


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
    """Paroles enregistrées dans un fichier (ou dans le .lrc à côté)."""
    chemin = Path(((request.get_json(silent=True) or {}).get("chemin") or "").strip())
    if not chemin.is_file():
        return jsonify({"error": "Fichier introuvable."}), 404
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


@app.route("/api/ouvrir-dossier", methods=["POST"])
def api_ouvrir_dossier():
    import os
    dossier = ((request.get_json(silent=True) or {}).get("folder") or "").strip() or str(DEFAULT_DEST)
    try:
        os.startfile(dossier)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/connexion")
def api_connexion():
    """Internet est-il joignable ? (YouTube puis Cloudflare, délais courts)."""
    for url in ("https://www.youtube.com/generate_204", "https://1.1.1.1/"):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=4):
                return jsonify({"ok": True})
        except urllib.error.HTTPError:
            return jsonify({"ok": True})  # le serveur a répondu : la connexion marche
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
    threading.Thread(target=_prechauffer_recherche, daemon=True).start()
    print(f"MusicFlow lancé sur http://127.0.0.1:5090  (dossier par défaut : {DEFAULT_DEST})")
    app.run(host="127.0.0.1", port=5090, debug=False, threaded=True)
