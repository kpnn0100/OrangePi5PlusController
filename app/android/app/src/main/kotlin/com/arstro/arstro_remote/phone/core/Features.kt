package com.arstro.arstro_remote.phone.core

import com.arstro.arstro_remote.phone.features.AppsFeature
import com.arstro.arstro_remote.phone.features.CallFeature
import com.arstro.arstro_remote.phone.features.CameraFeature
import com.arstro.arstro_remote.phone.features.ClipboardFeature
import com.arstro.arstro_remote.phone.features.ContactsFeature
import com.arstro.arstro_remote.phone.features.FilesFeature
import com.arstro.arstro_remote.phone.features.FindFeature
import com.arstro.arstro_remote.phone.features.MessagesFeature
import com.arstro.arstro_remote.phone.features.MicFeature
import com.arstro.arstro_remote.phone.features.NotificationsFeature
import com.arstro.arstro_remote.phone.features.ScreenFeature
import com.arstro.arstro_remote.phone.features.ShellFeature
import com.arstro.arstro_remote.phone.features.StatusFeature
import com.arstro.arstro_remote.phone.features.ToolsFeature

/** Every feature of the phone, in the order the app and the web show them (PHONE.md "Features").
 *  The features agent adds each feature here as soon as its class compiles. */
object Features {
    val all: List<Feature> by lazy {
        listOf<Feature>(
            StatusFeature, ScreenFeature, CameraFeature, CallFeature, MicFeature, MessagesFeature, ContactsFeature,
            FindFeature, FilesFeature, NotificationsFeature, ClipboardFeature, AppsFeature, ToolsFeature, ShellFeature,
        )
    }

    fun byId(id: String): Feature? = all.firstOrNull { it.id == id }
}
