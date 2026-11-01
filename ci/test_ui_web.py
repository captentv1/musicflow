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


def js_click_text(d, sel, text):
    return d.execute_script("const b=[...document.querySelectorAll(arguments[0])].find(x=>x.offsetParent!==null&&x.textContent.trim()===arguments[1]); if(b){b.click(); return true} return false", sel, text)


def test_welcome(d):
    """Fresh storage = first launch on PC: welcome pages, theme, format, guided tour across every tab."""
    d.get("http://127.0.0.1:5090/")
    d.execute_script("localStorage.clear()")
    d.refresh()
    t, dt = wait(d, "const h=document.querySelector('.mf-bienv h2'); return h && h.textContent", 20)
    rec("welcome shown on first launch", t and "Welcome" in t, f"{t} after {dt:.0f}s")
    shot(d, "welcome-1")
    pages = []
    for _ in range(4):
        js_click_text(d, ".mf-bienv button", "Next")
        time.sleep(0.4)
        pages.append(d.execute_script("return document.querySelector('.mf-bienv h2').textContent"))
        if "theme" in pages[-1]:
            n = d.execute_script("return document.querySelectorAll('.mf-bienv .theme-opt').length")
            rec("welcome: theme choices shown", n >= 10, f"{n} themes")
            d.execute_script("document.querySelector('.mf-bienv .theme-opt[data-theme=\"verre-ocean\"]').click()")
            rec("welcome: theme applied at once", d.execute_script("return document.documentElement.getAttribute('data-theme')") == "verre-ocean")
            shot(d, "welcome-theme")
        if "format" in pages[-1]:
            d.execute_script("document.querySelector('.mf-bienv input[value=\"320\"]').click()")
            shot(d, "welcome-format")
    rec("welcome pages in order", pages == ["What’s inside", "Choose your theme", "Your format", "Ready!"], pages)
    js_click_text(d, ".mf-bienv button", "Start the tour")
    time.sleep(0.8)
    rec("themes back in Settings after welcome", d.execute_script("return !!document.querySelector('#view-accounts #themes')"))
    rec("format chosen in welcome saved", d.execute_script("return reglages().format === '320' && document.getElementById('s-quality').value === '320'"))
    vues, titres = [], []
    for _ in range(60):
        if not d.execute_script("return MF_tourActif()"):
            break
        v = d.execute_script("return (document.querySelector('.view.active')||{}).id")
        titres.append(d.execute_script("return document.querySelector('#mf-tour .bulle h3').textContent"))
        if not vues or vues[-1] != v:
            vues.append(v)
            shot(d, f"tour-{len(vues)}-{v}")
        d.execute_script("document.querySelector('#mf-tour [data-a=suiv]').click()")
        time.sleep(0.6)
    attendu = ["view-search", "view-link", "view-artist", "view-library", "view-downloads", "view-accounts"]
    rec("guided tour changes tab by itself, in order", vues == attendu, vues)
    rec("guided tour covers radio, Discover, picking songs",
        all(any(k in x for x in titres) for k in ("Spotify radio", "Discover", "Pick songs", "Shortcuts")), titres)
    rec("tour ends back on Search", d.execute_script("return document.querySelector('.view.active').id") == "view-search")
    d.refresh()
    time.sleep(3)
    rec("welcome not shown on second launch", not d.execute_script("return !!document.querySelector('.mf-bienv')"))
    rec("theme kept after restart", d.execute_script("return document.documentElement.getAttribute('data-theme')") == "verre-ocean")
    # “?” on a tab: tour of THAT tab only, no tab change
    d.execute_script("setActiveView('link'); document.getElementById('mf-aide').click()")
    time.sleep(1)
    autres = set()
    for _ in range(15):
        if not d.execute_script("return MF_tourActif()"):
            break
        autres.add(d.execute_script("return document.querySelector('.view.active').id"))
        d.execute_script("document.querySelector('#mf-tour [data-a=suiv]').click()")
        time.sleep(0.5)
    rec("'?' tutorial stays on the current tab", autres == {"view-link"}, autres)


