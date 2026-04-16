/*
 * Copyright (C) 2017 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
 * in compliance with the License. You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software distributed under the License
 * is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
 * or implied. See the License for the specific language governing permissions and limitations under
 * the License.
 */

package nl.rogro82.pipup

import android.app.Activity
import android.graphics.Bitmap
import android.graphics.Color
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import nl.rogro82.pipup.Utils.getIpAddress

class MainActivity : Activity() {
    private val pairingStore by lazy { PairingStore(this) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // start service in foreground

        val textViewConnection = findViewById<TextView>(R.id.textViewConnection)
        val textViewServerAddress = findViewById<TextView>(R.id.textViewServerAddress)
        val textViewPairingCode = findViewById<TextView>(R.id.textViewPairingCode)
        val textViewPairingStatus = findViewById<TextView>(R.id.textViewPairingStatus)
        val imageViewQrCode = findViewById<ImageView>(R.id.imageViewQrCode)

        when(val ipAddress = getIpAddress()) {
            is String -> {
                textViewConnection.setText(R.string.server_running)
                textViewServerAddress.apply {
                    visibility = View.VISIBLE
                    text = resources.getString(
                        R.string.server_address,
                        ipAddress,
                        PiPupService.SERVER_PORT
                    )
                }
                val pending = pairingStore.getOrCreatePendingPairing(
                    tvName = Build.MODEL ?: "Android TV",
                    ipAddress = ipAddress,
                    port = PiPupService.SERVER_PORT,
                )
                val isPaired = pairingStore.getSharedSecret() != null
                textViewPairingStatus.setText(if (isPaired) R.string.pairing_status_paired else R.string.pairing_status_waiting)
                if (pending != null) {
                    textViewPairingCode.text = pending.manualCode
                    imageViewQrCode.visibility = View.VISIBLE
                    imageViewQrCode.setImageBitmap(buildQrCode(pending.qrPayload))
                } else {
                    textViewPairingCode.setText(R.string.pairing_code_hidden)
                    imageViewQrCode.visibility = View.INVISIBLE
                    imageViewQrCode.setImageBitmap(null)
                }
            }
            else -> {
                textViewConnection.setText(R.string.no_network_connection)
                textViewServerAddress.visibility = View.INVISIBLE
                textViewPairingCode.text = "-"
                imageViewQrCode.setImageBitmap(null)
            }
        }


        val serviceIntent = Intent(this, PiPupService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }
    }

    private fun buildQrCode(payload: String): Bitmap {
        val writer = QRCodeWriter()
        val matrix = writer.encode(payload, BarcodeFormat.QR_CODE, 512, 512)
        val width = matrix.width
        val height = matrix.height
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        for (x in 0 until width) {
            for (y in 0 until height) {
                bitmap.setPixel(x, y, if (matrix[x, y]) Color.BLACK else Color.WHITE)
            }
        }
        return bitmap
    }
}
