package com.musicflow.app

import android.annotation.SuppressLint
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import android.webkit.WebViewClient
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

object SpotifySearch {
    private const val UA_DESKTOP =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/124.0 Safari/537.36"

    private const val JS_RESULTS = """
(function(max){
  if (window.__mfAncienne) return [];
  const out = [];
  for (const row of document.querySelectorAll('[data-testid="tracklist-row"]')) {
    const titleLink = row.querySelector('[data-testid="internal-track-link"]')
                   || row.querySelector('a[href*="/track/"]');
    const titleText = titleLink ? titleLink.textContent.trim() : '';
    if (!titleText) continue;
    const artistNames = [...row.querySelectorAll('a[href*="/artist/"]')]
                       .map(a => a.textContent.trim()).filter(Boolean).join(', ');
    const album = row.querySelector('a[href*="/album/"]');
    const img = row.querySelector('img');
    const durations = (row.innerText || '').match(/\b\d{1,2}:\d{2}\b/g) || [];
    out.push({ title: titleText, artist: artistNames, album: album ? album.textContent.trim() : '',
               cover: img ? img.src : '', duration: durations.length ? durations[durations.length - 1] : '',
               href: titleLink.href || '' });
    if (out.length >= max) break;
  }
  return out;
})(%d)"""

    private val ui = Handler(Looper.getMainLooper())
    private var appContext: Context? = null
    private var webView: WebView? = null
    private val lock = Any()

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun viewArg(): WebView {
        webView?.let { return it }
        val w = WebView(appContext!!)
        w.settings.javaScriptEnabled = true
        w.settings.domStorageEnabled = true
        w.settings.userAgentString = UA_DESKTOP
        w.settings.blockNetworkImage = true
        w.settings.loadWithOverviewMode = true
        w.settings.useWideViewPort = true
        w.webViewClient = WebViewClient()
        w.layout(0, 0, 1280, 2000)
        webView = w
        return w
    }

    @JvmStatic
    fun search(searchQuery: String, limit: Int, timeoutMs: Long): String {
        if (appContext == null || searchQuery.isBlank()) return "[]"
        synchronized(lock) {
            val url = "https://open.spotify.com/search/" +
                android.net.Uri.encode(searchQuery.trim()) + "/tracks"
            load(url)

            val startedAt = System.currentTimeMillis()
            var previousCount = -1
            var lastValue = "[]"
            while (System.currentTimeMillis() - startedAt < timeoutMs) {
                Thread.sleep(600)
                val downloaded = CountDownLatch(1)
                var resultJson = "[]"
                ui.post {
                    val w = webView
                    if (w == null) { downloaded.countDown(); return@post }
                    w.evaluateJavascript(JS_RESULTS.format(limit)) { v ->
                        resultJson = if (v.isNullOrBlank() || v == "null") "[]" else v
                        downloaded.countDown()
                    }
                }
                downloaded.await(3, TimeUnit.SECONDS)
                val n = try { org.json.JSONArray(resultJson).length() } catch (e: Exception) { 0 }
                if (n > 0) lastValue = resultJson
                if (n > 0 && (n >= limit || n == previousCount)) return resultJson
                previousCount = n
            }
            return lastValue
        }
    }

    private const val RADIO_WORDS = "radio|راديو|رادیو|ラジオ|라디오|радио|радіо|電台|电台"

    private const val JS_FIRST_LINK = """
(function(sel){ if (window.__mfAncienne) return ''; const a = document.querySelector(sel); return a ? a.href : ''; })(%s)"""

    private val JS_MENU_RADIO = """
(function(){
  if (window.__mfAncienne) return '';
  const words = new RegExp('$RADIO_WORDS', 'i');
  const item = [...document.querySelectorAll('[role="menu"] [role="menuitem"], [role="menu"] button')]
    .find(x => words.test(x.innerText || ''));
  if (item) { item.click(); return 'clic'; }
  const b = document.querySelector('[data-testid="action-bar-row"] [data-testid="more-button"]')
         || document.querySelector('main [data-testid="more-button"]');
  if (b) { b.click(); return 'menu'; }
  return '';
})()"""

