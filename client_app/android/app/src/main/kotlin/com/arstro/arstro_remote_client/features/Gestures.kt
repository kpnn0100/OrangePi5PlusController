package com.arstro.arstro_remote_client.features

/*
 * Pure geometry of the remote control (PHONE.md "screen"): a viewer's 0..1 coordinates of the picture
 * it sees -> touch points in pixels of the screen as it is now. The screen video always shows the screen
 * in its current rotation (the virtual display is resized on every rotation), so x/y map to the current
 * width / height (which swap when the phone turns). No Android classes - JVM unit tests.
 */
object Gestures {
    data class Pt(val x: Float, val y: Float)

    /** One finger: its path (one point = a press in place) over `durationMs`. */
    data class Stroke(val points: List<Pt>, val durationMs: Long, val startMs: Long = 0)

    const val TAP_MS = 40L
    const val LONG_MS = 800L
    const val SWIPE_MS = 300L
    const val MAX_MS = 10_000L

    /** 0..1 -> a pixel inside a `w` x `h` screen (clamped to the screen). */
    fun toScreen(x: Double, y: Double, w: Int, h: Int): Pt {
        require(w > 0 && h > 0)
        val cx = if (x.isNaN()) 0.5 else x.coerceIn(0.0, 1.0)
        val cy = if (y.isNaN()) 0.5 else y.coerceIn(0.0, 1.0)
        return Pt((cx * (w - 1)).toFloat(), (cy * (h - 1)).toFloat())
    }

    fun tap(x: Double, y: Double, w: Int, h: Int) = Stroke(listOf(toScreen(x, y, w, h)), TAP_MS)

    fun longPress(x: Double, y: Double, w: Int, h: Int, longPressTimeoutMs: Long = 500) =
        Stroke(listOf(toScreen(x, y, w, h)), maxOf(LONG_MS, longPressTimeoutMs + 300))

    fun swipe(x1: Double, y1: Double, x2: Double, y2: Double, ms: Long?, w: Int, h: Int): Stroke {
        val a = toScreen(x1, y1, w, h)
        val b = toScreen(x2, y2, w, h)
        val d = (ms ?: SWIPE_MS).coerceIn(10, MAX_MS)
        return if (a == b) Stroke(listOf(a), d) else Stroke(listOf(a, b), d)
    }

    /**
     * Scroll by `dy` screen heights at (x, y), + = down (the content moves up, like a mouse wheel): the finger
     * drags from y to y - dy, slowly enough not to fling, kept inside the screen. null when nothing moves.
     */
    fun scroll(x: Double, y: Double, dy: Double, w: Int, h: Int): Stroke? {
        if (dy.isNaN() || dy == 0.0) return null
        val startY = y.coerceIn(EDGE, 1 - EDGE)
        val endY = (startY - dy).coerceIn(EDGE, 1 - EDGE)
        if (Math.abs(endY - startY) < 1e-3) return null
        val ms = (150 + 600 * Math.abs(endY - startY)).toLong()
        return Stroke(listOf(toScreen(x, startY, w, h), toScreen(x, endY, w, h)), ms)
    }

    private const val EDGE = 0.02
}

/** Typing into a text field: insert at the selection, "\b" deletes one character (or the selection). */
object TextEdit {
    data class Result(val text: String, val cursor: Int)

    fun apply(current: String, selStart: Int, selEnd: Int, input: String): Result {
        var s = selStart
        var e = selEnd
        if (s < 0 || e < 0 || s > current.length || e > current.length) { s = current.length; e = s }
        if (s > e) { val t = s; s = e; e = t }
        val sb = StringBuilder(current)
        var cursor = s
        var selection = e > s
        for (ch in input) {
            if (selection) {
                sb.delete(s, e)
                cursor = s
                selection = false
                if (ch == '\b') continue
            }
            if (ch == '\b') {
                if (cursor > 0) {
                    // keep surrogate pairs together
                    val n = if (cursor >= 2 && Character.isSurrogatePair(sb[cursor - 2], sb[cursor - 1])) 2 else 1
                    sb.delete(cursor - n, cursor)
                    cursor -= n
                }
            } else {
                sb.insert(cursor, ch)
                cursor++
            }
        }
        return Result(sb.toString(), cursor)
    }
}
