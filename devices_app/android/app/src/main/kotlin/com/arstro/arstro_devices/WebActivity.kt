package com.arstro.arstro_devices

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Base64
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebStorage
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.ProgressBar
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import com.arstro.arstro_devices.core.Hub
import com.arstro.arstro_devices.core.Stream
import org.json.JSONObject
import java.io.File
import java.io.OutputStream
import java.net.URI
import java.util.concurrent.Executors

/*
 * One port of one device in a WebView (MGR-04): the page comes from the local gateway
 * (http://<label>.localhost:<port>/), so it works like on the device's own network - logins,
 * WebSockets, uploads, downloads, full screen. Links to other sites open in the browser.
 */
class WebActivity : ComponentActivity() {
    companion object {
        fun intent(ctx: Context, sid: String, device: String, name: String, port: Int, url: String, host: String): Intent =
            Intent(ctx, WebActivity::class.java).putExtra("sid", sid).putExtra("device", device).putExtra("name", name)
                .putExtra("port", port).putExtra("url", url).putExtra("host", host)

        // the Arstro tokens (docs/design.md): bg, line, surface-3, text, muted, primary
        private const val BG = 0xFF0C0C0B.toInt()
        private const val LINE = 0xFF242422.toInt()
        private const val TOAST = 0xFF222220.toInt()
        private const val TEXT = 0xFFEDEDEA.toInt()
        private const val DIM = 0xFF868379.toInt()
        private const val PRIMARY = 0xFFEDEDEA.toInt()

        /** Runs first in every page of the gateway's origin: <a download> of blob:/data: URLs (the web
         *  UI's log and CSV exports) go to the app, which a WebView cannot save by itself. */
        private val DOWNLOAD_SCRIPT = """
            (() => {
              if (window.__arstroApp) return; window.__arstroApp = true;
              const blobs = new Map();
              const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
              URL.createObjectURL = (o) => { const u = create(o); if (o instanceof Blob) blobs.set(u, o); return u; };
              URL.revokeObjectURL = (u) => setTimeout(() => { blobs.delete(u); revoke(u); }, 60000);
              async function save(a) {
                let blob = blobs.get(a.href);
                if (!blob) { try { blob = await (await fetch(a.href)).blob(); } catch (e) { return; } }
                const id = Math.random().toString(36).slice(2);
                const name = a.getAttribute('download') || 'download';
                ArstroApp.postMessage(JSON.stringify({ t: 'start', id, name, type: blob.type || 'application/octet-stream', size: blob.size }));
                for (let i = 0; i < blob.size; i += 393216) {
                  const b = new Uint8Array(await blob.slice(i, i + 393216).arrayBuffer());
                  let s = '';
                  for (let j = 0; j < b.length; j += 32768) s += String.fromCharCode.apply(null, b.subarray(j, j + 32768));
                  ArstroApp.postMessage(JSON.stringify({ t: 'chunk', id, data: btoa(s) }));
                }
                ArstroApp.postMessage(JSON.stringify({ t: 'end', id }));
              }
              const local = (a) => a && a.href && /^(blob|data):/.test(a.href) && a.hasAttribute('download');
              const click = HTMLAnchorElement.prototype.click;
              HTMLAnchorElement.prototype.click = function () { if (local(this)) { save(this); return; } return click.call(this); };
              document.addEventListener('click', (e) => {
                const a = e.target && e.target.closest && e.target.closest('a');
                if (local(a)) { e.preventDefault(); save(a); }
              }, true);
            })();
        """.trimIndent()
    }

    private lateinit var web: WebView
    private lateinit var column: LinearLayout
    private lateinit var sub: TextView
    private lateinit var progress: ProgressBar
    private lateinit var status: TextView
    private lateinit var sid: String
    private lateinit var device: String
    private lateinit var host: String
    private var port = 0
    private var released = false
    private var custom: View? = null
    private var customCallback: WebChromeClient.CustomViewCallback? = null
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val blobSaves = HashMap<String, Pair<OutputStream, () -> String>>()

