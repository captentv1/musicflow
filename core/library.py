from __future__ import annotations

import urllib.request
from pathlib import Path

import album_info
import lyrics
import tag_quality
import audio_check


def _title_artist(file_path: Path, tags: dict):
    title_text, artist_name = tags.get("titre") or "", tags.get("artiste") or ""
    if not title_text:
        name_ = file_path.stem
        if " - " in name_:
            title_text, artist_name = [x.strip() for x in name_.split(" - ", 1)]
        else:
            title_text = name_
    return title_text, artist_name


def list_files(folder_path, with_tags: bool = True, max_items: int = 5000):
    d = Path(folder_path)
    items = []
    try:
        files = sorted((f for f in d.iterdir() if f.suffix.lower() in audio_check.EXTENSIONS_AUDIO),
                          key=lambda f: f.stat().st_mtime, reverse=True)[:max_items]
    except Exception:
        return []
    for f in files:
        tags = tag_quality.read_value(f) if with_tags else {}
        title_text, artist_name = _title_artist(f, tags)
        st = f.stat()
        items.append({"chemin": str(f), "nom": f.name, "titre": title_text, "artiste": artist_name,
                      "album": tags.get("album", ""), "genre": tags.get("genre", ""), "annee": tags.get("annee", ""),
                      "duree": tags.get("duree", 0), "taille": st.st_size, "date": int(st.st_mtime),
                      "pochette": tags.get("pochette", False), "paroles": tags.get("paroles", False),
                      "format": f.suffix.lower().lstrip(".")})
    return items


def retag(file_path, with_lyrics: bool = True, force_cover: bool = False) -> list[str]:
    f = Path(file_path)
    tags = tag_quality.read_value(f)
    title_text, artist_name = _title_artist(f, tags)
    done_items = []
    if not tags.get("titre") or not tags.get("artiste"):
        if not tag_quality.write_value(f, {"titre": title_text, "artiste": artist_name}):
            done_items.append("title/artist")
    info_data = album_info.find_items(title_text, (artist_name or "").split(",")[0].strip(), tags.get("duree") or 0)
    if info_data:
        if not album_info.embed(f, info_data):
            done_items.append("album/genre/track/year")
        if info_data.get("pochette_hd") and (force_cover or not tags.get("pochette")):
            try:
                req = urllib.request.Request(info_data["pochette_hd"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    if not tag_quality.cover_art(f, r.read()):
                        done_items.append("HD cover art")
            except Exception:
                pass
    if with_lyrics and not tags.get("paroles"):
        p = lyrics.find_items(title_text, (artist_name or "").split(",")[0].strip(), tags.get("duree") or 0)
        if p and not lyrics.embed(f, p):
            done_items.append("lyrics")
    return done_items


def copy_info(source, target_path) -> None:
    import mutagen
    src = Path(source)
    tags = tag_quality.read_value(src)
    tag_quality.write_value(target_path, {k: tags.get(k, "") for k in ("titre", "artiste", "album", "genre", "annee", "piste", "artiste_album")})
    data = None
    try:
        f = mutagen.File(str(src))
        t = f.tags if f else None
        if getattr(f, "pictures", None):
            data = f.pictures[0].data
        elif t is not None:
            for k in list(t.keys()):
                if str(k).startswith("APIC"):
                    data = t[k].data; break
            if data is None and "covr" in t:
                data = bytes(t["covr"][0])
            if data is None and "METADATA_BLOCK_PICTURE" in t:
                import base64
                from mutagen.flac import Picture
                data = Picture(base64.b64decode(t["METADATA_BLOCK_PICTURE"][0])).data
    except Exception:
        data = None
    if data:
        tag_quality.cover_art(target_path, data)
    try:
        f = mutagen.File(str(src)); t = f.tags if f else None
        text_value = ""
        if t is not None:
            for k in list(t.keys()):
                if str(k).startswith("USLT"):
                    text_value = str(t[k].text); break
            for k in ("LYRICS", "\xa9lyr"):
                if not text_value and k in t:
                    text_value = str(t[k][0])
        if text_value:
            lyrics.embed(target_path, {"plain": text_value, "synced": ""}, False)
    except Exception:
        pass


def conversion_args(source, target_path, fmt: str) -> list[str]:
    codec = {"opus": ["-c:a", "libopus", "-b:a", "160k"], "aac": ["-c:a", "aac", "-b:a", "256k"],
             "192": ["-c:a", "libmp3lame", "-b:a", "192k"], "320": ["-c:a", "libmp3lame", "-b:a", "320k"],
             "128": ["-c:a", "libmp3lame", "-b:a", "128k"]}.get(fmt, ["-c:a", "libopus", "-b:a", "160k"])
    return ["-y", "-loglevel", "error", "-i", str(source), "-vn", "-map", "0:a:0"] + codec + [str(target_path)]


def extension_for(fmt: str) -> str:
    return {"opus": ".opus", "aac": ".m4a"}.get(fmt, ".mp3")