def test_format(d):
    d.execute_script("setActiveView('search'); const s=document.getElementById('s-quality'); s.value='wav'; s.dispatchEvent(new Event('change',{bubbles:true}))")
    rec("format picked in Search applied to every tab", d.execute_script(
        "return ['l-quality','a-quality','r-format'].every(i=>document.getElementById(i).value==='wav')"))
    d.refresh()
    time.sleep(3)
    vals = d.execute_script("return ['s-quality','l-quality','a-quality','r-format'].map(i=>document.getElementById(i).value)")
    rec("format kept after restart (no return to Opus)", vals == ["wav"] * 4, vals)
    txt = d.execute_script("const p=document.getElementById('s-quality-picker'); return p ? p.textContent : ''")
    rec("format label shown after restart", "WAV" in txt, txt)
    d.execute_script("const s=document.getElementById('l-quality'); s.value='opus'; s.dispatchEvent(new Event('change',{bubbles:true}))")


def test_update_web(d):
    """Update check against the real latest GitHub release (PC path in Chrome: no Android bridge)."""
    d.execute_script("setActiveView('accounts')")
    t, _ = wait(d, "const e=document.getElementById('u-etat'); return e && /Up to date|available|Could not/.test(e.textContent) && e.textContent", 30)
    rec("update: current version reported up to date", t and "Up to date (3.6.0)" in t, t)
    d.execute_script("window.MF_VERSION='3.0.0'; localStorage.removeItem('musicflow.majPlusTard'); MF_verifierMaj(true)")
    t, _ = wait(d, "const h=document.querySelector('.mf-modal h2'); return h && h.textContent", 30)
    rec("update: popup when a newer release exists", t and t.startswith("New version"), t)
    shot(d, "update-popup")
    btn = d.execute_script("return document.getElementById('u-btn').textContent")
    rec("update: Settings button offers the new version", btn.startswith("Update to"), btn)
    d.execute_script("[...document.querySelectorAll('.mf-modal button')].find(b=>b.textContent==='Later').click()")
    rec("update: Later closes and is remembered", d.execute_script(
        "return !document.querySelector('.mf-modal') && !!localStorage.getItem('musicflow.majPlusTard')"))
    d.execute_script("MF_verifierMaj(true)")
    time.sleep(4)
    rec("update: no popup again for the same version", not d.execute_script("return !!document.querySelector('.mf-modal')"))
    d.execute_script("window.__ouvert=null; window.open=(u)=>{window.__ouvert=u}; document.getElementById('u-btn').click()")
    u = d.execute_script("return window.__ouvert")
    rec("update: Install downloads the release file", bool(u) and "/releases/download/" in u, u)
    shot(d, "update-settings")
    d.execute_script("window.MF_VERSION='3.6.0'")


def test_artist_bar(d):
    d.execute_script("setActiveView('artist'); document.getElementById('a-q').value='Stromae'; document.getElementById('a-go').click()")
    wait(d, "return document.querySelectorAll('#a-artistes > *').length")
    wait(d, "const c=document.querySelector('#a-artistes > *'); if(c){c.click(); return 1}", 5)
    n, dt = wait(d, "return document.querySelectorAll('#a-top .a-titre input').length", 90)
    rec("artist: songs listed", n, f"{n} in {dt:.0f}s")
    rec("artist: how-to shown", d.execute_script("const a=document.querySelector('.mf-aideArtiste'); return a && a.offsetParent!==null"))
    rec("artist: radio button", d.execute_script("return !!document.getElementById('a-radio')"))
    d.execute_script("const c=document.querySelectorAll('#a-top .a-titre input'); c[0].click(); c[1].click(); c[c.length-1].scrollIntoView()")
    time.sleep(0.6)
    bar = d.execute_script("const b=document.querySelector('#view-artist .mf-selbar'); const r=b.getBoundingClientRect(); return b.classList.contains('on') && r.bottom<=innerHeight && r.top>0 ? b.querySelector('span').textContent : ''")
    rec("artist: bar visible at the bottom after ticking (even scrolled down)", bar.startswith("2 track"), bar)
    shot(d, "artist-bar")
    d.execute_script("[...document.querySelectorAll('#view-artist .mf-selbar button')].find(b=>b.textContent==='Clear').click()")
    time.sleep(0.4)
    rec("artist: Clear unticks everything", d.execute_script(
        "return !document.querySelector('#a-fiche input:checked') && !document.querySelector('#view-artist .mf-selbar.on') && /\\(0\\)/.test(document.getElementById('a-selDl').textContent)"))
    avant = d.execute_script("return TACHES.length")
    d.execute_script("const c=document.querySelectorAll('#a-top .a-titre input'); c[0].click(); c[c.length-1].scrollIntoView()")
    time.sleep(0.4)
    d.execute_script("[...document.querySelectorAll('#view-artist .mf-selbar button')].find(b=>b.textContent==='Download').click()")
    n, _ = wait(d, f"return TACHES.length > {avant}", 30)
    rec("artist: Download from the bar sends the selection to the queue", n)
    toast, _ = wait(d, "const t=document.getElementById('mf-toast'); return t && t.classList.contains('on') && t.textContent", 10)
    rec("artist: confirmation visible while scrolled down (toast)", toast, toast)
    shot(d, "artist-toast")


