"""Backend test of the iPhone app running in the iOS Simulator (run by .github/workflows/ios.yml).
The Simulator shares the Mac's network: the app's server answers on 127.0.0.1:5090.

Usage: python ci/test_ios.py <output folder> <simulator udid>  — writes <output>/results.json
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OUT, DEV = Path(sys.argv[1]), sys.argv[2]
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:5090"
R = []


def record(name, ok, detail=""):
    R.append({"test": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS " if ok else "FAIL ") + name + " | " + str(detail)[:200], flush=True)


def api(path, body=None, timeout=180):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": repr(e)}


def shot(name):
    subprocess.run(["xcrun", "simctl", "io", DEV, "screenshot", str(OUT / f"sim-{name}.png")], capture_output=True)


def main():
    debut = time.time()
    while time.time() - debut < 240:
        try:
            urllib.request.urlopen(BASE + "/", timeout=3)
            break
        except Exception:
            time.sleep(3)
    record("app starts (server answers)", time.time() - debut < 240, f"{time.time() - debut:.0f}s")
    time.sleep(8)
    shot("01-start")
    c, d = api("/")
    page = urllib.request.urlopen(BASE + "/", timeout=10).read().decode("utf-8", "replace")
    record("page knows it runs on iPhone", "MF_PLATFORM='ios'" in page)
    c, d = api("/api/version")
    record("version", c == 200 and d.get("plateforme"), d)

    for src, q in [("spotify", "Blinding Lights"), ("deezer", "Djadja"), ("youtube", "Ya Lili Balti"), ("spotify", "강남스타일")]:
        t = time.time(); c, d = api("/api/search", {"title": q, "source": src, "limit": 8})
        res = d.get("results") or []
        record(f"search {src}: {q}", c == 200 and res, f"{len(res)} in {time.time() - t:.0f}s: " + "; ".join(f"{r.get('title')} — {r.get('uploader')}" for r in res[:3]))
    for url in ["https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj", "https://www.deezer.com/fr/playlist/3155776842"]:
        c, d = api("/api/resolve-link", {"url": url})
        record(f"link {url}", c == 200 and d.get("results"), f"{d.get('playlist_name')}: {len(d.get('results') or [])}")
    for p in ("/api/decouvrir/top", "/api/decouvrir/pays-FR"):
        t = time.time(); c, d = api(p)
        record(f"GET {p}", c == 200, f"{time.time() - t:.0f}s")
    c, d = api("/api/artiste/recherche", {"q": "Stromae"})
    record("artist search", c == 200 and d.get("artistes"))

    for title, artist in [("Dernière danse", "Indila"), ("Ya Lili", "Balti")]:
        c, m = api("/api/resolve-track", {"query": f"{title} {artist}", "title": title, "artist": artist})
        url = (m.get("result") or {}).get("url")
        if not url:
            record(f"download {title}", False, f"no match {m}")
            continue
        c, d = api("/api/download", {"url": url, "save_name": f"{title} - {artist}", "quality": "aac",
                                     "track_title": title, "artist": artist})
        t = time.time(); job = {}
        while time.time() - t < 600:
            _, job = api("/api/status/" + d.get("job_id", ""))
            if job.get("status") in ("done", "error"):
                break
            time.sleep(3)
        log = job.get("log") or []
        record(f"download {title} — {artist}", job.get("status") == "done", f"{time.time() - t:.0f}s {job.get('file')} | " + " / ".join(log[-4:]))
        record(f"lyrics {title}", any("Lyrics added" in l for l in log), [l for l in log if "yric" in l][-1:])
    c, d = api("/api/bibliotheque", {})
    record("library lists the downloads", c == 200 and d.get("morceaux"), f"{len(d.get('morceaux') or [])} files in {d.get('dossier')}")
    shot("02-after-tests")
    (OUT / "results.json").write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{sum(r['ok'] for r in R)}/{len(R)} ok")


main()
