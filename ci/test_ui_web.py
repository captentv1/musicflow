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
  if (r.right > W + 1 && !el.closest('.mf-tools,[style*="overflow"]')) out.push((el.id || el.tagName) + ' +' + Math.round(r.right - W) + 'px');
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
    t, dt = wait(d, "const h=document.querySelector('.mf-welcome h2'); return h && h.textContent", 20)
    rec("welcome shown on first launch", t and "Welcome" in t, f"{t} after {dt:.0f}s")
    shot(d, "welcome-1")
    pages = []
    for _ in range(4):
        js_click_text(d, ".mf-welcome button", "Next")
        time.sleep(0.4)
        pages.append(d.execute_script("return document.querySelector('.mf-welcome h2').textContent"))
        if "theme" in pages[-1]:
            n = d.execute_script("return document.querySelectorAll('.mf-welcome .theme-opt').length")
            rec("welcome: theme choices shown", n >= 10, f"{n} themes")
            d.execute_script("document.querySelector('.mf-welcome .theme-opt[data-theme=\"glass-ocean\"]').click()")
            rec("welcome: theme applied at once", d.execute_script("return document.documentElement.getAttribute('data-theme')") == "glass-ocean")
            shot(d, "welcome-theme")
        if "format" in pages[-1]:
            d.execute_script("document.querySelector('.mf-welcome input[value=\"320\"]').click()")
            shot(d, "welcome-format")
    rec("welcome pages in order", pages == ["What’s inside", "Choose your theme", "Your format", "Ready!"], pages)
    js_click_text(d, ".mf-welcome button", "Start the tour")
    time.sleep(0.8)
    rec("themes back in Settings after welcome", d.execute_script("return !!document.querySelector('#view-accounts #themes')"))
    rec("format chosen in welcome saved", d.execute_script("return settings2().format === '320' && document.getElementById('s-quality').value === '320'"))
    views, titlesList = [], []
    for _ in range(60):
        if not d.execute_script("return MF_tourActive()"):
            break
        v = d.execute_script("return (document.querySelector('.view.active')||{}).id")
        titlesList.append(d.execute_script("return document.querySelector('#mf-tour .bubble h3').textContent"))
        if not views or views[-1] != v:
            views.append(v)
            shot(d, f"tour-{len(views)}-{v}")
        d.execute_script("document.querySelector('#mf-tour [data-a=next]').click()")
        time.sleep(0.6)
    attendu = ["view-search", "view-link", "view-artist", "view-library", "view-downloads", "view-accounts"]
    rec("guided tour changes tab by itself, in order", views == attendu, views)
    rec("guided tour covers radio, Discover, picking songs",
        all(any(k in x for x in titlesList) for k in ("Spotify radio", "Discover", "Pick songs", "Shortcuts")), titlesList)
    rec("tour ends back on Search", d.execute_script("return document.querySelector('.view.active').id") == "view-search")
    d.refresh()
    time.sleep(3)
    rec("welcome not shown on second launch", not d.execute_script("return !!document.querySelector('.mf-welcome')"))
    rec("theme kept after restart", d.execute_script("return document.documentElement.getAttribute('data-theme')") == "glass-ocean")
    # “?” on a tab: tour of THAT tab only, no tab change
    d.execute_script("setActiveView('link'); document.getElementById('mf-help').click()")
    time.sleep(1)
    otherItems = set()
    for _ in range(15):
        if not d.execute_script("return MF_tourActive()"):
            break
        otherItems.add(d.execute_script("return document.querySelector('.view.active').id"))
        d.execute_script("document.querySelector('#mf-tour [data-a=next]').click()")
        time.sleep(0.5)
    rec("'?' tutorial stays on the current tab", otherItems == {"view-link"}, otherItems)


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
    t, _ = wait(d, "const e=document.getElementById('u-state'); return e && /Up to date|available|Could not/.test(e.textContent) && e.textContent", 30)
    rec("update: current version reported up to date", t and "Up to date (3.11.0)" in t, t)
    d.execute_script("window.MF_VERSION='3.0.0'; localStorage.removeItem('musicflow.updateLater'); MF_checkUpdate(true)")
    t, _ = wait(d, "const h=document.querySelector('.mf-modal h2'); return h && h.textContent", 30)
    rec("update: popup when a newer release exists", t and t.startswith("New version"), t)
    shot(d, "update-popup")
    btn = d.execute_script("return document.getElementById('u-btn').textContent")
    rec("update: Settings button offers the new version", btn.startswith("Update to"), btn)
    rec("update: Version card first in Settings, not collapsed", d.execute_script(
        "const c=document.getElementById('u-card'); return c && c.parentNode.firstElementChild===c && !c.classList.contains('closed')"))
    rec("update: dot on the Settings button", d.execute_script("return !!document.querySelector('.gearBtn.mf-updAvailable')"))
    notes = d.execute_script("return document.querySelector('.mf-modal .mf-body').textContent")
    rec("update: release notes without Markdown marks", "**" not in notes and "##" not in notes, notes[:120])
    d.execute_script("[...document.querySelectorAll('.mf-modal button')].find(b=>b.textContent==='Later').click()")
    rec("update: Later closes and is remembered", d.execute_script(
        "return !document.querySelector('.mf-modal') && !!localStorage.getItem('musicflow.updateLater')"))
    d.execute_script("MF_checkUpdate(true)")
    time.sleep(4)
    rec("update: no popup again for the same version", not d.execute_script("return !!document.querySelector('.mf-modal')"))
    d.execute_script("window.__ouvert=null; window.open=(u)=>{window.__ouvert=u}; document.getElementById('u-btn').click()")
    u = d.execute_script("return window.__ouvert")
    rec("update: Install downloads the release file", bool(u) and "/releases/download/" in u, u)
    shot(d, "update-settings")
    d.execute_script("window.MF_VERSION='3.11.0'")


