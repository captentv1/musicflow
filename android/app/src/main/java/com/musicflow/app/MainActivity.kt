package com.musicflow.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.documentfile.provider.DocumentFile
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    companion object {
        private const val SERVER_PORT = 5090
        private const val PREF_TREE_URI = "chosen_tree_uri"
        @Volatile private var serverStarted = false
    }

    private lateinit var webView: WebView
    private lateinit var loadingView: LinearLayout
    private val prefs by lazy { getSharedPreferences("musicflow", MODE_PRIVATE) }

    // Sélecteur de dossier natif Android (Storage Access Framework) — l'utilisateur choisit
    // n'importe quel dossier (Musique, Téléchargements, une carte SD…), le choix est mémorisé
    // de façon persistante (takePersistableUriPermission) même après redémarrage de l'app.
    private val folderPickerLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val uri = result.data?.data
        if (result.resultCode == RESULT_OK && uri != null) {
            contentResolver.takePersistableUriPermission(
                uri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            )
            prefs.edit().putString(PREF_TREE_URI, uri.toString()).apply()
            notifyFolderChosen()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webview)
        loadingView = findViewById(R.id.loading)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.mediaPlaybackRequiresUserGesture = false
        webView.addJavascriptInterface(WebAppInterface(), "Android")
        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                loadingView.visibility = LinearLayout.GONE
            }
        }

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        if (!serverStarted) {
            serverStarted = true
            Thread { startPythonServer() }.start()
        }

        Thread { waitForServerThenLoad() }.start()
    }

    private fun startPythonServer() {
        try {
            val py = Python.getInstance()
            val module = py.getModule("app_mobile")

            val musicDir = getExternalFilesDir(Environment.DIRECTORY_MUSIC)
                ?: File(filesDir, "Music")
            musicDir.mkdirs()
            val configPath = File(filesDir, "config.json").absolutePath

            module.callAttr("configure", musicDir.absolutePath, configPath)
            module.callAttr("start_server") // bloquant — tourne pour toute la vie du process
        } catch (e: PyException) {
            e.printStackTrace()
        }
    }

    private fun waitForServerThenLoad() {
        var attempts = 0
        while (attempts < 100) {
            try {
                Socket("127.0.0.1", SERVER_PORT).close()
                runOnUiThread { webView.loadUrl("http://127.0.0.1:$SERVER_PORT/") }
                return
            } catch (e: Exception) {
                attempts++
                Thread.sleep(150)
            }
        }
        runOnUiThread { webView.loadUrl("http://127.0.0.1:$SERVER_PORT/") } // dernière tentative
    }

    private fun notifyFolderChosen() {
        val label = chosenFolderLabel()
        runOnUiThread {
            webView.evaluateJavascript(
                "window.onFolderChosen && window.onFolderChosen(${JSONObject.quote(label)})", null
            )
        }
    }

    private fun chosenFolderLabel(): String {
        val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return ""
        return try {
            DocumentFile.fromTreeUri(this, Uri.parse(uriStr))?.name ?: ""
        } catch (e: Exception) {
            ""
        }
    }

    /** Pont JavaScript <-> Android exposé à la page web sous window.Android. */
    inner class WebAppInterface {

        @JavascriptInterface
        fun isAndroidApp(): Boolean = true

        @JavascriptInterface
        fun chooseFolder() {
            val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
                addFlags(
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or
                        Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                        Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
                )
            }
            runOnUiThread { folderPickerLauncher.launch(intent) }
        }

        @JavascriptInterface
        fun getChosenFolderLabel(): String = chosenFolderLabel()

        /**
         * Déplace un fichier téléchargé (dans le stockage privé de l'app) vers le dossier choisi
         * par l'utilisateur via chooseFolder(). Appelé automatiquement après chaque téléchargement
         * terminé si un dossier a été choisi. Retourne le nom final du fichier, ou "" si aucun
         * dossier n'a été choisi (le fichier reste alors dans le dossier par défaut de l'app).
         */
        @JavascriptInterface
        fun moveToChosenFolder(sourcePath: String, suggestedName: String): String {
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return ""
            return try {
                val dir = DocumentFile.fromTreeUri(this@MainActivity, Uri.parse(uriStr)) ?: return ""
                val src = File(sourcePath)
                if (!src.exists()) return ""

                val mime = when (src.extension.lowercase()) {
                    "mp3" -> "audio/mpeg"
                    "opus" -> "audio/opus"
                    "webm" -> "audio/webm"
                    else -> "audio/mp4"
                }

                dir.findFile(suggestedName)?.delete() // évite les doublons "(1)" en cas de retest
                val newDoc = dir.createFile(mime, suggestedName) ?: return ""
                contentResolver.openOutputStream(newDoc.uri).use { out ->
                    FileInputStream(src).use { input -> input.copyTo(out!!) }
                }
                src.delete()
                newDoc.name ?: suggestedName
            } catch (e: Exception) {
                e.printStackTrace()
                ""
            }
        }

        /**
         * Récupère TOUS les morceaux d'une playlist Spotify PUBLIQUE en affichant la vraie
         * page (open.spotify.com) dans une WebView cachée (1x1, intégrée à l'app — pas un
         * onglet séparé) et en la faisant défiler comme le ferait une personne, pour lire
         * chaque ligne à l'écran. Aucune connexion Spotify requise pour une playlist publique.
         * Bloquant (jusqu'à 2 min pour une très grande playlist) — appelé depuis le JS de la page.
         * Retourne un JSON [{"title":..,"artist":..}, ...], ou "[]" en cas d'échec/timeout.
         */
        @JavascriptInterface
        fun scrapeSpotifyPlaylist(playlistUrl: String): String {
            val latch = CountDownLatch(1)
            val resultHolder = arrayOf("[]")
            var scraperWebView: WebView? = null

            runOnUiThread {
                val wv = WebView(this@MainActivity)
                scraperWebView = wv
                wv.settings.javaScriptEnabled = true
                wv.settings.domStorageEnabled = true
                wv.addJavascriptInterface(object {
                    @JavascriptInterface
                    fun onDone(json: String) {
                        resultHolder[0] = json
                        latch.countDown()
                    }
                }, "AndroidScraper")
                wv.webViewClient = object : WebViewClient() {
                    override fun onPageFinished(view: WebView?, url: String?) {
                        Handler(Looper.getMainLooper()).postDelayed({
                            wv.evaluateJavascript(SPOTIFY_SCRAPE_JS, null)
                        }, 2500)
                    }
                }
                // Attachée à la hiérarchie de vues (1x1, visible) pour que le moteur de rendu
                // la traite comme un contenu actif — pas mise en veille comme un onglet en arrière-plan.
                val root = findViewById<FrameLayout>(android.R.id.content)
                root.addView(wv, FrameLayout.LayoutParams(1, 1))
                wv.loadUrl(playlistUrl)
            }

            latch.await(120, TimeUnit.SECONDS)

            runOnUiThread {
                scraperWebView?.let { wv ->
                    (wv.parent as? android.view.ViewGroup)?.removeView(wv)
                    wv.destroy()
                }
            }
            return resultHolder[0]
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }
}

