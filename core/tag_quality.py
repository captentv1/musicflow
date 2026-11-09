from __future__ import annotations

import base64
import re
from pathlib import Path

_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+)[\)\]]", re.IGNORECASE)
_SMALL_WORDS = {"a", "an", "and", "the", "of", "in", "on", "at", "to", "for", "de", "du", "des", "la", "le",
           "les", "et", "en", "un", "une", "y", "el", "los", "las", "di", "da"}


def clean(title_text: str, artist_name: str, feat: bool = False, fix_case: bool = False):
    title_text, artist_name = title_text or "", artist_name or ""
    if feat:
        m = _FEAT.search(title_text)
        if m:
            guests = [x.strip() for x in re.split(r",|&| x | and ", m.group(1)) if x.strip()]
            title_text = _FEAT.sub("", title_text).strip()
            already = {a.strip().lower() for a in artist_name.split(",")}
            added_item = [i for i in guests if i.lower() not in already]
            if added_item:
                artist_name = ", ".join([artist_name] + added_item) if artist_name else ", ".join(added_item)
    if fix_case:
        def recase(t):
            letters = [c for c in t if c.isalpha()]
            if len(letters) < 4 or not (t.isupper() or t.islower()):
                return t
            words = t.lower().split(" ")
            return " ".join(m if (i and m in _SMALL_WORDS) else m[:1].upper() + m[1:] for i, m in enumerate(words))
        title_text, artist_name = recase(title_text), recase(artist_name)
    return title_text, artist_name


def write_base(file_path, title_text: str, artist_name: str) -> str:
    ext = Path(file_path).suffix.lower()
    try:
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(file_path))
            if title_text:
                f["\xa9nam"] = [title_text]
            if artist_name:
                f["\xa9ART"] = [artist_name]
                f["aART"] = [artist_name]
            f.save()
        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            f = OggOpus(str(file_path))
            if title_text:
                f["TITLE"] = [title_text]
            if artist_name:
                f["ARTIST"] = [artist_name]
                f["ALBUMARTIST"] = [artist_name]
            f.save()
        else:
            return f"unsupported format: {ext}"
    except Exception as exc:
        return f"writing metadata: {exc}"
    return ""


