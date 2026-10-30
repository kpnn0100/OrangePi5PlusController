package com.arstro.arstro_remote.phone.features

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Viewer coordinates -> touch paths (PHONE.md "screen"), and typing into a field. */
class GesturesTest {
    private val eps = 0.001f

    @Test fun mapsTheUnitSquareOntoTheScreen() {
        val w = 1080
        val h = 2400
        assertEquals(Gestures.Pt(0f, 0f), Gestures.toScreen(0.0, 0.0, w, h))
        assertEquals(Gestures.Pt(1079f, 2399f), Gestures.toScreen(1.0, 1.0, w, h))
        val mid = Gestures.toScreen(0.5, 0.25, w, h)
        assertEquals(539.5f, mid.x, eps)
        assertEquals(599.75f, mid.y, eps)
    }

    @Test fun clampsOutsideAndNaN() {
        assertEquals(Gestures.Pt(0f, 99f), Gestures.toScreen(-0.3, 7.0, 100, 100))
        val c = Gestures.toScreen(Double.NaN, Double.NaN, 101, 201)
        assertEquals(50f, c.x, eps)
        assertEquals(100f, c.y, eps)
    }

    @Test fun rotatedScreenUsesTheCurrentWidthAndHeight() {
        // the same viewer point on the phone held upright and turned to landscape (sizes swap)
        val portrait = Gestures.toScreen(0.25, 0.75, 1080, 2400)
        val landscape = Gestures.toScreen(0.25, 0.75, 2400, 1080)
        assertEquals(269.75f, portrait.x, eps)
        assertEquals(1799.25f, portrait.y, eps)
        assertEquals(599.75f, landscape.x, eps)
        assertEquals(809.25f, landscape.y, eps)
    }

    @Test fun tapAndLongPressArePressesInPlace() {
        val t = Gestures.tap(0.5, 0.5, 1000, 2000)
        assertEquals(1, t.points.size)
        assertEquals(Gestures.TAP_MS, t.durationMs)
        val l = Gestures.longPress(0.5, 0.5, 1000, 2000, longPressTimeoutMs = 400)
        assertEquals(1, l.points.size)
        assertEquals(Gestures.LONG_MS, l.durationMs)
        // a long "long press" setting still gets a press longer than it
        assertEquals(1800L, Gestures.longPress(0.5, 0.5, 1000, 2000, longPressTimeoutMs = 1500).durationMs)
    }

    @Test fun swipeHasTwoPointsAndABoundedDuration() {
        val s = Gestures.swipe(0.1, 0.9, 0.1, 0.1, null, 1001, 2001)
        assertEquals(listOf(Gestures.Pt(100f, 1800f), Gestures.Pt(100f, 200f)), s.points)
        assertEquals(Gestures.SWIPE_MS, s.durationMs)
        assertEquals(10L, Gestures.swipe(0.1, 0.9, 0.1, 0.1, 0, 100, 100).durationMs)
        assertEquals(Gestures.MAX_MS, Gestures.swipe(0.1, 0.9, 0.1, 0.1, 999_999, 100, 100).durationMs)
        // no movement: a press
        assertEquals(1, Gestures.swipe(0.5, 0.5, 0.5, 0.5, 100, 100, 100).points.size)
    }

    @Test fun scrollDownDragsTheFingerUp() {
        val s = Gestures.scroll(0.5, 0.6, 0.2, 1001, 1001)!!
        assertEquals(2, s.points.size)
        assertEquals(600f, s.points[0].y, eps)
        assertEquals(400f, s.points[1].y, eps)
        assertTrue(s.points[1].y < s.points[0].y)
        val up = Gestures.scroll(0.5, 0.4, -0.2, 1001, 1001)!!
        assertTrue(up.points[1].y > up.points[0].y)
    }

    @Test fun scrollStaysOnTheScreen() {
        val s = Gestures.scroll(0.5, 0.5, 3.0, 1001, 1001)!!
        assertEquals(20f, s.points[1].y, eps)          // clamped at 2 % from the top
        val t = Gestures.scroll(0.5, 1.0, -3.0, 1001, 1001)
        assertNull(t)                                   // already at the bottom edge: nothing moves
        assertNull(Gestures.scroll(0.5, 0.5, 0.0, 100, 100))
        assertNull(Gestures.scroll(0.5, 0.5, Double.NaN, 100, 100))
    }

    @Test fun typingInsertsAtTheCursor() {
        assertEquals(TextEdit.Result("hello world", 11), TextEdit.apply("hello", 5, 5, " world"))
        assertEquals(TextEdit.Result("heXllo", 3), TextEdit.apply("hello", 2, 2, "X"))
        // a selection is replaced
        assertEquals(TextEdit.Result("hXo", 2), TextEdit.apply("hello", 1, 4, "X"))
        // reversed selection
        assertEquals(TextEdit.Result("hXo", 2), TextEdit.apply("hello", 4, 1, "X"))
        // no / bad selection: at the end
        assertEquals(TextEdit.Result("hello!", 6), TextEdit.apply("hello", -1, -1, "!"))
        assertEquals(TextEdit.Result("hello!", 6), TextEdit.apply("hello", 9, 9, "!"))
    }

    @Test fun backspaceDeletes() {
        assertEquals(TextEdit.Result("hell", 4), TextEdit.apply("hello", 5, 5, "\b"))
        assertEquals(TextEdit.Result("hey", 3), TextEdit.apply("hello", 5, 5, "\b\b\by"))
        assertEquals(TextEdit.Result("", 0), TextEdit.apply("", 0, 0, "\b\b"))
        // backspace on a selection removes only the selection
        assertEquals(TextEdit.Result("ho", 1), TextEdit.apply("hello", 1, 4, "\b"))
        // an emoji (surrogate pair) goes in one step
        assertEquals(TextEdit.Result("a", 1), TextEdit.apply("a😀", 3, 3, "\b"))
    }
}
