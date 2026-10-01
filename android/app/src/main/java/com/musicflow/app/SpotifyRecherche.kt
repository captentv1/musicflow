package com.musicflow.app

import android.annotation.SuppressLint
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import android.webkit.WebViewClient
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Recherche Spotify sans connexion ni API : une WebView invisible charge la page de
 * recherche open.spotify.com et on lit les lignes de résultat dans le DOM — l'équivalent
 * Android du navigateur Edge piloté de la version PC (spotify_scan.py).
 *
 * Appelée depuis Python (Chaquopy) sur un thread du serveur Flask, jamais sur le thread UI.
 */
object SpotifyRecherche {
    private const val UA_BUREAU =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/124.0 Safari/537.36"

    // Même lecture par la structure des liens que _JS_RESULTATS côté PC.
    private const val JS_RESULTATS = """
(function(max){
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
        w.settings.blockNetworkImage = true // seules les URL des pochettes servent
        w.settings.loadWithOverviewMode = true
        w.settings.useWideViewPort = true
        w.webViewClient = WebViewClient()
        // Hors écran mais avec une vraie taille : Spotify ne rend pas les lignes dans une vue 0×0.
        w.layout(0, 0, 1280, 2000)
        webView = w
        return w
    }

    /** Retourne la liste des résultats en JSON ("[]" si rien ou si indisponible). */
    @JvmStatic
    fun chercher(requete: String, limite: Int, timeoutMs: Long): String {
        if (appContext == null || requete.isBlank()) return "[]"
        synchronized(verrou) {
            val url = "https://open.spotify.com/search/" +
                android.net.Uri.encode(requete.trim()) + "/tracks"
            val charge = CountDownLatch(1)
            ui.post {
                try { vue().loadUrl(url) } catch (e: Exception) { e.printStackTrace() }
                charge.countDown()
            }
            charge.await(5, TimeUnit.SECONDS)

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
                // On attend que la liste se stabilise (Spotify ajoute les lignes au fil du rendu)
                if (n > 0 && (n >= limite || n == precedent)) return resultat
                precedent = n
            }
            return dernier
        }
    }
}
