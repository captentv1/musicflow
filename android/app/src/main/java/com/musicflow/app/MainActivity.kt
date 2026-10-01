package com.musicflow.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.view.KeyEvent
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.documentfile.provider.DocumentFile
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.net.Socket

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

    // Notification (Android 13+) requise pour afficher la notif du service de premier plan
    // pendant un téléchargement en arrière-plan ; simple demande, l'app fonctionne sans
    // (le téléchargement continue, seule la notification de progression n'apparaît pas).
    private val notifPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        appliquerMargesSysteme()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
        ) {
            notifPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        webView = findViewById(R.id.webview)
        loadingView = findViewById(R.id.loading)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.mediaPlaybackRequiresUserGesture = false
        webView.addJavascriptInterface(WebAppInterface(), "Android")
        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                loadingView.visibility = LinearLayout.GONE
                // La page vient d'être (re)chargée : elle a perdu la variable CSS, on la
                // redonne, sinon son contenu repasserait sous la barre d'état.
                ViewCompat.requestApplyInsets(findViewById(android.R.id.content))
            }
        }

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }

        if (!serverStarted) {
            serverStarted = true
            Thread { startPythonServer() }.start()
        }

        afficherInterfaceImmediatement()
    }

    /**
     * Écarte le contenu des barres système.
     *
     * Depuis targetSdk 35, Android impose l'affichage bord à bord : sans ajustement, le
     * logo se superpose à l'horloge et le contenu passe sous la barre de navigation.
     * Les côtés et le bas sont écartés par un remplissage natif. Le haut, lui, est laissé
     * à la page : elle y étend sa propre barre supérieure, qui peint donc la zone de la
     * barre d'état aux couleurs du thème choisi — la hauteur lui est transmise en CSS.
     */
    private fun appliquerMargesSysteme() {
        val racine = findViewById<FrameLayout>(android.R.id.content)
        ViewCompat.setOnApplyWindowInsetsListener(racine) { vue, insets ->
            val barres = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            // En HAUT, on ne réserve rien : la page s'étend sous la barre d'état et sa
            // propre barre supérieure la peint à la couleur du thème choisi. Une marge
            // native y laisserait une bande noire, quel que soit le thème.
            // Sur les autres côtés, la marge évite que le contenu passe sous les barres.
            vue.setPadding(barres.left, 0, barres.right, barres.bottom)

            // Hauteur transmise à la page : elle s'en sert pour décaler son contenu.
            val densite = resources.displayMetrics.density
            val hautCss = (barres.top / densite).toInt()
            webView.evaluateJavascript(
                "document.documentElement.style.setProperty('--marge-haut','${hautCss}px')", null
            )
            insets
        }
    }

    /**
     * Affiche l'interface sans attendre le serveur.
     *
     * Auparavant on patientait jusqu'à ce que Flask écoute, puis on chargeait
     * http://127.0.0.1:5090/ — d'où plusieurs secondes de roue d'attente à chaque
     * ouverture. La page est maintenant lue depuis les ressources de l'APK et affichée
     * tout de suite.
     *
     * Le baseUrl est volontairement celui du serveur : la page hérite donc de cette
     * origine, et ses appels « /api/... » restent de MÊME origine. Cela évite d'avoir à
     * autoriser les requêtes inter-origines depuis file://, ce qui affaiblirait le bac à
     * sable de la WebView. Le JavaScript attend simplement que le serveur réponde avant
     * ses premiers appels.
     */
    private fun afficherInterfaceImmediatement() {
        try {
            val html = assets.open("index.html").bufferedReader().use { it.readText() }
            webView.loadDataWithBaseURL(
                "http://127.0.0.1:$SERVER_PORT/", html, "text/html", "utf-8", null
            )
        } catch (e: Exception) {
            // Ressource illisible : on retombe sur l'ancien comportement plutôt que
            // de laisser une page blanche.
            e.printStackTrace()
            Thread { waitForServerThenLoad() }.start()
        }
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
         *
         * Asynchrone (ne bloque pas la page appelante) : la progression est renvoyée via
         * window.onScrapeProgress(count, total) et le résultat final via
         * window.onScrapeComplete(jsonArray) sur la WebView principale.
         */
        @JavascriptInterface
        fun scrapeSpotifyPlaylist(playlistUrl: String) {
            runOnUiThread {
                val wv = WebView(this@MainActivity)
                wv.settings.javaScriptEnabled = true
                wv.settings.domStorageEnabled = true
                // Le lecteur web de Spotify a besoin des cookies pour s'initialiser ; sans
                // eux la page peut rester vide, ce qui donne « Aucun morceau lu ».
                CookieManager.getInstance().setAcceptCookie(true)
                CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)
                // User-agent bureau : avec l'UA Android par défaut, open.spotify.com sert sa
                // version mobile, dont la structure HTML (data-testid, aria-rowindex…) diffère
                // de la version bureau sur laquelle repose le script de lecture de la playlist.
                wv.settings.userAgentString =
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                wv.addJavascriptInterface(object {
                    @JavascriptInterface
                    fun onProgress(count: Int, total: Int) {
                        runOnUiThread {
                            webView.evaluateJavascript(
                                "window.onScrapeProgress && window.onScrapeProgress($count, $total)", null
                            )
                        }
                    }

                    /** Diagnostic : ce que la WebView cachée voit réellement, affiché dans
                     *  l'interface. Sans ça, un échec ne dit pas s'il vient du chargement de
                     *  la page, d'une demande de connexion, ou de la lecture des lignes. */
                    @JavascriptInterface
                    fun onDiag(info: String) {
                        runOnUiThread {
                            webView.evaluateJavascript(
                                "window.onScrapeDiag && window.onScrapeDiag(${JSONObject.quote(info)})", null
                            )
                        }
                    }

                    @JavascriptInterface
                    fun onDone(json: String) {
                        runOnUiThread {
                            webView.evaluateJavascript(
                                "window.onScrapeComplete && window.onScrapeComplete(${JSONObject.quote(json)})", null
                            )
                            (wv.parent as? android.view.ViewGroup)?.removeView(wv)
                            wv.destroy()
                        }
                    }
                }, "AndroidScraper")
                wv.webViewClient = object : WebViewClient() {
                    override fun onPageFinished(view: WebView?, url: String?) {
                        Handler(Looper.getMainLooper()).postDelayed({
                            wv.evaluateJavascript(SPOTIFY_SCRAPE_JS, null)
                        }, 2500)
                    }
                }
                // Taille réelle (écran) + visibilité VISIBLE : Chromium (le moteur de la WebView)
                // suit lui aussi la Page Visibility API et met en veille le rendu/la liste
                // virtualisée si la vue est trop petite (1x1) ou en GONE/INVISIBLE — exactement
                // le même problème qu'un onglet Chrome en arrière-plan. On la sort simplement de
                // l'écran par translation (translationX) pour qu'elle reste invisible à l'œil
                // sans jamais être « en arrière-plan » du point de vue du moteur de rendu.
                val metrics = resources.displayMetrics
                val root = findViewById<FrameLayout>(android.R.id.content)
                root.addView(wv, FrameLayout.LayoutParams(metrics.widthPixels, metrics.heightPixels))
                wv.translationX = (metrics.widthPixels * 3).toFloat()
                wv.loadUrl(playlistUrl)
            }
        }

        /**
         * Démarre le service de premier plan (notification persistante) pour que le
         * téléchargement/transfert en cours continue même si l'app passe en arrière-plan.
         * Appelé depuis le JS au début de chaque téléchargement/transfert.
         */
        @JavascriptInterface
        fun startDownloadService() {
            val intent = Intent(this@MainActivity, DownloadForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent)
            else startService(intent)
        }

        /** Met à jour le texte (et la barre de progression, -1 = indéterminée) de la notification. */
        @JavascriptInterface
        @JvmOverloads
        fun updateDownloadProgress(text: String, percent: Int = -1) {
            val intent = Intent(this@MainActivity, DownloadForegroundService::class.java)
                .putExtra(DownloadForegroundService.EXTRA_TEXT, text)
                .putExtra(DownloadForegroundService.EXTRA_PERCENT, percent)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent)
            else startService(intent)
        }

        /** Arrête le service — appelé une fois tous les téléchargements/transferts terminés. */
        @JavascriptInterface
        fun stopDownloadService() {
            stopService(Intent(this@MainActivity, DownloadForegroundService::class.java))
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
          function extractTotal() {
            // Le texte varie selon la langue du compte (songs / titres / chansons…) et le
            // séparateur de milliers peut être une espace normale, insécable ou une virgule.
            const m = document.body.innerText.match(/([\d][\d\s ,.]*)\s*(songs?|titres?|chansons?)/i);
            return m ? parseInt(m[1].replace(/[^\d]/g, ''), 10) : 0;
          }
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
              // Extraction par la STRUCTURE des liens, pas par la position des textes.
              // Lire « la 2e chaîne de la ligne » est fragile : selon la largeur et la
              // présence du badge « Explicite », on récupérait un artiste ou le badge « E »
              // à la place du titre. Le titre porte data-testid="internal-track-link", les
              // artistes sont des liens /artist/ — insensible à la langue et à la mise en page.
              const lienTitre = row.querySelector('[data-testid="internal-track-link"]')
                             || row.querySelector('a[href*="/track/"]');
              const titre = lienTitre ? lienTitre.textContent.trim() : '';
              const artistes = [...row.querySelectorAll('a[href*="/artist/"]')]
                                 .map(a => a.textContent.trim()).filter(Boolean).join(', ');
              if (titre) store[idx] = { title: titre, artist: artistes };
            });
          }
          const store = {};
          (async () => {
            /* Attente des lignes de piste. 7,5 s ne suffisaient pas : sur téléphone la page
               Spotify (lourde, rendue en JS) met bien plus longtemps que sur PC. On patiente
               jusqu'à 40 s en signalant l'attente, au lieu d'abandonner en silence. */
            let scroller = null;
            for (let wait = 0; wait < 80 && !scroller; wait++) {
              if (document.querySelectorAll('[data-testid="tracklist-row"]').length > 0) scroller = findScrollContainer();
              else {
                if (wait % 6 === 0) window.AndroidScraper.onProgress(0, 0);
                await new Promise(r => setTimeout(r, 500));
              }
            }
            if (!scroller) {
              // Rapporter ce que la page contient vraiment : sans ça, impossible de savoir si
              // elle n'a pas chargé, si Spotify demande une connexion, ou si la structure a changé.
              const txt = (document.body && document.body.innerText) || '';
              window.AndroidScraper.onDiag(JSON.stringify({
                url: location.href.slice(0, 120),
                titre: document.title.slice(0, 80),
                lignes: document.querySelectorAll('[data-testid="tracklist-row"]').length,
                grilles: document.querySelectorAll('[role="grid"]').length,
                taille_texte: txt.length,
                connexion_demandee: /\b(log in|se connecter|sign up)\b/i.test(txt),
                debut_texte: txt.slice(0, 200).replace(/\s+/g, ' ')
              }));
              window.AndroidScraper.onDone('[]');
              return;
            }
            const total = extractTotal();
            extractVisible(store);
            window.AndroidScraper.onProgress(Object.keys(store).length, total);
            /* Balayage par POSITIONS ABSOLUES — même algorithme que la version PC
               (spotify_scan.py), où il lit 1047 titres sur 1046, soit 100 %.

               Deux pièges mesurés sur la vraie page Spotify :
               - scrollTop = scrollHeight saute directement à la fin : tous les titres
                 intermédiaires sont perdus ;
               - la liste virtualisée réserve toute sa hauteur dès le départ (~58 000 px
                 pour 1046 titres), donc scrollHeight ne grandit JAMAIS — une condition
                 d'arrêt qui l'attend coupe le scan au bout de deux secondes.
               Comme la hauteur totale est connue d'avance, la position de la ligne N est
               calculable : on visite donc des positions fixes qui se chevauchent, au lieu
               d'espérer qu'un défilement continu couvre tout. Le défilement continu
               s'arrêtait dès que Spotify tardait à servir un lot (460, 545 sur 1046). */
            const hauteur = scroller.scrollHeight;
            const visible = scroller.clientHeight || 600;
            const step = Math.max(120, visible - 150);   // chevauchement entre positions
            const positions = [];
            for (let p = 0; p <= Math.max(hauteur - visible, 0) + step; p += step) positions.push(p);
            if (!positions.length) positions.push(0);

            // Garde-fou : une playlist très longue ne doit pas faire tourner le scan
            // indéfiniment. Au-delà, on rend ce qui a été lu — l'interface annonce alors
            // « Scan incomplet : X sur Y » plutôt que de faire passer la liste pour entière.
            const limite = Date.now() + 10 * 60 * 1000;
            let tick = 0;
            for (let tour = 1; tour <= 2; tour++) {   // 2e passe : ce qui n'avait pas chargé
              for (const pos of positions) {
                if (Date.now() > limite) break;
                scroller.scrollTop = pos;
                // Laisser la liste rendre ses lignes : trop vite, on lit des positions
                // encore vides (87 % au lieu de 100 % lors des essais sur PC).
                await new Promise(r => setTimeout(r, tour === 1 ? 320 : 500));
                extractVisible(store);
                if (++tick % 3 === 0) window.AndroidScraper.onProgress(Object.keys(store).length, total);
                if (total && Object.keys(store).length >= total) break;
              }
              if (total && Object.keys(store).length >= total) break;
            }

            /* Rattrapage ciblé : il manque souvent quelques lignes après le balayage
               (un lot pas encore rendu au moment du passage). Plutôt que tout refaire,
               on saute directement à la position calculée de chaque index absent. */
            if (total && Object.keys(store).length < total) {
              const parEcran = Math.max(1, Math.floor(document.querySelectorAll('[data-testid="tracklist-row"]').length / 2));
              let passesSansGain = 0;
              for (let passe = 0; passe < 30; passe++) {
                const avant = Object.keys(store).length;
                const manquants = [];
                for (let i = 1; i <= total + 1; i++) if (!(i in store)) manquants.push(i);
                if (!manquants.length) break;
                for (let k = 0; k < manquants.length; k += parEcran) {
                  const pos = Math.max(0, Math.round((manquants[k] / total) * hauteur) - Math.floor(visible / 2));
                  scroller.scrollTop = pos;
                  await new Promise(r => setTimeout(r, 750));
                  extractVisible(store);
                }
                window.AndroidScraper.onProgress(Object.keys(store).length, total);
                if (Object.keys(store).length >= total) break;
                // Un lot peut mettre plusieurs secondes à être servi : renoncer au bout
                // de deux passes laissait des trous (968 titres sur 1046 mesurés sur PC).
                if (Object.keys(store).length === avant) {
                  if (++passesSansGain >= 5) break;
                } else {
                  passesSansGain = 0;
                }
              }
            }
            const arr = Object.keys(store).sort((a, b) => (+a) - (+b)).map(k => store[k]);
            window.AndroidScraper.onProgress(arr.length, total);
            window.AndroidScraper.onDone(JSON.stringify(arr));
          })();
        })();
        """
