from __future__ import annotations

import re
import threading
import unicodedata
from pathlib import Path

_START = "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:start_silence=0.2"
SILENCE_FILTER = f"{_START},areverse,{_START},areverse"


def silence_filter(duration_s) -> str:
    try:
        duration_s = float(duration_s or 0)
    except (TypeError, ValueError):
        duration_s = 0
    if duration_s <= 60:
        return SILENCE_FILTER
    cut = int(duration_s - 30)
    return (f"asplit=2[a][b];[a]atrim=end={cut},asetpts=PTS-STARTPTS,{_START}[a1];"
            f"[b]atrim=start={cut},asetpts=PTS-STARTPTS,areverse,{_START},areverse[b1];"
            f"[a1][b1]concat=n=2:v=0:a=1")

MAX_GAP_S = 15

_IN_PROGRESS: set[str] = set()
_IN_PROGRESS_LOCK = threading.Lock()

EXTENSIONS_AUDIO = (".mp3", ".flac", ".wav", ".m4a", ".opus", ".ogg", ".mp4", ".webm")


def to_seconds(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text_value = str(value or "").strip()
    if not text_value:
        return 0
    if text_value.isdigit():
        return int(text_value)
    total = 0
    for track_item in text_value.split(":"):
        if not track_item.isdigit():
            return 0
        total = total * 60 + int(track_item)
    return total


def file_duration(file_path) -> float:
    try:
        import mutagen
        f = mutagen.File(str(file_path))
        return float(f.info.length) if f and f.info else 0.0
    except Exception:
        return 0.0


def analysis_excerpt(file_path) -> list[str]:
    duration = file_duration(file_path)
    if duration <= 90:
        return []
    return ["-ss", str(int(duration / 2 - 30)), "-t", "60"]


def check_duration(file_path, expected) -> str:
    expected_s = to_seconds(expected)
    if not expected_s:
        return ""
    actual = file_duration(file_path)
    if not actual:
        return ""
    gap = actual - expected_s
    if abs(gap) <= MAX_GAP_S:
        return ""
    fmt = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
    return f"{fmt(actual)} instead of {fmt(expected_s)}"


def _normalize(text_value: str) -> str:
    t = unicodedata.normalize("NFD", text_value or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"\((feat|ft|with)[^)]*\)|\[[^\]]*\]|\s-\s.*(remaster|version|edit|mix|live).*$", "", t)
    return re.sub(r"[\W_]+", " ", t).strip()


def duplicate_in_folder(folder_path: Path, file_stem: str):
    key_name = _normalize(file_stem)
    if not key_name:
        return None
    try:
        for f in folder_path.iterdir():
            if f.suffix.lower() in EXTENSIONS_AUDIO and _normalize(f.stem) == key_name:
                return f
    except Exception:
        return None
    return None


def reserve_keys(file_path) -> bool:
    key_name = _normalize(Path(file_path).stem) + "|" + str(Path(file_path).parent).lower()
    with _IN_PROGRESS_LOCK:
        if key_name in _IN_PROGRESS:
            return False
        _IN_PROGRESS.add(key_name)
        return True


def release(file_path) -> None:
    key_name = _normalize(Path(file_path).stem) + "|" + str(Path(file_path).parent).lower()
    with _IN_PROGRESS_LOCK:
        _IN_PROGRESS.discard(key_name)
