package com.sakurasep.kaneopilot

import okhttp3.CertificatePinner
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.security.KeyStore
import java.util.Base64
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

class ApiException(val code: String, message: String, val status: Int) : IOException(message)
class MobileApi(val connection: Connection) {
    private val origin = validateEndpoint(connection.endpoint)
    private val certificate = verifiedCertificate(connection.certificateDer, connection.certificateSha256)
    private val trustStore = KeyStore.getInstance(KeyStore.getDefaultType()).apply { load(null); setCertificateEntry("paired-mac", certificate) }
    private val delegateTrust = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply { init(trustStore) }.trustManagers.filterIsInstance<X509TrustManager>().single()
    private val trust = object : X509TrustManager {
        override fun getAcceptedIssuers() = delegateTrust.acceptedIssuers
        override fun checkClientTrusted(chain: Array<java.security.cert.X509Certificate>, authType: String) = delegateTrust.checkClientTrusted(chain, authType)
        override fun checkServerTrusted(chain: Array<java.security.cert.X509Certificate>, authType: String) {
            delegateTrust.checkServerTrusted(chain, authType)
            if (chain.isEmpty() || sha256(chain.first().encoded) != connection.certificateSha256)
                throw java.security.cert.CertificateException("配对证书指纹不一致")
        }
    }
    private val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }
    // Keep OkHttp's default hostname verification. This CA is specific to one paired connection.
    private val client = OkHttpClient.Builder().sslSocketFactory(ssl.socketFactory, trust)
        .certificatePinner(CertificatePinner.Builder().add(origin.host, "sha256/" + Base64.getEncoder().encodeToString(
            java.security.MessageDigest.getInstance("SHA-256").digest(certificate.publicKey.encoded))).build())
        .followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false)
        .connectTimeout(12, TimeUnit.SECONDS).readTimeout(130, TimeUnit.SECONDS).callTimeout(135, TimeUnit.SECONDS).build()

    private fun request(path: String, body: JSONObject? = null, authenticated: Boolean = true): JSONObject {
        require(path in setOf("pair", "query", "actions") || path.matches(Regex("operations/[A-Za-z0-9-]{1,128}")))
        val builder = Request.Builder().url("${connection.endpoint}/mobile/v1/$path")
        if (authenticated) builder.header("Authorization", "Bearer ${connection.token}")
        if (body != null) builder.post(body.toString().toRequestBody("application/json; charset=utf-8".toMediaType()))
        client.newCall(builder.build()).execute().use { response ->
            val source = response.body?.source() ?: throw IOException("Mac 返回空响应")
            source.request(4L * 1024 * 1024 + 1)
            val bytes = source.buffer.readByteArray()
            require(bytes.size <= 4 * 1024 * 1024) { "Mac 响应过大" }
            val result = try { JSONObject(String(bytes, Charsets.UTF_8)) } catch (_: Exception) { throw IOException("Mac 响应格式无效") }
            if (!response.isSuccessful) throw ApiException(result.text("code", "http_error"), result.text("error", "连接失败 (${response.code})"), response.code)
            require(result.optInt("api_version") == 1) { "Mac 接口版本不兼容" }
            return result
        }
    }
    fun pair(pairing: Pairing, deviceName: String): Connection {
        val result = request("pair", json("pairing_token" to pairing.token, "device_name" to deviceName), false)
        require(result.getString("server_id") == pairing.serverId) { "Mac 身份与配对内容不一致" }
        require(result.getString("device_token").length >= 32 && result.getString("device_id").isNotBlank()) { "设备凭据无效" }
        return connection.copy(deviceId = result.getString("device_id"), token = result.getString("device_token"))
    }
    /** Return the core envelope intact (get-task.task, list-tasks.tasks, etc.). */
    fun query(type: String, args: JSONObject = JSONObject()) = request("query", json("type" to type, "args" to args)).getJSONObject("data")
    fun action(action: String, operationId: String, args: JSONObject): JSONObject {
        require(action in mobileActions) { "手机不支持此操作" }
        return request("actions", json("action" to action, "operation_id" to operationId, "args" to args))
    }
    fun operation(id: String): JSONObject = request("operations/$id")
}
