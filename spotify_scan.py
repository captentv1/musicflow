"""Scan complet d'une playlist Spotify sur PC, via un navigateur piloté (Selenium + Edge).

Pourquoi ce module existe
-------------------------
Sans connexion, Spotify ne donne jamais la playlist entière, quel que soit le moyen :
la page « embed » plafonne à 100 titres (et ignore offset/page/limit), et le lecteur
web n'affiche qu'une quarantaine de lignes à un visiteur anonyme — ni les vraies
touches clavier ni la molette réelle ne débloquent la suite. Mesuré sur une playlist
de 1046 titres.

La seule voie restante est une session connectée. Ce module utilise donc un profil
Edge dédié à MusicFlow : l'utilisateur s'y connecte **lui-même** une seule fois
(fenêtre visible), et les scans suivants réutilisent la session, sans fenêtre.
MusicFlow ne voit ni ne saisit jamais le mot de passe.

Edge est utilisé parce qu'il est présent sur toute installation Windows : aucun
navigateur supplémentaire à télécharger.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from pathlib import Path

PROFIL = Path(__file__).resolve().parent / ".navigateur-spotify"

# Extraction d'une ligne de piste, par la STRUCTURE des liens et non par la position
# des textes. Lire « la 2e chaîne de la ligne » est fragile : selon la largeur de la
# fenêtre et la présence du badge « Explicite », on récupérait un artiste ou le badge
# « E » à la place du titre. Le titre porte data-testid="internal-track-link" et les
# artistes sont des liens /artist/ — insensible à la langue et à la mise en page.
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
    """Selenium ou Edge manquant : le scan ne peut pas démarrer."""


def _options(headless: bool, profil: Path = PROFIL, rapide: bool = False):
    try:
        from selenium.webdriver.edge.options import Options
    except ImportError as exc:  # pragma: no cover - dépend de l'installation
        raise ScanIndisponible(
            "Selenium n'est pas installé. Lance : python -m pip install selenium"
        ) from exc
    o = Options()
    o.add_argument(f"--user-data-dir={profil}")
    if headless:
        o.add_argument("--headless=new")
    # Fenêtre haute : le lecteur web rend d'autant plus de lignes d'un coup.
    o.add_argument("--window-size=1280,2000")
    o.add_argument("--mute-audio")
    o.add_argument("--disable-gpu")
    o.add_argument("--log-level=3")
    # Évite le crash headless « DevToolsActivePort file doesn't exist » observé sous
    # charge (beaucoup d'onglets Edge déjà ouverts) : le sandbox et le /dev/shm partagé
    # peinent à s'initialiser à temps, Chromium abandonne avant même de démarrer.
    o.add_argument("--no-sandbox")
    o.add_argument("--disable-dev-shm-usage")
    o.add_experimental_option("excludeSwitches", ["enable-logging"])
    if rapide:
        # Recherche : on lit le DOM dès qu'il apparaît (sans attendre la fin du chargement
        # de la page, très lourde) et on ne télécharge pas les images — seules leurs URL servent.
        o.page_load_strategy = "none"
        o.add_argument("--blink-settings=imagesEnabled=false")
    return o


def _ouvrir(headless: bool, profil: Path = PROFIL, rapide: bool = False):
    from selenium import webdriver

    profil.mkdir(parents=True, exist_ok=True)
    try:
        return webdriver.Edge(options=_options(headless, profil, rapide))
    except Exception as exc:
        raise ScanIndisponible(f"Impossible de démarrer Edge : {exc}") from exc


def est_connecte(driver) -> bool:
    """Vrai si la session Spotify du profil est connectée."""
    texte = driver.execute_script("return (document.body && document.body.innerText) || ''")
    return not any(m in texte.lower() for m in ("log in", "se connecter", "sign up"))


def ouvrir_connexion(timeout: int = 300) -> bool:
    """Ouvre une fenêtre Edge visible pour que l'utilisateur se connecte lui-même.

    Rend la main dès que la connexion est détectée, ou au bout de `timeout`.
    MusicFlow ne saisit aucun identifiant : l'utilisateur tape tout dans cette fenêtre.
    """
    d = _ouvrir(headless=False)
    try:
        d.get("https://accounts.spotify.com/login")
        debut = time.time()
        while time.time() - debut < timeout:
            time.sleep(2)
            try:
                if "open.spotify.com" in d.current_url and est_connecte(d):
                    time.sleep(2)  # laisser les cookies s'écrire
                    return True
            except Exception:
                return False  # fenêtre fermée par l'utilisateur
        return False
    finally:
        try:
            d.quit()
        except Exception:
            pass


def scanner(url: str, on_progress=None, timeout_total: int = 600):
    """Lit tous les morceaux d'une playlist/album Spotify.

    Retourne (morceaux, total_annonce, connecte). `morceaux` est une liste de
    {"title", "artist"} dans l'ordre de la playlist. Si `total_annonce` dépasse le
    nombre lu, la lecture est incomplète — l'appelant doit le dire clairement plutôt
    que de faire passer la liste pour entière.
    """
    def progres(lus, total):
        if on_progress:
            try:
                on_progress(lus, total)
            except Exception:
                pass

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

        # Balayage par positions ABSOLUES plutôt qu'en défilant en continu.
        # La liste virtualisée réserve toute sa hauteur dès le départ, donc la position
        # d'une ligne est calculable : ligne N ≈ (N / total) * hauteur. Le défilement
        # continu, lui, s'arrêtait dès que Spotify tardait à servir un lot — d'où des
        # scans incomplets et très variables (460, 545, … sur 1046).
        pas = max(120, visible - 150)  # chevauchement entre deux positions
        positions = list(range(0, max(hauteur - visible, 0) + pas, pas)) or [0]

        for tour in (1, 2):  # 2e passage pour combler ce qui n'avait pas encore chargé
            for pos in positions:
                if time.time() - debut > timeout_total:
                    break
                d.execute_script("arguments[0].scrollTop = arguments[1]", conteneur, pos)
                # Laisser la liste virtualisée rendre les lignes : trop vite, on lit des
                # positions encore vides (scans à 87 % au lieu de 99 %).
                time.sleep(0.32 if tour == 1 else 0.5)
                store = d.execute_script(_JS_EXTRAIRE, store) or store
                progres(len(store), total)
                if total and len(store) >= total:
                    break
            if (total and len(store) >= total) or time.time() - debut > timeout_total:
                break

        # Rattrapage ciblé : quelques lignes manquent souvent après le balayage (un lot
        # pas encore rendu au moment du passage). Plutôt que de tout refaire, on saute
        # directement à la position calculée de chaque index absent.
        if total and len(store) < total:
            lignes = int(d.execute_script(
                "return document.querySelectorAll('[data-testid=\"tracklist-row\"]').length") or 12)
            # On répète tant que ça progresse : un nombre de passes fixe s'arrêtait
            # alors qu'il restait du temps et des lignes à récupérer.
            passes_sans_gain = 0
            for _ in range(30):
                avant = len(store)
                manquants = [i for i in range(1, total + 2) if str(i) not in store]
                if not manquants or time.time() - debut > timeout_total:
                    break
                # Une visite couvre ~un écran de lignes : on ne cible qu'un index sur N.
                cibles = manquants[:: max(1, lignes // 2)]   # chevauchement plus large
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
                    # Un lot peut mettre plusieurs secondes à être servi : abandonner au
                    # bout de deux passes laissait des trous (968 titres sur 1046 lors
                    # d'un essai). On insiste davantage avant de renoncer.
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
# Recherche d'un morceau (titre/artiste exacts + pochette), par navigateur piloté.
#
# L'API officielle Spotify (Client Credentials) refuse désormais /search pour les
# applis sans quota étendu (« Active premium subscription required for the owner
# of the app »). La page de recherche publique, elle, fonctionne sans connexion —
# mêmes lignes [data-testid="tracklist-row"] que le scan de playlist ci-dessus.
# Un seul navigateur headless est gardé ouvert entre deux recherches : en réouvrir
# un à chaque fois coûterait plusieurs secondes de démarrage par morceau.
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

# Tous les résultats de la page de recherche (même lecture par la structure des liens
# que _JS_PREMIER_RESULTAT), avec la durée affichée et le lien du morceau.
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
    """Cherche un morceau sur la page de recherche Spotify (sans connexion requise).

    Retourne {"title", "artist", "cover"} du premier résultat, ou None si rien
    n'est trouvé ou si le navigateur ne peut pas être démarré (pas d'Edge/Selenium).

    Réutilise le même profil que le scan de playlist (session déjà connectée le cas
    échéant). Edge refuse deux instances sur le même --user-data-dir en même temps :
    si un scan complet tourne au même moment, cette recherche échoue simplement et
    la fonction rend None — le téléchargement continue avec le titre YouTube brut.
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
    """Recherche Spotify complète pour l'onglet Rechercher (sans connexion, sans API).

    Même navigateur piloté que chercher_morceau, mais rend la liste des résultats :
    {"title", "artist", "album", "cover", "duration", "href"}. Liste vide si rien
    n'est trouvé ou si Edge/Selenium est indisponible — l'appelant retombe sur YouTube.
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
            # On attend que la liste se stabilise (Spotify ajoute les lignes au fil du rendu)
            if resultats and (len(resultats) >= limite or len(resultats) == precedent):
                return resultats
            precedent = len(resultats)
            time.sleep(0.6)
        return resultats if resultats else []
