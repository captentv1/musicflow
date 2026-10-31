"""Full scan of a Spotify playlist on PC, through an automated browser (Selenium + Edge).

Why this module exists
----------------------
Without signing in, Spotify never gives the whole playlist, whatever the method:
the "embed" page caps at 100 tracks (and ignores offset/page/limit), and the web
player only shows about forty rows to an anonymous visitor — neither real key
presses nor the real mouse wheel unlock the rest. Measured on a playlist
of 1046 tracks.

The only way left is a signed-in session. So this module uses an Edge profile
dedicated to MusicFlow: the user signs in **themselves** only once
(visible window), and later scans reuse the session, with no window.
MusicFlow never sees nor types the password.

Edge is used because it ships with every Windows install: no extra
browser to download.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse
from pathlib import Path

import os as _os, sys as _sys
# .exe build (PyInstaller): data goes to %APPDATA%\MusicFlow (the exe folder is temporary)
_DATA = (Path(_os.environ.get("APPDATA", Path.home())) / "MusicFlow") if getattr(_sys, "frozen", False) else Path(__file__).resolve().parent
_DATA.mkdir(parents=True, exist_ok=True)
PROFIL = _DATA / ".navigateur-spotify"

# Extracts a track row by the STRUCTURE of its links, not by the position
# of its texts. Reading "the 2nd string of the row" is fragile: depending on the window
# width and the "Explicit" badge, we would get an artist or the "E" badge
# instead of the title. The title has data-testid="internal-track-link" and the
# artists are /artist/ links — independent of language and layout.
_JS_EXTRAIRE = """
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

_JS_CONTENEUR = """
let el = document.querySelectorAll('[data-testid="tracklist-row"]')[0];
for (let i = 0; i < 20 && el; i++) {
  if (getComputedStyle(el).overflowY === 'scroll') return el;
  el = el.parentElement;
}
return document.scrollingElement;
"""


class ScanIndisponible(RuntimeError):
    """Selenium or Edge missing: the scan cannot start."""


def _options(headless: bool, profil: Path = PROFIL, rapide: bool = False):
    try:
        from selenium.webdriver.edge.options import Options
    except ImportError as exc:  # pragma: no cover - depends on the installation
        raise ScanIndisponible(
            "Selenium is not installed. Run: python -m pip install selenium"
        ) from exc
    o = Options()
    o.add_argument(f"--user-data-dir={profil}")
    if headless:
        o.add_argument("--headless=new")
    # Tall window: the web player renders more rows at once.
    o.add_argument("--window-size=1280,2000")
    o.add_argument("--mute-audio")
    o.add_argument("--disable-gpu")
    o.add_argument("--log-level=3")
    # Avoids the headless crash "DevToolsActivePort file doesn't exist" seen under
    # load (many Edge tabs already open): the sandbox and the shared /dev/shm
    # struggle to initialize in time, Chromium gives up before even starting.
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_experimental_option("excludeSwitches", ["enable-logging"])
    if rapide:
        # Search: the DOM is read as soon as it appears (without waiting for the very heavy
        # page to finish loading) and images are not downloaded — only their URLs are used.
        # "eager": wait for the page to be ready (DOM), not all its resources.
        # ("none" no longer works: Spotify stayed on its home page.)
        o.page_load_strategy = "eager"
        o.add_argument("--blink-settings=imagesEnabled=false")
    return o


def _ouvrir(headless: bool, profil: Path = PROFIL, rapide: bool = False):
    from selenium import webdriver

    profil.mkdir(parents=True, exist_ok=True)
    try:
        return webdriver.Edge(options=_options(headless, profil, rapide))
    except Exception as exc:
        raise ScanIndisponible(f"Cannot start Edge: {exc}") from exc


def est_connecte(driver) -> bool:
    """True if the profile's Spotify session is signed in."""
    texte = driver.execute_script("return (document.body && document.body.innerText) || ''")
    return not any(m in texte.lower() for m in ("log in", "se connecter", "sign up"))


