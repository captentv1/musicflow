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
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
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

    // Native Android folder picker (Storage Access Framework): the user picks
    // any folder (Music, Downloads, an SD card…); the choice is stored
    // persistently (takePersistableUriPermission), even after the app restarts.
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

    // Notification permission (Android 13+) needed to show the foreground service notification
    // during a background download; just a request, the app works without it
    // (the download continues, only the progress notification does not appear).
    // File picker for the page's <input type="file"> (track import, restore)
    // Wakes the page every 3 s during a download: in the background, Android
    // heavily throttles the page's timers (the queue used to fall asleep).
    private val reveil = Handler(Looper.getMainLooper())
    private var reveilActif = false
    private val tic = object : Runnable {
        override fun run() {
            if (!reveilActif) return
            try { webView.evaluateJavascript("window.MF_tic && MF_tic()", null) } catch (e: Exception) { }
            reveil.postDelayed(this, 3000)
        }
    }

    private var fichierCallback: ValueCallback<Array<Uri>>? = null
    // Text shared from another app (Spotify/YouTube link…), passed to the page
    private var partageEnAttente: String? = null

    private fun lirePartage(intent: Intent?) {
        if (intent?.action == Intent.ACTION_SEND && intent.type?.startsWith("text/") == true) {
            partageEnAttente = intent.getStringExtra(Intent.EXTRA_TEXT)
        }
    }

    private fun transmettrePartage() {
        val texte = partageEnAttente ?: return
        partageEnAttente = null
        val js = org.json.JSONObject.quote(texte)
        webView.evaluateJavascript(
            "window.MF_partage ? MF_partage($js) : (window.__partageEnAttente = $js)", null)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        lirePartage(intent)
        if (::webView.isInitialized) transmettrePartage()
    }
    private val fichierLauncher = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        fichierCallback?.onReceiveValue(if (uri != null) arrayOf(uri) else null)
        fichierCallback = null
    }

    // Voice search: phone speech recognition, text sent back to the page (MF_voix)
    private val voixLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
        val texte = res.data?.getStringArrayListExtra(android.speech.RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()
        val js = org.json.JSONObject.quote(texte ?: "")
        webView.evaluateJavascript("window.MF_voix && MF_voix($js)", null)
    }

    private val notifPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        appliquerMargesSysteme()
        SpotifyRecherche.init(this)
        lirePartage(intent)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
        ) {
            notifPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        webView = findViewById(R.id.webview)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            webView.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false)
        }
        loadingView = findViewById(R.id.loading)

        webView.settings.javaScriptEnabled = true
        webView.settings.domStorageEnabled = true
        webView.settings.mediaPlaybackRequiresUserGesture = false
        webView.addJavascriptInterface(WebAppInterface(), "Android")
        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView?, callback: ValueCallback<Array<Uri>>?, params: FileChooserParams?
            ): Boolean {
                fichierCallback?.onReceiveValue(null)
                fichierCallback = callback
                return try { fichierLauncher.launch("*/*"); true } catch (e: Exception) { fichierCallback = null; false }
            }
        }
        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                loadingView.visibility = LinearLayout.GONE
                transmettrePartage()
                // The page has just been (re)loaded: it lost the CSS variable, so we give it
                // back, otherwise its content would slide under the status bar again.
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
     * Keeps the content clear of the system bars.
     *
     * Since targetSdk 35, Android enforces edge-to-edge display: without adjustment, the
     * logo overlaps the clock and the content goes under the navigation bar.
     * The sides and bottom are inset with native padding. The top is left
     * to the page: it extends its own top bar there, which therefore paints the
     * status bar area in the chosen theme's colors; the height is passed to it via CSS.
     */
    private fun appliquerMargesSysteme() {
        val racine = findViewById<FrameLayout>(android.R.id.content)
        ViewCompat.setOnApplyWindowInsetsListener(racine) { vue, insets ->
            val barres = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            // At the TOP, nothing is reserved: the page extends under the status bar and its
            // own top bar paints it in the chosen theme's color. A native margin
            // there would leave a black band, whatever the theme.
            // On the other sides, the margin keeps the content from going under the bars.
            vue.setPadding(barres.left, 0, barres.right, barres.bottom)

            // Height passed to the page: it uses it to offset its content.
            val densite = resources.displayMetrics.density
            val hautCss = (barres.top / densite).toInt()
            webView.evaluateJavascript(
                "document.documentElement.style.setProperty('--marge-haut','${hautCss}px')", null
            )
            insets
        }
    }

    /**
     * Shows the interface without waiting for the server.
     *
     * Previously we waited until Flask was listening, then loaded
     * http://127.0.0.1:5090/, hence several seconds of spinner on every
     * launch. The page is now read from the APK resources and shown
     * right away.
     *
     * The baseUrl is deliberately the server's: the page therefore inherits that
     * origin, and its “/api/...” calls stay SAME-origin. This avoids having to
     * allow cross-origin requests from file://, which would weaken the WebView
     * sandbox. The JavaScript simply waits for the server to respond before
     * its first calls.
     */
    private fun afficherInterfaceImmediatement() {
        try {
            val html = assets.open("index.html").bufferedReader().use { it.readText() }
            webView.loadDataWithBaseURL(
                "http://127.0.0.1:$SERVER_PORT/", html, "text/html", "utf-8", null
            )
        } catch (e: Exception) {
            // Unreadable resource: fall back to the old behavior rather than
            // leaving a blank page.
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
            module.callAttr("start_server") // blocking: runs for the whole life of the process
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
        runOnUiThread { webView.loadUrl("http://127.0.0.1:$SERVER_PORT/") } // last attempt
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

    /** JavaScript <-> Android bridge exposed to the web page as window.Android. */
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

        /** Opens a link in the matching app (Spotify, YouTube) or the browser. */
        @JavascriptInterface
        fun openExternal(url: String) {
            runOnUiThread {
                try { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                catch (e: Exception) { }
            }
        }

        /** Android share sheet (WhatsApp, Messages…). */
        @JavascriptInterface
        fun shareText(text: String) {
            runOnUiThread {
                try {
                    val i = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text)
                    startActivity(Intent.createChooser(i, "Partager").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                } catch (e: Exception) { }
            }
        }

        /** Starts speech recognition (true if available on the phone). */
        @JavascriptInterface
        fun startVoiceSearch(): Boolean {
            val intent = Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE_MODEL, android.speech.RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(android.speech.RecognizerIntent.EXTRA_PROMPT, "Say the title or the artist")
            if (intent.resolveActivity(packageManager) == null) return false
            runOnUiThread {
                try { voixLauncher.launch(intent) } catch (e: Exception) {
                    webView.evaluateJavascript("window.MF_voix && MF_voix('')", null)
                }
            }
            return true
        }

        /**
         * Names of the files already in the chosen folder (JSON), so we don't
         * re-download what we already have. Direct query on the folder's children:
         * DocumentFile.listFiles() would be very slow on a folder of thousands of tracks.
         * "[]" if no folder has been chosen or if it is unreadable.
         */
        /** Library: name, size and date of every file in the chosen folder (JSON). */
        @JavascriptInterface
        fun listChosenFolderDetails(): String {
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return "[]"
            return try {
                val tree = Uri.parse(uriStr)
                val enfants = android.provider.DocumentsContract.buildChildDocumentsUriUsingTree(
                    tree, android.provider.DocumentsContract.getTreeDocumentId(tree))
                val sortie = org.json.JSONArray()
                contentResolver.query(enfants, arrayOf(
                    android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                    android.provider.DocumentsContract.Document.COLUMN_SIZE,
                    android.provider.DocumentsContract.Document.COLUMN_LAST_MODIFIED), null, null, null)?.use { c ->
                    while (c.moveToNext()) {
                        sortie.put(org.json.JSONObject()
                            .put("nom", c.getString(0) ?: "")
                            .put("taille", c.getLong(1))
                            .put("date", c.getLong(2) / 1000))
                    }
                }
                sortie.toString()
            } catch (e: Exception) { "[]" }
        }

        /** Copies a file from the chosen folder into the app cache (to edit its tags).
         *  Returns the path of the copy, or "" if not found. It is then put back
         *  with moveToChosenFolder(path, name). */
        @JavascriptInterface
        fun copyFromChosenFolder(name: String): String {
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return ""
            return try {
                val dir = DocumentFile.fromTreeUri(this@MainActivity, Uri.parse(uriStr)) ?: return ""
                val doc = dir.findFile(name) ?: return ""
                val dest = File(File(cacheDir, "edition").apply { mkdirs() }, name)
                contentResolver.openInputStream(doc.uri)?.use { inp -> dest.outputStream().use { inp.copyTo(it) } }
                dest.absolutePath
            } catch (e: Exception) { "" }
        }

        /** Deletes a file from the chosen folder (after converting it to a lighter format). */
        @JavascriptInterface
        fun deleteFromChosenFolder(name: String): Boolean {
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return false
            return try {
                val dir = DocumentFile.fromTreeUri(this@MainActivity, Uri.parse(uriStr)) ?: return false
                dir.findFile(name)?.delete() ?: false
            } catch (e: Exception) { false }
        }

        /** Saves a text (CSV/JSON export) in the chosen folder. Returns the name, or "". */
        @JavascriptInterface
        fun saveTextToChosenFolder(name: String, mime: String, text: String): String {
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return ""
            return try {
                val dir = DocumentFile.fromTreeUri(this@MainActivity, Uri.parse(uriStr)) ?: return ""
                dir.findFile(name)?.delete()
                val doc = dir.createFile(mime, name) ?: return ""
                contentResolver.openOutputStream(doc.uri)?.use { it.write(text.toByteArray(Charsets.UTF_8)) }
                doc.name ?: name
            } catch (e: Exception) { "" }
        }

        @JavascriptInterface
        fun listChosenFolder(): String {
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return "[]"
            return try {
                val tree = Uri.parse(uriStr)
                val enfants = android.provider.DocumentsContract.buildChildDocumentsUriUsingTree(
                    tree, android.provider.DocumentsContract.getTreeDocumentId(tree))
                val noms = org.json.JSONArray()
                contentResolver.query(enfants,
                    arrayOf(android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME),
                    null, null, null)?.use { c ->
                    while (c.moveToNext()) c.getString(0)?.let { noms.put(it) }
                }
                noms.toString()
            } catch (e: Exception) {
                "[]"
            }
        }

        /**
         * Moves a downloaded file (in the app's private storage) to the folder chosen
         * by the user via chooseFolder(). Called automatically after each finished
         * download if a folder has been chosen. Returns the final file name, or "" if no
         * folder has been chosen (the file then stays in the app's default folder).
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
                    "flac" -> "audio/flac"
                    "wav" -> "audio/wav"
                    "lrc" -> "text/plain"
                    "opus" -> "audio/opus"
                    "webm" -> "audio/webm"
                    else -> "audio/mp4"
                }

                dir.findFile(suggestedName)?.delete() // avoids "(1)" duplicates on retest
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
         * Fetches ALL the tracks of a PUBLIC Spotify playlist by displaying the real
         * page (open.spotify.com) in a hidden WebView (1x1, embedded in the app, not a
         * separate tab) and scrolling it the way a person would, to read
         * each row on screen. No Spotify login needed for a public playlist.
         *
         * Asynchronous (does not block the calling page): progress is sent back via
         * window.onScrapeProgress(count, total) and the final result via
         * window.onScrapeComplete(jsonArray) on the main WebView.
         */
        @JavascriptInterface
        fun scrapeSpotifyPlaylist(playlistUrl: String) {
            runOnUiThread {
                val wv = WebView(this@MainActivity)
                wv.settings.javaScriptEnabled = true
                wv.settings.domStorageEnabled = true
                // Spotify's web player needs cookies to initialize; without
                // them the page may stay empty, which gives “No track read”.
                CookieManager.getInstance().setAcceptCookie(true)
                CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)
                // Desktop user agent: with the default Android UA, open.spotify.com serves its
                // mobile version, whose HTML structure (data-testid, aria-rowindex…) differs
                // from the desktop version the playlist reading script relies on.
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

                    /** Diagnostics: what the hidden WebView really sees, shown in
                     *  the interface. Without it, a failure doesn't tell whether it comes from page
                     *  loading, a login prompt, or reading the rows. */
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
                // Real (screen) size + VISIBLE visibility: Chromium (the WebView engine)
                // also follows the Page Visibility API and suspends rendering / the virtualized
                // list if the view is too small (1x1) or GONE/INVISIBLE, exactly
                // the same problem as a background Chrome tab. We simply move it off
                // screen by translation (translationX) so it stays invisible to the eye
                // without ever being “in the background” from the rendering engine's point of view.
                val metrics = resources.displayMetrics
                val root = findViewById<FrameLayout>(android.R.id.content)
                root.addView(wv, FrameLayout.LayoutParams(metrics.widthPixels, metrics.heightPixels))
                wv.translationX = (metrics.widthPixels * 3).toFloat()
                wv.loadUrl(playlistUrl)
            }
        }

        /**
         * Starts the foreground service (persistent notification) so that the
         * current download/transfer continues even if the app goes to the background.
         * Called from JS at the start of each download/transfer.
         */
        @JavascriptInterface
        fun startDownloadService() {
            val intent = Intent(this@MainActivity, DownloadForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent)
            else startService(intent)
            runOnUiThread { if (!reveilActif) { reveilActif = true; reveil.post(tic) } }
        }

        /** Is the app exempt from battery optimization (essential on Samsung)? */
        @JavascriptInterface
        fun isIgnoringBatteryOptimizations(): Boolean {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true
            val pm = getSystemService(POWER_SERVICE) as android.os.PowerManager
            return pm.isIgnoringBatteryOptimizations(packageName)
        }

        /** Opens the system prompt “Allow MusicFlow to run in the background”. */
        @JavascriptInterface
        fun requestIgnoreBatteryOptimizations() {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return
            runOnUiThread {
                try {
                    startActivity(Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:$packageName")))
                } catch (e: Exception) {
                    try { startActivity(Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) } catch (e2: Exception) { }
                }
            }
        }

        /** Updates the notification text (and progress bar, -1 = indeterminate). */
        @JavascriptInterface
        @JvmOverloads
        fun updateDownloadProgress(text: String, percent: Int = -1) {
            val intent = Intent(this@MainActivity, DownloadForegroundService::class.java)
                .putExtra(DownloadForegroundService.EXTRA_TEXT, text)
                .putExtra(DownloadForegroundService.EXTRA_PERCENT, percent)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent)
            else startService(intent)
        }

        /** Stops the service; called once all downloads/transfers are finished. */
        @JavascriptInterface
        fun stopDownloadService() {
            stopService(Intent(this@MainActivity, DownloadForegroundService::class.java))
            runOnUiThread { reveilActif = false; reveil.removeCallbacks(tic) }
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
            // The text varies with the account language (songs / titres / chansons…) and the
            // thousands separator may be a normal space, a non-breaking space or a comma.
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
              // Extraction by link STRUCTURE, not by text position.
              // Reading “the 2nd string of the row” is fragile: depending on width and the
              // presence of the “Explicit” badge, we got an artist or the “E” badge
              // instead of the title. The title has data-testid="internal-track-link", the
              // artists are /artist/ links: independent of language and layout.
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
            /* Wait for the track rows. 7.5 s was not enough: on a phone the Spotify page
               (heavy, rendered in JS) takes much longer than on PC. We wait
               up to 40 s while reporting the wait, instead of silently giving up. */
            let scroller = null;
            for (let wait = 0; wait < 80 && !scroller; wait++) {
              if (document.querySelectorAll('[data-testid="tracklist-row"]').length > 0) scroller = findScrollContainer();
              else {
                if (wait % 6 === 0) window.AndroidScraper.onProgress(0, 0);
                await new Promise(r => setTimeout(r, 500));
              }
            }
            if (!scroller) {
              // Report what the page really contains: without this, there is no way to know whether
              // it didn't load, Spotify asks for a login, or the structure changed.
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
            /* Scan by ABSOLUTE POSITIONS: same algorithm as the PC version
               (spotify_scan.py), where it reads 1047 tracks out of 1046, i.e. 100%.

               Two pitfalls measured on the real Spotify page:
               - scrollTop = scrollHeight jumps straight to the end: all the intermediate
                 tracks are lost;
               - the virtualized list reserves its full height from the start (~58,000 px
                 for 1046 tracks), so scrollHeight NEVER grows; a stop condition
                 waiting for it cuts the scan after two seconds.
               Since the total height is known in advance, the position of row N can be
               computed: we therefore visit fixed overlapping positions, instead of
               hoping a continuous scroll covers everything. Continuous scrolling
               stopped as soon as Spotify was slow to serve a batch (460, 545 out of 1046). */
            const hauteur = scroller.scrollHeight;
            const visible = scroller.clientHeight || 600;
            const step = Math.max(120, visible - 150);   // overlap between positions
            const positions = [];
            for (let p = 0; p <= Math.max(hauteur - visible, 0) + step; p += step) positions.push(p);
            if (!positions.length) positions.push(0);

            // Safeguard: a very long playlist must not keep the scan running
            // forever. Past the limit, we return what was read; the interface then shows
            // “Incomplete scan: X out of Y” rather than passing the list off as complete.
            const limite = Date.now() + 10 * 60 * 1000;
            let tick = 0;
            for (let tour = 1; tour <= 2; tour++) {   // 2nd pass: what had not loaded
              for (const pos of positions) {
                if (Date.now() > limite) break;
                scroller.scrollTop = pos;
                // Let the list render its rows: too fast, and we read positions
                // that are still empty (87% instead of 100% in PC tests).
                await new Promise(r => setTimeout(r, tour === 1 ? 320 : 500));
                extractVisible(store);
                if (++tick % 3 === 0) window.AndroidScraper.onProgress(Object.keys(store).length, total);
                if (total && Object.keys(store).length >= total) break;
              }
              if (total && Object.keys(store).length >= total) break;
            }

            /* Targeted catch-up: a few rows are often missing after the scan
               (a batch not yet rendered when we passed). Rather than redo everything,
               we jump straight to the computed position of each missing index. */
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
                // A batch can take several seconds to be served: giving up after
                // two passes left gaps (968 tracks out of 1046 measured on PC).
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