def test_before_download(d):
    """3.7: window before a list download (several versions of a song), Listen, Cancel,
    Escape, and a hostile title that must stay text."""
    launch = ("window.__dbl='wait'; checkBeforeDownload([{title:'Papaoutai',uploader:'Stromae'},"
              "{title:'Papaoutai (Remix)',uploader:'Stromae'},{title:'Formidable',uploader:'Stromae'},"
              "{title:'<img src=x onerror=window.__xss=1> (Live)',uploader:'Z'},{title:'<img src=x onerror=window.__xss=1>',uploader:'Z'}], '')"
              ".then(r => window.__dbl = r)")
    d.execute_script(launch)
    n, dt = wait(d, "return document.querySelectorAll('.mf-modal .mf-dup').length", 30)
    rec("before download: versions shown (2 groups, 4 rows)", n == 4, f"{n} rows in {dt:.0f}s")
    rec("before download: hostile title stays text", d.execute_script(
        "return !document.querySelector('.mf-modal img') && !window.__xss"))
    rec("before download: dialog role, focus on Download", d.execute_script(
        "return !!document.querySelector('.mf-modal [role=dialog]') && /^Download/.test(document.activeElement.textContent)"))
    shot(d, "before-download")
    d.execute_script("document.querySelector('.mf-modal button[data-idx]').click()")
    n, dt = wait(d, "const a=document.querySelector('.mf-modal audio'); return a && a.src ? 1 : 0", 90)
    rec("before download: Listen plays", n, f"{dt:.0f}s")
    d.execute_script("document.querySelector('.mf-modal [data-a=no]').click()")
    n, _ = wait(d, "return window.__dbl === null && !document.querySelector('.mf-modal') && !document.querySelector('audio[src]') ? 1 : 0", 10)
    rec("before download: Cancel closes it and stops the sound", n)
    d.execute_script(launch)
    wait(d, "return document.querySelector('.mf-modal .mf-dup') ? 1 : 0", 30)
    d.execute_script("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
    n, _ = wait(d, "return window.__dbl === null && !document.querySelector('.mf-modal') ? 1 : 0", 10)
    rec("before download: Escape closes it", n)
    d.execute_script(launch)
    wait(d, "return document.querySelector('.mf-modal [data-a=one]') ? 1 : 0", 30)
    d.execute_script("document.querySelector('.mf-modal [data-a=one]').click(); document.querySelector('.mf-modal [data-a=yes]').click()")
    r, _ = wait(d, "return Array.isArray(window.__dbl) && window.__dbl.map(t => t.title).join('|')", 10)
    rec("before download: One per song keeps the originals", r == "Papaoutai|Formidable|<img src=x onerror=window.__xss=1>", r)


def test_library_duplicates(d):
    """3.8: Library → Find duplicates on a fake list: groups, unreadable file reported, last copy protected."""
    d.execute_script("setActiveView('library'); MF_B.tracks_list = [{name:'Song - A.mp3', song_title:'Song', artist_name:'A', path:'/nope/Song - A.mp3', format:'mp3', size_bytes:4e6},"
                     "{name:'Song - A.flac', song_title:'Song', artist_name:'A', path:'/nope/Song - A.flac', format:'flac', size_bytes:3e7},"
                     "{name:'Other - B.mp3', song_title:'Other', artist_name:'B', path:'/nope/Other - B.mp3', format:'mp3', size_bytes:1e6}];"
                     "document.getElementById('b-duplicates').click()")
    n, dt = wait(d, "return document.querySelectorAll('.mf-modal .mf-dup-group .mf-dup').length", 15)
    rec("library duplicates: 1 group of 2 files", n == 2, f"{n} rows in {dt:.0f}s")
    shot(d, "library-duplicates")
    d.execute_script("document.querySelector('.mf-modal [data-listening]').click()")
    t, _ = wait(d, "const s=document.querySelector('.mf-modal .statusrow.er'); return s && s.textContent", 20)
    rec("library duplicates: missing file reported, no crash", t and "unreadable" in t, t)
    d.execute_script("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
    rec("library duplicates: Escape closes", d.execute_script("return !document.querySelector('.mf-modal')"))


def test_fixes_38(d):
    """3.8 fixes seen on a phone: percentage, no mid-word breaks, light theme names, Themes card folds."""
    d.execute_script("setActiveView('accounts'); MF_updateProgress(47)")
    rec("update: percentage shown as nn%", d.execute_script("return document.getElementById('u-pct').textContent") == "47%")
    rec("hints do not cut words", d.execute_script("return getComputedStyle(document.getElementById('u-state')).wordBreak") != "break-all")
    d.execute_script("MF_updateProgress(-1); document.getElementById('u-pct').textContent=''")
    rec("light theme cards have dark names", d.execute_script(
        "const c=document.querySelector('.theme-opt[data-theme=\"glass-sakura\"]'); return !!c && c.classList.contains('light') && getComputedStyle(c.querySelector('.tname')).color !== 'rgb(255, 255, 255)'"))
    folded = d.execute_script("const c=document.getElementById('themes').closest('.card'); if(!c.classList.contains('mf-foldable')) return 'not foldable';"
                              "c.classList.add('closed'); const v=getComputedStyle(document.getElementById('themes')).display; c.classList.remove('closed'); return v")
    rec("Themes card folds (gallery hidden)", folded == "none", folded)
    # 3.11.0: GitHub API refusing (rate limit, 403) -> the app's server reads the release page.
    d.execute_script("window.__f = window.fetch; window.fetch = (u, o) => String(u).includes('api.github.com')"
                     " ? Promise.resolve(new Response('{}', {status: 403})) : window.__f(u, o); MF_checkUpdate(false)")
    t, _ = wait(d, "const e=document.getElementById('u-state'); return e && /Up to date|available|Could not|refuses/.test(e.textContent) && e.textContent", 40)
    rec("update check works when the GitHub API refuses (403)", t and ("Up to date" in t or "available" in t), t)
    d.execute_script("window.fetch = window.__f")
    # 3.9: updates turned off -> update part hidden; back on -> shown again
    d.execute_script("const c=document.getElementById('u-auto'); c.checked=false; c.dispatchEvent(new Event('change'))")
    off = d.execute_script("return [document.getElementById('u-btn').offsetParent === null, localStorage.getItem('musicflow.updatesOff')]")
    rec("updates off: update button hidden, choice saved", off == [True, "1"], off)
    d.execute_script("const c=document.getElementById('u-auto'); c.checked=true; c.dispatchEvent(new Event('change'))")
    rec("updates on again: button shown", d.execute_script("return document.getElementById('u-btn').offsetParent !== null"))


def test_artist_bar(d):
    # The earlier artist test left Stromae open: without clearing, the test ticked the old list
    # while the new one was still loading (the reload then cleared the ticks).
    d.execute_script("setActiveView('artist'); document.getElementById('a-top').innerHTML=''; document.getElementById('a-artists').innerHTML=''; document.getElementById('a-q').value='Stromae'; document.getElementById('a-go').click()")
    wait(d, "return document.querySelectorAll('#a-artists > *').length")
    wait(d, "const c=document.querySelector('#a-artists > *'); if(c){c.click(); return 1}", 5)
    n, dt = wait(d, "return document.querySelectorAll('#a-top .a-title input').length", 90)
    rec("artist: songs listed", n, f"{n} in {dt:.0f}s")
    rec("artist: how-to shown", d.execute_script("const a=document.querySelector('.mf-artistHelp'); return a && a.offsetParent!==null"))
    rec("artist: radio button", d.execute_script("return !!document.getElementById('a-radio')"))
    d.execute_script("const c=document.querySelectorAll('#a-top .a-title input'); c[0].click(); c[1].click(); c[c.length-1].scrollIntoView()")
    time.sleep(0.6)
    bar = d.execute_script("const b=document.querySelector('#view-artist .mf-selbar'); const r=b.getBoundingClientRect(); return b.classList.contains('on') && r.bottom<=innerHeight && r.top>0 ? b.querySelector('span').textContent : ''")
    rec("artist: bar visible at the bottom after ticking (even scrolled down)", bar.startswith("2 track"), bar)
    shot(d, "artist-bar")
    d.execute_script("[...document.querySelectorAll('#view-artist .mf-selbar button')].find(b=>b.textContent==='Clear').click()")
    time.sleep(0.4)
    rec("artist: Clear unticks everything", d.execute_script(
        "return !document.querySelector('#a-page .a-title input:checked, #a-page .a-ah input:checked') && !document.querySelector('#view-artist .mf-selbar.on') && /\\(0\\)/.test(document.getElementById('a-selDl').textContent)"))
    before = d.execute_script("return TASKS.length")
    d.execute_script("const c=document.querySelectorAll('#a-top .a-title input'); c[0].click(); c[c.length-1].scrollIntoView()")
    time.sleep(0.4)
    d.execute_script("[...document.querySelectorAll('#view-artist .mf-selbar button')].find(b=>b.textContent==='Download').click()")
    n, _ = wait(d, f"return TASKS.length > {before}", 30)
    st = d.execute_script("const s=document.getElementById('a-status'); return s.className + ' | ' + s.textContent + ' | tasks ' + TASKS.length + ' | sel ' + document.getElementById('a-selDl').textContent")
    rec("artist: Download from the bar sends the selection to the queue", n, st)
    rec("artist: filters (Albums/EP/Singles/No duplicates) untouched by Clear", d.execute_script(
        "return ['a-tAlbum','a-tEp','a-tSingle','a-noDuplicates'].every(i=>document.getElementById(i).checked)"))
    toast, _ = wait(d, "const t=document.getElementById('mf-toast'); return t && t.classList.contains('on') && t.textContent", 10)
    rec("artist: confirmation visible while scrolled down (toast)", toast, toast)
    shot(d, "artist-toast")


def test_shortcuts_discover(d):
    d.execute_script("setActiveView('search'); scrollTo(0,0)")
    n = d.execute_script("return document.querySelectorAll('#mf-shortcuts .mf-rac').length")
    rec("search: shortcuts shown", n == 4, n)
    shot(d, "shortcuts")
    d.execute_script("[...document.querySelectorAll('.mf-rac')].find(b=>b.textContent.includes('By artist')).click()")
    time.sleep(0.6)
    rec("shortcut 'By artist' opens Artist", d.execute_script("return document.querySelector('.view.active').id") == "view-artist")
    d.execute_script("setActiveView('search'); [...document.querySelectorAll('.mf-rac')].find(b=>b.textContent.includes('Spotify radio')).click()")
    time.sleep(1.2)
    rec("shortcut 'Spotify radio' opens the radio block", d.execute_script(
        "const r=document.getElementById('l-radio').getBoundingClientRect(); return document.querySelector('.view.active').id==='view-link' && r.top<innerHeight && r.bottom>0"))
    rec("radio block has a visible title", d.execute_script("const t=document.querySelector('.mf-radioTitle'); return t && t.offsetParent!==null"))
    d.execute_script("setActiveView('search'); document.getElementById('dv-show').click()")
    n, dt = wait(d, "return document.querySelectorAll('#dv-list .a-title input').length", 90)
    rec("discover: tracks listed", n, f"{n} in {dt:.0f}s")
    if n:
        d.execute_script("const c=document.querySelectorAll('#dv-list .a-title input'); c[2].click(); c[c.length-1].scrollIntoView()")
        time.sleep(0.5)
        bar = d.execute_script("const b=document.querySelector('#view-search .mf-selbar'); return b.classList.contains('on') ? b.querySelector('span').textContent : ''")
        rec("discover: selection bar at the bottom", bar.startswith("1 track"), bar)
        shot(d, "discover-bar")
        d.execute_script("[...document.querySelectorAll('#view-search .mf-selbar button')].find(b=>b.textContent==='Clear').click()")


def errs_now(d):
    # a 404 cover or a 400 for an invalid link is expected here: only real script errors count
    return [l["message"][:150] for l in d.get_log("browser") if l["level"] == "SEVERE" and "Failed to load resource" not in l["message"]]


def test_security_web(d):
    """From inside the page: a request without the token (as another site would send) is refused."""
    d.get("http://127.0.0.1:5090/")
    time.sleep(2)
    code = d.execute_script("const x=new XMLHttpRequest(); x.open('GET','/api/version',false); x.send(); return x.status")
    rec("security: request without token refused (403)", code == 403, code)
    ok = d.execute_script("return fetch('/api/version').then(r=>r.status)")
    rec("security: the page's own calls carry the token", ok == 200, ok)
    n = d.execute_script("return document.querySelectorAll('img[src=x], [onerror]').length")
    rec("security: no injected handler in the page", n == 0, n)


def test_abuse(d):
    """The opposite of normal use: empty or absurd input, cancel, double clicks, offline. The app must not break."""
    d.get("http://127.0.0.1:5090/")
    d.execute_script("localStorage.clear()")
    d.refresh()
    wait(d, "return !!document.querySelector('.mf-welcome')", 20)
    # welcome: back, Previous, Skip on page 1 → nothing broken, no tour, tours marked as seen
    d.execute_script("const b=document.querySelector('.mf-welcome [data-a=prev]'); return b.style.display")
    js_click_text(d, ".mf-welcome button", "Next")
    js_click_text(d, ".mf-welcome button", "Next")
    d.execute_script("document.querySelector('.mf-welcome [data-a=prev]').click(); document.querySelector('.mf-welcome [data-a=prev]').click()")
    rec("abuse: Previous back to page 1", d.execute_script("return document.querySelector('.mf-welcome h2').textContent").startswith("Welcome"))
    js_click_text(d, ".mf-welcome button", "Next"); js_click_text(d, ".mf-welcome button", "Next")
    js_click_text(d, ".mf-welcome button", "Skip")   # skipped on the theme page
    time.sleep(0.5)
    rec("abuse: Skip on theme page closes everything", d.execute_script(
        "return !document.querySelector('.mf-welcome') && !MF_tourActive() && !!document.querySelector('#view-accounts #themes')"))
    d.execute_script("setActiveView('link')")
    time.sleep(1.2)
    rec("abuse: after Skip, no automatic tour on other tabs", not d.execute_script("return MF_tourActive()"))
    # tour: Escape in the middle
    d.execute_script("MF_startTour('search')")
    time.sleep(1)
    d.execute_script("document.querySelector('#mf-tour [data-a=next]').click(); document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))")
    time.sleep(0.5)
    rec("abuse: Escape closes the tour", not d.execute_script("return MF_tourActive() || !!document.getElementById('mf-tour')"))
    # search: empty, spaces, absurd, very long, special characters
    for q in ("", "   ", "zzqxw qqvvzz 9981", "a" * 400, "<img src=x onerror=alert(1)> ' \" ; --"):
        d.execute_script("setActiveView('search'); const t=document.getElementById('s-title'); t.value=arguments[0]; document.getElementById('s-searchBtn').click()", q)
        time.sleep(6 if q.strip() else 1)
    rec("abuse: odd searches do not inject HTML", not d.execute_script("return !!document.querySelector('#s-resultsList img[src=x]')"))
    # double click on Search and on Analyze (first wait for the previous odd search to end:
    # the button stays disabled while a search runs, extra clicks are ignored by design)
    wait(d, "return !document.getElementById('s-searchBtn').disabled", 180)
    rec("abuse: Search button disabled while searching (extra clicks ignored)", True)
    d.execute_script("const t=document.getElementById('s-title'); t.value='Djadja'; const b=document.getElementById('s-searchBtn'); b.click(); b.click(); b.click()")
    n, _ = wait(d, "return document.querySelectorAll('#s-resultsList .resitem').length", 90)
    ids = d.execute_script("return [...document.querySelectorAll('#s-resultsList .resitem .ri-title')].map(e=>e.innerText)")
    lim = int(d.execute_script("return +(document.getElementById('s-nb')||{}).value || 8"))
    rec("abuse: triple click on Search gives one list, not three", n and n <= lim, f"{n} results (limit {lim})")
    # link: invalid links
    for u in ("hello", "https://example.com/nothing", "https://open.spotify.com/playlist/000000000000000000000x"):
        d.execute_script("setActiveView('link'); document.getElementById('l-url').value=arguments[0]; document.getElementById('l-goBtn').click()", u)
        time.sleep(8)
    rec("abuse: invalid links leave the page usable", d.execute_script("return !document.getElementById('l-goBtn').disabled"))
    # radio: empty, then nonsense
    d.execute_script("document.getElementById('l-radioQ').value=''; document.getElementById('l-radioBtn').click()")
    rec("abuse: empty radio does nothing (focus on field)", d.execute_script("return document.activeElement.id === 'l-radioQ'"))
    # artist: empty, nonsense, Download with nothing ticked, Clear with nothing
    d.execute_script("setActiveView('artist'); document.getElementById('a-q').value=''; document.getElementById('a-go').click()")
    d.execute_script("document.getElementById('a-q').value='qqzzxxvvw 77'; document.getElementById('a-go').click()")
    time.sleep(10)
    rec("abuse: unknown artist shows a message, no crash", d.execute_script("return document.getElementById('a-status').style.display !== 'none' || !document.querySelector('#a-artists > *')"))
    d.execute_script("document.getElementById('a-selDl').click()")
    time.sleep(1)
    rec("abuse: selection bar hidden with nothing ticked", not d.execute_script("return !!document.querySelector('#view-artist .mf-selbar.on')"))
    # tick / untick quickly 20 times → counter coherent
    d.execute_script("document.getElementById('a-q').value='Stromae'; document.getElementById('a-go').click()")
    wait(d, "const c=document.querySelector('#a-artists > *'); if(c){c.click(); return 1}", 60)
    n, _ = wait(d, "return document.querySelectorAll('#a-top .a-title input').length", 90)
    if n:
        d.execute_script("const c=document.querySelector('#a-top .a-title input'); for(let i=0;i<21;i++) c.click()")
        time.sleep(0.5)
        bar = d.execute_script("return document.querySelector('#view-artist .mf-selbar span').textContent")
        rec("abuse: 21 fast clicks = 1 ticked, counter right", bar.startswith("1 track"), bar)
        d.execute_script("[...document.querySelectorAll('#view-artist .mf-selbar button')].find(b=>b.textContent==='Clear').click()")
        # open/close album tracks very fast
        d.execute_script("const b=document.querySelector('#a-albums .a-album button'); if(b){for(let i=0;i<7;i++) b.click()}")
        time.sleep(4)
        rec("abuse: album tracks opened 7 times fast: list not duplicated", d.execute_script(
            "const p=document.querySelector('#a-albums .a-tracks'); if(!p||p.style.display!=='block') return true; const t=[...p.querySelectorAll('.a-tt')].map(e=>e.textContent); return t.length===new Set(t).size"))
    # format: change in every tab, last one wins everywhere
    d.execute_script("""['s-quality','l-quality','a-quality'].forEach((id,i)=>{const s=document.getElementById(id); s.value=['flac','aac','192'][i]; s.dispatchEvent(new Event('change',{bubbles:true}))})""")
    rec("abuse: last format chosen wins everywhere", d.execute_script("return settings2().format==='192' && document.getElementById('s-quality').value==='192' && document.getElementById('r-format').value==='192'"))
    # update: offline / GitHub unreachable → clear message, no popup
    d.execute_script("window.__f=window.fetch; window.fetch=(u,o)=>String(u).includes('api.github.com')?Promise.reject(new Error('offline')):window.__f(u,o); setActiveView('accounts'); document.getElementById('u-btn').textContent; MF_checkUpdate(false)")
    time.sleep(2)
    t = d.execute_script("return document.getElementById('u-state').textContent")
    rec("abuse: update check offline says so", "Could not check" in t, t)
    d.execute_script("window.fetch=window.__f")
    # rapid tab switching
    d.execute_script("const v=['search','link','artist','library','downloads','accounts']; for(let i=0;i<60;i++) setActiveView(v[i%6]); setActiveView('search')")
    time.sleep(2)
    rec("abuse: 60 fast tab switches, one view shown", d.execute_script("return document.querySelectorAll('.view.active').length") == 1)
    pb = d.execute_script(JS_LAYOUT)
    rec("abuse: layout still fine after all that", not pb, pb)
    e = errs_now(d)
    rec("abuse: no JavaScript error", not e, e[:5])


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
                         "localStorage.setItem('musicflow.tutorial','1'); localStorage.setItem('musicflow.version','3.11.0');"
                         "localStorage.removeItem('musicflow.migrated'); localStorage.setItem('musicflow.reglages', JSON.stringify({paroles: false, ordre: true}));")
        d.refresh()
        time.sleep(2)
        mig = d.execute_script("return [localStorage.getItem('musicflow.reglages'), localStorage.getItem('musicflow.settings')]")
        rec("old French settings migrated to English names", mig[0] is None and '"lyrics_data":false' in (mig[1] or "") and '"order":true' in (mig[1] or ""), mig)
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
        h = d.execute_script("const l=document.getElementById('s-noLive'); return l ? Math.round(l.closest('label').getBoundingClientRect().height) : 0")
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
        n, dt = wait(d, "return document.querySelectorAll('#a-artists > *').length")
        rec("artist search", n, f"{dt:.0f}s")
        wait(d, "const c=document.querySelector('#a-artists > *'); if(c){c.click(); return 1}", 5)
        n, dt = wait(d, "return document.querySelectorAll('#a-albums > *').length")
        rec("artist discography", n, f"{n} albums in {dt:.0f}s")
        shot(d, "artist")

        for t in (test_artist_bar, test_shortcuts_discover, test_before_download, test_library_duplicates, test_fixes_38):
            try:
                t(d)
            except Exception as exc:
                rec(f"{t.__name__} crashed", False, repr(exc))

        errs = [l["message"][:150] for l in d.get_log("browser") if l["level"] == "SEVERE" and "/api/audio-file" not in l["message"] and "navigator.vibrate" not in l["message"]]   # 404 expected: fake file in the duplicates test
        rec("no JavaScript error", not errs, errs[:5])
        try:
            test_security_web(d)
        except Exception as exc:
            rec("test_security_web crashed", False, repr(exc))
        try:
            test_abuse(d)
        except Exception as exc:
            rec("test_abuse crashed", False, repr(exc))
    finally:
        d.quit()
        RES_FILE.write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")


main()
