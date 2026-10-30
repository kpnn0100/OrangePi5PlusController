package com.arstro.arstro_remote_client.features

import android.Manifest
import android.annotation.SuppressLint
import android.content.ContentValues
import android.content.Context
import android.content.pm.ServiceInfo
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.MediaCodec
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.util.Log
import android.util.Size
import android.view.OrientationEventListener
import android.view.Surface
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.SurfaceRequest
import androidx.camera.core.UseCase
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.FallbackStrategy
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.MediaStoreOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import com.arstro.arstro_remote_client.core.Feature
import com.arstro.arstro_remote_client.core.OpError
import com.arstro.arstro_remote_client.core.PhoneContext
import com.arstro.arstro_remote_client.core.Response
import com.arstro.arstro_remote_client.core.WsHandler
import com.arstro.arstro_remote_client.core.WsSession
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Camera (PHONE.md "camera", PHN-05): live view (CameraX Preview -> H.264 encoder input surface ->
 * /ws/camera per "Video"), a photo now, recording an MP4 into Movies/Arstro, the torch, the list.
 * One camera (one facing) is used at a time: viewers of the same camera share the picture, anything
 * that wants the other camera gets an error until the first one is done.
 *
 * The picture is sent as the sensor delivers it; the config's `rotation` (clockwise degrees) tells the
 * viewer how to turn it upright for the way the phone is held now.
 */
