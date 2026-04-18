package io.slflowfoon.blueirisaihub.tv

import com.fasterxml.jackson.databind.JsonNode
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

data class SignedNotifyPayload(
    val camera_name: String? = null,
    val camera_id: String? = null,
    val rtsp_url: String? = null,
    val mjpg_url: String? = null,
    val duration: Int? = null,
    val tv_group: String? = null,
    val mute_audio: Boolean = false,
    val request_id: String? = null,
    val tag: String? = null
) {
    fun toPopupProps(): PopupProps {
        val mjpgUrl = mjpg_url?.trim().orEmpty()
        val rtspUrl = rtsp_url?.trim().orEmpty()

        val media = when {
            mjpgUrl.isNotEmpty() -> PopupProps.Media.Mjpeg(mjpgUrl)
            rtspUrl.isNotEmpty() -> PopupProps.Media.Video(rtspUrl, muteAudio = mute_audio)
            else -> throw SecurityException("missing rtsp_url or mjpg_url")
        }

        return PopupProps(
            duration = duration ?: PopupProps.DEFAULT_DURATION,
            title = camera_name,
            message = null,
            media = media
        )
    }
}

data class SignedNotifySigning(
    val algorithm: String? = null,
    val payload_hash: String? = null,
    val payload_format: String? = null
)

class Auth(private val secret: String) {
    fun sign(payload: String): String {
        val mac = Mac.getInstance(HMAC_SHA256)
        mac.init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), HMAC_SHA256))
        return mac.doFinal(payload.toByteArray(Charsets.UTF_8)).toHex()
    }

    fun verifySignedEnvelope(envelopeBody: String): SignedNotifyPayload {
        val envelope = Json.readTree(envelopeBody) ?: throw SecurityException("invalid signed envelope")
        if (!envelope.isObject) {
            throw SecurityException("invalid signed envelope")
        }

        val payloadNode = envelope.get("payload") ?: throw SecurityException("missing payload")
        val signature = envelope.get("signature")?.asText()?.trim()?.lowercase()
            ?: throw SecurityException("missing signature")

        val signingNode = envelope.get("signing")
        if (signingNode != null && signingNode.isObject) {
            val signing = Json.treeToValue(signingNode, SignedNotifySigning::class.java)
            val algorithm = signing.algorithm?.trim()?.lowercase()
            if (algorithm != null && algorithm != "hmac-sha256") {
                throw SecurityException("unsupported signing algorithm")
            }
            val payloadFormat = signing.payload_format?.trim()?.lowercase()
            if (payloadFormat != null && payloadFormat != "json") {
                throw SecurityException("unsupported payload format")
            }
            val payloadHash = signing.payload_hash?.trim()?.lowercase()
            val canonicalPayload = canonicalize(payloadNode)
            if (payloadHash != null && payloadHash != sha256Hex(canonicalPayload)) {
                throw SecurityException("payload hash mismatch")
            }
        }

        val canonicalPayload = canonicalize(payloadNode)
        if (signature != sign(canonicalPayload).lowercase()) {
            throw SecurityException("invalid signature")
        }

        return Json.treeToValue(payloadNode, SignedNotifyPayload::class.java)
    }

    fun buildPopup(envelopeBody: String): PopupProps {
        return verifySignedEnvelope(envelopeBody).toPopupProps()
    }

    private fun canonicalize(node: JsonNode): String {
        return when {
            node.isObject -> {
                val fieldNames = mutableListOf<String>()
                node.fieldNames().forEachRemaining { fieldNames.add(it) }
                fieldNames.sort()
                fieldNames.joinToString(separator = ",", prefix = "{", postfix = "}") { name ->
                    "${escapeJsonString(name)}:${canonicalize(node.get(name))}"
                }
            }
            node.isArray -> {
                val items = mutableListOf<String>()
                node.elements().forEachRemaining { items.add(canonicalize(it)) }
                items.joinToString(separator = ",", prefix = "[", postfix = "]")
            }
            node.isTextual -> escapeJsonString(node.asText())
            node.isNumber -> node.numberValue().toString()
            node.isBoolean -> node.asBoolean().toString()
            node.isNull -> "null"
            else -> escapeJsonString(node.asText())
        }
    }

    private fun escapeJsonString(value: String): String {
        val out = StringBuilder(value.length + 2)
        out.append('"')
        var index = 0
        while (index < value.length) {
            val codePoint = value.codePointAt(index)
            when (codePoint) {
                0x22 -> out.append("\\\"")
                0x5C -> out.append("\\\\")
                0x08 -> out.append("\\b")
                0x0C -> out.append("\\f")
                0x0A -> out.append("\\n")
                0x0D -> out.append("\\r")
                0x09 -> out.append("\\t")
                else -> {
                    if (codePoint < 0x20 || codePoint > 0x7E) {
                        appendUnicodeEscape(out, codePoint)
                    } else {
                        out.append(codePoint.toChar())
                    }
                }
            }
            index += Character.charCount(codePoint)
        }
        out.append('"')
        return out.toString()
    }

    private fun appendUnicodeEscape(out: StringBuilder, codePoint: Int) {
        if (codePoint <= 0xFFFF) {
            out.append("\\u")
            out.append(codePoint.toString(16).padStart(4, '0'))
            return
        }

        val chars = Character.toChars(codePoint)
        out.append("\\u")
        out.append(chars[0].code.toString(16).padStart(4, '0'))
        out.append("\\u")
        out.append(chars[1].code.toString(16).padStart(4, '0'))
    }

    private fun sha256Hex(value: String): String {
        val digest = java.security.MessageDigest.getInstance(SHA_256)
        return digest.digest(value.toByteArray(Charsets.UTF_8)).toHex()
    }

    private fun ByteArray.toHex(): String {
        return joinToString(separator = "") { byte -> "%02x".format(byte) }
    }

    private companion object {
        const val HMAC_SHA256 = "HmacSHA256"
        const val SHA_256 = "SHA-256"
    }
}
