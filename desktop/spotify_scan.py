from __future__ import annotations

import re
import threading
import time
import urllib.parse
from pathlib import Path

import os as _os, sys as _sys
_DATA = (Path(_os.environ.get("APPDATA", Path.home())) / "MusicFlow") if getattr(_sys, "frozen", False) else Path(__file__).resolve().parent.parent
_DATA.mkdir(parents=True, exist_ok=True)
PROFILE = _DATA / ".navigateur-spotify"

_JS_EXTRACT = """
const store = arguments[0] || {};
document.querySelectorAll('[data-testid="tracklist-row"]').forEach(row => {
  const idx = row.parentElement && row.parentElement.getAttribute('aria-rowindex');
  if (!idx) return;
  const lienTitre = row.querySelector('[data-testid="internal-track-link"]')
                 || row.querySelector('a[href*="/track/"]');
  const titre = lienTitre ? lienTitre.textContent.trim() : '';
  const artistes = [...row.querySelectorAll('a[href*="/artist/"]')]
                     .map(a => a.textContent.trim()).filter(Boolean).join(', ');
  if (titre) store[idx] = { title: titre, artist: artistes };
});
return store;
"""

_JS_TOTAL = r"""
const m = document.body.innerText.match(/([\d][\d\s ,.]*)\s*(songs?|titres?|chansons?)/i);
return m ? parseInt(m[1].replace(/[^\d]/g, ''), 10) : 0;
"""

_JS_CONTAINER = """
let el = document.querySelectorAll('[data-testid="tracklist-row"]')[0];
for (let i = 0; i < 20 && el; i++) {
  if (getComputedStyle(el).overflowY === 'scroll') return el;
  el = el.parentElement;
}
return document.scrollingElement;
"""


class ScanUnavailable(RuntimeError):
    pass


def _options(headless: bool, profile: Path = PROFILE, fast: bool = False):
    try:
        from selenium.webdriver.edge.options import Options
    except ImportError as exc:
        raise ScanUnavailable(
            "Selenium is not installed. Run: python -m pip install selenium"
        ) from exc
    o = Options()
    o.add_argument(f"--user-data-dir={profile}")
    if headless:
        o.add_argument("--headless=new")
    o.add_argument("--window-size=1280,2000")
    o.add_argument("--mute-audio")
    o.add_argument("--disable-gpu")
    o.add_argument("--log-level=3")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_experimental_option("excludeSwitches", ["enable-logging"])
    if fast:
        o.page_load_strategy = "eager"
        o.add_argument("--blink-settings=imagesEnabled=false")
    return o


def _open(headless: bool, profile: Path = PROFILE, fast: bool = False):
    from selenium import webdriver

    profile.mkdir(parents=True, exist_ok=True)
    try:
        return webdriver.Edge(options=_options(headless, profile, fast))
    except Exception as exc:
        raise ScanUnavailable(f"Cannot start Edge: {exc}") from exc


def is_logged_in(driver) -> bool:
    text_value = driver.execute_script("return (document.body && document.body.innerText) || ''")
    return not any(m in text_value.lower() for m in ("log in", "se connecter", "sign up"))


def open_connection(timeout: int = 300) -> bool:
    close_search()
    d = _open(headless=False)
    try:
        d.get("https://accounts.spotify.com/login")
        start_at = time.time()
        while time.time() - start_at < timeout:
            time.sleep(2)
            try:
                if "open.spotify.com" in d.current_url and is_logged_in(d):
                    time.sleep(2)
                    return True
            except Exception:
                return False
        return False
    finally:
        try:
            d.quit()
        except Exception:
            pass