def test_shortcuts_discover(d):
    d.execute_script("setActiveView('search'); scrollTo(0,0)")
    n = d.execute_script("return document.querySelectorAll('#mf-raccourcis .mf-rac').length")
    rec("search: shortcuts shown", n == 4, n)
    shot(d, "shortcuts")
    d.execute_script("[...document.querySelectorAll('.mf-rac')].find(b=>b.textContent.includes('By artist')).click()")
    time.sleep(0.6)
    rec("shortcut 'By artist' opens Artist", d.execute_script("return document.querySelector('.view.active').id") == "view-artist")
    d.execute_script("setActiveView('search'); [...document.querySelectorAll('.mf-rac')].find(b=>b.textContent.includes('Spotify radio')).click()")
    time.sleep(1.2)
    rec("shortcut 'Spotify radio' opens the radio block", d.execute_script(
        "const r=document.getElementById('l-radio').getBoundingClientRect(); return document.querySelector('.view.active').id==='view-link' && r.top<innerHeight && r.bottom>0"))
    rec("radio block has a visible title", d.execute_script("const t=document.querySelector('.mf-radioTitre'); return t && t.offsetParent!==null"))
    d.execute_script("setActiveView('search'); document.getElementById('dv-voir').click()")
    n, dt = wait(d, "return document.querySelectorAll('#dv-liste .a-titre input').length", 90)
    rec("discover: tracks listed", n, f"{n} in {dt:.0f}s")
    if n:
        d.execute_script("const c=document.querySelectorAll('#dv-liste .a-titre input'); c[2].click(); c[c.length-1].scrollIntoView()")
        time.sleep(0.5)
        bar = d.execute_script("const b=document.querySelector('#view-search .mf-selbar'); return b.classList.contains('on') ? b.querySelector('span').textContent : ''")
        rec("discover: selection bar at the bottom", bar.startswith("1 track"), bar)
        shot(d, "discover-bar")
        d.execute_script("[...document.querySelectorAll('#view-search .mf-selbar button')].find(b=>b.textContent==='Clear').click()")


def main():
    o = webdriver.ChromeOptions()
    o.add_argument("--headless=new")
    o.add_argument("--no-sandbox")
    o.add_experimental_option("mobileEmulation", {"deviceMetrics": {"width": 412, "height": 915, "pixelRatio": 2.6}})
    o.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    d = webdriver.Chrome(options=o)
    try:
        for t in (test_welcome, test_format, test_update_web):
            try:
                t(d)
            except Exception as exc:
                rec(f"{t.__name__} crashed", False, repr(exc))
        d.get("http://127.0.0.1:5090/")
        d.execute_script("localStorage.setItem('musicflow.tours', JSON.stringify({search:1,link:1,artist:1,library:1,downloads:1,accounts:1}));"
                         "localStorage.setItem('musicflow.tuto','1'); localStorage.setItem('musicflow.version','3.6.0');")
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

        vis = d.execute_script("const b=document.getElementById('l-radio'); return b && b.offsetParent !== null")
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

        for t in (test_artist_bar, test_shortcuts_discover):
            try:
                t(d)
            except Exception as exc:
                rec(f"{t.__name__} crashed", False, repr(exc))

        errs = [l["message"][:150] for l in d.get_log("browser") if l["level"] == "SEVERE"]
        rec("no JavaScript error", not errs, errs[:5])
    finally:
        d.quit()
        RES_FILE.write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")


main()