def cover_art(file_path, data: bytes, mime: str = "image/jpeg") -> str:
    ext = Path(file_path).suffix.lower()
    try:
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(str(file_path))
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            f["covr"] = [MP4Cover(data, imageformat=fmt)]
            f.save()
        elif ext in (".opus", ".ogg"):
            from mutagen.oggopus import OggOpus
            from mutagen.flac import Picture
            f = OggOpus(str(file_path))
            pic = Picture()
            pic.data, pic.type, pic.mime = data, 3, mime
            f["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
            f.save()
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(str(file_path))
            pic = Picture()
            pic.data, pic.type, pic.mime = data, 3, mime
            f.clear_pictures()
            f.add_picture(pic)
            f.save()
        elif ext in (".mp3", ".wav"):
            from mutagen.id3 import ID3, APIC, ID3NoHeaderError
            if ext == ".wav":
                from mutagen.wave import WAVE
                f = WAVE(str(file_path))
                if f.tags is None:
                    f.add_tags()
                f.tags.delall("APIC")
                f.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                f.save()
            else:
                try:
                    t = ID3(str(file_path))
                except ID3NoHeaderError:
                    t = ID3()
                t.delall("APIC")
                t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                t.save(str(file_path))
        else:
            return f"unsupported format: {ext}"
    except Exception as exc:
        return f"cover art: {exc}"
    return ""


def saturation(ffmpeg_output: str) -> str:
    m_max = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", ffmpeg_output or "")
    m_0db = re.search(r"histogram_0db:\s*(\d+)", ffmpeg_output or "")
    m_avg = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", ffmpeg_output or "")
    if m_avg and float(m_avg.group(1)) < -40:
        return "son presque inaudible"
    m_n = re.search(r"n_samples:\s*(\d+)", ffmpeg_output or "")
    if m_max and float(m_max.group(1)) >= 0 and m_0db and m_n and int(m_n.group(1)):
        if int(m_0db.group(1)) / int(m_n.group(1)) > 0.01:
            return "clipped audio"
    return ""


_ID3_FIELDS = {"song_title": "TIT2", "artist_name": "TPE1", "album": "TALB", "genre": "TCON", "year": "TDRC",
               "track_info": "TRCK", "album_artist": "TPE2"}
_VORBIS_FIELDS = {"song_title": "TITLE", "artist_name": "ARTIST", "album": "ALBUM", "genre": "GENRE", "year": "DATE",
                  "track_info": "TRACKNUMBER", "album_artist": "ALBUMARTIST"}
_MP4_FIELDS = {"song_title": "©nam", "artist_name": "©ART", "album": "©alb", "genre": "©gen", "year": "©day",
               "album_artist": "aART"}


def read_value(file_path) -> dict:
    import mutagen
    ext = Path(file_path).suffix.lower()
    out = {"song_title": "", "artist_name": "", "album": "", "genre": "", "year": "", "track_info": "", "album_artist": "",
           "length_s": 0, "cover_art": False, "lyrics_data": False}
    try:
        f = mutagen.File(str(file_path))
    except Exception:
        return out
    if f is None:
        return out
    out["length_s"] = int(getattr(f.info, "length", 0) or 0)
    t = f.tags
    if t is None:
        return out
    if ext in (".mp3", ".wav"):
        for key_name, frame in _ID3_FIELDS.items():
            if frame in t:
                out[key_name] = str(t[frame].text[0]) if t[frame].text else ""
        out["cover_art"] = any(k.startswith("APIC") for k in t.keys())
        out["lyrics_data"] = any(k.startswith("USLT") for k in t.keys())
    elif ext in (".flac", ".opus", ".ogg"):
        for key_name, name_ in _VORBIS_FIELDS.items():
            if name_ in t:
                out[key_name] = t[name_][0]
        out["cover_art"] = bool(getattr(f, "pictures", None)) or "METADATA_BLOCK_PICTURE" in t
        out["lyrics_data"] = "LYRICS" in t
    elif ext in (".m4a", ".mp4"):
        for key_name, name_ in _MP4_FIELDS.items():
            if name_ in t:
                out[key_name] = str(t[name_][0])
        if "trkn" in t:
            out["track_info"] = str(t["trkn"][0][0])
        out["cover_art"] = "covr" in t
        out["lyrics_data"] = "©lyr" in t
    return out


def write_value(file_path, fields_in: dict) -> str:
    ext = Path(file_path).suffix.lower()
    fields_in = {k: str(v).strip() for k, v in fields_in.items() if k in _ID3_FIELDS and v is not None}
    try:
        if ext in (".mp3", ".wav"):
            from mutagen import id3
            if ext == ".wav":
                from mutagen.wave import WAVE
                f = WAVE(str(file_path))
                if f.tags is None:
                    f.add_tags()
                t = f.tags
            else:
                try:
                    t = id3.ID3(str(file_path))
                except id3.ID3NoHeaderError:
                    t = id3.ID3()
            for key_name, val in fields_in.items():
                frame = _ID3_FIELDS[key_name]
                t.delall(frame)
                if val:
                    t.add(getattr(id3, frame)(encoding=3, text=val))
            if ext == ".wav":
                f.save()
            else:
                t.save(str(file_path))
        elif ext in (".flac", ".opus", ".ogg"):
            import mutagen
            f = mutagen.File(str(file_path))
            for key_name, val in fields_in.items():
                name_ = _VORBIS_FIELDS[key_name]
                if val:
                    f[name_] = [val]
                elif name_ in f:
                    del f[name_]
            f.save()
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            f = MP4(str(file_path))
            for key_name, val in fields_in.items():
                if key_name == "track_info":
                    if val.split("/")[0].isdigit():
                        f["trkn"] = [(int(val.split("/")[0]), 0)]
                    continue
                name_ = _MP4_FIELDS[key_name]
                if val:
                    f[name_] = [val]
                elif name_ in f:
                    del f[name_]
            f.save()
        else:
            return f"unsupported format: {ext}"
    except Exception as exc:
        return f"writing: {exc}"
    return ""
