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

        @Volatile var updateFollowedByPage = false
        private const val SERVER_PORT = 5090
        private const val PREF_TREE_URI = "chosen_tree_uri"
        @Volatile private var serverStarted = false
        private const val PAGE_LOCALE = "http://127.0.0.1:5090/"
        private const val ALLOWED_UPDATE_URL = "https://github.com/captentv1/musicflow/releases/download/"
        private val LOGIN_HOSTS = setOf("accounts.spotify.com", "accounts.google.com")

        val API_TOKEN: String by lazy {
            if (BuildConfig.CI_TOKEN.isNotEmpty()) return@lazy BuildConfig.CI_TOKEN
            val b = ByteArray(32).also { java.security.SecureRandom().nextBytes(it) }
            android.util.Base64.encodeToString(b,
                android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP or android.util.Base64.NO_PADDING)
        }
    }

    @Volatile private var isLocalPage = true

    private fun isLocal(url: String?) = url?.startsWith(PAGE_LOCALE) == true

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

    private val wakeHandler = Handler(Looper.getMainLooper())
    private var wakeActive = false
    private val tic = object : Runnable {
        override fun run() {
            if (!wakeActive) return
            try { webView.evaluateJavascript("window.MF_tic && MF_tic()", null) } catch (e: Exception) { }
            wakeHandler.postDelayed(this, 3000)
        }
    }

    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingShare: String? = null

    private fun readShare(intent: Intent?) {
        if (intent?.action == Intent.ACTION_SEND && intent.type?.startsWith("text/") == true) {
            pendingShare = intent.getStringExtra(Intent.EXTRA_TEXT)
        }
    }

    private fun sendShare() {
        val textValue = pendingShare ?: return
        pendingShare = null
        val js = org.json.JSONObject.quote(textValue)
        webView.evaluateJavascript(
            "window.MF_share ? MF_share($js) : (window.__pendingShare = $js)", null)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.getBooleanExtra(UpdateManager.EXTRA_INSTALLER, false)) installDownloadedApk()
        readShare(intent)
        if (::webView.isInitialized) sendShare()
    }
    private val fileLauncher = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        fileCallback?.onReceiveValue(if (uri != null) arrayOf(uri) else null)
        fileCallback = null
    }

    private val voiceLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { res ->
        val textValue = res.data?.getStringArrayListExtra(android.speech.RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()
        val js = org.json.JSONObject.quote(textValue ?: "")
        webView.evaluateJavascript("window.MF_voice && MF_voice($js)", null)
    }

    private val notifPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        if (intent?.getBooleanExtra(UpdateManager.EXTRA_INSTALLER, false) == true) installDownloadedApk()
        applySystemInsets()
        SpotifySearch.init(this)
        readShare(intent)

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
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try { fileLauncher.launch("*/*"); true } catch (e: Exception) { fileCallback = null; false }
            }
        }
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: android.webkit.WebResourceRequest?): Boolean {
                val url = request?.url ?: return false
                if (isLocal(url.toString()) || url.host in LOGIN_HOSTS) return false
                try { startActivity(Intent(Intent.ACTION_VIEW, url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) } catch (e: Exception) { }
                return true
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                isLocalPage = isLocal(url)
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                loadingView.visibility = LinearLayout.GONE
                sendShare()
                ViewCompat.requestApplyInsets(findViewById(android.R.id.content))
            }
        }

        showInterfaceNow()

        if (!serverStarted) {
            serverStarted = true
            Thread {
                if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
                startPythonServer()
            }.start()
        }
    }

    private fun applySystemInsets() {
        val rootView = findViewById<FrameLayout>(android.R.id.content)
        ViewCompat.setOnApplyWindowInsetsListener(rootView) { viewArg, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            viewArg.setPadding(bars.left, 0, bars.right, bars.bottom)

            val screenDensity = resources.displayMetrics.density
            val topCss = (bars.top / screenDensity).toInt()
            webView.evaluateJavascript(
                "document.documentElement.style.setProperty('--top-inset','${topCss}px')", null
            )
            insets
        }
    }

    private fun showInterfaceNow() {
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

    private var pendingApk: File? = null

    private fun callPage(js: String) = runOnUiThread { webView.evaluateJavascript(js, null) }

    private fun followUpdate() {
        if (updateFollowedByPage) return
        updateFollowedByPage = true
        Thread {
            try {
                var lastValue = -2
                while (true) {
                    val state = UpdateManager.state(this) ?: throw Exception("download cancelled")
                    when (state.first) {
                        android.app.DownloadManager.STATUS_SUCCESSFUL -> {
                            UpdateManager.finished(this)
                            val apk = UpdateManager.file(this)
                            UpdateManager.verify(this, apk)
                            callPage("window.MF_updateProgress && MF_updateProgress(100)")
                            if (!inForeground) {
                                UpdateManager.notifyReady(this, packageManager.getPackageArchiveInfo(apk.path, 0)?.versionName ?: "")
                                return@Thread
                            }
                            pendingApk = apk
                            runOnUiThread { installApk() }
                            return@Thread
                        }
                        android.app.DownloadManager.STATUS_FAILED -> { UpdateManager.finished(this); throw Exception("download failed") }
                        else -> if (state.second != lastValue) {
                            lastValue = state.second
                            callPage("window.MF_updateProgress && MF_updateProgress(${minOf(state.second, 99)})")
                        }
                    }
                    Thread.sleep(500)
                }
            } catch (e: Exception) {
                callPage("window.MF_updateError && MF_updateError(${JSONObject.quote(e.message ?: e.toString())})")
            } finally {
                updateFollowedByPage = false
            }
        }.start()
    }

    private fun installDownloadedApk() {
        UpdateManager.clearNotification(this)
        val apk = UpdateManager.file(this)
        try { UpdateManager.verify(this, apk) } catch (e: Exception) {
            callPage("window.MF_updateError && MF_updateError(${JSONObject.quote(e.message ?: "")})"); return
        }
        pendingApk = apk
        installApk()
    }

    private fun installApk() {
        val apk = pendingApk ?: return
        if (!packageManager.canRequestPackageInstalls()) {
            callPage("window.MF_updateProgress && MF_updateProgress(-2)")
            startActivity(Intent(android.provider.Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:$packageName")))
            return
        }
        pendingApk = null
        val uri = FileProvider.getUriForFile(this, "$packageName.fichiers", apk)
        startActivity(Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    @Volatile private var inForeground = false

    override fun onPause() {
        super.onPause()
        inForeground = false
    }

    override fun onResume() {
        super.onResume()
        inForeground = true
        val state = UpdateManager.state(this)
        if (state != null && state.first != android.app.DownloadManager.STATUS_FAILED) followUpdate()
        if (pendingApk != null) {
            if (packageManager.canRequestPackageInstalls()) installApk()
            else { pendingApk = null; callPage("window.MF_updateError && MF_updateError('permission to install apps refused')") }
        }
    }

    private fun isSimpleName(fileName: String) = fileName.isNotBlank() && !fileName.contains('/') && !fileName.contains('\\') && fileName != "." && fileName != ".."

    private fun isAppFile(filePath: String): Boolean = try {
        val f = File(filePath).canonicalPath
        listOfNotNull(getExternalFilesDir(Environment.DIRECTORY_MUSIC), File(filesDir, "Music"), cacheDir)
            .any { f.startsWith(it.canonicalPath + File.separator) }
    } catch (e: Exception) { false }

    inner class WebAppInterface {

        @JavascriptInterface
        fun isAndroidApp(): Boolean = true

        @JavascriptInterface
        fun isFirstLaunch(): Boolean {
            if (prefs.getBoolean("deja_lance", false)) return false
            prefs.edit().putBoolean("deja_lance", true).apply()
            return true
        }

        @JavascriptInterface
        fun appVersion(): String = packageManager.getPackageInfo(packageName, 0).versionName ?: ""

        @JavascriptInterface
        fun setLightStatusBar(light: Boolean) {
            runOnUiThread {
                androidx.core.view.WindowCompat.getInsetsController(window, window.decorView).apply {
                    isAppearanceLightStatusBars = light
                    isAppearanceLightNavigationBars = light
                }
            }
        }

        @JavascriptInterface
        fun installUpdate(apkUrl: String) {
            if (!isLocalPage) return
            // Security: updates only from this repository's GitHub releases.
            if (!apkUrl.startsWith(ALLOWED_UPDATE_URL) || !apkUrl.endsWith(".apk")) {
                callPage("window.MF_updateError && MF_updateError('download address refused')")
                return
            }
            UpdateManager.startDownload(this@MainActivity, apkUrl)
            followUpdate()
        }

        @JavascriptInterface
        fun updateReady(): String = if (isLocalPage) UpdateManager.readyVersion(this@MainActivity) else ""

        @JavascriptInterface
        fun installDownloadedUpdate() {
            if (!isLocalPage) return
            runOnUiThread { installDownloadedApk() }
        }

        @JavascriptInterface
        fun chooseFolder() {
            if (!isLocalPage) return
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
            if (!isLocalPage) return
            if (Uri.parse(url).scheme?.lowercase() !in setOf("https", "http", "spotify", "deezer", "vnd.youtube")) return
            runOnUiThread {
                try { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
                catch (e: Exception) { }
            }
        }

        @JavascriptInterface
        fun shareText(text: String) {
            if (!isLocalPage) return
            runOnUiThread {
                try {
                    val i = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text)
                    startActivity(Intent.createChooser(i, "Partager").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                } catch (e: Exception) { }
            }
        }

        @JavascriptInterface
        fun startVoiceSearch(): Boolean {
            if (!isLocalPage) return false
            val intent = Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE_MODEL, android.speech.RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(android.speech.RecognizerIntent.EXTRA_PROMPT, "Say the title or the artist")
            if (intent.resolveActivity(packageManager) == null) return false
            runOnUiThread {
                try { voiceLauncher.launch(intent) } catch (e: Exception) {
                    webView.evaluateJavascript("window.MF_voice && MF_voice('')", null)
                }
            }
            return true
        }

        @JavascriptInterface
        fun listChosenFolderDetails(): String {
            if (!isLocalPage) return "[]"
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return "[]"
            return try {
                val tree = Uri.parse(uriStr)
                val children = android.provider.DocumentsContract.buildChildDocumentsUriUsingTree(
                    tree, android.provider.DocumentsContract.getTreeDocumentId(tree))
                val output = org.json.JSONArray()
                contentResolver.query(children, arrayOf(
                    android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                    android.provider.DocumentsContract.Document.COLUMN_SIZE,
                    android.provider.DocumentsContract.Document.COLUMN_LAST_MODIFIED), null, null, null)?.use { c ->
                    while (c.moveToNext()) {
                        output.put(org.json.JSONObject()
                            .put("nom", c.getString(0) ?: "")
                            .put("taille", c.getLong(1))
                            .put("date", c.getLong(2) / 1000))
                    }
                }
                output.toString()
            } catch (e: Exception) { "[]" }
        }

        @JavascriptInterface
        fun copyFromChosenFolder(name: String): String {
            if (!isLocalPage) return ""
            if (!isSimpleName(name)) return ""
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
            if (!isLocalPage) return false
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return false
            return try {
                val dir = DocumentFile.fromTreeUri(this@MainActivity, Uri.parse(uriStr)) ?: return false
                dir.findFile(name)?.delete() ?: false
            } catch (e: Exception) { false }
        }

        @JavascriptInterface
        fun saveTextToChosenFolder(name: String, mime: String, text: String): String {
            if (!isLocalPage) return ""
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
            if (!isLocalPage) return "[]"
            val uriStr = prefs.getString(PREF_TREE_URI, null) ?: return "[]"
            return try {
                val tree = Uri.parse(uriStr)
                val children = android.provider.DocumentsContract.buildChildDocumentsUriUsingTree(
                    tree, android.provider.DocumentsContract.getTreeDocumentId(tree))
                val names = org.json.JSONArray()
                contentResolver.query(children,
                    arrayOf(android.provider.DocumentsContract.Document.COLUMN_DISPLAY_NAME),
                    null, null, null)?.use { c ->
                    while (c.moveToNext()) c.getString(0)?.let { names.put(it) }
                }
                names.toString()
            } catch (e: Exception) {
                "[]"
            }
        }

        @JavascriptInterface
        fun moveToChosenFolder(sourcePath: String, suggestedName: String): String {
            if (!isLocalPage) return ""
            if (!isAppFile(sourcePath) || !isSimpleName(suggestedName)) return ""
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
            if (!isLocalPage) return
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
            runOnUiThread { if (!wakeActive) { wakeActive = true; wakeHandler.post(tic) } }
        }

        @JavascriptInterface
        fun isIgnoringBatteryOptimizations(): Boolean {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true
            val pm = getSystemService(POWER_SERVICE) as android.os.PowerManager
            return pm.isIgnoringBatteryOptimizations(packageName)
        }

        @JavascriptInterface
        fun requestIgnoreBatteryOptimizations() {
            if (!isLocalPage) return
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
            runOnUiThread { wakeActive = false; wakeHandler.removeCallbacks(tic) }
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
            const m = document.body.innerText.match(/([\d][\d\s ,.]*)\s*(songs?|titlesList?|chansons?)/i);
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
              const titleLink = row.querySelector('[data-testid="internal-track-link"]')
                             || row.querySelector('a[href*="/track/"]');
              const titleText = titleLink ? titleLink.textContent.trim() : '';
              const artistNames = [...row.querySelectorAll('a[href*="/artist/"]')]
                                 .map(a => a.textContent.trim()).filter(Boolean).join(', ');
              if (titleText) store[idx] = { title: titleText, artist: artistNames };
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
                titleText: document.title.slice(0, 80),
                rows: document.querySelectorAll('[data-testid="tracklist-row"]').length,
                grids: document.querySelectorAll('[role="grid"]').length,
                text_size: txt.length,
                login_requested: /\b(log in|se connecter|sign up)\b/i.test(txt),
                text_start: txt.slice(0, 200).replace(/\s+/g, ' ')
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

            const limitN = Date.now() + 10 * 60 * 1000;
            let tick = 0;
            for (let tour = 1; tour <= 2; tour++) {
              for (const pos of positions) {
                if (Date.now() > limitN) break;
                scroller.scrollTop = pos;
                await new Promise(r => setTimeout(r, tour === 1 ? 320 : 500));
                extractVisible(store);
                if (++tick % 3 === 0) window.AndroidScraper.onProgress(Object.keys(store).length, total);
                if (total && Object.keys(store).length >= total) break;
              }
              if (total && Object.keys(store).length >= total) break;
            }

            if (total && Object.keys(store).length < total) {
              const perScreen = Math.max(1, Math.floor(document.querySelectorAll('[data-testid="tracklist-row"]').length / 2));
              let passesWithoutGain = 0;
              for (let passe = 0; passe < 30; passe++) {
                const before = Object.keys(store).length;
                const missing = [];
                for (let i = 1; i <= total + 1; i++) if (!(i in store)) missing.push(i);
                if (!missing.length) break;
                for (let k = 0; k < missing.length; k += perScreen) {
                  const pos = Math.max(0, Math.round((missing[k] / total) * hauteur) - Math.floor(visible / 2));
                  scroller.scrollTop = pos;
                  await new Promise(r => setTimeout(r, 750));
                  extractVisible(store);
                }
                window.AndroidScraper.onProgress(Object.keys(store).length, total);
                if (Object.keys(store).length >= total) break;
                if (Object.keys(store).length === before) {
                  if (++passesWithoutGain >= 5) break;
                } else {
                  passesWithoutGain = 0;
                }
              }
            }
            const arr = Object.keys(store).sort((a, b) => (+a) - (+b)).map(k => store[k]);
            window.AndroidScraper.onProgress(arr.length, total);
            window.AndroidScraper.onDone(JSON.stringify(arr));
          })();
        })();
        """
