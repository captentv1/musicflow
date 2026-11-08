from __future__ import annotations

import re
import threading
import time
import urllib.parse
from pathlib import Path

import os as _os, sys as _sys
_DATA = (Path(_os.environ.get("APPDATA", Path.home())) / "MusicFlow") if getattr(_sys, "frozen", False) else Path(__file__).resolve().parent.parent
_DATA.mkdir(parents=True, exist_ok=True)
PROFIL = _DATA / ".navigateur-spotify"

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
    pass


def _options(headless: bool, profil: Path = PROFIL, rapide: bool = False):
    try:
        from selenium.webdriver.edge.options import Options
    except ImportError as exc:
        raise ScanIndisponible(
            "Selenium is not installed. Run: python -m pip install selenium"
        ) from exc
    o = Options()
    o.add_argument(f"--user-data-dir={profil}")
    if headless:
        o.add_argument("--headless=new")
    o.add_argument("--window-size=1280,2000")
    o.add_argument("--mute-audio")
    o.add_argument("--disable-gpu")
    o.add_argument("--log-level=3")
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_experimental_option("excludeSwitches", ["enable-logging"])
    if rapide:
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
    texte = driver.execute_script("return (document.body && document.body.innerText) || ''")
    return not any(m in texte.lower() for m in ("log in", "se connecter", "sign up"))


def ouvrir_connexion(timeout: int = 300) -> bool:
    fermer_recherche()
    d = _ouvrir(headless=False)
    try:
        d.get("https://accounts.spotify.com/login")
        debut = time.time()
        while time.time() - debut < timeout:
            time.sleep(2)
            try:
                if "open.spotify.com" in d.current_url and est_connecte(d):
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

        pas = max(120, visible - 150)
        positions = list(range(0, max(hauteur - visible, 0) + pas, pas)) or [0]

        for tour in (1, 2):
            for pos in positions:
                if time.time() - debut > timeout_total:
                    break
                d.execute_script("arguments[0].scrollTop = arguments[1]", conteneur, pos)
                time.sleep(0.32 if tour == 1 else 0.5)
                store = d.execute_script(_JS_EXTRAIRE, store) or store
                progres(len(store), total)
                if total and len(store) >= total:
                    break
            if (total and len(store) >= total) or time.time() - debut > timeout_total:
                break

        if total and len(store) < total:
            lignes = int(d.execute_script(
                "return document.querySelectorAll('[data-testid=\"tracklist-row\"]').length") or 12)
            passes_sans_gain = 0
            for _ in range(30):
                avant = len(store)
                manquants = [i for i in range(1, total + 2) if str(i) not in store]
                if not manquants or time.time() - debut > timeout_total:
                    break
                cibles = manquants[:: max(1, lignes // 2)]
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
            if resultats and (len(resultats) >= limite or len(resultats) == precedent):
                return resultats
            precedent = len(resultats)
            time.sleep(0.6)
        return resultats if resultats else []


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
    for p in officielles:
        if re.search(_RADIO_MOTS, p["name"], re.I) and cle in p["name"].casefold():
            return p["href"]
    for p in officielles:
        if re.search(_RADIO_MOTS, p["name"], re.I):
            return p["href"]
    return None


def trouver_radio(requete: str, genre: str = "track") -> dict | None:
    global _driver_recherche
    requete = (requete or "").strip()
    if not requete:
        return None
    genre = "artist" if genre == "artist" else "track"
    lien = re.search(r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|artist)/", requete)
    if lien:
        genre = lien.group(1)

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
