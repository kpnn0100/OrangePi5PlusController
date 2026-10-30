package com.arstro.arstro_remote.phone.features

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.os.Build
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.arstro.arstro_remote.phone.core.OpError
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The remote control of the screen (PHONE.md "screen", PHN-04): the accessibility service that performs
 * the viewer's taps, swipes, keys and typing. It does nothing on its own and reads no window content
 * except the focused text field when the viewer types.
 */
class ControlService : AccessibilityService() {
    companion object {
        @Volatile var instance: ControlService? = null
            private set
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        ScreenFeature.controlChanged()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {}

    override fun onUnbind(intent: Intent?): Boolean {
        if (instance === this) instance = null
        ScreenFeature.controlChanged()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        ScreenFeature.controlChanged()
        super.onDestroy()
    }

    /** Performs the strokes; with `wait` blocks until Android reports it done (not on the main thread). */
    fun gesture(strokes: List<Gestures.Stroke>, wait: Boolean) {
        val b = GestureDescription.Builder()
        var total = 0L
        for (s in strokes) {
            val p = Path()
            p.moveTo(s.points[0].x, s.points[0].y)
            for (pt in s.points.drop(1)) p.lineTo(pt.x, pt.y)
            b.addStroke(GestureDescription.StrokeDescription(p, s.startMs, s.durationMs))
            total = maxOf(total, s.startMs + s.durationMs)
        }
        val latch = CountDownLatch(1)
        var done = false
        val cb = object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) { done = true; latch.countDown() }
            override fun onCancelled(gestureDescription: GestureDescription?) { latch.countDown() }
        }
        val sent = onMain { dispatchGesture(b.build(), cb, null) }
        if (!sent) throw OpError("the phone refused the touch - is remote control (Accessibility) still on?", 409)
        if (wait && !isMainThread()) {
            latch.await(total + 3000, TimeUnit.MILLISECONDS)
            if (!done) throw OpError("the touch was interrupted on the phone (someone touched the screen?)", 409)
        }
    }

    fun gesture(stroke: Gestures.Stroke, wait: Boolean) = gesture(listOf(stroke), wait)

    fun global(action: Int) {
        if (!onMain { performGlobalAction(action) }) throw OpError("the phone did not do it", 409)
    }

    /**
     * Types `text` into the focused text field, at its cursor ("\b" deletes, a lone "\n" presses the
     * keyboard's Enter where Android allows it). Password fields hide their text, so there the text
     * replaces the field's content.
     */
    fun type(text: String) {
        onMain {
            val node = findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                ?: throw OpError("no text field has the focus - tap one first", 409)
            if (!node.isEditable) throw OpError("the focused element is not a text field - tap one first", 409)
            if (text == "\n" && Build.VERSION.SDK_INT >= 30) {
                if (node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) return@onMain
            }
            val result = if (node.isPassword) {
                TextEdit.Result(text.replace("\b", ""), text.replace("\b", "").length)
            } else {
                val hint = Build.VERSION.SDK_INT >= 26 && node.isShowingHintText
                val current = if (hint) "" else node.text?.toString() ?: ""
                TextEdit.apply(current, node.textSelectionStart, node.textSelectionEnd, text)
            }
            val args = Bundle()
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, result.text)
            if (!node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
                throw OpError("the text field did not take the text", 409)
            }
            val sel = Bundle()
            sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, result.cursor)
            sel.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, result.cursor)
            node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
        }
    }
}
