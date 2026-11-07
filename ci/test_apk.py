"""End-to-end test of the MusicFlow APK on an emulator (run by .github/workflows/test-apk.yml).

1. installs and starts the app;
2. calls the app's own Flask server (port 5090 inside the phone, forwarded by adb):
   search, links, Spotify radio, lyrics, a real download;
3. walks through every tab of the interface and takes screenshots.

Usage: python ci/test_apk.py <apk> <output folder>
Everything lands in the output folder: results.json, screenshots, logcat.
"""
import json
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

APK, OUT = sys.argv[1], Path(sys.argv[2])
OLD_APK = sys.argv[3] if len(sys.argv) > 3 else None   # older build: tests the in-app update
OLD_VER = sys.argv[4] if len(sys.argv) > 4 else "3.0.0"   # its versionName
OUT.mkdir(parents=True, exist_ok=True)
PKG = "com.musicflow.app"
BASE = "http://127.0.0.1:5090"
RESULTS = []
CI_TOKEN = "ci-test-token"   # the test APK is built with -PciToken=ci-test-token
LIMIT = 560   # y of the bottom tab bar, measured at start (screen_size)


def adb(*args, timeout=120, check=False):
    r = subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace"))
    return r.stdout.decode("utf-8", errors="replace")


def record(name, ok, detail=""):
    RESULTS.append({"test": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS " if ok else "FAIL ") + name + " | " + str(detail)[:200], flush=True)


def api(path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json", "X-MF-Token": CI_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def wait_job(job_id, timeout=600):
    debut = time.time()
    job = {}
    while time.time() - debut < timeout:
        _, job = api("/api/status/" + job_id)
        if job.get("status") in ("done", "error"):
            break
        time.sleep(3)
    return job


# ----------------------------------------------------------------------------- setup
def start_app():
    adb("install", "-r", "-g", APK, timeout=600, check=True)
    for perm in ("android.permission.POST_NOTIFICATIONS",):
        adb("shell", "pm", "grant", PKG, perm)
    adb("shell", "dumpsys", "deviceidle", "whitelist", "+" + PKG)
    adb("logcat", "-c")
    adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
    adb("forward", "tcp:5090", "tcp:5090", check=True)
    debut = time.time()
    while time.time() - debut < 300:   # Python starts slowly under ARM translation
        try:
            urllib.request.urlopen(BASE + "/", timeout=5)
            record("app starts (Flask server answers)", True, f"{time.time() - debut:.0f}s")
            return True
        except Exception:
            time.sleep(5)
    record("app starts (Flask server answers)", False, "no answer after 300 s")
    return False


# ----------------------------------------------------------------------------- backend
def raw_status(path, headers):
    req = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return repr(e)


def test_security():
    """The opposite of the page: another app / web page calling the local API."""
    record("security: API refused without token", raw_status("/api/version", {}) == 403)
    record("security: API refused with a wrong token", raw_status("/api/version", {"X-MF-Token": "nope"}) == 403)
    record("security: API refused for a foreign Host (DNS rebinding)",
           raw_status("/api/version", {"X-MF-Token": CI_TOKEN, "Host": "evil.example:5090"}) == 403)
    record("security: API answers with the token", raw_status("/api/version", {"X-MF-Token": CI_TOKEN}) == 200)
    # 3.7: file endpoints only take audio files (a system file is refused, never converted or read).
    for path, body in (("/api/reduire", {"chemin": "/system/build.prop", "supprimer_original": False}),
                       ("/api/retaguer", {"chemin": "/system/build.prop"}),
                       ("/api/paroles-fichier", {"chemin": "/system/build.prop"})):
        code, _ = api(path, body, timeout=60)
        record(f"security: {path} refuses a non-audio file", code == 404, code)
    req = urllib.request.Request(BASE + "/api/accounts/spotify/config", data=b'{"client_id":"x","client_secret":"y"}',
                                 headers={"Content-Type": "text/plain"})
    try:
        code = urllib.request.urlopen(req, timeout=30).status
    except urllib.error.HTTPError as e:
        code = e.code
    record("security: cross-site text/plain POST refused", code == 403, code)
    with urllib.request.urlopen(BASE + "/", timeout=30) as r:
        h = {k.lower(): v for k, v in r.headers.items()}
    record("security: anti-framing and nosniff headers",
           h.get("x-frame-options") == "DENY" and h.get("x-content-type-options") == "nosniff", h)


def test_backend():
    test_security()
    # Search in several languages, through each source
    for source, q in [("spotify", "Blinding Lights"), ("deezer", "Dernière danse Indila"),
                      ("youtube", "Ya Lili Balti"), ("spotify", "강남스타일"), ("spotify", "Djadja"), ("deezer", "Amr Diab")]:
        t = time.time()
        code, d = api("/api/search", {"title": q, "source": source, "limit": 8})
        res = d.get("results") or []
        # results must match THIS query (a stale page once returned the previous search)
        juste = any(q.split()[0].casefold() in f"{r.get('title')} {r.get('uploader')}".casefold() for r in res[:3])
        record(f"search {source}: {q}", code == 200 and res and juste,
               f"{len(res)} results in {time.time() - t:.0f}s: " + "; ".join(f"{r.get('title')} — {r.get('uploader')}" for r in res[:3]) or d)

    # Links
    for url in ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
                "https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj",
                "https://www.deezer.com/fr/playlist/3155776842"]:
        code, d = api("/api/resolve-link", {"url": url})
        res = d.get("results") or []
        record(f"link: {url}", code == 200 and res, f"{d.get('playlist_name')}: {len(res)} tracks" if res else d)

    # Spotify radio (the new feature), then reading the radio playlist
    for q, kind in [("Blinding Lights The Weeknd", "track"), ("Amr Diab", "artist"),
                    ("Djadja Aya Nakamura", "track"), ("https://open.spotify.com/artist/1Xyo4u8uXC1ZmMpatF05PJ", "artist")]:
        t = time.time()
        code, d = api("/api/spotify-radio", {"query": q, "kind": kind}, timeout=240)
        ok = code == 200 and "/playlist/" in (d.get("url") or "")
        detail = f"{time.time() - t:.0f}s {d}"
        if ok:
            c2, l = api("/api/resolve-link", {"url": d["url"]})
            res = l.get("results") or []
            # the radio must be the one asked for (a stale page once returned the previous radio)
            nom = (l.get("playlist_name") or "").casefold()
            attendu = {"Blinding Lights The Weeknd": "blinding", "Amr Diab": "amr diab",
                       "Djadja Aya Nakamura": "djadja"}.get(q, "weeknd")
            ok = c2 == 200 and len(res) >= 10 and attendu in nom
            detail += f" -> {l.get('playlist_name')}: {len(res)} tracks: " + "; ".join(r.get("title", "") for r in res[:4])
        record(f"radio {kind}: {q}", ok, detail)

    # Discover, artist, caches
    for path in ("/api/decouvrir/top", "/api/decouvrir/genres", "/api/decouvrir/pays-FR"):
        t = time.time(); code, d = api(path, timeout=120)
        record(f"GET {path}", code == 200 and any(isinstance(v, list) and v for v in d.values()), f"{time.time() - t:.0f}s")
    t = time.time(); code, d = api("/api/artiste/recherche", {"q": "Stromae"})
    record("artist search Stromae", code == 200 and d.get("artistes"), f"{time.time() - t:.0f}s")
    body = {"query": "Dynamite BTS", "title": "Dynamite", "artist": "BTS", "duration": "3:19"}
    t = time.time(); api("/api/resolve-track", body); t1 = time.time() - t
    t = time.time(); code, d = api("/api/resolve-track", body); t2 = time.time() - t
    record("resolve-track remembered (2nd call instant)", code == 200 and t2 < 1, f"1st {t1:.1f}s, 2nd {t2:.1f}s")

    # Real downloads with lyrics, several languages
    for title, artist in [("Ya Lili", "Balti"), ("Gangnam Style", "PSY"), ("Dernière danse", "Indila")]:
        code, m = api("/api/resolve-track", {"query": f"{title} {artist}"})
        url = (m.get("result") or {}).get("url")
        if not url:
            record(f"download: {title}", False, f"no YouTube match {m}")
            continue
        code, d = api("/api/download", {"url": url, "save_name": f"{title} - {artist}", "quality": "opus",
                                         "track_title": title, "artist": artist})
        job = wait_job(d.get("job_id", ""), timeout=900) if code == 200 else d
        log = job.get("log") or []
        paroles = [l for l in log if "yric" in l]
        record(f"download: {title} — {artist}", job.get("status") == "done",
               f"{job.get('file')} | {paroles[-1:] or log[-2:]}")
        record(f"lyrics embedded: {title}", any("Lyrics added" in l for l in log), paroles[-1:] or "no lyrics line")


# ----------------------------------------------------------------------------- interface
def shot(name):
    with open(OUT / f"{name}.png", "wb") as f:
        f.write(subprocess.run(["adb", "exec-out", "screencap", "-p"], capture_output=True, timeout=60).stdout)


def nodes():
    """Visible nodes of the screen (the WebView exposes its page through accessibility)."""
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", timeout=60)
    xml = adb("shell", "cat", "/sdcard/ui.xml")
    try:
        root = ET.fromstring(xml[xml.index("<"):])
    except Exception:
        return []
    out = []
    for n in root.iter("node"):
        b = re.findall(r"\d+", n.get("bounds", ""))
        if len(b) == 4:
            out.append({"text": (n.get("text") or n.get("content-desc") or "").strip(),
                        "x": (int(b[0]) + int(b[2])) // 2, "y": (int(b[1]) + int(b[3])) // 2,
                        "cls": n.get("class", "")})
    return out


def tap(text, exact=True, last=False, timeout=20, scroll=False):
    """Taps the node showing text. scroll=True scrolls down the page while looking
    (the small emulator screen hides the bottom of each tab)."""
    debut = time.time()
    while time.time() - debut < timeout:
        found = [n for n in nodes() if (n["text"] == text if exact else text.lower() in n["text"].lower())
                 and n["y"] < LIMIT]   # above the bottom tab bar
        if found:
            n = found[-1] if last else found[0]
            adb("shell", "input", "tap", str(n["x"]), str(n["y"]))
            time.sleep(1.5)
            return True
        if scroll:
            adb("shell", "input", "swipe", "160", "450", "160", "200", "400")
            time.sleep(1)
        else:
            time.sleep(2)
    return False


def tap_tab(text):
    """Bottom tab bar: those nodes are below the y limit of tap()."""
    for n in nodes():
        if n["text"] == text and n["y"] >= LIMIT:
            adb("shell", "input", "tap", str(n["x"]), str(n["y"]))
            time.sleep(1.5)
            return True
    return False


def type_text(text):
    adb("shell", "input", "text", text.replace(" ", "%s"))
    time.sleep(1)


def test_interface():
    time.sleep(5)
    shot("01-start")
    for _ in range(8):                    # first-run tutorial: skip every step
        if not tap("Skip", timeout=4):
            break
    shot("02-search")
    for i, (tab, name) in enumerate([("Link", "link"), ("Artist", "artist"), ("Library", "library"),
                                     ("Downloads", "downloads"), ("Search", "search-again")], 3):
        for _ in range(3):
            tap("Skip", timeout=2)
        ok = tap_tab(tab)
        time.sleep(3)
        shot(f"{i:02d}-{name}")
        record(f"tab {tab} opens", ok)

    # Search a song from the interface (the field shows only a placeholder: tap below its label)
    lab = [n for n in nodes() if n["text"] == "Song name"]
    if lab:
        adb("shell", "input", "tap", str(lab[0]["x"] + 60), str(lab[0]["y"] + 35))
        time.sleep(1)
    type_text("Djadja")
    adb("shell", "input", "keyevent", "66")
    time.sleep(25)
    shot("10-search-results")
    record("search results shown", any("Djadja" in n["text"] for n in nodes()))

    # Spotify radio from the Link tab
    tap_tab("Link")
    for _ in range(3):
        tap("Skip", timeout=2)
    ok = tap("Import radio", timeout=30, scroll=True)   # scrolls the block into view
    if ok:
        adb("shell", "input", "keyevent", "4")          # nothing typed yet: close any keyboard
        ok = tap("Blinding Lights The Weeknd", exact=False, timeout=10)
    record("radio block visible in Link tab", ok)
    shot("11-link-radio-block")
    if ok:
        type_text("Amr Diab")
        adb("shell", "input", "keyevent", "4")          # hide the keyboard
        tap("Song radio", timeout=5)
        tap("Artist radio", last=True, timeout=5)
        tap("Import radio", timeout=5)
        debut = time.time()
        vu = False
        while time.time() - debut < 180:
            time.sleep(10)
            textes = " ".join(n["text"] for n in nodes())
            if re.search(r"Tracks\s*\d+|Amr Diab Radio|deselect all|Ya Ana Ya La|Aayshalak", textes):
                vu = True
                break
        time.sleep(5)
        shot("12-radio-imported")
        record("radio imported from the interface", vu, f"{time.time() - debut:.0f}s")


def screen_size():
    """Bottom tab bar limit from the real screen size (64 css px tall)."""
    global LIMIT
    try:
        h = int(re.findall(r"(\d+)x(\d+)", adb("shell", "wm", "size"))[-1][1])
        dpi = int(re.findall(r"(\d+)", adb("shell", "wm", "density"))[-1])
        LIMIT = h - int(70 * dpi / 160)
    except Exception:
        pass
    print("screen limit", LIMIT, flush=True)


def screen_text():
    return " | ".join(n["text"] for n in nodes() if n["text"])


def wait_text(pattern, timeout=60):
    debut = time.time()
    while time.time() - debut < timeout:
        t = screen_text()
        if re.search(pattern, t):
            return t
        time.sleep(2)
    return None


def version_installed():
    m = re.search(r"versionName=(\S+)", adb("shell", "dumpsys", "package", PKG))
    return m.group(1) if m else None


def launch():
    adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")


def open_settings():
    for n in nodes():
        if n["text"] in ("⚙️", "⚙", "Settings", "Comptes") and n["y"] < 300:
            adb("shell", "input", "tap", str(n["x"]), str(n["y"]))
            time.sleep(2)
            return True
    return False


# ----------------------------------------------------------------------------- in-app update
def test_update_native():
    """Old build (3.0.0) -> popup -> Later -> Settings "Update to x" -> download -> permission -> installer."""
    adb("uninstall", PKG)
    adb("install", "-g", OLD_APK, timeout=600, check=True)
    record("update: old test build installed", version_installed() == OLD_VER, version_installed())
    adb("shell", "dumpsys", "deviceidle", "whitelist", "+" + PKG)
    launch()
    t = wait_text(r"Welcome to MusicFlow", 240)
    record("update: welcome shown on first launch", t, (t or "")[:200])
    tap("Skip", timeout=10)
    t = wait_text(r"New version \d+\.\d+", 90)
    record("update: popup 'New version' shown", t, (t or screen_text())[:300])
    shot("u1-update-popup")
    tap("Later", timeout=10)
    time.sleep(2)
    record("update: 'Later' closes the popup", not re.search(r"New version \d", screen_text()))
    open_settings()
    ok = tap("Update to", exact=False, timeout=60, scroll=True)
    record("update: Settings shows 'Update to x'", ok)
    shot("u2-settings-update")
    if not ok:
        return
    p = wait_text(r"Downloading the update", 30)
    record("update: download progress shown", p, (p or screen_text())[:200])
    shot("u2b-downloading")
    t = wait_text(r"Allow from this source|Install unknown apps|Do you want to (update|install)", 240)
    shot("u3-after-download")
    record("update: APK downloaded, Android asks the user", t, (t or screen_text())[:300])
    if t and re.search(r"Allow from this source|Install unknown apps", t):
        # the opposite first: refuse the permission (back without allowing)
        adb("shell", "input", "keyevent", "4")
        r = wait_text(r"permission to install apps refused", 30)
        record("update (refused): app says the permission was refused", r, (r or screen_text())[:300])
        shot("u3b-refused")
        ok = tap("Update to", exact=False, timeout=20, scroll=True)
        record("update (refused): button usable again", ok)
        t = wait_text(r"Allow from this source|Install unknown apps", 240)
        record("update: permission asked again after a refusal", t, (t or screen_text())[:300])
        tap("Allow from this source", timeout=10)
        shot("u4-permission")
        adb("shell", "input", "keyevent", "4")
        t = wait_text(r"Do you want to (update|install)", 60)
        record("update: installer opens after the permission", t, (t or screen_text())[:300])
    shot("u5-installer")
    if t and re.search(r"Do you want to (update|install)", t):
        # the opposite again: cancel the installer, then retry from Settings
        tap("Cancel", timeout=10)
        time.sleep(6)
        record("update (cancelled): old version still works", version_installed() == OLD_VER,
               screen_text()[:200])
        ok = tap("Update to", exact=False, timeout=30, scroll=True)
        t = wait_text(r"Do you want to (update|install)", 240)
        record("update (cancelled): retry reopens the installer", ok and t, (t or screen_text())[:300])
    if t and re.search(r"Do you want to (update|install)", t):
        tap("Update", timeout=10) or tap("Install", timeout=5)
        debut = time.time()
        while time.time() - debut < 240 and version_installed() == OLD_VER:
            time.sleep(5)
        time.sleep(3)
        shot("u6-installed")
        v = version_installed()
        record("update: new version installed by the app", v and v != OLD_VER, f"{v} | {screen_text()[:200]}")


# ----------------------------------------------------------------------------- welcome
def test_welcome_native():
    t = wait_text(r"Welcome to MusicFlow", 240)
    record("welcome: shown on first launch", t, (t or screen_text())[:200])
    shot("w1-welcome")
    for page, name in [("What’s inside", "inside"), ("Choose your theme", "theme"), ("Your format", "format"), ("Ready!", "ready")]:
        tap("Next", timeout=10)
        t = wait_text(re.escape(page), 15)
        record(f"welcome page: {page}", t, (t or screen_text())[:200])
        if name == "theme":
            record("welcome: theme picked", tap("Ocean", timeout=8))
        if name == "format":
            record("welcome: format picked", tap("MP3 320 kbps", timeout=8))
        shot(f"w2-{name}")
    tap("Start the tour", timeout=10)
    vus = []
    for i in range(50):
        time.sleep(1.5)
        vus.append(screen_text())
        if i in (0, 9, 16, 22):
            shot(f"w3-tour-{i:02d}")
        if tap("Let’s go!", timeout=2):
            break
        if not tap("Next ›", timeout=8):
            break
    tout = " ".join(vus)
    for onglet in ("Link", "Artist", "Library", "Downloads", "Settings"):
        record(f"welcome tour changes tab by itself: {onglet}", f"{onglet} — " in tout)
    record("welcome tour finished", "Let’s go!" not in screen_text(), f"{len(vus)} steps")
    time.sleep(2)
    shot("w4-after-tour")
    record("welcome: chosen format shown in Search", wait_text(r"MP3 320 kbps", 15))
    # second launch, then after clearing every app cache: no welcome again
    for etape in ("second launch", "after clearing the cache"):
        adb("shell", "am", "force-stop", PKG)
        if etape != "second launch":
            adb("shell", "pm", "trim-caches", "999G")
        launch()
        time.sleep(30)
        shot("w5-" + etape.replace(" ", "-"))
        record(f"welcome: not shown on {etape}", not re.search(r"Welcome to MusicFlow", screen_text()))
    # the opposite of normal use: no network, back button spam, rotation
    adb("shell", "cmd", "connectivity", "airplane-mode", "enable")
    adb("shell", "am", "force-stop", PKG)
    launch()
    t = wait_text(r"Song name", 90)   # the accessibility dump can lag right after launch
    shot("x1-offline")
    record("abuse: app opens without network", t, (t or screen_text())[:200])
    adb("shell", "cmd", "connectivity", "airplane-mode", "disable")
    time.sleep(10)
    for _ in range(6):
        adb("shell", "input", "keyevent", "4")
    time.sleep(2)
    launch()
    time.sleep(8)
    record("abuse: back button spam, app reopens", "Search" in screen_text())
    adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
    adb("shell", "settings", "put", "system", "user_rotation", "1")
    time.sleep(5)
    shot("x2-landscape")
    record("abuse: landscape keeps the page", "Search" in screen_text())
    adb("shell", "settings", "put", "system", "user_rotation", "0")
    time.sleep(3)


def main():
    screen_size()
    try:
        if OLD_APK:
            test_update_native()
        adb("uninstall", PKG)   # fresh install: real first launch
        if start_app():
            test_welcome_native()
            test_backend()
            shot("02-app")
    except Exception as exc:
        record("test script crashed", False, repr(exc))
    finally:
        shot("99-end")
        (OUT / "logcat.txt").write_text(adb("logcat", "-d", timeout=120), encoding="utf-8")
        (OUT / "results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8")
        ok = sum(r["ok"] for r in RESULTS)
        print(f"\n{ok}/{len(RESULTS)} tests passed")


main()
