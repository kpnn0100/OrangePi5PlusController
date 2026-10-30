package com.arstro.arstro_remote.phone.features

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.text.format.DateUtils
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The full-screen "Arstro call" (PHONE.md "Call"): over the lock screen, Answer / Decline while ringing,
 * then the call time, Speaker, Mute and End. Everything happens in CallFeature; this only shows it.
 */
class CallActivity : Activity() {
    private lateinit var name: TextView
    private lateinit var status: TextView
    private lateinit var ringingRow: LinearLayout
    private lateinit var activeRow: LinearLayout
    private lateinit var speakerBtn: Button
    private lateinit var muteBtn: Button
    private val listener: () -> Unit = { render() }
    private val tick = Runnable { render() }      // render() schedules the next one while active

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
        setContentView(build())
        handle(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handle(intent)
    }

    override fun onStart() {
        super.onStart()
        CallFeature.addListener(listener)
        render()
    }

    override fun onStop() {
        CallFeature.removeListener(listener)
        mainHandler.removeCallbacks(tick)
        super.onStop()
    }

    private fun handle(intent: Intent?) {
        when (intent?.action) {
            CallFeature.ACTION_ANSWER -> CallFeature.answer()
            CallFeature.ACTION_DECLINE -> CallFeature.decline()
            CallFeature.ACTION_HANGUP -> CallFeature.hangUp()
        }
        render()
    }

    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()

    private fun button(text: String, color: Int, onClick: () -> Unit) = Button(this).apply {
        this.text = text
        setTextColor(Color.WHITE)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
        isAllCaps = false
        background = GradientDrawable().apply {
            cornerRadius = dp(32).toFloat()
            setColor(color)
        }
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
        root.addView(TextView(this).apply {
            text = "Arstro call"
            setTextColor(MUTED)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
            gravity = Gravity.CENTER
        })
        name = TextView(this).apply {
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 32f)
            typeface = Typeface.DEFAULT_BOLD
            gravity = Gravity.CENTER
            setPadding(0, dp(16), 0, dp(8))
        }
        root.addView(name)
        status = TextView(this).apply {
            setTextColor(MUTED)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 18f)
            gravity = Gravity.CENTER
        }
        root.addView(status)
        root.addView(View(this), LinearLayout.LayoutParams(1, 0, 1f))

        ringingRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
        val lp = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { setMargins(dp(8), 0, dp(8), 0) }
        ringingRow.addView(button("Decline", RED) { CallFeature.decline() }, lp)
        ringingRow.addView(button("Answer", GREEN) { CallFeature.answer() }, LinearLayout.LayoutParams(lp))
        root.addView(ringingRow)

        activeRow = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER }
        val toggles = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
        speakerBtn = button("Speaker", GREY) { CallFeature.setSpeaker(!CallFeature.speaker) }
        muteBtn = button("Mute", GREY) { CallFeature.setMuted(!CallFeature.muted) }
        toggles.addView(speakerBtn, LinearLayout.LayoutParams(lp))
        toggles.addView(muteBtn, LinearLayout.LayoutParams(lp))
        activeRow.addView(toggles)
        activeRow.addView(button("End call", RED) { CallFeature.hangUp() },
            LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply {
                setMargins(dp(8), dp(24), dp(8), 0)
            })
        root.addView(activeRow)
        return root
    }

    private fun render() {
        if (!::name.isInitialized) return
        name.text = CallFeature.caller
        when (CallFeature.state) {
            "ringing" -> {
                status.text = "is calling you"
                ringingRow.visibility = View.VISIBLE
                activeRow.visibility = View.GONE
            }
            "active" -> {
                val s = (System.currentTimeMillis() - CallFeature.since) / 1000
                status.text = DateUtils.formatElapsedTime(maxOf(0, s))
                ringingRow.visibility = View.GONE
                activeRow.visibility = View.VISIBLE
                (speakerBtn.background as? GradientDrawable)?.setColor(if (CallFeature.speaker) ACCENT else GREY)
                (muteBtn.background as? GradientDrawable)?.setColor(if (CallFeature.muted) ACCENT else GREY)
                mainHandler.removeCallbacks(tick)
                mainHandler.postDelayed(tick, 1000)
            }
            "ended" -> {
                status.text = "Call ended - ${CallFeature.reason ?: ""}".trimEnd(' ', '-')
                ringingRow.visibility = View.GONE
                activeRow.visibility = View.GONE
                mainHandler.postDelayed({ if (CallFeature.state != "ringing" && CallFeature.state != "active") finish() }, 1500)
            }
            else -> finish()
        }
    }

    companion object {
        private val BG = Color.parseColor("#101418")
        private val MUTED = Color.parseColor("#9AA4AE")
        private val GREEN = Color.parseColor("#1E9E5A")
        private val RED = Color.parseColor("#D93A3A")
        private val GREY = Color.parseColor("#2A3138")
        private val ACCENT = Color.parseColor("#3A7BD5")
    }
}
