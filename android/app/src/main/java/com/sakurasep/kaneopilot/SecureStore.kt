package com.sakurasep.kaneopilot

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.json.JSONObject
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Only connection secrets and unresolved operation identities are persisted; never task content. */
class SecureStore(context: Context) {
    private val prefs = context.getSharedPreferences("mobile_secure", Context.MODE_PRIVATE)
    private val alias = "KaneoPilot.mobile.v1"
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    fun load(): JSONObject? {
        val raw = prefs.getString("sealed", null) ?: return null
        val obj = JSONObject(raw)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.getDecoder().decode(obj.getString("iv"))))
        cipher.updateAAD("KaneoPilot.mobile.v1".toByteArray())
        return JSONObject(String(cipher.doFinal(Base64.getDecoder().decode(obj.getString("ciphertext"))), Charsets.UTF_8))
    }
    fun save(value: JSONObject) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        cipher.updateAAD("KaneoPilot.mobile.v1".toByteArray())
        val ciphertext = cipher.doFinal(value.toString().toByteArray(Charsets.UTF_8))
        check(prefs.edit().putString("sealed", json("iv" to Base64.getEncoder().encodeToString(cipher.iv),
            "ciphertext" to Base64.getEncoder().encodeToString(ciphertext)).toString()).commit()) { "无法安全保存连接凭据" }
    }
    fun clear() { check(prefs.edit().clear().commit()) { "无法清除连接凭据" } }
}
