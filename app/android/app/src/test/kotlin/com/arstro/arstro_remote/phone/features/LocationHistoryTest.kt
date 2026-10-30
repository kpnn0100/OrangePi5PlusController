package com.arstro.arstro_remote.phone.features

import com.arstro.arstro_remote.phone.features.LocationHistory.Fix
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** Find my phone's 7-day history: records, pruning, newest-first lists (PHONE.md "find.history"). */
class LocationHistoryTest {
    private val day = 24L * 3600 * 1000
    private val now = 1_800_000_000_000L

    @Test fun recordsRoundTrip() {
        val f = Fix(10.762622, 106.660172, 12.5, now)
        assertEquals("$now,10.762622,106.660172,12.5", LocationHistory.format(f))
        assertEquals(f, LocationHistory.parse(LocationHistory.format(f)))
        assertEquals(Fix(-33.9, 151.2, 0.0, 5), LocationHistory.parse(" 5,-33.9,151.2,0 \n"))
    }

    @Test fun damagedRecordsAreSkipped() {
        assertNull(LocationHistory.parse(""))
        assertNull(LocationHistory.parse("garbage"))
        assertNull(LocationHistory.parse("1,2,3"))
        assertNull(LocationHistory.parse("1,2,3,4,5"))
        assertNull(LocationHistory.parse("x,2,3,4"))
        assertNull(LocationHistory.parse("1,91,3,4"))       // latitude out of range
        assertNull(LocationHistory.parse("1,2,181,4"))      // longitude out of range
        assertNull(LocationHistory.parse("1,2,3,-4"))       // negative accuracy
        assertNull(LocationHistory.parse("1,2,3,NaN"))
    }

    @Test fun pruneKeepsSevenDaysOldestFirst() {
        val fixes = listOf(
            Fix(1.0, 1.0, 5.0, now - 8 * day),          // too old
            Fix(2.0, 2.0, 5.0, now - 1000),
            Fix(3.0, 3.0, 5.0, now - 7 * day + 1),      // just inside
            Fix(4.0, 4.0, 5.0, now + 3 * day),          // clock far in the future: dropped
        )
        val kept = LocationHistory.prune(fixes, now)
        assertEquals(listOf(3.0, 2.0), kept.map { it.lat })
    }

    @Test fun fileKeepsNewestFirstWithLimit() {
        val dir = Files.createTempDirectory("arstro-history").toFile()
        try {
            val file = File(dir, "phone/find_history.txt")
            val h = LocationHistory(file)
            assertTrue(h.list(now, 10).isEmpty())
            assertNull(h.lastTime(now))
            h.add(Fix(1.0, 1.0, 5.0, now - 9 * day), now - 9 * day)
            h.add(Fix(2.0, 2.0, 5.0, now - 2 * day), now - 2 * day)
            h.add(Fix(3.0, 3.0, 5.0, now - day), now - day)
            h.add(Fix(4.0, 4.0, 5.0, now), now)
            // the 9-day-old fix was dropped when newer ones were added
            assertEquals(3, file.readLines().size)
            assertEquals(listOf(4.0, 3.0, 2.0), h.list(now, 10).map { it.lat })
            assertEquals(listOf(4.0, 3.0), h.list(now, 2).map { it.lat })
            assertEquals(now, h.lastTime(now))
            // a damaged line does not lose the others
            file.appendText("half a line\n")
            assertEquals(3, h.list(now, 10).size)
            // a week later only the newest is left
            assertEquals(listOf(4.0), h.list(now + 7 * day - 1, 10).map { it.lat })
            h.clear()
            assertTrue(h.list(now, 10).isEmpty())
        } finally {
            dir.deleteRecursively()
        }
    }
}