    private val JS_PLAYLISTS_RADIO = """
(function(keyOf){
  if (window.__mfAncienne) return '';
  const words = new RegExp('$RADIO_WORDS', 'i');
  const listes = [...document.querySelectorAll('a[href*="/playlist/37i9dQZF1E"]')].map(a => ({
    name: (a.innerText || a.getAttribute('title') || '').trim().split('\n')[0],
    href: a.href.split('?')[0] }));
  const radios = listes.filter(p => words.test(p.name));
  const exacte = radios.find(p => p.name.toLowerCase().includes(keyOf));
  return (exacte || radios[0] || {}).href || '';
})(%s)"""

    private fun load(url: String) {
        val done = CountDownLatch(1)
        ui.post {
            try {
                val w = viewArg()
                if (w.url == null) {
                    w.loadUrl(url)
                    done.countDown()
                    return@post
                }
                w.evaluateJavascript("window.__mfAncienne = true") {
                    try { w.loadUrl(url) } catch (e: Exception) { e.printStackTrace() }
                    done.countDown()
                }
            } catch (e: Exception) { e.printStackTrace(); done.countDown() }
        }
        done.await(5, TimeUnit.SECONDS)
    }

    private fun js(script: String): String {
        val downloaded = CountDownLatch(1)
        var resultJson = ""
        ui.post {
            val w = webView
            if (w == null) { downloaded.countDown(); return@post }
            w.evaluateJavascript(script) { v ->
                resultJson = try { org.json.JSONTokener(v ?: "").nextValue() as? String ?: "" } catch (e: Exception) { "" }
                downloaded.countDown()
            }
        }
        downloaded.await(3, TimeUnit.SECONDS)
        return resultJson
    }

    private fun currentUrl(): String = js("window.__mfAncienne ? '' : location.href")

    private fun waitFor(script: String, timeoutMs: Long): String {
        val startedAt = System.currentTimeMillis()
        while (System.currentTimeMillis() - startedAt < timeoutMs) {
            Thread.sleep(600)
            val r = js(script)
            if (r.isNotEmpty()) return r
        }
        return ""
    }

    @JvmStatic
    fun radio(searchQuery: String, genre: String, timeoutMs: Long): String {
        if (appContext == null || searchQuery.isBlank()) return ""
        synchronized(lock) {
            val q = searchQuery.trim()
            val link = Regex("open\\.spotify\\.com/(?:intl-[\\w-]+/)?(track|artist)/").find(q)
            val type = link?.groupValues?.get(1) ?: (if (genre == "artist") "artist" else "track")
            var page = if (link != null) q.substringBefore('?') else ""
            if (page.isEmpty()) {
                val tab = if (type == "artist") "artists" else "tracks"
                load("https://open.spotify.com/search/" + android.net.Uri.encode(q) + "/" + tab)
                val sel = if (type == "artist") "main a[href*=\"/artist/\"]"
                          else "[data-testid=\"tracklist-row\"] a[href*=\"/track/\"]"
                page = waitFor(JS_FIRST_LINK.format(org.json.JSONObject.quote(sel)), 25000)
            }
            if (page.isNotEmpty()) {
                for (attempt in 0 until 2) {
                    load(page)
                    val startedAt = System.currentTimeMillis()
                    while (System.currentTimeMillis() - startedAt < timeoutMs / 2) {
                        Thread.sleep(700)
                        val url = currentUrl()
                        if (url.contains("/playlist/")) return url.substringBefore('?')
                        js(JS_MENU_RADIO)
                    }
                }
            }
            if (link != null) return ""
            load("https://open.spotify.com/search/" + android.net.Uri.encode("$q radio") + "/playlists")
            return waitFor(JS_PLAYLISTS_RADIO.format(org.json.JSONObject.quote(q.lowercase())), 25000)
        }
    }
}
