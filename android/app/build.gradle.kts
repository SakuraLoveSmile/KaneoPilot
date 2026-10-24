plugins { id("com.android.application"); id("org.jetbrains.kotlin.android"); id("org.jetbrains.kotlin.plugin.compose") }
val releaseKeys = listOf("ANDROID_KEYSTORE_PATH", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD")
val hasReleaseKeys = releaseKeys.all { !System.getenv(it).isNullOrBlank() }
android {
    namespace = "com.sakurasep.kaneopilot"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.sakurasep.kaneopilot"
        minSdk = 26
        targetSdk = 36
        versionCode = providers.gradleProperty("versionCode").orElse("1").get().toInt().also { require(it > 0) }
        versionName = providers.gradleProperty("versionName").orElse("0.1.0").get()
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    if (hasReleaseKeys) signingConfigs.create("release") {
        storeFile = file(System.getenv("ANDROID_KEYSTORE_PATH"))
        storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
        keyAlias = System.getenv("ANDROID_KEY_ALIAS")
        keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
    }
    buildTypes {
        getByName("release") { if (hasReleaseKeys) signingConfig = signingConfigs.getByName("release"); isMinifyEnabled = false }
        create("updateAcceptance") {
            initWith(getByName("release"))
            signingConfig = signingConfigs.getByName("debug")
            isDebuggable = false
            matchingFallbacks += listOf("release")
        }
    }
    testBuildType = if (providers.gradleProperty("updateAcceptanceTests").orNull == "true") "updateAcceptance" else "debug"
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true; buildConfig = true }
    testOptions { unitTests.isReturnDefaultValues = true }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}
tasks.configureEach {
    if (name.matches(Regex("(?i).*(assemble|bundle|package|validateSigning).*release.*"))) doFirst {
        check(hasReleaseKeys) { "Release 签名缺失：请配置固定 ANDROID_KEYSTORE_PATH/PASSWORD、ANDROID_KEY_ALIAS/PASSWORD；不会生成临时密钥" }
    }
}
dependencies {
    implementation(platform("androidx.compose:compose-bom:2025.10.01"))
    implementation("androidx.activity:activity-compose:1.11.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.4")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    implementation("io.noties.markwon:core:4.6.2")
    debugImplementation("androidx.compose.ui:ui-tooling")
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    testImplementation("com.squareup.okhttp3:okhttp-tls:4.12.0")
    testImplementation("org.json:json:20250517")
    androidTestImplementation(platform("androidx.compose:compose-bom:2025.10.01"))
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    // 3.7 uses TestLooperManager on Android 16 instead of reflecting legacy MessageQueue.mMessages.
    // Keep this explicit: older transitive Espresso versions can stall on a concurrent MessageQueue.
    androidTestImplementation("androidx.test.espresso:espresso-core:3.7.0")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    add("updateAcceptanceImplementation", "androidx.compose.ui:ui-test-manifest")
}
