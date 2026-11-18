package com.arstro.arstro_remote_client.features

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream

/** Smaller pictures for the still-picture views (plain http, no live video in the browser). */
object Jpeg {
    /** The JPEG scaled so its long side is at most `max`, turned upright (EXIF orientation applied). */
    fun shrink(jpeg: ByteArray, max: Int, quality: Int = 80): ByteArray {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, bounds)
        val long = maxOf(bounds.outWidth, bounds.outHeight)
        if (long <= 0) return jpeg
        var sample = 1
        while (long / (sample * 2) >= max) sample *= 2
        val src = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, BitmapFactory.Options().apply { inSampleSize = sample }) ?: return jpeg
        val deg = try {
            when (ExifInterface(ByteArrayInputStream(jpeg)).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
                ExifInterface.ORIENTATION_ROTATE_90 -> 90
                ExifInterface.ORIENTATION_ROTATE_180 -> 180
                ExifInterface.ORIENTATION_ROTATE_270 -> 270
                else -> 0
            }
        } catch (e: Exception) { 0 }
        val k = minOf(1f, max.toFloat() / maxOf(src.width, src.height))
        val m = Matrix().apply { if (k < 1f) postScale(k, k); if (deg != 0) postRotate(deg.toFloat()) }
        val out = if (k < 1f || deg != 0) Bitmap.createBitmap(src, 0, 0, src.width, src.height, m, true) else src
        return compress(out, quality).also { if (out !== src) out.recycle(); src.recycle() }
    }

    fun compress(b: Bitmap, quality: Int = 80): ByteArray =
        ByteArrayOutputStream().also { b.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()

    /** A bitmap scaled so its long side is at most `max` (the same one when already small enough). */
    fun fit(b: Bitmap, max: Int): Bitmap {
        val k = max.toFloat() / maxOf(b.width, b.height)
        return if (k >= 1f) b else Bitmap.createScaledBitmap(b, (b.width * k).toInt().coerceAtLeast(1), (b.height * k).toInt().coerceAtLeast(1), true)
    }
}
