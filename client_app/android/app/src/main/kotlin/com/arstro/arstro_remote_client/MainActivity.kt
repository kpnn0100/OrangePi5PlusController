package com.arstro.arstro_remote_client

import android.content.Intent
import com.arstro.arstro_remote_client.core.PhoneBridge
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

/** The app's one screen: the Flutter UI (the phone page + the first-run wizard) over PhoneBridge. */
class MainActivity : FlutterActivity() {
    private var phone: PhoneBridge? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        phone = PhoneBridge(this, flutterEngine.dartExecutor.binaryMessenger)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        if (phone?.onRequestPermissionsResult(requestCode) != true) {
            super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (phone?.onActivityResult(requestCode) != true) {
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
        phone?.dispose()
        phone = null
        super.onDestroy()
    }
}
