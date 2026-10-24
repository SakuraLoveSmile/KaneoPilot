package com.sakurasep.kaneopilot

import android.Manifest
import android.content.pm.PackageManager
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private val model: ConsoleModel by viewModels()
    private val updater: SoftwareUpdateModel by viewModels()
    private val updateInstaller = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { updater.installationReturned() }
    private val updatePermission = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        updater.permissionReturned(::launchUpdateInstaller)
    }
    private val scanner = registerForActivityResult(ScanContract()) { result -> result.contents?.let { model.pair(it) } }
    private val permission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { allowed ->
        if (allowed) scan() else model.setNotice("未授予相机权限；仍可粘贴配对内容")
    }
    private fun scan() { scanner.launch(ScanOptions().setDesiredBarcodeFormats(ScanOptions.QR_CODE)
        .setPrompt("扫描 Mac「手机连接」的配对二维码").setBeepEnabled(false).setOrientationLocked(false)) }
    private fun launchUpdateInstaller(intent: Intent) { updateInstaller.launch(intent) }
    private fun installUpdate() {
        updater.prepareInstall(onPermissionNeeded = {
            updatePermission.launch(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
        }, onReady = ::launchUpdateInstaller)
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent { KaneoPilotApp(model, updater, ::installUpdate) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) scan()
            else permission.launch(Manifest.permission.CAMERA)
        } }
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch {
                    model.foreground(true)
                    try { while (true) { model.refreshInBackground(); delay(5000) } }
                    finally { model.foreground(false) }
                }
                launch {
                    updater.foreground(true)
                    try { while (true) { updater.foregroundTick(); delay(30_000) } }
                    finally { updater.foreground(false) }
                }
            }
        }
    }
}