    private val pickFiles = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        fileCallback?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(r.resultCode, r.data))
        fileCallback = null
    }

    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    /** Geist from the Flutter assets (the app's own type), else the system font. */
    private fun geist(weight: String): Typeface? =
        try { Typeface.createFromAsset(assets, "flutter_assets/fonts/Geist-$weight.ttf") } catch (_: Exception) { null }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Hub.init(this)
        sid = intent.getStringExtra("sid") ?: return finish()
        device = intent.getStringExtra("device") ?: return finish()
        host = intent.getStringExtra("host") ?: return finish()
        port = intent.getIntExtra("port", 0)
        val name = intent.getStringExtra("name") ?: device
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        WindowInsetsControllerCompat(window, window.decorView).isAppearanceLightStatusBars = false

        val root = FrameLayout(this).apply { setBackgroundColor(BG) }
        column = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(BG) }
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(BG)
            minimumHeight = dp(52)
            setPadding(dp(4), 0, dp(4), 0)
        }
        val ripple = TypedValue().also { theme.resolveAttribute(android.R.attr.selectableItemBackgroundBorderless, it, true) }.resourceId
        fun iconButton(res: Int, label: String, onClick: (View) -> Unit) = ImageButton(this).apply {
            setImageResource(res)
            contentDescription = label
            setBackgroundResource(ripple)
            setOnClickListener(onClick)
            layoutParams = LinearLayout.LayoutParams(dp(48), dp(48))
        }
        bar.addView(iconButton(R.drawable.ic_back, "Back to the devices") { finish() })
        val titles = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        titles.addView(TextView(this).apply {
            text = name
            setTextColor(TEXT)
            textSize = 15f
            geist("SemiBold")?.let { typeface = it }
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
        })
        sub = TextView(this).apply {
            text = "Port $port"
            setTextColor(DIM)
            textSize = 12f
            geist("Regular")?.let { typeface = it }
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        titles.addView(sub)
        bar.addView(titles)
        bar.addView(iconButton(R.drawable.ic_refresh, "Reload") { web.reload() })
        bar.addView(iconButton(R.drawable.ic_more, "More") { menu(it) })
        column.addView(bar)
        column.addView(View(this).apply { setBackgroundColor(LINE) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)))
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progressTintList = android.content.res.ColorStateList.valueOf(PRIMARY)
            progressBackgroundTintList = android.content.res.ColorStateList.valueOf(BG)
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(2))
        }
        column.addView(progress)
        web = WebView(this).apply {
            setBackgroundColor(BG)
            layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
        }
        column.addView(web)
        status = TextView(this).apply {
            setTextColor(TEXT)
            setBackgroundColor(TOAST)
            setPadding(dp(16), dp(10), dp(16), dp(10))
            textSize = 13f
            geist("Regular")?.let { typeface = it }
            visibility = View.GONE
        }
        column.addView(status)
        root.addView(column, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            if (custom == null) column.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            insets
        }

        setupWeb()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                when {
                    custom != null -> web.webChromeClient?.onHideCustomView()
                    web.canGoBack() -> web.goBack()
                    else -> finish()
                }
            }
        })
        if (savedInstanceState == null || web.restoreState(savedInstanceState) == null) {
            web.loadUrl(intent.getStringExtra("url") ?: "http://$host/")
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWeb() {
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) WebView.setWebContentsDebuggingEnabled(true)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            allowFileAccess = false
            allowContentAccess = false
            setSupportMultipleWindows(false)
            loadWithOverviewMode = true
            useWideViewPort = true
            textZoom = 100
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }
        CookieManager.getInstance().setAcceptCookie(true)
        val origin = "http://$host"
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            WebViewCompat.addWebMessageListener(web, "ArstroApp", setOf(origin)) { _, message, _, _, _ ->
                message.data?.let { onPageMessage(it) }
            }
            if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                WebViewCompat.addDocumentStartJavaScript(web, DOWNLOAD_SCRIPT, setOf(origin))
            }
        }
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                if ((u.scheme == "http" || u.scheme == "https") && u.authority == host) return false
                if (u.scheme == "blob" || u.scheme == "data" || u.scheme == "about") return false
                if (u.host?.let { it == "localhost" || it.endsWith(".localhost") || it.startsWith("127.") } == true) return true
                try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (_: ActivityNotFoundException) {}
                return true
            }

            override fun onRenderProcessGone(view: WebView, detail: android.webkit.RenderProcessGoneDetail): Boolean {
                finish()
                return true
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView, p: Int) {
                progress.progress = p
                progress.visibility = if (p >= 100) View.INVISIBLE else View.VISIBLE
            }

            override fun onReceivedTitle(view: WebView, title: String?) {
                val t = title?.trim().orEmpty()
                if (t.isEmpty() || t.startsWith("http") || t.contains(".localhost")) return
                sub.text = "Port $port · $t"
                io.execute { Hub.titleRecent(sid, device, port, t) }
            }

            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try {
                    pickFiles.launch(params.createIntent())
                    true
                } catch (e: ActivityNotFoundException) {
                    fileCallback = null
                    false
                }
            }

            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                if (custom != null) { callback.onCustomViewHidden(); return }
                custom = view
                customCallback = callback
                (window.decorView as FrameLayout).addView(view, FrameLayout.LayoutParams(-1, -1))
                column.visibility = View.GONE
                WindowInsetsControllerCompat(window, window.decorView).apply {
                    hide(WindowInsetsCompat.Type.systemBars())
                    systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                }
            }

            override fun onHideCustomView() {
                val v = custom ?: return
                (window.decorView as FrameLayout).removeView(v)
                custom = null
                column.visibility = View.VISIBLE
                WindowInsetsControllerCompat(window, window.decorView).show(WindowInsetsCompat.Type.systemBars())
                customCallback?.onCustomViewHidden()
                customCallback = null
            }

            override fun onPermissionRequest(request: PermissionRequest) = request.deny()
        }
        web.setDownloadListener { url, userAgent, disposition, mime, _ -> download(url, userAgent, disposition, mime) }
    }

    private fun menu(anchor: View) {
        val m = PopupMenu(this, anchor)
        m.menu.add(0, 1, 0, "Reload")
        m.menu.add(0, 2, 1, "Sign out of this page")
        m.menu.add(0, 3, 2, "Close")
        m.setOnMenuItemClickListener {
            when (it.itemId) {
                1 -> web.reload()
                2 -> signOut()
                3 -> finish()
            }
            true
        }
        m.show()
    }

    /** Forget this page's cookies and storage (its own origin only). */
    private fun signOut() {
        val url = "http://$host/"
        val cm = CookieManager.getInstance()
        cm.getCookie(url)?.split(';')?.map { it.substringBefore('=').trim() }?.filter { it.isNotEmpty() }?.forEach {
            cm.setCookie(url, "$it=; Max-Age=0; Path=/")
        }
        cm.flush()
        WebStorage.getInstance().deleteOrigin("http://$host")
        web.clearCache(false)
        web.loadUrl(url)
    }

    // ------------------------------------------------------------------ downloads
    private fun show(text: String?, hideAfterMs: Long = 0) = main.post {
        status.text = text
        status.visibility = if (text == null) View.GONE else View.VISIBLE
        if (hideAfterMs > 0) main.postDelayed({ if (status.text == text) status.visibility = View.GONE }, hideAfterMs)
    }

    private fun fileName(disposition: String?, url: String, mime: String?): String {
        val d = disposition.orEmpty()
        Regex("filename\\*\\s*=\\s*UTF-8''([^;]+)", RegexOption.IGNORE_CASE).find(d)?.let {
            return clean(Uri.decode(it.groupValues[1]))
        }
        Regex("filename\\s*=\\s*\"?([^\";]+)\"?", RegexOption.IGNORE_CASE).find(d)?.let { return clean(it.groupValues[1]) }
        return clean(URLUtil.guessFileName(url, d, mime))
    }

    private fun clean(name: String) = name.replace(Regex("[/\\\\:*?\"<>|\\x00-\\x1f]"), "_").trim().ifEmpty { "download" }.take(120)

    /** A new file in Downloads -> (its stream, a function that finishes it and returns where it is). */
    private fun newDownload(name: String, mime: String): Pair<OutputStream, () -> String> {
        if (Build.VERSION.SDK_INT >= 29) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mime)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw java.io.IOException("can't create the file in Downloads")
            val out = contentResolver.openOutputStream(uri) ?: throw java.io.IOException("can't write to Downloads")
            return out to {
                out.close()
                contentResolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
                "Downloads/$name"
            }
        }
        val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: filesDir
        val f = File(dir, name)
        val out = f.outputStream()
        return out to { out.close(); f.path }
    }

    private fun download(url: String, userAgent: String?, disposition: String?, mime: String?) {
        val u = try { URI(url) } catch (e: Exception) { return }
        if (u.rawAuthority != host) return
        val name = fileName(disposition, url, mime)
        val cookie = CookieManager.getInstance().getCookie(url)
        show("Downloading $name…")
        io.execute {
            var s: Stream? = null
            try {
                val headers = ArrayList<Pair<String, String>>()
                if (!cookie.isNullOrEmpty()) headers.add("Cookie" to cookie)
                if (!userAgent.isNullOrEmpty()) headers.add("User-Agent" to userAgent)
                val path = (u.rawPath ?: "/").ifEmpty { "/" } + (u.rawQuery?.let { "?$it" } ?: "")
                val (res, stream) = Hub.fetch(sid, device, port, path, headers)
                s = stream
                val st = res.optInt("status")
                if (st >= 400) throw java.io.IOException("the device answered $st")
                val (out, finish) = newDownload(name, mime ?: "application/octet-stream")
                var got = 0L
                var shown = 0L
                while (true) {
                    when (val e = stream.next(120_000)) {
                        is Stream.Event.Data -> {
                            out.write(e.bytes)
                            got += e.bytes.size
                            if (got - shown > 512 * 1024) { shown = got; show("Downloading $name… ${got / (1024 * 1024)} MB") }
                        }
                        is Stream.Event.Json -> if (e.t == "end") break else throw java.io.IOException(e.msg.optString("error", e.t))
                        is Stream.Event.Closed -> throw java.io.IOException(e.reason)
                    }
                }
                show("Saved to ${finish()}", 4000)
            } catch (e: Exception) {
                s?.cancel()
                show("Download failed: ${e.message}", 6000)
            } finally {
                s?.close()
            }
        }
    }

    /** blob:/data: downloads from the page script, in order: start, chunk..., end. */
    private fun onPageMessage(text: String) {
        val m = try { JSONObject(text) } catch (e: Exception) { return }
        val id = m.optString("id")
        when (m.optString("t")) {
            "start" -> {
                val name = clean(m.optString("name"))
                val mime = m.optString("type").ifEmpty { "application/octet-stream" }
                show("Downloading $name…")
                io.execute {
                    try { blobSaves[id] = newDownload(name, mime) } catch (e: Exception) { show("Download failed: ${e.message}", 6000) }
                }
            }
            "chunk" -> {
                val data = m.optString("data")
                io.execute { blobSaves[id]?.first?.write(Base64.decode(data, Base64.DEFAULT)) }
            }
            "end" -> io.execute {
                val save = blobSaves.remove(id) ?: return@execute
                try { show("Saved to ${save.second()}", 4000) } catch (e: Exception) { show("Download failed: ${e.message}", 6000) }
            }
        }
    }

    private fun releaseGateway() {
        if (released || !::sid.isInitialized) return
        released = true
        val (s, d, p) = Triple(sid, device, port)
        io.execute { Hub.release(s, d, p) }
    }

    override fun onDestroy() {
        if (::web.isInitialized) {
            web.stopLoading()
            web.destroy()
        }
        releaseGateway()
        io.shutdown()
        super.onDestroy()
    }
}
