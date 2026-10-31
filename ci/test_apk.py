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
OUT.mkdir(parents=True, exist_ok=True)
PKG = "com.musicflow.app"
BASE = "http://127.0.0.1:5090"
RESULTS = []


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
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
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
def test_backend():
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
                 and n["y"] < 560]   # above the bottom tab bar
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
        if n["text"] == text and n["y"] >= 560:
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


def main():
    try:
        if start_app():
            test_backend()
            shot("02-app")      # native screen; the interface itself is tested by ci/test_ui_web.py
    except Exception as exc:
        record("test script crashed", False, repr(exc))
    finally:
        shot("99-end")
        (OUT / "logcat.txt").write_text(adb("logcat", "-d", timeout=120), encoding="utf-8")
        (OUT / "results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1), encoding="utf-8")
        ok = sum(r["ok"] for r in RESULTS)
        print(f"\n{ok}/{len(RESULTS)} tests passed")


main()
