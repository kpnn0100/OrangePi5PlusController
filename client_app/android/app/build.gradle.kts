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
    namespace = "com.arstro.arstro_remote_client"
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
        applicationId = "com.arstro.arstro_remote_client"
        minSdk = flutter.minSdkVersion
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

// The connection to the device manager (WebSocket + HTTP pipes), the camera, a lifecycle for the
// service (docs/phone/PHONE.md); JVM unit tests run the relay protocol against
// docs/relay/vectors.json and the real relay server.
dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")
    implementation("androidx.camera:camera-core:1.4.1")
    implementation("androidx.camera:camera-camera2:1.4.1")
    implementation("androidx.camera:camera-lifecycle:1.4.1")
    implementation("androidx.camera:camera-video:1.4.1")
    // CameraX hands out Guava ListenableFutures (getInstance, enableTorch): the interface on the compile path
    implementation("com.google.guava:guava:33.3.1-android")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
