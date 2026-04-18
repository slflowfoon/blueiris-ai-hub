package nl.rogro82.pipup

import android.content.Context
import java.util.Locale
import java.util.UUID

data class PendingPairing(
    val deviceId: String,
    val tvName: String,
    val ipAddress: String,
    val port: Int,
    val manualCode: String,
    val issuedAtEpochSeconds: Long,
    val qrPayload: String,
)

class PairingStore(context: Context) {
    private val prefs = context.getSharedPreferences("pairing", Context.MODE_PRIVATE)
    private val currentSecretKey = "shared_secret"
    private val currentDeviceIdKey = "shared_secret_device_id"
    private val pendingManualCodeKey = "pending_manual_code"
    private val pendingIssuedAtKey = "pending_issued_at"
    private val pendingTvNameKey = "pending_tv_name"
    private val pendingIpAddressKey = "pending_ip_address"
    private val pendingPortKey = "pending_port"
    private val generatedDeviceIdKey = "device_id"

    fun getOrCreateDeviceId(): String {
        prefs.getString(generatedDeviceIdKey, null)?.let { return it }

        val deviceId = UUID.randomUUID().toString()
        prefs.edit().putString(generatedDeviceIdKey, deviceId).apply()
        return deviceId
    }

    fun createPendingPairing(
        tvName: String,
        ipAddress: String,
        port: Int,
        nowEpochSeconds: Long = System.currentTimeMillis() / 1000L,
    ): PendingPairing {
        val deviceId = getOrCreateDeviceId()
        val manualCode = UUID.randomUUID()
            .toString()
            .replace("-", "")
            .take(6)
            .uppercase(Locale.US)

        prefs.edit()
            .putString(pendingManualCodeKey, manualCode)
            .putLong(pendingIssuedAtKey, nowEpochSeconds)
            .putString(pendingTvNameKey, tvName)
            .putString(pendingIpAddressKey, ipAddress)
            .putInt(pendingPortKey, port)
            .apply()

        return buildPendingPairing(
            deviceId = deviceId,
            tvName = tvName,
            ipAddress = ipAddress,
            port = port,
            manualCode = manualCode,
            issuedAtEpochSeconds = nowEpochSeconds,
        )
    }

    fun getOrCreatePendingPairing(
        tvName: String,
        ipAddress: String,
        port: Int,
        nowEpochSeconds: Long = System.currentTimeMillis() / 1000L,
    ): PendingPairing? {
        if (getSharedSecret() != null) {
            clearPendingPairing()
            return null
        }

        return getPendingPairing(nowEpochSeconds) ?: createPendingPairing(
            tvName = tvName,
            ipAddress = ipAddress,
            port = port,
            nowEpochSeconds = nowEpochSeconds,
        )
    }

    fun completePendingPairing(
        manualCode: String,
        sharedSecret: String,
        nowEpochSeconds: Long = System.currentTimeMillis() / 1000L,
    ): PendingPairing? {
        val pending = getPendingPairing(nowEpochSeconds) ?: return null
        if (!manualCode.trim().uppercase(Locale.US).equals(pending.manualCode, ignoreCase = false)) {
            return null
        }

        saveSharedSecret(pending.deviceId, sharedSecret)
        clearPendingPairing()
        return pending
    }

    fun getPendingPairing(nowEpochSeconds: Long = System.currentTimeMillis() / 1000L): PendingPairing? {
        val manualCode = prefs.getString(pendingManualCodeKey, null) ?: return null
        val issuedAt = prefs.getLong(pendingIssuedAtKey, 0L)
        val tvName = prefs.getString(pendingTvNameKey, null) ?: return null
        val ipAddress = prefs.getString(pendingIpAddressKey, null) ?: return null
        val port = prefs.getInt(pendingPortKey, 7979)
        if (issuedAt <= 0L || nowEpochSeconds - issuedAt > PAIRING_TTL_SECONDS) {
            clearPendingPairing()
            return null
        }

        return buildPendingPairing(
            deviceId = getOrCreateDeviceId(),
            tvName = tvName,
            ipAddress = ipAddress,
            port = port,
            manualCode = manualCode,
            issuedAtEpochSeconds = issuedAt,
        )
    }

    private fun clearPendingPairing() {
        prefs.edit()
            .remove(pendingManualCodeKey)
            .remove(pendingIssuedAtKey)
            .remove(pendingTvNameKey)
            .remove(pendingIpAddressKey)
            .remove(pendingPortKey)
            .apply()
    }

    private fun buildPendingPairing(
        deviceId: String,
        tvName: String,
        ipAddress: String,
        port: Int,
        manualCode: String,
        issuedAtEpochSeconds: Long,
    ): PendingPairing {
        val qrPayload = Json.writeValueAsString(
            mapOf(
                "tv_name" to tvName,
                "ip_address" to ipAddress,
                "port" to port,
                "device_id" to deviceId,
                "manual_code" to manualCode,
            )
        )
        return PendingPairing(
            deviceId = deviceId,
            tvName = tvName,
            ipAddress = ipAddress,
            port = port,
            manualCode = manualCode,
            issuedAtEpochSeconds = issuedAtEpochSeconds,
            qrPayload = qrPayload,
        )
    }

    fun saveSharedSecret(deviceId: String, secret: String) {
        prefs.edit()
            .putString("secret:$deviceId", secret)
            .putString(currentSecretKey, secret)
            .putString(currentDeviceIdKey, deviceId)
            .apply()
    }

    fun getSharedSecret(deviceId: String): String? {
        return prefs.getString("secret:$deviceId", null)
    }

    fun getSharedSecret(): String? {
        prefs.getString(currentSecretKey, null)?.let { return it }

        val legacyDeviceId = prefs.getString(currentDeviceIdKey, null)
        if (!legacyDeviceId.isNullOrBlank()) {
            prefs.getString("secret:$legacyDeviceId", null)?.let { return it }
        }

        val legacySecrets = prefs.all
            .filterKeys { key -> key.startsWith("secret:") }
            .values
            .mapNotNull { it as? String }

        return legacySecrets.singleOrNull()
    }

    fun getCurrentDeviceId(): String? {
        return prefs.getString(currentDeviceIdKey, null)
    }

    private companion object {
        const val PAIRING_TTL_SECONDS = 300L
    }
}
