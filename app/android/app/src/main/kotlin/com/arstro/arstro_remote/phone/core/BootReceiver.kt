package com.arstro.arstro_remote.phone.core

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/** Starts "Share this phone" at boot and after an update of the app - if it was on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (!Store(context).enabled) return
        try {
            PhoneService.start(context)
        } catch (e: Exception) {
            Log.w(PhoneService.TAG, "could not start sharing this phone: ${e.javaClass.simpleName}")
        }
    }
}