private const val SPOTIFY_SCRAPE_JS = """
        (function() {
          function findScrollContainer() {
            let el = document.querySelectorAll('[data-testid="tracklist-row"]')[0];
            for (let i = 0; i < 20 && el; i++) {
              if (getComputedStyle(el).overflowY === 'scroll') return el;
              el = el.parentElement;
            }
            return document.scrollingElement;
          }
          function extractVisible(store) {
            document.querySelectorAll('[data-testid="tracklist-row"]').forEach(row => {
              const parent = row.parentElement;
              const idx = parent && parent.getAttribute('aria-rowindex');
              if (!idx) return;
              const leaves = [];
              row.querySelectorAll('div, a, span').forEach(el => {
                if (el.children.length === 0 && el.textContent.trim()) leaves.push(el.textContent.trim());
              });
              if (leaves.length >= 3) store[idx] = { title: leaves[1], artist: leaves[2] };
            });
          }
          const store = {};
          (async () => {
            let scroller = null;
            for (let wait = 0; wait < 25 && !scroller; wait++) {
              if (document.querySelectorAll('[data-testid="tracklist-row"]').length > 0) scroller = findScrollContainer();
              else await new Promise(r => setTimeout(r, 300));
            }
            if (!scroller) { window.AndroidScraper.onDone('[]'); return; }
            extractVisible(store);
            let stableRounds = 0, lastTop = -1;
            for (let i = 0; i < 700; i++) {
              scroller.scrollTop += 2200;
              await new Promise(r => setTimeout(r, 180));
              extractVisible(store);
              if (scroller.scrollTop === lastTop) { stableRounds++; if (stableRounds > 4) break; }
              else stableRounds = 0;
              lastTop = scroller.scrollTop;
              if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 5) {
                await new Promise(r => setTimeout(r, 300));
                extractVisible(store);
                break;
              }
            }
            const arr = Object.keys(store).sort((a, b) => (+a) - (+b)).map(k => store[k]);
            window.AndroidScraper.onDone(JSON.stringify(arr));
          })();
        })();
        """
