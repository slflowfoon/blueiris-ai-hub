package io.slflowfoon.blueirisaihub.tv

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class AuthTest {
    @Test
    fun signsCanonicalPayloadWithHexHmacSha256() {
        val auth = Auth("secret-123")

        assertEquals(
            "360d72cb0bc38bebab0cfc32a0183c7120bf36ee8387018ea665bf332f94a6a0",
            auth.sign("""{"camera_name":"Driveway","duration":20,"request_id":"req-1","rtsp_url":"rtsp://cam/live","tag":"[tag]","tv_group":"driveway"}""")
        )
    }

    @Test
    fun signsCanonicalPayloadWithEscapedNonAsciiText() {
        val auth = Auth("secret-123")

        assertEquals(
            "2f979f2575c03eb2b21f005f72e6dcb367baa8c331a8fff4d821a2e72d83aa9a",
            auth.sign("""{"camera_name":"Caf\u00e9","duration":20,"request_id":"req-\u00f1","rtsp_url":"rtsp://cam/live","tag":"[t\u00e4g]","tv_group":"driv\u00e9way"}""")
        )
    }

    @Test
    fun verifiesSignedEnvelopeWithCanonicalPayloadOrdering() {
        val auth = Auth("secret-123")
        val envelope = """
            {
              "payload": {
                "rtsp_url": "rtsp://cam/live",
                "duration": 20,
                "tag": "[tag]",
                "request_id": "req-1",
                "tv_group": "driveway",
                "camera_name": "Driveway"
              },
              "signature": "360d72cb0bc38bebab0cfc32a0183c7120bf36ee8387018ea665bf332f94a6a0",
              "signing": {
                "algorithm": "hmac-sha256",
                "payload_hash": "3cd5ac4246eda91da707b7ed4c2c22e28ddf9bdf3c5c6a1f2c43acca848a2b8a",
                "payload_format": "json"
              }
            }
        """.trimIndent()

        val payload = auth.verifySignedEnvelope(envelope)

        assertEquals("Driveway", payload.camera_name)
        assertEquals("rtsp://cam/live", payload.rtsp_url)
        assertEquals(20, payload.duration)
        assertEquals("req-1", payload.request_id)
    }

    @Test
    fun rejectsInvalidSignature() {
        val auth = Auth("secret-123")

        assertThrows(SecurityException::class.java) {
            auth.verifySignedEnvelope(
                """
                {
                  "payload": {
                    "camera_name": "Driveway",
                    "rtsp_url": "rtsp://cam/live",
                    "duration": 20
                  },
                  "signature": "bad-signature",
                  "signing": {
                    "algorithm": "hmac-sha256",
                    "payload_format": "json"
                  }
                }
                """.trimIndent()
            )
        }
    }

    @Test
    fun rejectsMultipartNotifyRequests() {
        assertThrows(SecurityException::class.java) {
            OverlayReceiverService.parseNotifyPopup(
                "multipart/form-data; boundary=----test",
                "payload=not-json",
                "secret-123"
            )
        }
    }

    @Test
    fun allowsSignedEnvelopeToBecomePopupProps() {
        val auth = Auth("secret-123")
        val popup = auth.buildPopup(
            """
            {
              "payload": {
                "camera_name": "Driveway",
                "rtsp_url": "rtsp://cam/live",
                "duration": 15,
                "request_id": "req-2"
              },
              "signature": "be074cd02fee70f4ef15352fc27b9c32476078861d1308b7710844015422efd3",
              "signing": {
                "algorithm": "hmac-sha256",
                "payload_format": "json"
              }
            }
            """.trimIndent()
        )

        assertEquals(15, popup.duration)
        assertEquals("Driveway", popup.title)
        assertEquals("rtsp://cam/live", (popup.media as PopupProps.Media.Video).uri)
    }

    @Test
    fun verifiesSignedEnvelopeWithEscapedNonAsciiText() {
        val auth = Auth("secret-123")
        val envelope = """
            {
              "payload": {
                "camera_name": "Caf\u00e9",
                "duration": 20,
                "request_id": "req-\u00f1",
                "rtsp_url": "rtsp://cam/live",
                "tag": "[t\u00e4g]",
                "tv_group": "driv\u00e9way"
              },
              "signature": "2f979f2575c03eb2b21f005f72e6dcb367baa8c331a8fff4d821a2e72d83aa9a",
              "signing": {
                "algorithm": "hmac-sha256",
                "payload_hash": "cfa2f9665c8fad00e0f62fdb2a711e528aa8ab0f56eefff6e062c0f688178a18",
                "payload_format": "json"
              }
            }
        """.trimIndent()

        val payload = auth.verifySignedEnvelope(envelope)

        assertEquals("Café", payload.camera_name)
        assertEquals("req-ñ", payload.request_id)
        assertEquals("drivéway", payload.tv_group)
    }
}
