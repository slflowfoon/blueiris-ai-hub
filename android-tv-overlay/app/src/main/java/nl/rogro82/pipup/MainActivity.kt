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
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.FileProvider
import nl.rogro82.pipup.Utils.getIpAddress
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : Activity() {
    private val pairingStore by lazy { PairingStore(this) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val textViewConnection = findViewById<TextView>(R.id.textViewConnection)
        val textViewServerAddress = findViewById<TextView>(R.id.textViewServerAddress)
        val textViewPairingCode = findViewById<TextView>(R.id.textViewPairingCode)
        val textViewPairingStatus = findViewById<TextView>(R.id.textViewPairingStatus)
        val buttonCheckUpdate = findViewById<Button>(R.id.buttonCheckUpdate)

        when (val ipAddress = getIpAddress()) {
            is String -> {
                textViewConnection.setText(R.string.server_running)
                textViewServerAddress.apply {
                    visibility = View.VISIBLE
                    text = resources.getString(R.string.server_address, ipAddress, PiPupService.SERVER_PORT)
                }
                val pending = pairingStore.getOrCreatePendingPairing(
                    tvName = Build.MODEL ?: "Android TV",
                    ipAddress = ipAddress,
                    port = PiPupService.SERVER_PORT,
                )
                val isPaired = pairingStore.getSharedSecret() != null
                textViewPairingStatus.setText(if (isPaired) R.string.pairing_status_paired else R.string.pairing_status_waiting)
                textViewPairingCode.text = if (pending != null) pending.manualCode else getString(R.string.pairing_code_hidden)
            }
            else -> {
                textViewConnection.setText(R.string.no_network_connection)
                textViewServerAddress.visibility = View.INVISIBLE
                textViewPairingCode.text = "-"
            }
        }

        buttonCheckUpdate.setOnClickListener { checkForUpdate(buttonCheckUpdate) }

        val serviceIntent = Intent(this, PiPupService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }
    }

    private fun checkForUpdate(btn: Button) {
        btn.isEnabled = false
        btn.text = "Checking…"
        Thread {
            try {
                val conn = URL("https://api.github.com/repos/slflowfoon/blueiris-ai-hub/releases/latest")
                    .openConnection() as HttpURLConnection
                conn.setRequestProperty("Accept", "application/vnd.github+json")
                conn.connectTimeout = 8000
                conn.readTimeout = 8000
                val body = conn.inputStream.bufferedReader().readText()
                val json = JSONObject(body)
                val latestTag = json.getString("tag_name").trimStart('v')
                val currentVersion = BuildConfig.VERSION_NAME.trimStart('v')

                if (latestTag == currentVersion) {
                    runOnUiThread {
                        btn.isEnabled = true
                        btn.text = "Check for Updates"
                        Toast.makeText(this, "Already up to date (v$currentVersion)", Toast.LENGTH_SHORT).show()
                    }
                    return@Thread
                }

                // Find APK asset URL
                val assets = json.getJSONArray("assets")
                var apkUrl: String? = null
                for (i in 0 until assets.length()) {
                    val asset = assets.getJSONObject(i)
                    if (asset.getString("name").endsWith(".apk")) {
                        apkUrl = asset.getString("browser_download_url")
                        break
                    }
                }

                if (apkUrl == null) {
                    runOnUiThread {
                        btn.isEnabled = true
                        btn.text = "Check for Updates"
                        Toast.makeText(this, "v$latestTag available but no APK attached yet", Toast.LENGTH_LONG).show()
                    }
                    return@Thread
                }

                runOnUiThread { btn.text = "Downloading…" }
                downloadAndInstall(apkUrl, latestTag, btn)

            } catch (e: Exception) {
                runOnUiThread {
                    btn.isEnabled = true
                    btn.text = "Check for Updates"
                    Toast.makeText(this, "Update check failed", Toast.LENGTH_SHORT).show()
                }
            }
        }.also { it.isDaemon = true; it.start() }
    }

    private fun downloadAndInstall(apkUrl: String, version: String, btn: Button) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
                !packageManager.canRequestPackageInstalls()
            ) {
                runOnUiThread {
                    btn.isEnabled = true
                    btn.text = "Check for Updates"
                    Toast.makeText(this, "Enable 'Install unknown apps' for PiPup first", Toast.LENGTH_LONG).show()
                    startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
                }
                return
            }

            val apkFile = File(cacheDir, "pipup-update.apk")
            val conn = URL(apkUrl).openConnection() as HttpURLConnection
            conn.connectTimeout = 10000
            conn.readTimeout = 60000
            conn.connect()
            val total = conn.contentLength

            FileOutputStream(apkFile).use { out ->
                conn.inputStream.use { input ->
                    val buf = ByteArray(8192)
                    var downloaded = 0
                    var n: Int
                    while (input.read(buf).also { n = it } >= 0) {
                        out.write(buf, 0, n)
                        downloaded += n
                        if (total > 0) {
                            val pct = (downloaded * 100) / total
                            runOnUiThread { btn.text = "Downloading $pct%" }
                        }
                    }
                }
            }

            val uri = FileProvider.getUriForFile(this, "nl.rogro82.pipup.fileprovider", apkFile)
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK
            }
            runOnUiThread {
                btn.isEnabled = true
                btn.text = "Check for Updates"
                startActivity(intent)
            }
        } catch (e: Exception) {
            runOnUiThread {
                btn.isEnabled = true
                btn.text = "Check for Updates"
                Toast.makeText(this, "Download failed: ${e.message}", Toast.LENGTH_SHORT).show()
            }
        }
    }
}
