package com.arstro.arstro_remote_client.core

import com.arstro.arstro_remote_client.features.AppsFeature
import com.arstro.arstro_remote_client.features.CallFeature
import com.arstro.arstro_remote_client.features.CameraFeature
import com.arstro.arstro_remote_client.features.ClipboardFeature
import com.arstro.arstro_remote_client.features.ContactsFeature
import com.arstro.arstro_remote_client.features.FilesFeature
import com.arstro.arstro_remote_client.features.FindFeature
import com.arstro.arstro_remote_client.features.MessagesFeature
import com.arstro.arstro_remote_client.features.MicFeature
import com.arstro.arstro_remote_client.features.NotificationsFeature
import com.arstro.arstro_remote_client.features.ScreenFeature
import com.arstro.arstro_remote_client.features.ShellFeature
import com.arstro.arstro_remote_client.features.StatusFeature
import com.arstro.arstro_remote_client.features.ToolsFeature

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
