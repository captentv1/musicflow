from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path

_LOCK = threading.Lock()
_PATH: Path | None = None
_INDEX: dict | None = None
_EXTERNAL_NAMES: set[str] = set()

_EXT = (".mp3", ".flac", ".wav", ".m4a", ".opus", ".ogg", ".mp4", ".webm")
_NOISE = re.compile(
    r"\b(official|officiel|officielle|music|video|vid[eé]o|audio|lyrics?|lyric|paroles?|clip|"
    r"visuali[sz]er|hd|hq|4k|mv|full|version|original|explicit|clean|topic)\b", re.I)
_SUFFIX = re.compile(r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|live|from|score|soundtrack|mono|stereo|"
                      r"bonus|demo|radio|single|extended)\b.*$", re.I)


def configure_index(file_path) -> None:
    global _PATH, _INDEX
    with _LOCK:
        _PATH = Path(file_path)
        _INDEX = None


def _load() -> dict:
    global _INDEX
    if _INDEX is None:
        try:
            _INDEX = json.loads(_PATH.read_text(encoding="utf-8")) if _PATH and _PATH.exists() else {}
        except Exception:
            _INDEX = {}
        _OLD = {"pistes": "tracks_index", "dossiers": "folders", "cles": "keys", "dossier": "folder_path", "nom": "name"}
        def _migrate(o):
            if isinstance(o, dict):
                return {_OLD.get(k, k): _migrate(v) for k, v in o.items()}
            return o
        _INDEX = _migrate(_INDEX)
        _INDEX.setdefault("tracks_index", {})
        _INDEX.setdefault("folders", {})
    return _INDEX


def _save() -> None:
    if not _PATH:
        return
    try:
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_INDEX, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception:
        pass


