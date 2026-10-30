package com.arstro.arstro_remote.phone.features

import com.arstro.arstro_remote.phone.features.StoragePaths.RangeResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Files: paths stay inside the storage root, Range headers, download names (PHONE.md "files"). */
class StoragePathsTest {
    private val root = "/storage/emulated/0"

    @Test fun rootForms() {
        assertEquals(root, StoragePaths.resolve(root, null))
        assertEquals(root, StoragePaths.resolve(root, ""))
        assertEquals(root, StoragePaths.resolve(root, "/"))
        assertEquals(root, StoragePaths.resolve(root, root))
        assertEquals(root, StoragePaths.resolve(root, "$root/"))
        assertEquals(root, StoragePaths.resolve("$root/", "."))
    }

    @Test fun relativeAndAbsolute() {
        assertEquals("$root/DCIM/Camera", StoragePaths.resolve(root, "/DCIM/Camera"))
        assertEquals("$root/DCIM/Camera", StoragePaths.resolve(root, "DCIM/Camera/"))
        assertEquals("$root/DCIM/Camera", StoragePaths.resolve(root, "$root/DCIM//./Camera"))
        assertEquals("$root/a b/ç.txt", StoragePaths.resolve(root, "/a b/ç.txt"))
        // a look-alike prefix is a folder inside the root, not another root
        assertEquals("$root/storage/emulated/01/x", StoragePaths.resolve(root, "/storage/emulated/01/x"))
    }

    @Test fun dotDotStaysInside() {
        assertEquals("$root/DCIM", StoragePaths.resolve(root, "/DCIM/Camera/.."))
        assertEquals(root, StoragePaths.resolve(root, "/DCIM/.."))
        assertNull(StoragePaths.resolve(root, ".."))
        assertNull(StoragePaths.resolve(root, "/../../data/data"))
        assertNull(StoragePaths.resolve(root, "$root/../1"))
        assertNull(StoragePaths.resolve(root, "/DCIM/../../.."))
        assertNull(StoragePaths.resolve(root, "/a\u0000b"))
    }

    @Test fun insideCheck() {
        assertTrue(StoragePaths.inside(root, root))
        assertTrue(StoragePaths.inside(root, "$root/x"))
        assertFalse(StoragePaths.inside(root, "/storage/emulated/01"))
        assertFalse(StoragePaths.inside(root, "/data/data/x"))
    }

    @Test fun shownPathsAndParents() {
        assertEquals("/", StoragePaths.display(root, root))
        assertEquals("/DCIM/Camera", StoragePaths.display(root, "$root/DCIM/Camera"))
        assertNull(StoragePaths.parent("/"))
        assertEquals("/", StoragePaths.parent("/DCIM"))
        assertEquals("/DCIM", StoragePaths.parent("/DCIM/Camera"))
    }

    @Test fun names() {
        assertTrue(StoragePaths.validName("photo 1.jpg"))
        assertTrue(StoragePaths.validName(".hidden"))
        assertFalse(StoragePaths.validName(null))
        assertFalse(StoragePaths.validName(""))
        assertFalse(StoragePaths.validName("."))
        assertFalse(StoragePaths.validName(".."))
        assertFalse(StoragePaths.validName("a/b"))
        assertFalse(StoragePaths.validName("x".repeat(256)))
    }

    @Test fun ranges() {
        val total = 1000L
        assertEquals(RangeResult.Whole, StoragePaths.parseRange(null, total))
        assertEquals(RangeResult.Part(0, 499), StoragePaths.parseRange("bytes=0-499", total))
        assertEquals(RangeResult.Part(500, 999), StoragePaths.parseRange("bytes=500-", total))
        assertEquals(RangeResult.Part(900, 999), StoragePaths.parseRange("bytes=-100", total))
        assertEquals(RangeResult.Part(0, 999), StoragePaths.parseRange("bytes=-5000", total))
        assertEquals(RangeResult.Part(990, 999), StoragePaths.parseRange("bytes=990-5000", total))
        assertEquals(RangeResult.Part(0, 0), StoragePaths.parseRange("bytes=0-0", total))
        assertEquals(500L, (StoragePaths.parseRange("bytes=0-499", total) as RangeResult.Part).length)
        // only the first of several ranges
        assertEquals(RangeResult.Part(0, 9), StoragePaths.parseRange("bytes=0-9, 20-29", total))
        assertEquals(RangeResult.Unsatisfiable, StoragePaths.parseRange("bytes=1000-", total))
        assertEquals(RangeResult.Unsatisfiable, StoragePaths.parseRange("bytes=-0", total))
        assertEquals(RangeResult.Unsatisfiable, StoragePaths.parseRange("bytes=0-", 0))
        // nonsense is ignored: the whole file
        assertEquals(RangeResult.Whole, StoragePaths.parseRange("items=0-1", total))
        assertEquals(RangeResult.Whole, StoragePaths.parseRange("bytes=abc", total))
        assertEquals(RangeResult.Whole, StoragePaths.parseRange("bytes=9-3", total))
    }

    @Test fun downloadNames() {
        assertEquals("attachment; filename=\"a.txt\"; filename*=UTF-8''a.txt", StoragePaths.contentDisposition("a.txt"))
        assertEquals("inline; filename=\"a b.png\"; filename*=UTF-8''a%20b.png", StoragePaths.contentDisposition("a b.png", inline = true))
        val d = StoragePaths.contentDisposition("ảnh \"1\".jpg")
        assertTrue(d, d.startsWith("attachment; filename=\"_nh _1_.jpg\"; filename*=UTF-8''"))
        assertTrue(d, d.endsWith("%E1%BA%A3nh%20%221%22.jpg"))
    }
}