def scanner(url: str, on_progress=None, timeout_total: int = 600):
    def progress_value(read_items, total):
        if on_progress:
            try:
                on_progress(read_items, total)
            except Exception:
                pass

    close_search()
    d = _open(headless=True)
    try:
        d.get(url)
        time.sleep(8)
        connected = is_logged_in(d)
        total = int(d.execute_script(_JS_TOTAL) or 0)
        progress_value(0, total)

        store = d.execute_script(_JS_EXTRACT, {}) or {}
        container = d.execute_script(_JS_CONTAINER)
        if container is None:
            return [], total, connected

        start_at = time.time()
        height = int(d.execute_script("return arguments[0].scrollHeight", container) or 0)
        visible = int(d.execute_script("return arguments[0].clientHeight", container) or 600)

        step = max(120, visible - 150)
        positions = list(range(0, max(height - visible, 0) + step, step)) or [0]

        for round_n in (1, 2):
            for pos in positions:
                if time.time() - start_at > timeout_total:
                    break
                d.execute_script("arguments[0].scrollTop = arguments[1]", container, pos)
                time.sleep(0.32 if round_n == 1 else 0.5)
                store = d.execute_script(_JS_EXTRACT, store) or store
                progress_value(len(store), total)
                if total and len(store) >= total:
                    break
            if (total and len(store) >= total) or time.time() - start_at > timeout_total:
                break

        if total and len(store) < total:
            lines = int(d.execute_script(
                "return document.querySelectorAll('[data-testid=\"tracklist-row\"]').length") or 12)
            passes_without_gain = 0
            for _ in range(30):
                before = len(store)
                missing = [i for i in range(1, total + 2) if str(i) not in store]
                if not missing or time.time() - start_at > timeout_total:
                    break
                target_paths = missing[:: max(1, lines // 2)]
                for idx in target_paths:
                    if time.time() - start_at > timeout_total:
                        break
                    pos = int((idx / max(total, 1)) * height) - visible // 2
                    d.execute_script("arguments[0].scrollTop = arguments[1]",
                                     container, max(0, pos))
                    time.sleep(0.75)
                    store = d.execute_script(_JS_EXTRACT, store) or store
                progress_value(len(store), total)
                if len(store) >= total:
                    break
                if len(store) == before:
                    passes_without_gain += 1
                    if passes_without_gain >= 5:
                        break
                else:
                    passes_without_gain = 0

        track_items = [store[k] for k in sorted(store, key=lambda x: int(x))]
        return track_items, total, connected
    finally:
        try:
            d.quit()
        except Exception:
            pass


_JS_FIRST_RESULT = """
const row = document.querySelector('[data-testid="tracklist-row"]');
if (!row) return null;
const lienTitre = row.querySelector('[data-testid="internal-track-link"]')
               || row.querySelector('a[href*="/track/"]');
const titre = lienTitre ? lienTitre.textContent.trim() : '';
if (!titre) return null;
const artistes = [...row.querySelectorAll('a[href*="/artist/"]')]
                   .map(a => a.textContent.trim()).filter(Boolean).join(', ');
const img = row.querySelector('img');
return { title: titre, artist: artistes, cover: img ? img.src : '' };
"""

_JS_RESULTS = """
const max = arguments[0] || 10;
const out = [];
for (const row of document.querySelectorAll('[data-testid="tracklist-row"]')) {
  const lienTitre = row.querySelector('[data-testid="internal-track-link"]')
                 || row.querySelector('a[href*="/track/"]');
  const titre = lienTitre ? lienTitre.textContent.trim() : '';
  if (!titre) continue;
  const artistes = [...row.querySelectorAll('a[href*="/artist/"]')]
                     .map(a => a.textContent.trim()).filter(Boolean).join(', ');
  const album = row.querySelector('a[href*="/album/"]');
  const img = row.querySelector('img');
  const durees = (row.innerText || '').match(/\\b\\d{1,2}:\\d{2}\\b/g) || [];
  out.push({ title: titre, artist: artistes, album: album ? album.textContent.trim() : '',
             cover: img ? img.src : '', duration: durees.length ? durees[durees.length - 1] : '',
             href: lienTitre.href || '' });
  if (out.length >= max) break;
}
return out;
"""

_search_driver = None
_search_driver_lock = threading.Lock()


def _search_driver_valid(d) -> bool:
    try:
        _ = d.current_url
        return True
    except Exception:
        return False


def search_track_item(title_text: str, artist_name: str = "", timeout: int = 12):
    global _search_driver
    q = f"{title_text} {artist_name}".strip()
    if not q:
        return None
    url = f"https://open.spotify.com/search/{urllib.parse.quote(q)}/tracks"

    with _search_driver_lock:
        try:
            if _search_driver is None or not _search_driver_valid(_search_driver):
                _search_driver = _open(headless=True, profile=PROFILE, fast=True)
            d = _search_driver
            d.get(url)
        except Exception:
            _search_driver = None
            return None

        start_at = time.time()
        while time.time() - start_at < timeout:
            try:
                result_item = d.execute_script(_JS_FIRST_RESULT)
            except Exception:
                result_item = None
            if result_item and result_item.get("title"):
                return result_item
            time.sleep(0.5)
        return None


def search_track_items(search_query: str, limit_n: int = 10, timeout: int = 15) -> list[dict]:
    global _search_driver
    q = (search_query or "").strip()
    if not q:
        return []
    url = f"https://open.spotify.com/search/{urllib.parse.quote(q)}/tracks"

    with _search_driver_lock:
        try:
            if _search_driver is None or not _search_driver_valid(_search_driver):
                _search_driver = _open(headless=True, profile=PROFILE, fast=True)
            d = _search_driver
            d.get(url)
        except Exception:
            _search_driver = None
            return []

        start_at = time.time()
        previous = -1
        while time.time() - start_at < timeout:
            try:
                result_items = d.execute_script(_JS_RESULTS, limit_n) or []
            except Exception:
                result_items = []
            if result_items and (len(result_items) >= limit_n or len(result_items) == previous):
                return result_items
            previous = len(result_items)
            time.sleep(0.6)
        return result_items if result_items else []


_RADIO_WORDS = r"radio|راديو|رادیو|ラジオ|라디오|радио|радіо|電台|电台"

_JS_FIRST_LINK = """
const sel = arguments[0];
const a = document.querySelector(sel);
return a ? a.href : null;
"""

_JS_CLICK_RADIO = """
const mots = new RegExp(arguments[0], 'i');
const item = [...document.querySelectorAll('[role="menu"] [role="menuitem"], [role="menu"] button')]
  .find(x => mots.test(x.innerText || ''));
if (!item) return false;
item.click();
return true;
"""

_JS_OPEN_MENU = """
const b = document.querySelector('[data-testid="action-bar-row"] [data-testid="more-button"]')
       || document.querySelector('main [data-testid="more-button"]');
if (!b) return false;
b.click();
return true;
"""

_JS_PLAYLISTS = """
return [...document.querySelectorAll('a[href*="/playlist/"]')].map(a => ({
  name: (a.innerText || a.getAttribute('title') || '').trim().split('\n')[0],
  href: a.href.split('?')[0]
}));
"""


def close_search():
    global _search_driver
    with _search_driver_lock:
        if _search_driver is not None:
            try:
                _search_driver.quit()
            except Exception:
                pass
            _search_driver = None


def _wait(d, js, *args, timeout: float = 12, step: float = 0.5):
    start_at = time.time()
    while time.time() - start_at < timeout:
        try:
            r = d.execute_script(js, *args)
        except Exception:
            r = None
        if r:
            return r
        time.sleep(step)
    return None


def _radio_by_menu(d, page_url: str) -> str | None:
    for _ in range(2):
        d.get(page_url)
        if not _wait(d, _JS_OPEN_MENU, timeout=15):
            continue
        if not _wait(d, _JS_CLICK_RADIO, _RADIO_WORDS, timeout=6):
            continue
        start_at = time.time()
        while time.time() - start_at < 15:
            if "/playlist/" in d.current_url:
                return d.current_url.split("?")[0]
            time.sleep(0.5)
    return None


def _radio_by_search(d, search_query: str) -> str | None:
    d.get(f"https://open.spotify.com/search/{urllib.parse.quote(search_query + ' radio')}/playlists")
    lists = _wait(d, _JS_PLAYLISTS, timeout=15) or []
    official = [p for p in lists if "/playlist/37i9dQZF1E" in p["href"]]
    key_name = search_query.casefold()
    for p in official:
        if re.search(_RADIO_WORDS, p["name"], re.I) and key_name in p["name"].casefold():
            return p["href"]
    for p in official:
        if re.search(_RADIO_WORDS, p["name"], re.I):
            return p["href"]
    return None


def find_radio(search_query: str, genre: str = "track") -> dict | None:
    global _search_driver
    search_query = (search_query or "").strip()
    if not search_query:
        return None
    genre = "artist" if genre == "artist" else "track"
    link = re.search(r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|artist)/", search_query)
    if link:
        genre = link.group(1)

    with _search_driver_lock:
        try:
            if _search_driver is None or not _search_driver_valid(_search_driver):
                _search_driver = _open(headless=True, profile=PROFILE, fast=True)
            d = _search_driver
        except ScanUnavailable:
            raise
        except Exception:
            _search_driver = None
            return None

        page = search_query if re.search(rf"open\.spotify\.com/(intl-[\w-]+/)?{genre}/", search_query) else None
        if not page:
            tab = "artists" if genre == "artist" else "tracks"
            d.get(f"https://open.spotify.com/search/{urllib.parse.quote(search_query)}/{tab}")
            sel = ('main a[href*="/artist/"]' if genre == "artist"
                   else '[data-testid="tracklist-row"] a[href*="/track/"]')
            page = _wait(d, _JS_FIRST_LINK, sel, timeout=15)
        if page:
            url = _radio_by_menu(d, page)
            if url:
                return {"url": url, "source": "menu"}
        if "open.spotify.com" in search_query:
            return None
        url = _radio_by_search(d, search_query)
        return {"url": url, "source": "search"} if url else None
