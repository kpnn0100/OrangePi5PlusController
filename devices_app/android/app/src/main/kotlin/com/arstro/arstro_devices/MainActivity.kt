package com.arstro.arstro_devices

import com.arstro.arstro_devices.core.Hub
import com.arstro.arstro_devices.core.HubError
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

/** The Flutter UI (the device list, MGR-02) and the Kotlin side (Hub) talk over one method channel,
 *  "arstro/devices". Every call runs off the main thread; JSON goes back as text. */
class MainActivity : FlutterActivity() {
    private val io = Executors.newCachedThreadPool()

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        Hub.init(this)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "arstro/devices").setMethodCallHandler { call, result ->
            io.execute {
                try {
                    val out = handle(call).let { if (it == Unit) null else it }
                    runOnUiThread { result.success(out) }
                } catch (e: HubError) {
                    runOnUiThread { result.error(e.code, e.message, null) }
                } catch (e: Exception) {
                    runOnUiThread { result.error("error", e.message ?: e.javaClass.simpleName, null) }
                }
            }
        }
    }

    private fun handle(call: MethodCall): Any? {
        fun str(k: String): String = call.argument<String>(k) ?: throw HubError("bad_input", "missing $k")
        fun int(k: String): Int = call.argument<Int>(k) ?: throw HubError("bad_input", "missing $k")
        return when (call.method) {
            "servers" -> Hub.servers().toString()
            "probe" -> Hub.probe(str("input"), call.argument<String>("password")).toString()
            "saveServer" -> Hub.saveServer(call.argument<String>("id"), str("url"), str("name"),
                                           call.argument<String>("version") ?: "", call.argument<String>("password"))
            "removeServer" -> Hub.removeServer(str("id"))
            "select" -> Hub.select(str("id"))
            "devices" -> Hub.devices(str("server")).toString()
            "setKey" -> Hub.setKey(str("server"), str("device"), str("key"))
            "forgetKey" -> Hub.forgetKey(str("server"), str("device"))
            "check" -> Hub.check(str("server"), str("device")).toString()
            "ports" -> Hub.ports(str("server"), str("device")).toString()
            "recents" -> Hub.recents(str("server")).toString()
            "removeRecent" -> Hub.removeRecent(str("server"), str("device"), int("port"))
            "open" -> {
                val sid = str("server")
                val device = str("device")
                val name = str("name")
                val port = int("port")
                val r = Hub.open(sid, device, name, port)
                runOnUiThread {
                    startActivity(WebActivity.intent(this, sid, device, name, port, r.getString("url"), r.getString("host")))
                }
                r.toString()
            }
            else -> throw HubError("unknown", "unknown call ${call.method}")
        }
    }
}
