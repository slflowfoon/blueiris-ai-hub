package nl.rogro82.pipup

import android.app.*
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.IBinder
import androidx.core.app.NotificationCompat
import android.util.Log
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import fi.iki.elonen.NanoHTTPD
import fi.iki.elonen.NanoHTTPD.newFixedLengthResponse
import java.nio.charset.StandardCharsets

data class PairingCompleteRequest(
    val manual_code: String? = null,
    val shared_secret: String? = null,
)

data class PairingCompleteResponse(
    val tv_name: String,
    val ip_address: String,
    val port: Int,
    val device_id: String,
)

class PiPupService : Service(), WebServer.Handler {
    private val mHandler: Handler = Handler()
    private var mOverlay: FrameLayout? = null
    private var mPopup: PopupView? = null
    private var mCurrentPopupProps: PopupProps? = null
    private var mExpanded = false
    private val mPairingStore by lazy { PairingStore(this) }
    private lateinit var mWebServer: WebServer

    override fun onCreate() {
        super.onCreate()

        initNotificationChannel("service_channel", "Service channel", "Service channel")

        val pendingIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )

        val mBuilder = NotificationCompat.Builder(this, "service_channel")
            .setContentTitle("PiPup")
            .setContentText("Service running")
            .setContentIntent(pendingIntent)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setCategory(Notification.CATEGORY_SERVICE)
            .setAutoCancel(false)
            .setOngoing(true)

        startForeground(ONGOING_NOTIFICATION_ID, mBuilder.build())

        mWebServer = WebServer(SERVER_PORT, this).apply {
            start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
        }

