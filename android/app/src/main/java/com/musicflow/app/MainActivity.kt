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
import androidx.core.content.FileProvider
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.documentfile.provider.DocumentFile
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.net.HttpURLConnection
import java.net.Socket
import java.net.URL

class MainActivity : AppCompatActivity() {

    companion object {

        @Volatile var majSuivieParLaPage = false
        private const val SERVER_PORT = 5090
        private const val PREF_TREE_URI = "chosen_tree_uri"
        @Volatile private var serverStarted = false
        private const val PAGE_LOCALE = "http://127.0.0.1:5090/"
        private const val MAJ_AUTORISEE = "https://github.com/captentv1/musicflow/releases/download/"
        private val HOTES_CONNEXION = setOf("accounts.spotify.com", "accounts.google.com")

        val API_TOKEN: String by lazy {
            if (BuildConfig.CI_TOKEN.isNotEmpty()) return@lazy BuildConfig.CI_TOKEN
            val b = ByteArray(32).also { java.security.SecureRandom().nextBytes(it) }
            android.util.Base64.encodeToString(b,
                android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP or android.util.Base64.NO_PADDING)
        }
    }

    @Volatile private var pageLocale = true

    private fun estLocal(url: String?) = url?.startsWith(PAGE_LOCALE) == true

    private lateinit var webView: WebView
    private lateinit var loadingView: LinearLayout
    private val prefs by lazy { getSharedPreferences("musicflow", MODE_PRIVATE) }

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
        if (intent.getBooleanExtra(UpdateManager.EXTRA_INSTALLER, false)) installerMajTelechargee()
        lirePartage(intent)
        if (::webView.isInitialized) transmettrePartage()
    }
    private val fichierLauncher = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        fichierCallback?.onReceiveValue(if (uri != null) arrayOf(uri) else null)
        fichierCallback = null
    }

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
        if (intent?.getBooleanExtra(UpdateManager.EXTRA_INSTALLER, false) == true) installerMajTelechargee()
        appliquerMargesSysteme()
        SpotifySearch.init(this)
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
            override fun shouldOverrideUrlLoading(view: WebView?, request: android.webkit.WebResourceRequest?): Boolean {
                val url = request?.url ?: return false
                if (estLocal(url.toString()) || url.host in HOTES_CONNEXION) return false
                try { startActivity(Intent(Intent.ACTION_VIEW, url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) } catch (e: Exception) { }
                return true
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                pageLocale = estLocal(url)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                loadingView.visibility = LinearLayout.GONE
                transmettrePartage()
                ViewCompat.requestApplyInsets(findViewById(android.R.id.content))
            }
        }

        afficherInterfaceImmediatement()

        if (!serverStarted) {
            serverStarted = true
            Thread {
                if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
                startPythonServer()
            }.start()
        }
    }

    private fun appliquerMargesSysteme() {
        val racine = findViewById<FrameLayout>(android.R.id.content)
        ViewCompat.setOnApplyWindowInsetsListener(racine) { vue, insets ->
            val barres = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            vue.setPadding(barres.left, 0, barres.right, barres.bottom)

            val densite = resources.displayMetrics.density
            val hautCss = (barres.top / densite).toInt()
            webView.evaluateJavascript(
                "document.documentElement.style.setProperty('--marge-haut','${hautCss}px')", null
            )
            insets
        }
    }

    private fun afficherInterfaceImmediatement() {
        try {
            val html = assets.open("index.html").bufferedReader().use { it.readText() }
                .replaceFirst("<head>", "<head><script>window.MF_TOKEN='$API_TOKEN'</script>")
            webView.loadDataWithBaseURL(PAGE_LOCALE, html, "text/html", "utf-8", PAGE_LOCALE)
        } catch (e: Exception) {
            e.printStackTrace()
            Thread { waitForServerThenLoad() }.start()
        }
    }

    private fun startPythonServer() {
        try {
            val py = Python.getInstance()
            val module = py.getModule("mobile_app")

            val musicDir = getExternalFilesDir(Environment.DIRECTORY_MUSIC)
                ?: File(filesDir, "Music")
            musicDir.mkdirs()
            val configPath = File(filesDir, "config.json").absolutePath

            module.callAttr("configure", musicDir.absolutePath, configPath, API_TOKEN, BuildConfig.CI_TOKEN.isNotEmpty())
            module.callAttr("start_server")
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
        runOnUiThread { webView.loadUrl("http://127.0.0.1:$SERVER_PORT/") }
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

    private var apkEnAttente: File? = null

    private fun appelerPage(js: String) = runOnUiThread { webView.evaluateJavascript(js, null) }

    private fun suivreMaj() {
        if (majSuivieParLaPage) return
        majSuivieParLaPage = true
        Thread {
            try {
                var dernier = -2
                while (true) {
                    val etat = UpdateManager.etat(this) ?: throw Exception("download cancelled")
                    when (etat.first) {
                        android.app.DownloadManager.STATUS_SUCCESSFUL -> {
                            UpdateManager.termine(this)
                            val apk = UpdateManager.fichier(this)
                            UpdateManager.verifier(this, apk)
                            appelerPage("window.MF_majProgres && MF_majProgres(100)")
                            if (!auPremierPlan) {
                                UpdateManager.notifierPrete(this, packageManager.getPackageArchiveInfo(apk.path, 0)?.versionName ?: "")
                                return@Thread
                            }
                            apkEnAttente = apk
                            runOnUiThread { installerApk() }
                            return@Thread
                        }
                        android.app.DownloadManager.STATUS_FAILED -> { UpdateManager.termine(this); throw Exception("download failed") }
                        else -> if (etat.second != dernier) {
                            dernier = etat.second
                            appelerPage("window.MF_majProgres && MF_majProgres(${minOf(etat.second, 99)})")
                        }
                    }
                    Thread.sleep(500)
                }
            } catch (e: Exception) {
                appelerPage("window.MF_majErreur && MF_majErreur(${JSONObject.quote(e.message ?: e.toString())})")
            } finally {
                majSuivieParLaPage = false
            }
        }.start()
    }

    private fun installerMajTelechargee() {
        UpdateManager.effacerNotification(this)
        val apk = UpdateManager.fichier(this)
        try { UpdateManager.verifier(this, apk) } catch (e: Exception) {
            appelerPage("window.MF_majErreur && MF_majErreur(${JSONObject.quote(e.message ?: "")})"); return
        }
        apkEnAttente = apk
        installerApk()
    }

    private fun installerApk() {
        val apk = apkEnAttente ?: return
        if (!packageManager.canRequestPackageInstalls()) {
            appelerPage("window.MF_majProgres && MF_majProgres(-2)")
            startActivity(Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:$packageName")))
            return
        }
        apkEnAttente = null
        val uri = FileProvider.getUriForFile(this, "$packageName.fichiers", apk)
        startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    @Volatile private var auPremierPlan = false

    override fun onPause() {
        super.onPause()
        auPremierPlan = false
    }

    override fun onResume() {
        super.onResume()
        auPremierPlan = true
        val etat = UpdateManager.etat(this)
        if (etat != null && etat.first != android.app.DownloadManager.STATUS_FAILED) suivreMaj()
        if (apkEnAttente != null) {
            if (packageManager.canRequestPackageInstalls()) installerApk()
            else { apkEnAttente = null; appelerPage("window.MF_majErreur && MF_majErreur('permission to install apps refused')") }
        }
    }

    private fun nomSimple(nom: String) = nom.isNotBlank() && !nom.contains('/') && !nom.contains('\\') && nom != "." && nom != ".."

    private fun fichierDeLApp(chemin: String): Boolean = try {
        val f = File(chemin).canonicalPath
        listOfNotNull(getExternalFilesDir(Environment.DIRECTORY_MUSIC), File(filesDir, "Music"), cacheDir)
            .any { f.startsWith(it.canonicalPath + File.separator) }
    } catch (e: Exception) { false }

    inner class WebAppInterface {

        @JavascriptInterface
        fun isAndroidApp(): Boolean = true

        @JavascriptInterface
        fun premierLancement(): Boolean {
            if (prefs.getBoolean("deja_lance", false)) return false
            prefs.edit().putBoolean("deja_lance", true).apply()
            return true
        }

        @JavascriptInterface
        fun appVersion(): String = packageManager.getPackageInfo(packageName, 0).versionName ?: ""

        @JavascriptInterface
        fun setLightStatusBar(clair: Boolean) {
            runOnUiThread {
                androidx.core.view.WindowCompat.getInsetsController(window, window.decorView).apply {
                    isAppearanceLightStatusBars = clair
                    isAppearanceLightNavigationBars = clair
                }
            }
        }

        @JavascriptInterface
        fun installUpdate(apkUrl: String) {
            if (!pageLocale) return
            // Security: updates only from this repository's GitHub releases.
            if (!apkUrl.startsWith(MAJ_AUTORISEE) || !apkUrl.endsWith(".apk")) {
                appelerPage("window.MF_majErreur && MF_majErreur('download address refused')")
                return
            }
            UpdateManager.lancer(this@MainActivity, apkUrl)
            suivreMaj()
        }

        @JavascriptInterface
        fun updateReady(): String = if (pageLocale) UpdateManager.prete(this@MainActivity) else ""

        @JavascriptInterface
        fun installDownloadedUpdate() {
            if (!pageLocale) return
            runOnUiThread { installerMajTelechargee() }
        }

        @JavascriptInterface
        fun chooseFolder() {
            if (!pageLocale) return
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

        @JavascriptInterface
        fun openExternal(url: String) {
            if (!pageLocale) return
            if (Uri.parse(url).scheme?.lowercase() !in setOf("https", "http", "spotify", "deezer", "vnd.youtube")) return
            runOnUiThread {
                try { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                catch (e: Exception) { }
            }
        }

        @JavascriptInterface
        fun shareText(text: String) {
            if (!pageLocale) return
            runOnUiThread {
                try {
                    val i = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text)
                    startActivity(Intent.createChooser(i, "Partager").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                } catch (e: Exception) { }
            }
        }

        @JavascriptInterface
        fun startVoiceSearch(): Boolean {
            if (!pageLocale) return false
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

        @JavascriptInterface
        fun listChosenFolderDetails(): String {
            if (!pageLocale) return "[]"
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

        @JavascriptInterface
        fun copyFromChosenFolder(name: String): String {
            if (!pageLocale) return ""
            if (!nomSimple(name)) return ""
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return ""
            return try {
                val dir = DocumentFile.fromTreeUri(this@MainActivity, Uri.parse(uriStr)) ?: return ""
                val doc = dir.findFile(name) ?: return ""
                val dest = File(File(cacheDir, "edition").apply { mkdirs() }, name)
                contentResolver.openInputStream(doc.uri)?.use { inp -> dest.outputStream().use { inp.copyTo(it) } }
                dest.absolutePath
            } catch (e: Exception) { "" }
        }

        @JavascriptInterface
        fun deleteFromChosenFolder(name: String): Boolean {
            if (!pageLocale) return false
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return false
            return try {
                val dir = DocumentFile.fromTreeUri(this@MainActivity, Uri.parse(uriStr)) ?: return false
                dir.findFile(name)?.delete() ?: false
            } catch (e: Exception) { false }
        }

        @JavascriptInterface
        fun saveTextToChosenFolder(name: String, mime: String, text: String): String {
            if (!pageLocale) return ""
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
            if (!pageLocale) return "[]"
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

        @JavascriptInterface
        fun moveToChosenFolder(sourcePath: String, suggestedName: String): String {
            if (!pageLocale) return ""
            if (!fichierDeLApp(sourcePath) || !nomSimple(suggestedName)) return ""
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

                dir.findFile(suggestedName)?.delete()
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

        @JavascriptInterface
        fun scrapeSpotifyPlaylist(playlistUrl: String) {
            if (!pageLocale) return
            runOnUiThread {
                val wv = WebView(this@MainActivity)
                wv.settings.javaScriptEnabled = true
                wv.settings.domStorageEnabled = true
                CookieManager.getInstance().setAcceptCookie(true)
                CookieManager.getInstance().setAcceptThirdPartyCookies(wv, true)
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
                val metrics = resources.displayMetrics
                val root = findViewById<FrameLayout>(android.R.id.content)
                root.addView(wv, FrameLayout.LayoutParams(metrics.widthPixels, metrics.heightPixels))
                wv.translationX = (metrics.widthPixels * 3).toFloat()
                wv.loadUrl(playlistUrl)
            }
        }

        @JavascriptInterface
        fun startDownloadService() {
            val intent = Intent(this@MainActivity, DownloadForegroundService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent)
            else startService(intent)
            runOnUiThread { if (!reveilActif) { reveilActif = true; reveil.post(tic) } }
        }

        @JavascriptInterface
        fun isIgnoringBatteryOptimizations(): Boolean {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true
            val pm = getSystemService(POWER_SERVICE) as android.os.PowerManager
            return pm.isIgnoringBatteryOptimizations(packageName)
        }

        @JavascriptInterface
        fun requestIgnoreBatteryOptimizations() {
            if (!pageLocale) return
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

        @JavascriptInterface
        @JvmOverloads
        fun updateDownloadProgress(text: String, percent: Int = -1) {
            val intent = Intent(this@MainActivity, DownloadForegroundService::class.java)
                .putExtra(DownloadForegroundService.EXTRA_TEXT, text)
                .putExtra(DownloadForegroundService.EXTRA_PERCENT, percent)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(intent)
            else startService(intent)
        }

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

            let scroller = null;
            for (let wait = 0; wait < 80 && !scroller; wait++) {
              if (document.querySelectorAll('[data-testid="tracklist-row"]').length > 0) scroller = findScrollContainer();
              else {
                if (wait % 6 === 0) window.AndroidScraper.onProgress(0, 0);
                await new Promise(r => setTimeout(r, 500));
              }
            }
            if (!scroller) {
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

            const hauteur = scroller.scrollHeight;
            const visible = scroller.clientHeight || 600;
            const step = Math.max(120, visible - 150);
            const positions = [];
            for (let p = 0; p <= Math.max(hauteur - visible, 0) + step; p += step) positions.push(p);
            if (!positions.length) positions.push(0);

            const limite = Date.now() + 10 * 60 * 1000;
            let tick = 0;
            for (let tour = 1; tour <= 2; tour++) {
              for (const pos of positions) {
                if (Date.now() > limite) break;
                scroller.scrollTop = pos;
                await new Promise(r => setTimeout(r, tour === 1 ? 320 : 500));
                extractVisible(store);
                if (++tick % 3 === 0) window.AndroidScraper.onProgress(Object.keys(store).length, total);
                if (total && Object.keys(store).length >= total) break;
              }
              if (total && Object.keys(store).length >= total) break;
            }

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