def _norm(text_value: str) -> str:
    t = unicodedata.normalize("NFKD", (text_value or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", t))


def norm_artist(artist_name: str) -> str:
    a = (artist_name or "").strip()
    for sep in (",", " feat. ", " feat ", " ft. ", " ft ", " featuring ", " & ", " x ", " et ", " and "):
        i = a.lower().find(sep)
        if i > 0:
            a = a[:i]
    a = re.sub(r"\s*(-\s*topic|vevo|official)\s*$", "", a, flags=re.I)
    a = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", a)
    return _norm(_NOISE.sub(" ", a)) or _norm(a)


def norm_title(title_text: str, artist_name: str = "") -> str:
    t = title_text or ""
    if " - " in t and artist_name:
        left, right = t.split(" - ", 1)
        na = norm_artist(artist_name)
        if na and na in _norm(left):
            t = right
        elif na and na in _norm(right):
            t = left
    t = _SUFFIX.sub("", t)
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t)
    t = re.sub(r"\s(feat|ft|featuring)\.?\s.*$", " ", t, flags=re.I)
    t = _NOISE.sub(" ", t)
    return _norm(t) or _norm(title_text)


def key_list(title_text: str, artist_name: str, video: str = "", isrc: str = "") -> list[str]:
    out = []
    nt = norm_title(title_text, artist_name)
    if nt:
        out.append(f"t:{nt}|{norm_artist(artist_name)}")
    if video:
        out.append(f"v:{video}")
    if isrc:
        out.append(f"i:{isrc.upper()}")
    return out


def name_keys(name_: str) -> list[str]:
    track_items = (name_ or "").rsplit(".", 1)[0] if (name_ or "").lower().endswith(_EXT) else (name_ or "")
    track_items = track_items.split(" - ", 1)
    if len(track_items) != 2:
        return key_list(track_items[0], "")
    a, b = track_items
    return list(dict.fromkeys(key_list(a, b) + key_list(b, a)))


def _exists(entry: dict) -> bool:
    file_path = Path(entry.get("folder_path") or "") / (entry.get("name") or "")
    try:
        if file_path.is_file():
            return True
    except Exception:
        pass
    return (entry.get("name") or "") in _EXTERNAL_NAMES


def _add(idx: dict, folder_path: str, name_: str, track_keys: list[str]) -> None:
    track_id = f"{folder_path}|{name_}".lower()
    old = idx["tracks_index"].get(track_id, {})
    all_items = list(dict.fromkeys((old.get("keys") or []) + track_keys))
    idx["tracks_index"][track_id] = {"folder_path": folder_path, "name": name_, "keys": all_items}


def _read_title_artist(file_path: Path):
    try:
        import tag_quality
        tags = tag_quality.read_value(file_path)
        if tags.get("song_title"):
            return tags["song_title"], tags.get("artist_name") or ""
    except Exception:
        pass
    track_items = file_path.stem.rsplit(" - ", 1)
    return (track_items[0], track_items[1]) if len(track_items) == 2 else (file_path.stem, "")


def index_folder(folder_path) -> None:
    folder_path = str(folder_path)
    try:
        files = [f for f in Path(folder_path).iterdir() if f.suffix.lower() in _EXT]
    except Exception:
        return
    with _LOCK:
        idx = _load()
        seen = idx["folders"].setdefault(folder_path, {})
        new_value = False
        for f in files:
            try:
                mtime = int(f.stat().st_mtime)
            except Exception:
                continue
            if seen.get(f.name) == mtime:
                continue
            title_text, artist_name = _read_title_artist(f)
            _add(idx, folder_path, f.name, key_list(title_text, artist_name) + name_keys(f.name))
            seen[f.name] = mtime
            new_value = True
        if new_value:
            _save()


def set_external_names(names) -> None:
    global _EXTERNAL_NAMES
    names = {n for n in (names or []) if str(n).lower().endswith(_EXT)}
    with _LOCK:
        _EXTERNAL_NAMES = names
        idx = _load()
        known = {e.get("name") for e in idx["tracks_index"].values()}
        new_value = False
        for n in names - known:
            _add(idx, "", n, name_keys(n))
            new_value = True
        if new_value:
            _save()


def find_existing(wanted_keys: list[str], except_path: str = "") -> str | None:
    if not wanted_keys:
        return None
    wanted = set(wanted_keys)
    prefixes = [k for k in wanted if k.startswith("t:") and k.endswith("|")]
    wanted |= {k.split("|", 1)[0] + "|" for k in wanted if k.startswith("t:")}
    with _LOCK:
        idx = _load()
        stale = []
        found = None
        for track_id, e in idx["tracks_index"].items():
            entry_keys = e.get("keys") or []
            if not (wanted.intersection(entry_keys) or any(c.startswith(p) for p in prefixes for c in entry_keys)):
                continue
            file_path = str(Path(e.get("folder_path") or "") / e.get("name", "")) if e.get("folder_path") else e.get("name", "")
            if except_path and file_path.lower() == str(except_path).lower():
                continue
            if _exists(e):
                found = file_path
                break
            stale.append(track_id)
        for track_id in stale:
            idx["tracks_index"].pop(track_id, None)
        if stale:
            _save()
        return found


_IN_PROGRESS: set[str] = set()


def reserve_keys(track_keys: list[str]) -> bool:
    with _LOCK:
        if _IN_PROGRESS.intersection(track_keys):
            return False
        _IN_PROGRESS.update(track_keys)
        return True


def release(track_keys: list[str]) -> None:
    with _LOCK:
        _IN_PROGRESS.difference_update(track_keys)


def video_id_of(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", url or "")
    if m:
        return m.group(1)
    return (url or "").split("?")[0].rstrip("/").lower()


def record(file_path, track_keys: list[str], final_name: str = "") -> None:
    file_path = Path(file_path)
    with _LOCK:
        idx = _load()
        _add(idx, str(file_path.parent), file_path.name, track_keys)
        if final_name:
            _add(idx, "", final_name, track_keys)
            _EXTERNAL_NAMES.add(final_name)
        try:
            idx["folders"].setdefault(str(file_path.parent), {})[file_path.name] = int(file_path.stat().st_mtime)
        except Exception:
            pass
        _save()