        Log.d(LOG_TAG, "WebServer started")
    }

    override fun onDestroy() {
        super.onDestroy()

        mWebServer.stop()
    }

    override fun onBind(intent: Intent): IBinder {
        TODO("Return the communication channel to the service.")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    private fun initNotificationChannel(id: String, name: String, description: String) {
        if (Build.VERSION.SDK_INT < 26) {
            return
        }
        val notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(id, name,
            NotificationManager.IMPORTANCE_DEFAULT
        )
        channel.description = description
        notificationManager.createNotificationChannel(channel)
    }

    private fun removePopup(removeOverlay: Boolean = false) {

        mHandler.removeCallbacksAndMessages(null)
        mCurrentPopupProps = null
        mExpanded = false

        mPopup = mPopup?.let {
            it.destroy()
            null
        }

        mOverlay?.apply {

            removeAllViews()
            if (removeOverlay) {
                val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
                wm.removeViewImmediate(mOverlay)

                mOverlay = null
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun createPopup(popup: PopupProps, expanded: Boolean = false) {
        try {

            Log.d(LOG_TAG, "Create popup: $popup expanded=$expanded")

            // remove current popup

            removePopup()
            mCurrentPopupProps = popup
            mExpanded = expanded

            // create or reuse the current overlay

            mOverlay = when (val overlay = mOverlay) {
                is FrameLayout -> overlay
                else -> FrameLayout(this).apply {

                    setPadding(0, 0, 0, 0)
                    isFocusable = true
                    isFocusableInTouchMode = true
                    descendantFocusability = ViewGroup.FOCUS_AFTER_DESCENDANTS
                    setOnKeyListener { _, keyCode, event -> handleOverlayKey(event, keyCode) }

                    val layoutFlags: Int = when {
                        Build.VERSION.SDK_INT >= Build.VERSION_CODES.O -> WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                        else -> WindowManager.LayoutParams.TYPE_TOAST
                    }

                    val params = WindowManager.LayoutParams(
                        WindowManager.LayoutParams.MATCH_PARENT,
                        WindowManager.LayoutParams.MATCH_PARENT,
                        layoutFlags,
                        0,
                        PixelFormat.TRANSLUCENT
                    )

                    val wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
                    wm.addView(this, params)
                }
            }.also {

                // inflate the popup layout

                mPopup = PopupView.build(this, popup).apply {
                    isFocusable = true
                    isFocusableInTouchMode = true
                    setOnKeyListener { _, keyCode, event -> handleOverlayKey(event, keyCode) }
                    setOnClickListener { expandCurrentPopup() }
                }

                it.addView(mPopup, FrameLayout.LayoutParams(
                    if (expanded) ViewGroup.LayoutParams.MATCH_PARENT else ViewGroup.LayoutParams.WRAP_CONTENT,
                    if (expanded) ViewGroup.LayoutParams.MATCH_PARENT else ViewGroup.LayoutParams.WRAP_CONTENT
                ). apply {

                    // position the popup

                    gravity = when(popup.position) {
                        PopupProps.Position.TopRight -> Gravity.TOP or Gravity.END
                        PopupProps.Position.TopLeft -> Gravity.TOP or Gravity.START
                        PopupProps.Position.BottomRight -> Gravity.BOTTOM or Gravity.END
                        PopupProps.Position.BottomLeft -> Gravity.BOTTOM or Gravity.START
                        PopupProps.Position.Center -> Gravity.CENTER
                    }
                })

                it.post {
                    (mPopup ?: it).requestFocus()
                }
            }

            // schedule removal

            if (shouldAutoDismiss(popup, expanded)) {
                mHandler.postDelayed({
                    removePopup(true)
                }, (popup.duration * 1000).toLong())
            }

        } catch (ex: Throwable) {
            ex.printStackTrace()
        }
    }

    private fun handleOverlayKey(event: KeyEvent?, keyCode: Int): Boolean {
        if (event?.action != KeyEvent.ACTION_UP) {
            return false
        }
        return when (keyCode) {
            KeyEvent.KEYCODE_BACK -> {
                removePopup(true)
                true
            }
            KeyEvent.KEYCODE_DPAD_CENTER,
            KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_NUMPAD_ENTER -> {
                expandCurrentPopup()
                true
            }
            else -> false
        }
    }

    private fun expandCurrentPopup() {
        val popup = mCurrentPopupProps ?: return
        if (mExpanded) {
            return
        }
        createPopup(
            buildExpandedPopup(
                popup,
                resources.displayMetrics.widthPixels,
                resources.displayMetrics.heightPixels,
            ),
            expanded = true,
        )
    }

    override fun handleHttpRequest(session: NanoHTTPD.IHTTPSession?): NanoHTTPD.Response {
        return session?.let {
            when(session.method) {
                NanoHTTPD.Method.POST -> {

                    when(session.uri) {
                        "/cancel" -> {
                            mHandler.post {
                                removePopup(true)
                            }
                            OK()
                        }
                        "/notify" -> {
                            try {
                                val contentType = session.headers["content-type"] ?: APPLICATION_JSON
                                val body = readRequestBody(session)
                                val sharedSecret = mPairingStore.getSharedSecret()
                                    ?: throw SecurityException("device not paired")
                                val popup = parseNotifyPopup(contentType, body, sharedSecret)

                                Log.d(LOG_TAG, "received popup: $popup")

                                mHandler.post {
                                    createPopup(popup)
                                }

                                OK("$popup")


                            } catch (ex: SecurityException) {
                                Log.w(LOG_TAG, ex.message ?: "unauthorized request")
                                Unauthorized(ex.message)
                            } catch (ex: Throwable) {
                                Log.e(LOG_TAG, ex.message ?: "invalid request")
                                InvalidRequest(ex.message)
                            }
                        }
                        "/pair/complete" -> {
                            try {
                                val body = readRequestBody(session)
                                val request = Json.readValue(body, PairingCompleteRequest::class.java)
                                val manualCode = request.manual_code?.trim().orEmpty()
                                val sharedSecret = request.shared_secret?.trim().orEmpty()
                                if (manualCode.isEmpty()) {
                                    throw SecurityException("missing manual_code")
                                }
                                if (sharedSecret.isEmpty()) {
                                    throw SecurityException("missing shared_secret")
                                }
                                val pairing = mPairingStore.completePendingPairing(manualCode, sharedSecret)
                                    ?: throw SecurityException("invalid pairing code")
                                val ipAddress = Utils.getIpAddress()
                                    ?: throw SecurityException("device has no network address")
                                val response = PairingCompleteResponse(
                                    tv_name = pairing.tvName,
                                    ip_address = ipAddress,
                                    port = SERVER_PORT,
                                    device_id = pairing.deviceId,
                                )
                                OK(Json.writeValueAsString(response), APPLICATION_JSON)
                            } catch (ex: SecurityException) {
                                Log.w(LOG_TAG, ex.message ?: "unauthorized request")
                                Unauthorized(ex.message)
                            } catch (ex: Throwable) {
                                Log.e(LOG_TAG, ex.message ?: "invalid request")
                                InvalidRequest(ex.message)
                            }
                        }
                        else -> InvalidRequest("unknown uri: ${session.uri}")
                    }
                }
                else -> InvalidRequest("invalid method")
            }
        } ?: InvalidRequest()
    }

    private fun readRequestBody(session: NanoHTTPD.IHTTPSession): String {
        val contentLength = session.headers["content-length"]?.toIntOrNull() ?: 0
        if (contentLength == 0) {
            return ""
        }

        val content = ByteArray(contentLength)
        var totalRead = 0
        while (totalRead < contentLength) {
            val read = session.inputStream.read(content, totalRead, contentLength - totalRead)
            if (read <= 0) {
                break
            }
            totalRead += read
        }

        return String(content, 0, totalRead, StandardCharsets.UTF_8)
    }

    companion object {
        const val LOG_TAG = "PiPupService"
        const val SERVER_PORT = 7979
        const val ONGOING_NOTIFICATION_ID = 123
        const val MULTIPART_FORM_DATA = "multipart/form-data"
        const val APPLICATION_JSON = "application/json"

        fun OK(message: String? = null, contentType: String = "text/plain"): NanoHTTPD.Response =
            newFixedLengthResponse(NanoHTTPD.Response.Status.OK, contentType, message)
        fun Unauthorized(message: String? = null): NanoHTTPD.Response = newFixedLengthResponse(NanoHTTPD.Response.Status.UNAUTHORIZED, "text/plain", "unauthorized: $message")
        fun InvalidRequest(message: String? = null): NanoHTTPD.Response = newFixedLengthResponse(NanoHTTPD.Response.Status.BAD_REQUEST, "text/plain", "invalid request: $message")

        internal fun parseNotifyPopup(contentType: String, body: String, sharedSecret: String): PopupProps {
            return when {
                contentType.startsWith(APPLICATION_JSON) -> Auth(sharedSecret).buildPopup(body)
                contentType.startsWith(MULTIPART_FORM_DATA) -> throw SecurityException("multipart notify is not supported")
                else -> throw SecurityException("invalid content-type")
            }
        }

        internal fun buildExpandedPopup(popup: PopupProps, screenWidth: Int, screenHeight: Int): PopupProps {
            val expandedMedia = when (val media = popup.media) {
                is PopupProps.Media.Video -> media.copy(width = screenWidth)
                is PopupProps.Media.Image -> media.copy(width = screenWidth)
                is PopupProps.Media.Bitmap -> media.copy(width = screenWidth)
                is PopupProps.Media.Mjpeg -> media.copy(width = screenWidth)
                is PopupProps.Media.Web -> media.copy(width = screenWidth, height = screenHeight)
                else -> media
            }
            return popup.copy(
                duration = 0,
                position = PopupProps.Position.Center,
                media = expandedMedia,
            )
        }

        internal fun shouldAutoDismiss(popup: PopupProps, expanded: Boolean): Boolean {
            return !expanded && popup.duration > 0
        }
    }
}
