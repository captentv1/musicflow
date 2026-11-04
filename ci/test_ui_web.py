"""Interface test of the APK through the app's own page (port 5090 forwarded by adb),
in headless Chrome at phone size. Same page and same Android backend as in the app;
only the Kotlin bridge (window.Android) is absent.

Usage: python ci/test_ui_web.py <output folder>  — appends to <output>/results.json
"""
import json
import sys
import time
from pathlib import Path

from selenium import webdriver

OUT = Path(sys.argv[1])
RES_FILE = OUT / "results.json"
R = json.loads(RES_FILE.read_text(encoding="utf-8")) if RES_FILE.exists() else []

JS_LAYOUT = r"""
const W = document.documentElement.clientWidth, out = [];
for (const el of document.querySelectorAll('.view.active *')) {
  const cs = getComputedStyle(el), r = el.getBoundingClientRect();
  if (cs.display === 'none' || !r.width || !r.height) continue;
  if (r.right > W + 1 && !el.closest('.mf-outils,[style*="overflow"]')) out.push((el.id || el.tagName) + ' +' + Math.round(r.right - W) + 'px');
}
if (document.documentElement.scrollWidth > W + 1) out.push('page scrolls sideways');
return out.slice(0, 10);
"""


def rec(name, ok, detail=""):
    R.append({"test": "UI " + name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + "UI " + name + " | " + str(detail)[:200], flush=True)


def wait(d, js, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        try:
            v = d.execute_script(js)
            if v:
                return v, time.time() - t
        except Exception:
            pass
        time.sleep(0.5)
    return None, time.time() - t


def shot(d, name):
    d.save_screenshot(str(OUT / f"web-{name}.png"))


def main():
    o = webdriver.ChromeOptions()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_experimental_option("mobileEmulation", {"deviceMetrics": {"width": 412, "height": 915, "pixelRatio": 2.6}})
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    try:
        d.get("http://127.0.0.1:5090/")
        d.execute_script("localStorage.setItem('musicflow.tours', JSON.stringify({search:1,link:1,artist:1,library:1,downloads:1,accounts:1}));"
                         "localStorage.setItem('musicflow.tuto','1'); localStorage.setItem('musicflow.version','3.6.2');")
        d.refresh()
        time.sleep(2)
        for v in ("search", "link", "artist", "library", "downloads", "accounts"):
            d.execute_script(f"setActiveView('{v}'); scrollTo(0,0)")
            time.sleep(1)
            pb = d.execute_script(JS_LAYOUT)
            rec(f"{v}: nothing overflows", not pb, pb)
            shot(d, v)

        d.execute_script("setActiveView('search'); const t=document.getElementById('s-title'); t.value='Djadja'; document.getElementById('s-searchBtn').click()")
        n, dt = wait(d, "return document.querySelectorAll('#s-resultsList .resitem').length")
        first = d.execute_script("const r=document.querySelector('#s-resultsList .resitem .ri-title'); return r && r.innerText")
        rec("search shows results", n, f"{n} in {dt:.0f}s, first: {first}")
        h = d.execute_script("const l=document.getElementById('s-sansLive'); return l ? Math.round(l.closest('label').getBoundingClientRect().height) : 0")
        rec("'No live' option on one line", 0 < h < 40, f"{h}px tall")
        shot(d, "search-results")

        d.execute_script("setActiveView('link'); document.getElementById('l-url').value='https://open.spotify.com/album/4yP0hdKOZPNshxUOjY0cZj'; document.getElementById('l-goBtn').click()")
        n, dt = wait(d, "return document.querySelectorAll('#l-resultsList .resitem').length")
        rec("link analyzed", n, f"{n} tracks in {dt:.0f}s")

        ios = d.execute_script("return window.MF_PLATFORM === 'ios'")
        vis = d.execute_script("const b=document.getElementById('l-radio'); return b && b.offsetParent !== null")
        if ios:
            # iPhone: radio needs the Android WebView bridge, so it is hidden; only M4A exists
            rec("iPhone: radio hidden", not vis)
            q = d.execute_script("return [...document.getElementById('l-quality').options].map(o=>o.value)")
            rec("iPhone: only M4A format", q == ["aac"], q)
            # 3.6.0: shortcuts without radio, no artist radio, format kept, Version card, selection bar
            rac = d.execute_script("return [...document.querySelectorAll('#mf-raccourcis .mf-rac .t')].map(e=>e.textContent)")
            rec("iPhone: shortcuts without Spotify radio", rac and "Spotify radio" not in rac, rac)
            rec("iPhone: no artist radio button", not d.execute_script("return !!document.getElementById('a-radio')"))
            rec("iPhone: format stays M4A", d.execute_script("return reglages().format") == "aac")
            d.execute_script("setActiveView('accounts')")
            t, _ = wait(d, "const e=document.getElementById('u-etat'); return e && e.textContent", 30)
            rec("iPhone: Version card answers", t, t)
            d.execute_script("localStorage.removeItem('musicflow.bienvenue'); localStorage.removeItem('musicflow.tours'); MF_bienvenue()")
            pages = []
            for _ in range(6):
                if not d.execute_script("return !!document.querySelector('.mf-bienv')"):
                    break
                pages.append(d.execute_script("return document.querySelector('.mf-bienv h2').textContent"))
                d.execute_script("document.querySelector('.mf-bienv [data-a=suiv]').click()")
                time.sleep(0.4)
            rec("iPhone: welcome without the format page", "Your format" not in pages and len(pages) == 4, pages)
            for _ in range(60):
                if not d.execute_script("return MF_tourActif()"):
                    break
                d.execute_script("document.querySelector('#mf-tour [data-a=suiv]').click()")
                time.sleep(0.3)
            shot(d, "ios-after-tour")
            errs =[l["message"][:150] for l in d.get_log("browser") if l["level"] == "SEVERE" and "Failed to load resource" not in l["message"]]
            rec("no JavaScript error", not errs, errs[:5])
            return
        rec("radio block visible", vis)
        h = d.execute_script("return Math.round(document.getElementById('l-radioQ').getBoundingClientRect().height)")
        rec("radio field normal height", 30 < h < 70, f"{h}px")
        d.execute_script("document.getElementById('l-radioQ').value='Amr Diab'; document.getElementById('l-radioKind').value='artist'; document.getElementById('l-radioBtn').click()")
        n, dt = wait(d, "return /37i9dQZF1E/.test(document.getElementById('l-url').value) && document.querySelectorAll('#l-resultsList .resitem').length >= 20 ? document.querySelectorAll('#l-resultsList .resitem').length : 0", 180)
        rec("radio imported from the interface", n, f"{n} tracks in {dt:.0f}s — {d.execute_script('return document.getElementById(\"l-radioHint\").textContent')}")
        d.execute_script("document.getElementById('l-radio').scrollIntoView()")
        shot(d, "radio")

        d.execute_script("setActiveView('artist'); document.getElementById('a-q').value='Stromae'; document.getElementById('a-go').click()")
        n, dt = wait(d, "return document.querySelectorAll('#a-artistes > *').length")
        rec("artist search", n, f"{dt:.0f}s")
        wait(d, "const c=document.querySelector('#a-artistes > *'); if(c){c.click(); return 1}", 5)
        n, dt = wait(d, "return document.querySelectorAll('#a-albums > *').length")
        rec("artist discography", n, f"{n} albums in {dt:.0f}s")
        shot(d, "artist")

        errs = [l["message"][:150] for l in d.get_log("browser") if l["level"] == "SEVERE" and "Failed to load resource" not in l["message"]]
        rec("no JavaScript error", not errs, errs[:5])
    finally:
        d.quit()
        RES_FILE.write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")


main()
