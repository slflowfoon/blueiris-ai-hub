package io.slflowfoon.blueirisaihub.tv

import android.content.Context
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

@RunWith(RobolectricTestRunner::class)
class PairingStoreTest {
    @Test
    fun saveAndReadSharedSecret() {
        val store = PairingStore(RuntimeEnvironment.getApplication())

        assertNull(store.getSharedSecret("device-1"))
        assertNull(store.getSharedSecret())
        assertNull(store.getCurrentDeviceId())

        store.saveSharedSecret("device-1", "secret-123")

        assertEquals("secret-123", store.getSharedSecret("device-1"))
        assertEquals("secret-123", store.getSharedSecret())
        assertEquals("device-1", store.getCurrentDeviceId())
    }

    @Test
    fun fallsBackToLegacySecretWhenCurrentSecretIsMissing() {
        val app = RuntimeEnvironment.getApplication()
        val prefs = app.getSharedPreferences("pairing", Context.MODE_PRIVATE)
        prefs.edit().clear().putString("secret:legacy-device", "legacy-secret").apply()

        val store = PairingStore(app)

        assertEquals("legacy-secret", store.getSharedSecret())
    }

    @Test
    fun prefersLegacySecretReferencedByStoredDeviceId() {
        val app = RuntimeEnvironment.getApplication()
        val prefs = app.getSharedPreferences("pairing", Context.MODE_PRIVATE)
        prefs.edit()
            .clear()
            .putString("shared_secret_device_id", "device-2")
            .putString("secret:device-2", "device-secret")
            .apply()

        val store = PairingStore(app)

        assertEquals("device-secret", store.getSharedSecret())
    }

    @Test
    fun createPendingPairingGeneratesManualCodeAndQrPayload() {
        val store = PairingStore(RuntimeEnvironment.getApplication())

        val pending = store.createPendingPairing(
            tvName = "Sony Lounge",
            ipAddress = "192.168.10.6",
            port = 7979,
            nowEpochSeconds = 1000L,
        )

        assertEquals("Sony Lounge", pending.tvName)
        assertEquals("192.168.10.6", pending.ipAddress)
        assertEquals(7979, pending.port)
        assertEquals(6, pending.manualCode.length)
        assertTrue(pending.manualCode.all { it in '0'..'9' || it in 'A'..'F' })

        val payload = Json.readTree(pending.qrPayload)
        assertEquals("Sony Lounge", payload.get("tv_name").asText())
        assertEquals("192.168.10.6", payload.get("ip_address").asText())
        assertEquals(7979, payload.get("port").asInt())
        assertEquals(pending.manualCode, payload.get("manual_code").asText())
        assertNotNull(payload.get("device_id").asText())
    }

    @Test
    fun completePendingPairingStoresSharedSecretForCurrentDevice() {
        val store = PairingStore(RuntimeEnvironment.getApplication())
        val pending = store.createPendingPairing(
            tvName = "Sony Lounge",
            ipAddress = "192.168.10.6",
            port = 7979,
            nowEpochSeconds = 1000L,
        )

        val completed = store.completePendingPairing(
            manualCode = pending.manualCode.lowercase(),
            sharedSecret = "secret-xyz",
            nowEpochSeconds = 1005L,
        )

        assertEquals(pending.deviceId, completed?.deviceId)
        assertEquals("secret-xyz", store.getSharedSecret(pending.deviceId))
        assertEquals("secret-xyz", store.getSharedSecret())
    }

    @Test
    fun getOrCreatePendingPairingReusesExistingPendingSession() {
        val store = PairingStore(RuntimeEnvironment.getApplication())

        val first = store.getOrCreatePendingPairing(
            tvName = "Sony Lounge",
            ipAddress = "192.168.10.6",
            port = 7979,
            nowEpochSeconds = 1000L,
        )
        val second = store.getOrCreatePendingPairing(
            tvName = "Sony Lounge",
            ipAddress = "192.168.10.6",
            port = 7979,
            nowEpochSeconds = 1005L,
        )

        assertEquals(first?.manualCode, second?.manualCode)
        assertEquals(first?.deviceId, second?.deviceId)
    }

    @Test
    fun getOrCreatePendingPairingReturnsNullWhenAlreadyPaired() {
        val store = PairingStore(RuntimeEnvironment.getApplication())
        store.saveSharedSecret("device-1", "secret-123")

        val pending = store.getOrCreatePendingPairing(
            tvName = "Sony Lounge",
            ipAddress = "192.168.10.6",
            port = 7979,
            nowEpochSeconds = 1000L,
        )

        assertNull(pending)
        assertNull(store.getPendingPairing(1000L))
    }
}