def ouvrir_connexion(timeout: int = 300) -> bool:
    """Opens a visible Edge window so the user can sign in themselves.

    Returns as soon as the sign-in is detected, or after `timeout`.
    MusicFlow types no credentials: the user types everything in this window.
    """
    fermer_recherche()
    d = _ouvrir(headless=False)
    try:
        d.get("https://accounts.spotify.com/login")
        debut = time.time()
        while time.time() - debut < timeout:
            time.sleep(2)
            try:
                if "open.spotify.com" in d.current_url and est_connecte(d):
                    time.sleep(2)  # let the cookies be written
                    return True
            except Exception:
                return False  # window closed by the user
        return False
    finally:
        try:
            d.quit()
        except Exception:
            pass


def scanner(url: str, on_progress=None, timeout_total: int = 600):
    """Reads all tracks of a Spotify playlist/album.

    Returns (morceaux, total_annonce, connecte). `morceaux` is a list of
    {"title", "artist"} in playlist order. If `total_annonce` exceeds the
    number read, the read is incomplete — the caller must say so clearly rather
    than pass the list off as complete.
    """
    def progres(lus, total):
        if on_progress:
            try:
                on_progress(lus, total)
            except Exception:
                pass

    fermer_recherche()
    d = _ouvrir(headless=True)
    try:
        d.get(url)
        time.sleep(8)
        connecte = est_connecte(d)
        total = int(d.execute_script(_JS_TOTAL) or 0)
        progres(0, total)

        store = d.execute_script(_JS_EXTRAIRE, {}) or {}
        conteneur = d.execute_script(_JS_CONTENEUR)
        if conteneur is None:
            return [], total, connecte

        debut = time.time()
        hauteur = int(d.execute_script("return arguments[0].scrollHeight", conteneur) or 0)
        visible = int(d.execute_script("return arguments[0].clientHeight", conteneur) or 600)

        # Sweep by ABSOLUTE positions rather than by scrolling continuously.
        # The virtualized list reserves its full height from the start, so the position
        # of a row can be computed: row N ≈ (N / total) * height. Continuous scrolling
        # stopped as soon as Spotify was slow to serve a batch — hence
        # incomplete and very variable scans (460, 545, … out of 1046).
        pas = max(120, visible - 150)  # overlap between two positions
        positions = list(range(0, max(hauteur - visible, 0) + pas, pas)) or [0]

        for tour in (1, 2):  # 2nd pass to fill in what had not loaded yet
            for pos in positions:
                if time.time() - debut > timeout_total:
                    break
                d.execute_script("arguments[0].scrollTop = arguments[1]", conteneur, pos)
                # Let the virtualized list render the rows: too fast and we read
                # still-empty positions (scans at 87 % instead of 99 %).
                time.sleep(0.32 if tour == 1 else 0.5)
                store = d.execute_script(_JS_EXTRAIRE, store) or store
                progres(len(store), total)
                if total and len(store) >= total:
                    break
            if (total and len(store) >= total) or time.time() - debut > timeout_total:
                break

        # Targeted catch-up: a few rows are often missing after the sweep (a batch
        # not rendered yet when we passed). Rather than redo everything, jump
        # straight to the computed position of each missing index.
        if total and len(store) < total:
            lignes = int(d.execute_script(
                "return document.querySelectorAll('[data-testid=\"tracklist-row\"]').length") or 12)
            # Repeat while it makes progress: a fixed number of passes stopped
            # while there was still time and rows left to fetch.
            passes_sans_gain = 0
            for _ in range(30):
                avant = len(store)
                manquants = [i for i in range(1, total + 2) if str(i) not in store]
                if not manquants or time.time() - debut > timeout_total:
                    break
                # One visit covers ~one screen of rows: only one index out of N is targeted.
                cibles = manquants[:: max(1, lignes // 2)]   # wider overlap
                for idx in cibles:
                    if time.time() - debut > timeout_total:
                        break
                    pos = int((idx / max(total, 1)) * hauteur) - visible // 2
                    d.execute_script("arguments[0].scrollTop = arguments[1]",
                                     conteneur, max(0, pos))
                    time.sleep(0.75)
                    store = d.execute_script(_JS_EXTRAIRE, store) or store
                progres(len(store), total)
                if len(store) >= total:
                    break
                if len(store) == avant:
                    # A batch can take several seconds to be served: giving up after
                    # two passes left gaps (968 tracks out of 1046 in
                    # one test). Keep trying longer before giving up.
                    passes_sans_gain += 1
                    if passes_sans_gain >= 5:
                        break
                else:
                    passes_sans_gain = 0

        morceaux = [store[k] for k in sorted(store, key=lambda x: int(x))]
        return morceaux, total, connecte
    finally:
        try:
            d.quit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Track search (exact title/artist + cover art), through the automated browser.
#
# The official Spotify API (Client Credentials) now refuses /search for
# apps without extended quota ("Active premium subscription required for the owner
# of the app"). The public search page, however, works without signing in —
# same [data-testid="tracklist-row"] rows as the playlist scan above.
# A single headless browser is kept open between searches: reopening
# one each time would cost several seconds of startup per track.
# ---------------------------------------------------------------------------

_JS_PREMIER_RESULTAT = """
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

# All results of the search page (same link-structure reading
# as _JS_PREMIER_RESULTAT), with the displayed duration and the track link.
_JS_RESULTATS = """
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

_driver_recherche = None
_driver_recherche_lock = threading.Lock()


def _driver_recherche_valide(d) -> bool:
    try:
        _ = d.current_url
        return True
    except Exception:
        return False


def chercher_morceau(titre: str, artiste: str = "", timeout: int = 12):
    """Searches a track on the Spotify search page (no sign-in required).

    Returns {"title", "artist", "cover"} of the first result, or None if nothing
    is found or the browser cannot be started (no Edge/Selenium).

    Reuses the same profile as the playlist scan (session already signed in if
    any). Edge refuses two instances on the same --user-data-dir at once:
    if a full scan runs at the same time, this search simply fails and
    the function returns None — the download goes on with the raw YouTube title.
    """
    global _driver_recherche
    q = f"{titre} {artiste}".strip()
    if not q:
        return None
    url = f"https://open.spotify.com/search/{urllib.parse.quote(q)}/tracks"

    with _driver_recherche_lock:
        try:
            if _driver_recherche is None or not _driver_recherche_valide(_driver_recherche):
                _driver_recherche = _ouvrir(headless=True, profil=PROFIL, rapide=True)
            d = _driver_recherche
            d.get(url)
        except Exception:
            _driver_recherche = None
            return None

        debut = time.time()
        while time.time() - debut < timeout:
            try:
                resultat = d.execute_script(_JS_PREMIER_RESULTAT)
            except Exception:
                resultat = None
            if resultat and resultat.get("title"):
                return resultat
            time.sleep(0.5)
        return None


def chercher_morceaux(requete: str, limite: int = 10, timeout: int = 15) -> list[dict]:
    """Full Spotify search for the Search tab (no sign-in, no API).

    Same automated browser as chercher_morceau, but returns the list of results:
    {"title", "artist", "album", "cover", "duration", "href"}. Empty list if nothing
    is found or Edge/Selenium is unavailable — the caller falls back to YouTube.
    """
    global _driver_recherche
    q = (requete or "").strip()
    if not q:
        return []
    url = f"https://open.spotify.com/search/{urllib.parse.quote(q)}/tracks"

    with _driver_recherche_lock:
        try:
            if _driver_recherche is None or not _driver_recherche_valide(_driver_recherche):
                _driver_recherche = _ouvrir(headless=True, profil=PROFIL, rapide=True)
            d = _driver_recherche
            d.get(url)
        except Exception:
            _driver_recherche = None
            return []

        debut = time.time()
        precedent = -1
        while time.time() - debut < timeout:
            try:
                resultats = d.execute_script(_JS_RESULTATS, limite) or []
            except Exception:
                resultats = []
            # Wait for the list to settle (Spotify adds rows as it renders)
            if resultats and (len(resultats) >= limite or len(resultats) == precedent):
                return resultats
            precedent = len(resultats)
            time.sleep(0.6)
        return resultats if resultats else []


# ---------------------------------------------------------------------------
# Spotify radios: "Radio <song>" / "Radio <artist>" playlists generated by Spotify.
#
# Two ways, from the most precise to the broadest:
#  1. open the track/artist page and click "Go to song/artist radio" in its "…" menu
#     (works without signing in; the label depends on the account language, so it
#     is matched on the word "radio" in several scripts);
#  2. search the playlists "<query> radio" and keep the ones made by Spotify
#     (their id starts with 37i9dQZF1E).
# The radio playlist is then read like any other playlist (resolve-link / full scan).
# ---------------------------------------------------------------------------

_RADIO_MOTS = r"radio|راديو|رادیو|ラジオ|라디오|радио|радіо|電台|电台"

_JS_PREMIER_LIEN = """
const sel = arguments[0];
const a = document.querySelector(sel);
return a ? a.href : null;
"""

_JS_CLIC_RADIO = """
const mots = new RegExp(arguments[0], 'i');
const item = [...document.querySelectorAll('[role="menu"] [role="menuitem"], [role="menu"] button')]
  .find(x => mots.test(x.innerText || ''));
if (!item) return false;
item.click();
return true;
"""

_JS_OUVRIR_MENU = """
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


def fermer_recherche():
    """Closes the search browser: Edge refuses a 2nd instance on the same profile,
    so the full scan and the sign-in window must free it first."""
    global _driver_recherche
    with _driver_recherche_lock:
        if _driver_recherche is not None:
            try:
                _driver_recherche.quit()
            except Exception:
                pass
            _driver_recherche = None


def _attendre(d, js, *args, timeout: float = 12, pas: float = 0.5):
    debut = time.time()
    while time.time() - debut < timeout:
        try:
            r = d.execute_script(js, *args)
        except Exception:
            r = None
        if r:
            return r
        time.sleep(pas)
    return None


def _radio_par_menu(d, page_url: str) -> str | None:
    # 2 tries: the "…" menu sometimes opens before its items are loaded.
    for _ in range(2):
        d.get(page_url)
        if not _attendre(d, _JS_OUVRIR_MENU, timeout=15):
            continue
        if not _attendre(d, _JS_CLIC_RADIO, _RADIO_MOTS, timeout=6):
            continue
        debut = time.time()
        while time.time() - debut < 15:
            if "/playlist/" in d.current_url:
                return d.current_url.split("?")[0]
            time.sleep(0.5)
    return None


def _radio_par_recherche(d, requete: str) -> str | None:
    d.get(f"https://open.spotify.com/search/{urllib.parse.quote(requete + ' radio')}/playlists")
    listes = _attendre(d, _JS_PLAYLISTS, timeout=15) or []
    officielles = [p for p in listes if "/playlist/37i9dQZF1E" in p["href"]]
    cle = requete.casefold()
    # "Radio <query>" first, then any Spotify radio mentioning the query
    for p in officielles:
        if re.search(_RADIO_MOTS, p["name"], re.I) and cle in p["name"].casefold():
            return p["href"]
    for p in officielles:
        if re.search(_RADIO_MOTS, p["name"], re.I):
            return p["href"]
    return None


def trouver_radio(requete: str, genre: str = "track") -> dict | None:
    """Spotify radio of a song (genre="track") or an artist (genre="artist").

    `requete` is a search text or a direct open.spotify.com track/artist link.
    Returns {"url", "source"} or None. Uses the search browser (same profile).
    """
    global _driver_recherche
    requete = (requete or "").strip()
    if not requete:
        return None
    genre = "artist" if genre == "artist" else "track"
    lien = re.search(r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|artist)/", requete)
    if lien:
        genre = lien.group(1)  # a pasted link says by itself whether it is a song or an artist

    with _driver_recherche_lock:
        try:
            if _driver_recherche is None or not _driver_recherche_valide(_driver_recherche):
                _driver_recherche = _ouvrir(headless=True, profil=PROFIL, rapide=True)
            d = _driver_recherche
        except ScanIndisponible:
            raise
        except Exception:
            _driver_recherche = None
            return None

        page = requete if re.search(rf"open\.spotify\.com/(intl-[\w-]+/)?{genre}/", requete) else None
        if not page:
            onglet = "artists" if genre == "artist" else "tracks"
            d.get(f"https://open.spotify.com/search/{urllib.parse.quote(requete)}/{onglet}")
            sel = ('main a[href*="/artist/"]' if genre == "artist"
                   else '[data-testid="tracklist-row"] a[href*="/track/"]')
            page = _attendre(d, _JS_PREMIER_LIEN, sel, timeout=15)
        if page:
            url = _radio_par_menu(d, page)
            if url:
                return {"url": url, "source": "menu"}
        if "open.spotify.com" in requete:
            return None
        url = _radio_par_recherche(d, requete)
        return {"url": url, "source": "search"} if url else None
