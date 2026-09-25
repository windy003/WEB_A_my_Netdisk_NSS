package com.netdisk.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.webkit.CookieManager
import android.webkit.URLUtil
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.appcompat.app.AppCompatActivity
import androidx.localbroadcastmanager.content.LocalBroadcastManager
import com.netdisk.app.services.AudioPlaybackService
import com.netdisk.app.storage.NetdiskDownloadManager
import com.netdisk.app.storage.PreferencesManager
import com.netdisk.app.ui.DownloadsActivity
import com.netdisk.app.ui.SettingsActivity
import com.netdisk.app.webview.JavaScriptBridge
import com.netdisk.app.webview.NetdiskWebChromeClient
import com.netdisk.app.webview.NetdiskWebViewClient

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var preferencesManager: PreferencesManager
    private lateinit var downloadManager: NetdiskDownloadManager
    private lateinit var webChromeClient: NetdiskWebChromeClient

    private val playbackStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                AudioPlaybackService.ACTION_PLAYBACK_STATE_CHANGED -> {
                    val state = intent.getStringExtra(AudioPlaybackService.EXTRA_STATE) ?: return
                    updateWebViewPlaybackState(state)
                }
                AudioPlaybackService.ACTION_PLAYBACK_ERROR -> {
                    notifyWebViewOfError()
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Initialize managers
        preferencesManager = PreferencesManager(this)
        downloadManager = NetdiskDownloadManager(this)

        // Check if this is first launch (no server configured)
        if (isFirstLaunch()) {
            // Redirect to settings to configure server
            startActivityForResult(Intent(this, SettingsActivity::class.java), REQUEST_INITIAL_SETUP)
            return
        }

        // Setup WebView
        webView = findViewById(R.id.webView)
        setupWebView()

        // Register broadcast receiver for playback state updates
        registerPlaybackReceiver()

        // Load server URL
        loadServerUrl()
    }

    private fun isFirstLaunch(): Boolean {
        // 直接检查用户是否已经配置过服务器
        return !preferencesManager.hasConfigured()
    }

    private fun setupWebView() {
        android.util.Log.d("MainActivity", "setupWebView() called")

        // 清除WebView缓存
        webView.clearCache(true)
        android.util.Log.d("MainActivity", "WebView cache cleared")

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true  // For localStorage
            databaseEnabled = true
            allowFileAccess = true
            allowContentAccess = true
            setSupportMultipleWindows(false)

            // 缓存模式
            cacheMode = WebSettings.LOAD_DEFAULT

            // Mixed content for HTTP servers
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            }

            // Enable zoom controls (optional)
            builtInZoomControls = true
            displayZoomControls = false

            // User agent (optional - can help with compatibility)
            userAgentString = "$userAgentString NetdiskAndroid/1.0"
        }

        android.util.Log.d("MainActivity", "WebView settings configured")

        // Set WebView clients
        webView.webViewClient = NetdiskWebViewClient(preferencesManager)
        webChromeClient = NetdiskWebChromeClient(this)
        webView.webChromeClient = webChromeClient

        android.util.Log.d("MainActivity", "WebView clients set")

        // Inject JavaScript bridge
        val jsBridge = JavaScriptBridge(this, downloadManager, preferencesManager)
        webView.addJavascriptInterface(jsBridge, "Android")

        android.util.Log.d("MainActivity", "JavaScript bridge injected")

        // Set download listener for handling file downloads
        webView.setDownloadListener { url, userAgent, contentDisposition, mimetype, contentLength ->
            android.util.Log.d("MainActivity", "Download requested: url=$url, mimetype=$mimetype")
            val filename = URLUtil.guessFileName(url, contentDisposition, mimetype)
            val netdiskPath = extractNetdiskPathFromUrl(url) ?: filename
            downloadManager.enqueueDownload(url, filename, netdiskPath)
        }

        android.util.Log.d("MainActivity", "Download listener set")

        // Restore cookies if available
        restoreCookies()

        android.util.Log.d("MainActivity", "setupWebView() completed")
    }

    /**
     * Recovers the file's netdisk-relative path (e.g. "1/2/3/file.ext") from a
     * "/download/<path>" URL, so the native download listener fallback can also
     * mirror the netdisk directory structure under Downloads/Netdisk.
     */
    private fun extractNetdiskPathFromUrl(url: String): String? {
        return try {
            val path = android.net.Uri.parse(url).path ?: return null
            val marker = "/download/"
            val idx = path.indexOf(marker)
            if (idx == -1) return null
            java.net.URLDecoder.decode(path.substring(idx + marker.length), "UTF-8")
        } catch (e: Exception) {
            null
        }
    }

    private fun loadServerUrl() {
        val serverUrl = preferencesManager.getServerUrl()
        android.util.Log.d("MainActivity", "=== 开始加载页面 ===")
        android.util.Log.d("MainActivity", "服务器URL: $serverUrl")
        android.util.Log.d("MainActivity", "URL类型: ${if (serverUrl.startsWith("https://")) "HTTPS" else if (serverUrl.startsWith("http://")) "HTTP" else "未知"}")

        if (!::webView.isInitialized) {
            android.util.Log.e("MainActivity", "ERROR: WebView not initialized when trying to load URL!")
            android.widget.Toast.makeText(this, "错误: WebView未初始化", android.widget.Toast.LENGTH_LONG).show()
            return
        }

        android.widget.Toast.makeText(this, "正在连接: $serverUrl", android.widget.Toast.LENGTH_LONG).show()
        android.util.Log.d("MainActivity", "WebView JavaScript enabled: ${webView.settings.javaScriptEnabled}")
        android.util.Log.d("MainActivity", "WebView DOM storage enabled: ${webView.settings.domStorageEnabled}")

        webView.loadUrl(serverUrl)
        android.util.Log.d("MainActivity", "WebView.loadUrl() called successfully")
        android.util.Log.d("MainActivity", "=== loadUrl调用完成，等待页面加载 ===")
    }

    private fun restoreCookies() {
        val savedCookies = preferencesManager.getAuthCookies()
        if (savedCookies != null) {
            val serverUrl = preferencesManager.getServerUrl()
            CookieManager.getInstance().setCookie(serverUrl, savedCookies)
        }
    }

    private fun registerPlaybackReceiver() {
        val filter = IntentFilter().apply {
            addAction(AudioPlaybackService.ACTION_PLAYBACK_STATE_CHANGED)
            addAction(AudioPlaybackService.ACTION_PLAYBACK_ERROR)
        }
        LocalBroadcastManager.getInstance(this).registerReceiver(playbackStateReceiver, filter)
    }

    private fun updateWebViewPlaybackState(stateJson: String) {
        // Call JavaScript function to update UI
        webView.post {
            webView.evaluateJavascript(
                "if (typeof window.onNativePlaybackStateChanged === 'function') { window.onNativePlaybackStateChanged('$stateJson'); }",
                null
            )
        }
    }

    private fun notifyWebViewOfError() {
        webView.post {
            webView.evaluateJavascript(
                "if (typeof window.onNativePlaybackError === 'function') { window.onNativePlaybackError(); }",
                null
            )
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_downloads -> {
                startActivity(Intent(this, DownloadsActivity::class.java))
                true
            }
            R.id.action_settings -> {
                startActivityForResult(Intent(this, SettingsActivity::class.java), REQUEST_SETTINGS_UPDATE)
                true
            }
            R.id.action_refresh -> {
                webView.reload()
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onBackPressed() {
        // 首先检查是否在全屏模式
        if (::webChromeClient.isInitialized && webChromeClient.isInFullscreen()) {
            webChromeClient.exitFullscreen()
            return
        }

        if (::webView.isInitialized) {
            // 尝试关闭全屏播放器
            webView.evaluateJavascript(
                """
                (function() {
                    var fullscreenPlayer = document.getElementById('fullscreenPlayer');
                    if (fullscreenPlayer && fullscreenPlayer.classList.contains('active')) {
                        closeFullscreenPlayer();
                        return true;
                    }
                    return false;
                })();
                """.trimIndent()
            ) { result ->
                android.util.Log.d("MainActivity", "Fullscreen player check result: $result")

                // 如果没有关闭全屏播放器，则执行正常的返回逻辑
                if (result == "false" || result == "null") {
                    if (webView.canGoBack()) {
                        webView.goBack()
                    } else {
                        super.onBackPressed()
                    }
                }
            }
        } else {
            super.onBackPressed()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)

        android.util.Log.d("MainActivity", "onActivityResult: requestCode=$requestCode, resultCode=$resultCode")

        when (requestCode) {
            REQUEST_INITIAL_SETUP -> {
                val isFirst = isFirstLaunch()
                android.util.Log.d("MainActivity", "isFirstLaunch=$isFirst, resultCode=$resultCode, RESULT_OK=$RESULT_OK")

                if (resultCode == RESULT_OK || !isFirst) {
                    // Setup completed, initialize WebView
                    android.util.Log.d("MainActivity", "Initializing WebView after settings...")

                    val webViewFromXml = findViewById<WebView>(R.id.webView)
                    if (webViewFromXml == null) {
                        android.util.Log.e("MainActivity", "ERROR: WebView is null from findViewById!")
                        android.widget.Toast.makeText(this, "错误: WebView未找到", android.widget.Toast.LENGTH_LONG).show()
                        return
                    }

                    webView = webViewFromXml
                    android.util.Log.d("MainActivity", "WebView found, setting up...")
                    setupWebView()
                    registerPlaybackReceiver()
                    loadServerUrl()
                } else {
                    // User cancelled initial setup, close app
                    android.util.Log.d("MainActivity", "User cancelled setup, closing app")
                    finish()
                }
            }
            REQUEST_SETTINGS_UPDATE -> {
                if (resultCode == RESULT_OK) {
                    // 服务器地址已更新，重新加载WebView
                    android.util.Log.d("MainActivity", "Settings updated, reloading server URL...")
                    if (::webView.isInitialized) {
                        restoreCookies()
                        loadServerUrl()
                    }
                }
            }
            else -> {
                // Handle file chooser result
                if (::webChromeClient.isInitialized) {
                    webChromeClient.onActivityResult(requestCode, resultCode, data)
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        LocalBroadcastManager.getInstance(this).unregisterReceiver(playbackStateReceiver)
    }

    override fun onResume() {
        super.onResume()

        android.util.Log.d("MainActivity", "onResume() called")
        android.util.Log.d("MainActivity", "webView initialized: ${::webView.isInitialized}")

        // If WebView is not initialized but server is configured, initialize it now
        if (!::webView.isInitialized && !isFirstLaunch()) {
            android.util.Log.d("MainActivity", "Initializing WebView in onResume")
            webView = findViewById(R.id.webView)
            setupWebView()
            registerPlaybackReceiver()
            loadServerUrl()
        }

        if (::webView.isInitialized) {
            android.util.Log.d("MainActivity", "Calling webView.onResume()")
            webView.onResume()
        } else {
            android.util.Log.d("MainActivity", "WebView not initialized in onResume")
        }
    }

    override fun onPause() {
        super.onPause()
        if (::webView.isInitialized) {
            webView.onPause()
        }
    }

    companion object {
        private const val REQUEST_INITIAL_SETUP = 1000
        private const val REQUEST_SETTINGS_UPDATE = 1001
    }
}