object CameraFeature : Feature("camera", "Camera") {
    override val permissions = listOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)

    private const val FG = "camera"
    private const val FG_REC = "camera.rec"
    private val lock = Any()

    @Volatile private var ctx: PhoneContext? = null
    private var provider: ProcessCameraProvider? = null
    private var owner: CamOwner? = null
    @Volatile private var facing: String? = null          // the bound camera, null when closed
    @Volatile private var camera: Camera? = null
    private var preview: Preview? = null
    private var imageCapture: ImageCapture? = null
    private var videoCapture: VideoCapture<Recorder>? = null
    @Volatile private var recording: Recording? = null
    @Volatile private var recordingPath: String? = null
    private var recordDone: CountDownLatch? = null
    @Volatile private var recordError: String? = null
    @Volatile private var photoBusy = false
    @Volatile private var encoder: AvcEncoder? = null
    @Volatile private var rotation = 0
    private var targetSize = Size(1280, 720)
    private var orientation: OrientationEventListener? = null
    private val hub = VideoHub { encoder?.requestKeyFrame() }

    // ------------------------------------------------------------------------------------- API

    override fun ops() = mapOf(
        "camera.list" to op { c, _ -> list(c.app) },
        "camera.torch" to op { c, a -> torch(c, a.bool("on") ?: throw OpError("\"on\" is missing (true or false)")) },
        "camera.record" to op { c, a -> record(c, a) },
    )

    override fun routes() = mapOf(
        "GET /api/camera/photo" to route { c, req ->
            val jpeg = photo(c, req.param("facing"))
            Response.bytes(jpeg, "image/jpeg", 200, listOf("Cache-Control" to "no-store"))
        },
    )

    override fun sockets() = mapOf("/ws/camera" to socket { c, s -> openViewer(c, s) })

    override fun start(ctx: PhoneContext) { this.ctx = ctx }

    override fun stop(ctx: PhoneContext) {
        hub.closeAll(1001, "$title was turned off on the phone")
        try { recording?.stop() } catch (e: Exception) { }
        try { onMain { closeCamera() } } catch (e: Exception) { }
        ctx.releaseForeground(FG)
        ctx.releaseForeground(FG_REC)
        this.ctx = null
    }

    override fun note(ctx: PhoneContext): String? = when {
        recording != null -> "recording a video"
        hub.count > 0 -> "live view (${facing ?: "back"} camera)"
        else -> null
    }

    // ------------------------------------------------------------------------------------- list / torch

    private fun cameraManager(app: Context) = app.getSystemService(CameraManager::class.java)

    private fun facingName(lens: Int?) = when (lens) {
        CameraCharacteristics.LENS_FACING_FRONT -> "front"
        CameraCharacteristics.LENS_FACING_BACK -> "back"
        else -> "external"
    }

    private fun list(app: Context): JSONArray {
        val cm = cameraManager(app)
        val out = JSONArray()
        for (id in cm.cameraIdList) {
            val ch = try { cm.getCameraCharacteristics(id) } catch (e: Exception) { continue }
            val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            val sizes = (map?.getOutputSizes(MediaCodec::class.java) ?: map?.getOutputSizes(SurfaceTexture::class.java) ?: emptyArray())
                .sortedByDescending { it.width.toLong() * it.height }
                .map { "${it.width}x${it.height}" }.distinct()
            out.put(JSONObject().put("facing", facingName(ch.get(CameraCharacteristics.LENS_FACING))).put("id", id)
                .put("sizes", JSONArray(sizes)).put("flash", ch.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true))
        }
        return out
    }

    private fun hasFacing(app: Context, f: String): Boolean {
        val want = if (f == "front") CameraCharacteristics.LENS_FACING_FRONT else CameraCharacteristics.LENS_FACING_BACK
        val cm = cameraManager(app)
        return cm.cameraIdList.any { id -> try { cm.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING) == want } catch (e: Exception) { false } }
    }

    private fun torch(c: PhoneContext, on: Boolean): JSONObject {
        val cam = camera
        if (cam != null && facing == "back" && cam.cameraInfo.hasFlashUnit()) {
            onMain { cam.cameraControl.enableTorch(on) }
        } else {
            Torch.set(c.app, on)
        }
        return jsonOf("on" to on)
    }

    // ------------------------------------------------------------------------------------- camera session

    private class CamOwner : LifecycleOwner {
        private val registry = LifecycleRegistry(this)
        override val lifecycle: Lifecycle get() = registry
        fun resume() { registry.currentState = Lifecycle.State.RESUMED }
        fun pause() { registry.currentState = Lifecycle.State.CREATED }
    }

    private fun checkFacing(want: String) {
        val f = facing
        if (f != null && f != want) {
            val what = when {
                recording != null -> "recording with the $f camera"
                hub.count > 0 -> "live view of the $f camera"
                else -> "the $f camera"
            }
            throw OpError("the camera is in use ($what) - stop it first", 409)
        }
    }

    private fun parseFacing(text: String?): String = when (text?.lowercase()) {
        null, "", "back", "rear" -> "back"
        "front", "selfie" -> "front"
        else -> throw OpError("facing must be back or front")
    }

    /** The provider (first use initialises CameraX; not on the main thread). */
    private fun provider(app: Context): ProcessCameraProvider {
        provider?.let { return it }
        val p = ProcessCameraProvider.getInstance(app).get(10, TimeUnit.SECONDS)
        provider = p
        return p
    }

    private fun selector(f: String) = if (f == "front") CameraSelector.DEFAULT_FRONT_CAMERA else CameraSelector.DEFAULT_BACK_CAMERA

    /** Binds `useCase` to the camera of facing `f` (opens it when closed). Main thread. */
    private fun bind(app: Context, f: String, vararg useCases: UseCase) {
        val p = provider ?: throw OpError("the camera is not ready - try again", 503)
        val o = owner ?: CamOwner().also { owner = it }
        o.resume()
        try {
            camera = p.bindToLifecycle(o, selector(f), *useCases)
        } catch (e: IllegalArgumentException) {
            throw OpError("this phone's camera cannot do that together with what it is doing now (${e.message?.take(80)})", 409)
        }
        facing = f
        startOrientation(app)
    }

    /** Closes the camera when nobody uses it any more. Main thread. */
    private fun closeIfIdle() {
        if (hub.count > 0 || recording != null || photoBusy) return
        closeCamera()
        ctx?.releaseForeground(FG)
    }

    private fun closeCamera() {
        try { provider?.unbindAll() } catch (e: Exception) { }
        owner?.pause()
        camera = null
        facing = null
        preview = null
        imageCapture = null
        videoCapture = null
        orientation?.disable()
        orientation = null
        encoder?.let { e -> encoder = null; Thread { e.release() }.start() }
    }

    private fun surfaceRotation(degrees: Int): Int = when (degrees) {
        in 45 until 135 -> Surface.ROTATION_270
        in 135 until 225 -> Surface.ROTATION_180
        in 225 until 315 -> Surface.ROTATION_90
        else -> Surface.ROTATION_0
    }

    private var currentTarget = Surface.ROTATION_0

    private fun startOrientation(app: Context) {
        if (orientation != null) return
        val l = object : OrientationEventListener(app) {
            override fun onOrientationChanged(deg: Int) {
                if (deg == ORIENTATION_UNKNOWN) return
                val r = surfaceRotation(deg)
                if (r == currentTarget) return
                currentTarget = r
                preview?.targetRotation = r
                imageCapture?.targetRotation = r
                videoCapture?.targetRotation = r
            }
        }
        if (l.canDetectOrientation()) l.enable()
        orientation = l
    }

    // ------------------------------------------------------------------------------------- live view

    private fun openViewer(c: PhoneContext, s: WsSession): WsHandler {
        ctx = c
        val f = try { parseFacing(s.query["facing"]?.firstOrNull()) } catch (e: OpError) { return refuse(s, e.message!!) }
        val size = VideoSizes.parse(s.query["size"]?.firstOrNull())
        try {
            checkFacing(f)
            if (!hasFacing(c.app, f)) throw OpError("no camera faces $f on this phone", 404)
        } catch (e: OpError) {
            return refuse(s, e.message!!)
        }
        val v = hub.add(s)
        Thread({
            try {
                c.foregroundBlocking(FG, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA, "camera")
                provider(c.app)
                onMain {
                    checkFacing(f)
                    if (preview == null) {
                        if (size != null) targetSize = Size(maxOf(size.first, size.second), minOf(size.first, size.second))
                        startPreview(c.app, f)
                    } else {
                        hub.greet(v)
                    }
                }
            } catch (e: Exception) {
                val msg = (e as? OpError)?.message ?: "the camera could not start (${e.javaClass.simpleName})"
                c.log(Log.WARN, "camera: live view failed: ${e.javaClass.simpleName}")
                hub.remove(s)
                try { s.sendText(JSONObject().put("t", "error").put("error", msg).toString()) } catch (x: Exception) { }
                try { s.close(1011, msg.take(120)) } catch (x: Exception) { }
                try { onMain { closeIfIdle() } } catch (x: Exception) { }
            }
        }, "arstro-camera-open").start()
        return object : WsHandler {
            override fun onText(session: WsSession, text: String) {
                val m = try { JSONObject(text) } catch (e: Exception) { return }
                when (m.optString("t")) {
                    "keyframe" -> { v.needKey = true; encoder?.requestKeyFrame() }
                    "quality" -> {
                        val max = m.int("max") ?: 0
                        v.fps = m.int("fps") ?: 0
                        v.kbps = m.int("kbps") ?: 0
                        if (max > 0 && max != v.max) {
                            v.max = max
                            mainHandler.post {
                                if (preview != null && facing != null) {
                                    targetSize = Size(max * 16 / 9, max)
                                    try { startPreview(c.app, facing!!) } catch (e: Exception) { }
                                }
                            }
                        }
                    }
                }
            }

            override fun onClose(session: WsSession, code: Int, reason: String) {
                hub.remove(session)
                if (hub.count == 0) mainHandler.post {
                    preview?.let { p -> try { provider?.unbind(p) } catch (e: Exception) { } }
                    preview = null
                    closeIfIdle()
                }
            }
        }
    }

    private fun refuse(s: WsSession, why: String): WsHandler {
        try { s.sendText(JSONObject().put("t", "error").put("error", why).toString()) } catch (e: Exception) { }
        try { s.close(1008, why.take(120)) } catch (e: Exception) { }
        return object : WsHandler {}
    }

    /** (Re)binds the preview to an encoder fed by the camera. Main thread. */
    private fun startPreview(app: Context, f: String) {
        preview?.let { p -> try { provider?.unbind(p) } catch (e: Exception) { } }
        val sel = ResolutionSelector.Builder()
            .setResolutionStrategy(ResolutionStrategy(targetSize, ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER))
            .build()
        val p = Preview.Builder().setResolutionSelector(sel).setTargetRotation(currentTarget).build()
        val exec = ContextCompat.getMainExecutor(app)
        p.setSurfaceProvider(exec) { req -> onSurfaceRequest(req) }
        preview = p
        val ic = imageCapture ?: ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
            .setTargetRotation(currentTarget).build().also { imageCapture = it }
        val cases = listOfNotNull<UseCase>(p, ic, videoCapture)
        bind(app, f, *cases.toTypedArray())
    }

    private val encListener = object : AvcEncoder.Listener {
        override fun onConfig(encoder: AvcEncoder, codec: String) {
            if (encoder === this@CameraFeature.encoder) hub.config(codec, encoder.width, encoder.height, rotation)
        }

        override fun onFrame(encoder: AvcEncoder, packet: ByteArray, key: Boolean) {
            if (encoder === this@CameraFeature.encoder) hub.frame(packet, key)
        }
    }

    private fun onSurfaceRequest(req: SurfaceRequest) {
        val app = ctx?.app ?: run { req.willNotProvideSurface(); return }
        val res = req.resolution
        val (_, qfps, qkbps) = hub.maxQuality()
        val fps = (if (qfps > 0) qfps else 30).coerceIn(5, 30)
        val bitrate = if (qkbps > 0) (qkbps * 1000).coerceIn(100_000, 20_000_000) else VideoSizes.bitrate(res.width, res.height, fps)
        val enc = try {
            AvcEncoder(res.width, res.height, fps, bitrate, 0, encListener)
        } catch (e: Exception) {
            req.willNotProvideSurface()
            hub.text(JSONObject().put("t", "error").put("error", "the phone's video encoder refused ${res.width}x${res.height}").toString())
            return
        }
        hub.clearConfig()
        encoder = enc      // the old one (if any) is released when CameraX gives its surface back
        val exec = ContextCompat.getMainExecutor(app)
        req.setTransformationInfoListener(exec) { info ->
            if (info.rotationDegrees != rotation) {
                rotation = info.rotationDegrees
                val codec = encoder?.codecString
                val e = encoder
                if (codec != null && e != null) hub.config(codec, e.width, e.height, rotation)
            }
        }
        req.provideSurface(enc.surface, exec) {
            if (encoder === enc) encoder = null
            Thread { enc.release() }.start()
        }
    }

    // ------------------------------------------------------------------------------------- photo

    private fun photo(c: PhoneContext, facingText: String?): ByteArray {
        ctx = c
        val f = if (facingText.isNullOrEmpty()) (facing ?: "back") else parseFacing(facingText)
        checkFacing(f)
        if (!hasFacing(c.app, f)) throw OpError("no camera faces $f on this phone", 404)
        if (photoBusy) throw OpError("a photo is being taken - try again", 409)
        photoBusy = true
        try {
            c.foregroundBlocking(FG, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA, "camera")
            provider(c.app)
            val latch = CountDownLatch(1)
            val out = ByteArrayOutputStream()
            var error: String? = null
            onMain {
                checkFacing(f)
                val ic = imageCapture ?: ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                    .setTargetRotation(currentTarget).build().also { imageCapture = it }
                if (facing == null || camera == null) bind(c.app, f, ic)
                else bind(c.app, f, *listOfNotNull<UseCase>(preview, ic, videoCapture).toTypedArray())
                ic.takePicture(ImageCapture.OutputFileOptions.Builder(out).build(), ContextCompat.getMainExecutor(c.app),
                    object : ImageCapture.OnImageSavedCallback {
                        override fun onImageSaved(outputFileResults: ImageCapture.OutputFileResults) { latch.countDown() }
                        override fun onError(exception: ImageCaptureException) {
                            error = "the photo failed (${exception.message?.take(80) ?: "camera error"})"
                            latch.countDown()
                        }
                    })
            }
            if (!latch.await(15, TimeUnit.SECONDS)) throw OpError("the camera took too long - try again", 504)
            error?.let { throw OpError(it, 500) }
            return out.toByteArray()
        } finally {
            photoBusy = false
            try { onMain { closeIfIdle() } } catch (e: Exception) { }
        }
    }

    // ------------------------------------------------------------------------------------- recording

    @SuppressLint("MissingPermission")
    private fun record(c: PhoneContext, a: JSONObject): JSONObject {
        ctx = c
        when (a.str("action")?.lowercase()) {
            "start" -> {
                if (recording != null) return jsonOf("recording" to true, "path" to recordingPath)
                val f = if (a.str("facing").isNullOrEmpty()) (facing ?: "back") else parseFacing(a.str("facing"))
                checkFacing(f)
                if (!hasFacing(c.app, f)) throw OpError("no camera faces $f on this phone", 404)
                val audio = c.app.granted(Manifest.permission.RECORD_AUDIO)
                c.foregroundBlocking(FG, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA, "camera")
                if (audio) c.foregroundBlocking(FG_REC, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE, "microphone")
                provider(c.app)
                val name = "Arstro_" + SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date()) + ".mp4"
                val done = CountDownLatch(1)
                try {
                    onMain {
                        checkFacing(f)
                        val vc = videoCapture ?: VideoCapture.withOutput(
                            Recorder.Builder().setQualitySelector(
                                QualitySelector.from(Quality.HD, FallbackStrategy.lowerQualityOrHigherThan(Quality.SD))).build()
                        ).also { videoCapture = it }
                        vc.targetRotation = currentTarget
                        bind(c.app, f, *listOfNotNull<UseCase>(preview, imageCapture, vc).toTypedArray())
                        val recorder = vc.output
                        val pending = if (Build.VERSION.SDK_INT >= 29) {
                            val values = ContentValues().apply {
                                put(MediaStore.Video.Media.DISPLAY_NAME, name)
                                put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
                                put(MediaStore.Video.Media.RELATIVE_PATH, "Movies/Arstro")
                            }
                            recorder.prepareRecording(c.app, MediaStoreOutputOptions.Builder(c.app.contentResolver,
                                MediaStore.Video.Media.EXTERNAL_CONTENT_URI).setContentValues(values).build())
                        } else {
                            @Suppress("DEPRECATION")
                            val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MOVIES), "Arstro")
                            dir.mkdirs()
                            recorder.prepareRecording(c.app, FileOutputOptions.Builder(File(dir, name)).build())
                        }
                        if (audio) pending.withAudioEnabled()
                        recordError = null
                        recordDone = done
                        recordingPath = "/Movies/Arstro/$name"
                        recording = pending.start(ContextCompat.getMainExecutor(c.app)) { ev ->
                            if (ev is VideoRecordEvent.Finalize) {
                                if (ev.hasError() && ev.error != VideoRecordEvent.Finalize.ERROR_SOURCE_INACTIVE) {
                                    recordError = "the recording failed (error ${ev.error})"
                                }
                                recording = null
                                c.releaseForeground(FG_REC)
                                done.countDown()
                                closeIfIdle()
                            }
                        }
                    }
                } catch (e: Exception) {
                    c.releaseForeground(FG_REC)
                    try { onMain { closeIfIdle() } } catch (x: Exception) { }
                    throw e
                }
                c.log(Log.INFO, "camera: recording started")
                return jsonOf("recording" to true, "path" to recordingPath)
            }
            "stop" -> {
                val r = recording ?: return jsonOf("recording" to false, "path" to recordingPath)
                val done = recordDone
                r.stop()
                done?.await(10, TimeUnit.SECONDS)
                c.log(Log.INFO, "camera: recording stopped")
                recordError?.let { throw OpError(it, 500) }
                return jsonOf("recording" to false, "path" to recordingPath)
            }
            else -> throw OpError("action must be start or stop")
        }
    }
}

/** The torch through CameraManager (needs no camera permission; fails while another app has the camera). */
object Torch {
    @Volatile var on = false
        private set

    fun set(app: Context, enable: Boolean) {
        val cm = app.getSystemService(CameraManager::class.java)
        val id = cm.cameraIdList.firstOrNull { id ->
            try {
                val ch = cm.getCameraCharacteristics(id)
                ch.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true &&
                    ch.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
            } catch (e: Exception) { false }
        } ?: cm.cameraIdList.firstOrNull { id ->
            try { cm.getCameraCharacteristics(id).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true } catch (e: Exception) { false }
        } ?: throw OpError("this phone has no torch", 404)
        try {
            cm.setTorchMode(id, enable)
            on = enable
        } catch (e: Exception) {
            throw OpError("the torch is not available now (the camera is in use?)", 409)
        }
    }
}
