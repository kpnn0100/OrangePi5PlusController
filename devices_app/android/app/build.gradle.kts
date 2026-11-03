import java.util.Properties

plugins {
    id("com.android.application")
    id("kotlin-android")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// Release signing: the same key as Arstro Remote - app/android/key.properties + the keystore it
// names in app/android/app/ (both kept out of VCS). Without them the release build uses the debug key.
val keystoreProperties = Properties().apply {
    val f = rootProject.file("../../app/android/key.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "com.arstro.arstro_devices"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    // ARM64 Linux hosts (the board, SET-07): CMake and the NDK's compilers run on x86-64 only (see app/).
    if (System.getProperty("os.name") == "Linux" && System.getProperty("os.arch") == "aarch64") {
        externalNativeBuild.cmake.path = null
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_17.toString()
    }

    defaultConfig {
        applicationId = "com.arstro.arstro_devices"
        minSdk = maxOf(flutter.minSdkVersion, 26)
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (keystoreProperties.containsKey("storeFile")) {
            create("release") {
                storeFile = rootProject.file("../../app/android/app/" + keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

}

flutter {
    source = "../.."
}

// The device manager connection (WebSocket + HTTP pipes, a copy of client_app's core: E2eTest checks
// the copies are the same), the WebView helpers (document-start script, message listener) and the
// activity results (file chooser). JVM unit tests run the viewer and the gateway against the real
// device manager and a real Python device.
dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.webkit:webkit:1.12.1")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
