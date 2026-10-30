package com.arstro.arstro_remote

import android.content.Intent
import com.arstro.arstro_remote.phone.core.PhoneBridge
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    private var bluetooth: BluetoothBridge? = null
    private var preview: PreviewBridge? = null
    private var download: DownloadBridge? = null
    private var phone: PhoneBridge? = null           // "Share this phone" (docs/phone/PHONE.md)

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val messenger = flutterEngine.dartExecutor.binaryMessenger
        bluetooth = BluetoothBridge(this, messenger)
        preview = PreviewBridge(flutterEngine.renderer, messenger)
        download = DownloadBridge(applicationContext, messenger)
        phone = PhoneBridge(this, messenger)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (phone?.onRequestPermissionsResult(requestCode) == true) return
        if (bluetooth?.onRequestPermissionsResult(requestCode, grantResults) != true) {
            super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (phone?.onActivityResult(requestCode) == true) return
        if (bluetooth?.onActivityResult(requestCode, resultCode) != true) {
            super.onActivityResult(requestCode, resultCode, data)
        }
    }

    override fun onResume() {
        super.onResume()
        phone?.onResume()
    }

    override fun onPause() {
        phone?.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        bluetooth?.dispose()
        bluetooth = null
        preview?.dispose()
        preview = null
        download?.dispose()
        download = null
        phone?.dispose()
        phone = null
        super.onDestroy()
    }
}
