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
    private const val UA_BUREAU =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/124.0 Safari/537.36"

    private const val JS_RESULTATS = """
(function(max){
  if (window.__mfAncienne) return [];
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
    const durees = (row.innerText || '').match(/\b\d{1,2}:\d{2}\b/g) || [];
    out.push({ title: titre, artist: artistes, album: album ? album.textContent.trim() : '',
               cover: img ? img.src : '', duration: durees.length ? durees[durees.length - 1] : '',
               href: lienTitre.href || '' });
    if (out.length >= max) break;
  }
  return out;
})(%d)"""

    private val ui = Handler(Looper.getMainLooper())
    private var appContext: Context? = null
    private var webView: WebView? = null
    private val verrou = Any()

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun vue(): WebView {
        webView?.let { return it }
        val w = WebView(appContext!!)
        w.settings.javaScriptEnabled = true
        w.settings.domStorageEnabled = true
        w.settings.userAgentString = UA_BUREAU
        w.settings.blockNetworkImage = true
        w.settings.loadWithOverviewMode = true
        w.settings.useWideViewPort = true
        w.webViewClient = WebViewClient()
        w.layout(0, 0, 1280, 2000)
        webView = w
        return w
    }

    @JvmStatic
    fun chercher(requete: String, limite: Int, timeoutMs: Long): String {
        if (appContext == null || requete.isBlank()) return "[]"
        synchronized(verrou) {
            val url = "https://open.spotify.com/search/" +
                android.net.Uri.encode(requete.trim()) + "/tracks"
            charger(url)

            val debut = System.currentTimeMillis()
            var precedent = -1
            var dernier = "[]"
            while (System.currentTimeMillis() - debut < timeoutMs) {
                Thread.sleep(600)
                val lu = CountDownLatch(1)
                var resultat = "[]"
                ui.post {
                    val w = webView
                    if (w == null) { lu.countDown(); return@post }
                    w.evaluateJavascript(JS_RESULTATS.format(limite)) { v ->
                        resultat = if (v.isNullOrBlank() || v == "null") "[]" else v
                        lu.countDown()
                    }
                }
                lu.await(3, TimeUnit.SECONDS)
                val n = try { org.json.JSONArray(resultat).length() } catch (e: Exception) { 0 }
                if (n > 0) dernier = resultat
                if (n > 0 && (n >= limite || n == precedent)) return resultat
                precedent = n
            }
            return dernier
        }
    }

    private const val MOTS_RADIO = "radio|راديو|رادیو|ラジオ|라디오|радио|радіо|電台|电台"

    private const val JS_PREMIER_LIEN = """
(function(sel){ if (window.__mfAncienne) return ''; const a = document.querySelector(sel); return a ? a.href : ''; })(%s)"""

    private val JS_MENU_RADIO = """
(function(){
  if (window.__mfAncienne) return '';
  const mots = new RegExp('$MOTS_RADIO', 'i');
  const item = [...document.querySelectorAll('[role="menu"] [role="menuitem"], [role="menu"] button')]
    .find(x => mots.test(x.innerText || ''));
  if (item) { item.click(); return 'clic'; }
  const b = document.querySelector('[data-testid="action-bar-row"] [data-testid="more-button"]')
         || document.querySelector('main [data-testid="more-button"]');
  if (b) { b.click(); return 'menu'; }
  return '';
})()"""

    private val JS_PLAYLISTS_RADIO = """
(function(cle){
  if (window.__mfAncienne) return '';
  const mots = new RegExp('$MOTS_RADIO', 'i');
  const listes = [...document.querySelectorAll('a[href*="/playlist/37i9dQZF1E"]')].map(a => ({
    name: (a.innerText || a.getAttribute('title') || '').trim().split('\n')[0],
    href: a.href.split('?')[0] }));
  const radios = listes.filter(p => mots.test(p.name));
  const exacte = radios.find(p => p.name.toLowerCase().includes(cle));
  return (exacte || radios[0] || {}).href || '';
})(%s)"""

    private fun charger(url: String) {
        val fait = CountDownLatch(1)
        ui.post {
            try {
                val w = vue()
                if (w.url == null) {
                    w.loadUrl(url)
                    fait.countDown()
                    return@post
                }
                w.evaluateJavascript("window.__mfAncienne = true") {
                    try { w.loadUrl(url) } catch (e: Exception) { e.printStackTrace() }
                    fait.countDown()
                }
            } catch (e: Exception) { e.printStackTrace(); fait.countDown() }
        }
        fait.await(5, TimeUnit.SECONDS)
    }

    private fun js(script: String): String {
        val lu = CountDownLatch(1)
        var resultat = ""
        ui.post {
            val w = webView
            if (w == null) { lu.countDown(); return@post }
            w.evaluateJavascript(script) { v ->
                resultat = try { org.json.JSONTokener(v ?: "").nextValue() as? String ?: "" } catch (e: Exception) { "" }
                lu.countDown()
            }
        }
        lu.await(3, TimeUnit.SECONDS)
        return resultat
    }

    private fun urlActuelle(): String = js("window.__mfAncienne ? '' : location.href")

    private fun attendre(script: String, timeoutMs: Long): String {
        val debut = System.currentTimeMillis()
        while (System.currentTimeMillis() - debut < timeoutMs) {
            Thread.sleep(600)
            val r = js(script)
            if (r.isNotEmpty()) return r
        }
        return ""
    }

    @JvmStatic
    fun radio(requete: String, genre: String, timeoutMs: Long): String {
        if (appContext == null || requete.isBlank()) return ""
        synchronized(verrou) {
            val q = requete.trim()
            val lien = Regex("open\\.spotify\\.com/(?:intl-[\\w-]+/)?(track|artist)/").find(q)
            val type = lien?.groupValues?.get(1) ?: (if (genre == "artist") "artist" else "track")
            var page = if (lien != null) q.substringBefore('?') else ""
            if (page.isEmpty()) {
                val onglet = if (type == "artist") "artists" else "tracks"
                charger("https://open.spotify.com/search/" + android.net.Uri.encode(q) + "/" + onglet)
                val sel = if (type == "artist") "main a[href*=\"/artist/\"]"
                          else "[data-testid=\"tracklist-row\"] a[href*=\"/track/\"]"
                page = attendre(JS_PREMIER_LIEN.format(org.json.JSONObject.quote(sel)), 25000)
            }
            if (page.isNotEmpty()) {
                for (essai in 0 until 2) {
                    charger(page)
                    val debut = System.currentTimeMillis()
                    while (System.currentTimeMillis() - debut < timeoutMs / 2) {
                        Thread.sleep(700)
                        val url = urlActuelle()
                        if (url.contains("/playlist/")) return url.substringBefore('?')
                        js(JS_MENU_RADIO)
                    }
                }
            }
            if (lien != null) return ""
            charger("https://open.spotify.com/search/" + android.net.Uri.encode("$q radio") + "/playlists")
            return attendre(JS_PLAYLISTS_RADIO.format(org.json.JSONObject.quote(q.lowercase())), 25000)
        }
    }
}
