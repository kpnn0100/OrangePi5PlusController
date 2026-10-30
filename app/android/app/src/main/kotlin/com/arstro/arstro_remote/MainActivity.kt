package com.arstro.arstro_remote

import android.content.Intent
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
    private var bluetooth: BluetoothBridge? = null
    private var preview: PreviewBridge? = null
    private var download: DownloadBridge? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val messenger = flutterEngine.dartExecutor.binaryMessenger
        bluetooth = BluetoothBridge(this, messenger)
        preview = PreviewBridge(flutterEngine.renderer, messenger)
        download = DownloadBridge(applicationContext, messenger)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (bluetooth?.onRequestPermissionsResult(requestCode, grantResults) != true) {
            super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (bluetooth?.onActivityResult(requestCode, resultCode) != true) {
            super.onActivityResult(requestCode, resultCode, data)
        }
    }

    override fun onDestroy() {
        bluetooth?.dispose()
        bluetooth = null
        preview?.dispose()
        preview = null
        download?.dispose()
        download = null
        super.onDestroy()
    }
}
