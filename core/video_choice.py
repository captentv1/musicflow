from __future__ import annotations

import re
import unicodedata

_UNWANTED = {
    "reaction": 60, "réaction": 60, "reacts": 60,
    "casting": 60, "audition": 60, "the voice": 55, "nouvelle star": 55,
    "interview": 55, "making of": 45, "behind the scenes": 45, "coulisses": 45,
    "tutorial": 50, "tuto": 50, "how to play": 50, "lesson": 45, "cours de": 45,
    "karaoke": 50, "karaoké": 50, "instrumental": 40, "backing track": 45,
    "cover": 40, "reprise": 35, "parodie": 50, "parody": 50,
    "live": 25, "concert": 30, "en direct": 25, "session": 20, "acoustic": 20,
    "nightcore": 45, "sped up": 40, "slowed": 40, "reverb": 30, "8d audio": 45,
    "mashup": 35, "medley": 35, "compilation": 40, "mix": 15, "megamix": 40,
    "full album": 45, "album complet": 45, "playlist": 35,
    "trailer": 40, "bande annonce": 40, "extrait": 25,
}

_BONUS = {
    "official audio": 30, "audio officiel": 30, "official music video": 18,
    "clip officiel": 18, "official video": 15, "audio": 10, "lyrics": 6,
    "paroles": 6, "hq": 4,
}


def _normalize(text_value: str) -> str:
    text_value = unicodedata.normalize("NFKD", (text_value or "").lower())
    text_value = "".join(c for c in text_value if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", text_value)


def _words(text_value: str) -> set[str]:
    return {m for m in _normalize(text_value).split() if len(m) > 2}


_SUFFIX = re.compile(r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|live|from|score|soundtrack|"
                      r"mono|stereo|bonus|demo|acoustic|radio|single|extended|instrumental|"
                      r"theme|ost|bande originale)\b.*$", re.I)
_BRACKETS = re.compile(r"[\(\[][^\)\]]*\b(feat|ft|with|from|remaster(ed)?|version|edit|mix|"
                          r"live|score|soundtrack|bonus|demo|radio|ost)\b[^\)\]]*[\)\]]", re.I)


def _core_title(title_text: str) -> str:
    short = _BRACKETS.sub(" ", _SUFFIX.sub("", title_text or "")).strip()
    return short or (title_text or "")


def _duration_seconds(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    txt = str(value or "").strip()
    if not txt:
        return 0
    if txt.isdigit():
        return int(txt)
    parts = txt.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def _penalties(video_title: str, search_query: str) -> int:
    v = _normalize(video_title)
    q = _normalize(search_query)
    total = 0
    for term, weight in _UNWANTED.items():
        t = _normalize(term)
        if t in v and t not in q:
            total += weight
    return total


def note_down(candidate: dict, title_text: str, artist_name: str, duration_s: int, search_query: str = "") -> float:
    video_title = candidate.get("title") or ""
    channel = candidate.get("uploader") or ""
    search_query = search_query or f"{title_text} {artist_name}"
    note = 0.0

    video_duration = _duration_seconds(candidate.get("duration"))
    if duration_s > 0 and video_duration > 0:
        gap = abs(video_duration - duration_s)
        if gap <= 3:
            note += 100
        elif gap <= 8:
            note += 80
        elif gap <= 20:
            note += 45
        elif gap <= 45:
            note += 10
        else:
            note -= min(120, gap)
    elif video_duration > 900:
        note -= 60

    title_words = _words(_core_title(title_text))
    if title_words:
        present = len(title_words & _words(video_title))
        note += 45 * (present / len(title_words))

    artist_words = _words(artist_name)
    if artist_words:
        target_path = _words(video_title) | _words(channel)
        note += 35 * (len(artist_words & target_path) / len(artist_words))

    if re.search(r"-\s*topic$", channel.strip(), re.I):
        note += 40

    for term, weight in _BONUS.items():
        if _normalize(term) in _normalize(video_title):
            note += weight

    note -= _penalties(video_title, search_query)
    return note


def choose(candidates: list[dict], title_text: str, artist_name: str, duration, search_query: str = ""):
    if not candidates:
        return None, 0.0
    duration_s = _duration_seconds(duration)
    notes = [(note_down(c, title_text, artist_name, duration_s, search_query), c) for c in candidates]
    notes.sort(key=lambda x: x[0], reverse=True)
    return notes[0][1], notes[0][0]


RELIABLE_THRESHOLD = 20


def reliable(candidate, title_text: str, note: float) -> bool:
    if not candidate:
        return False
    words = _words(_core_title(title_text))
    if words and len(words & _words(candidate.get("title") or "")) / len(words) < 0.5:
        return note >= UNTITLED_THRESHOLD
    return note >= RELIABLE_THRESHOLD


UNTITLED_THRESHOLD = 135
