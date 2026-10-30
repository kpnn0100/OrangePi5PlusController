package com.arstro.arstro_remote.phone.features

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.window.OnBackInvokedDispatcher
import java.lang.ref.WeakReference

/**
 * Find my phone over the lock screen (PHONE.md "find"): while ringing, "touch to stop"; in lost mode the
 * owner's message and a number to call, kept until find.lost_off. Closes itself when neither is on.
 */
class LostModeActivity : Activity() {
    companion object {
        private var current: WeakReference<LostModeActivity>? = null

        /** Redraws (or closes) the open screen after a change. Main thread. */
        fun refreshAll() {                       // ops run on relay threads: views only on the main thread
            val a = current?.get() ?: return
            a.runOnUiThread { a.render() }
        }

        private val BG = Color.parseColor("#101418")
        private val MUTED = Color.parseColor("#9AA4AE")
        private val ACCENT = Color.parseColor("#3A7BD5")
        private val RED = Color.parseColor("#D93A3A")
    }

    private lateinit var title: TextView
    private lateinit var message: TextView
    private lateinit var contact: TextView
    private lateinit var callBtn: Button
    private lateinit var stopBtn: Button

    @Suppress("DEPRECATION")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.statusBarColor = BG
        window.navigationBarColor = BG
        if (Build.VERSION.SDK_INT >= 33) {
            // lost mode: Back does nothing (the screen stays until the owner turns lost mode off)
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT) {
                if (!FindFeature.isLost(applicationContext)) finish()
            }
        }
        setContentView(build())
        current = WeakReference(this)
    }

    override fun onResume() {
        super.onResume()
        current = WeakReference(this)
        render()
    }

    override fun onDestroy() {
        if (current?.get() === this) current = null
        super.onDestroy()
    }

    @Deprecated("Android < 13")
    @Suppress("DEPRECATION")
    @SuppressLint("GestureBackNavigation")   // Android 13+: the OnBackInvokedCallback in onCreate
    override fun onBackPressed() {
        if (!FindFeature.isLost(applicationContext)) super.onBackPressed()
    }

    override fun dispatchTouchEvent(ev: MotionEvent): Boolean {
        if (ev.actionMasked == MotionEvent.ACTION_DOWN && FindFeature.ringing) {
            FindFeature.ringStop("the screen was touched")
        }
        return super.dispatchTouchEvent(ev)
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (FindFeature.ringing && (keyCode == KeyEvent.KEYCODE_VOLUME_DOWN || keyCode == KeyEvent.KEYCODE_VOLUME_UP)) {
            FindFeature.ringStop("a key was pressed")
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    private fun button(text: String, color: Int, onClick: () -> Unit) = Button(this).apply {
        this.text = text
        setTextColor(Color.WHITE)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
        isAllCaps = false
        background = GradientDrawable().apply { cornerRadius = dp(32).toFloat(); setColor(color) }
        setPadding(dp(24), dp(14), dp(24), dp(14))
        setOnClickListener { onClick() }
    }

    private fun build(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setBackgroundColor(BG)
            fitsSystemWindows = true
            setPadding(dp(24), dp(72), dp(24), dp(48))
        }
        title = TextView(this).apply {
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 30f)
            typeface = Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER
        }
        root.addView(title)
        message = TextView(this).apply {
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 20f)
            gravity = Gravity.CENTER
            setPadding(0, dp(24), 0, dp(8))
        }
        root.addView(message)
        contact = TextView(this).apply {
            setTextColor(MUTED)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
            gravity = Gravity.CENTER
            setTextIsSelectable(true)
            setPadding(0, dp(16), 0, 0)
        }
        root.addView(contact)
        root.addView(View(this), LinearLayout.LayoutParams(1, 0, 1f))
        val full = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            .apply { setMargins(0, dp(12), 0, 0) }
        stopBtn = button("Stop the sound", RED) { FindFeature.ringStop("stopped on the phone") }
        root.addView(stopBtn, full)
        callBtn = button("Call", ACCENT) {
            val number = FindFeature.lostContact(applicationContext) ?: return@button
            try {
                // the dialer (no call permission): Android may ask to unlock first
                startActivity(Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(number))).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            } catch (e: Exception) { }
        }
        root.addView(callBtn, LinearLayout.LayoutParams(full))
        return root
    }

    fun render() {
        if (!::title.isInitialized) return
        val app = applicationContext
        val lost = FindFeature.isLost(app)
        val ringing = FindFeature.ringing
        if (!lost && !ringing) {
            finish()
            return
        }
        if (lost) {
            title.text = "This phone is lost"
            val msg = FindFeature.lostMessage(app)
            message.text = msg.ifEmpty { "Please return it to its owner." }
            val number = FindFeature.lostContact(app)
            contact.visibility = if (number != null) View.VISIBLE else View.GONE
            contact.text = if (number != null) "Please call: $number" else ""
            callBtn.visibility = if (number != null) View.VISIBLE else View.GONE
            callBtn.text = if (number != null) "Call $number" else "Call"
        } else {
            title.text = "Find my phone"
            message.text = "Someone is looking for this phone.\nTouch the screen to stop the sound."
            contact.visibility = View.GONE
            callBtn.visibility = View.GONE
        }
        stopBtn.visibility = if (ringing) View.VISIBLE else View.GONE
    }
}
