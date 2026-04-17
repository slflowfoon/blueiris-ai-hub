package nl.rogro82.pipup

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Color
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.WebView
import android.widget.*
import com.bumptech.glide.Glide
import com.bumptech.glide.load.engine.DiskCacheStrategy
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL

// TODO: convert dimensions from px to dp

@SuppressLint("ViewConstructor")
sealed class PopupView(context: Context, val popup: PopupProps) : LinearLayout(context) {

    open fun create() {
        inflate(context, R.layout.popup,this)

        layoutParams = LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT
        ).apply {
            orientation = VERTICAL
            minimumWidth = 240
        }

        setPadding(0,0,0,0)

        val title = findViewById<TextView>(R.id.popup_title)
        val message = findViewById<TextView>(R.id.popup_message)
        val frame = findViewById<FrameLayout>(R.id.popup_frame)

        if(popup.media == null) {
            removeView(frame)
        }

        if(popup.title.isNullOrEmpty()) {
            removeView(title)
        } else {
            title.text = popup.title
            title.textSize = popup.titleSize
            title.setTextColor(Color.parseColor(popup.titleColor))
        }

        if(popup.message.isNullOrEmpty()) {
            removeView(message)
        } else {
            message.text = popup.message
            message.textSize = popup.messageSize
            message.setTextColor(Color.parseColor(popup.messageColor))
        }

        setBackgroundColor(Color.parseColor(popup.backgroundColor))
    }

    open fun destroy() {}

    private class Default(context: Context, popup: PopupProps) : PopupView(context, popup) {
        init { create() }
    }

    private class Video(context: Context, popup: PopupProps, val media: PopupProps.Media.Video): PopupView(context, popup) {
        private lateinit var playerView: PlayerView
        private lateinit var player: ExoPlayer

        init { create() }

        override fun create() {
            super.create()

            visibility = View.INVISIBLE

            val frame = findViewById<FrameLayout>(R.id.popup_frame)

            playerView = PlayerView(context).apply {
                useController = false
                resizeMode = androidx.media3.ui.AspectRatioFrameLayout.RESIZE_MODE_FIT
            }

            player = ExoPlayer.Builder(context).build().apply {
                addListener(object : Player.Listener {
                    override fun onPlaybackStateChanged(playbackState: Int) {
                        if (playbackState == Player.STATE_READY) {
                            playerView.layoutParams = FrameLayout.LayoutParams(
                                media.width,
                                WindowManager.LayoutParams.WRAP_CONTENT
                            ).apply {
                                gravity = Gravity.CENTER
                            }
                            this@Video.visibility = View.VISIBLE
                        }
                    }

                    override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                        Log.e(LOG_TAG, "video playback error: ${error.message}")
                    }
                })
                setMediaItem(MediaItem.fromUri(Uri.parse(media.uri)))
                prepare()
                playWhenReady = true
                volume = if (media.muteAudio) 0f else 1f
            }

            playerView.player = player
            frame.addView(playerView, FrameLayout.LayoutParams(1, 1))
        }

        override fun destroy() {
            try {
                playerView.player = null
                player.release()
            } catch(e: Throwable) {}
        }
    }

    private class Image(context: Context, popup: PopupProps, val media: PopupProps.Media.Image): PopupView(context, popup) {
        init { create() }

        override fun create() {
            super.create()

            val frame = findViewById<FrameLayout>(R.id.popup_frame)

            try {
                val imageView = ImageView(context)

                val layoutParams =
                    FrameLayout.LayoutParams(media.width, WindowManager.LayoutParams.WRAP_CONTENT).apply {
                        gravity = Gravity.CENTER
                    }

                frame.addView(imageView, layoutParams)

                Glide.with(context)
                    .load(Uri.parse(media.uri))
                    .diskCacheStrategy(DiskCacheStrategy.NONE)
                    .skipMemoryCache(true)
                    .into(imageView)

            } catch(e: Throwable) {
                removeView(frame)
            }
        }
    }

    private class Bitmap(context: Context, popup: PopupProps, val media: PopupProps.Media.Bitmap): PopupView(context, popup) {
        var mImageView: ImageView? = null

        init { create() }

        override fun create() {
            super.create()

            val frame = findViewById<FrameLayout>(R.id.popup_frame)
            mImageView = ImageView(context).apply {
                setImageBitmap(media.image)
            }

            val scaledHeight = ((media.width.toFloat() / media.image.width) * media.image.height).toInt()
            val layoutParams =
                FrameLayout.LayoutParams(media.width, scaledHeight).apply {
                    gravity = Gravity.CENTER
                }

            frame.addView(mImageView, layoutParams)
        }

        override fun destroy() {
            try {
                mImageView?.setImageDrawable(null)
                media.image.recycle()
            } catch(e: Throwable) {}
        }
    }

    private class Mjpeg(context: Context, popup: PopupProps, val media: PopupProps.Media.Mjpeg): PopupView(context, popup) {
        private val mainHandler = Handler(Looper.getMainLooper())
        private var imageView: ImageView? = null
        private var imageLayoutParams: FrameLayout.LayoutParams? = null
        @Volatile private var running = false
        private var thread: Thread? = null

        init { create() }

        override fun create() {
            super.create()
            visibility = View.INVISIBLE
            val frame = findViewById<FrameLayout>(R.id.popup_frame)
            imageView = ImageView(context).apply {
                scaleType = ImageView.ScaleType.FIT_CENTER
                adjustViewBounds = true
            }
            imageLayoutParams = FrameLayout.LayoutParams(media.width, FrameLayout.LayoutParams.WRAP_CONTENT).apply {
                gravity = Gravity.CENTER
            }
            frame.addView(imageView, imageLayoutParams)
            running = true
            thread = Thread {
                try {
                    val conn = (URL(media.uri).openConnection() as HttpURLConnection).apply {
                        connectTimeout = 5000
                        readTimeout = 30000
                        connect()
                    }
                    val stream = conn.inputStream
                    val buf = ByteArrayOutputStream()
                    var prev = -1
                    val tmp = ByteArray(4096)
                    while (running) {
                        val n = stream.read(tmp)
                        if (n < 0) break
                        for (i in 0 until n) {
                            val b = tmp[i].toInt() and 0xFF
                            buf.write(b)
                            // detect JPEG EOI
                            if (prev == 0xFF && b == 0xD9) {
                                val bytes = buf.toByteArray()
                                // find SOI start
                                val soiIdx = run {
                                    var idx = -1
                                    for (k in 0 until bytes.size - 1) {
                                        if ((bytes[k].toInt() and 0xFF) == 0xFF && (bytes[k+1].toInt() and 0xFF) == 0xD8) {
                                            idx = k; break
                                        }
                                    }
                                    idx
                                }
                                if (soiIdx >= 0) {
                                    val jpegBytes = bytes.copyOfRange(soiIdx, bytes.size)
                                    val bmp = BitmapFactory.decodeByteArray(jpegBytes, 0, jpegBytes.size)
                                    if (bmp != null) {
                                        mainHandler.post {
                                            val targetHeight = scaledHeight(media.width, bmp.width, bmp.height)
                                            imageLayoutParams?.height = targetHeight
                                            imageView?.layoutParams = imageLayoutParams
                                            imageView?.setImageBitmap(bmp)
                                            this@Mjpeg.visibility = View.VISIBLE
                                        }
                                    }
                                }
                                buf.reset()
                            }
                            prev = b
                        }
                    }
                    stream.close()
                } catch (e: Exception) {
                    Log.e(LOG_TAG, "MJPEG stream error: ${e.message}")
                }
            }.also { it.isDaemon = true; it.start() }
        }

        override fun destroy() {
            running = false
            thread?.interrupt()
            imageView?.setImageDrawable(null)
        }
    }

    private class Web(context: Context, popup: PopupProps, val media: PopupProps.Media.Web): PopupView(context, popup) {
        private lateinit var webView: WebView
        init { create() }

        override fun create() {
            super.create()

            val frame = findViewById<FrameLayout>(R.id.popup_frame)
            webView = WebView(context).apply {
                with(settings) {
                    loadWithOverviewMode = true
                    useWideViewPort = true
                    javaScriptEnabled = true
                    domStorageEnabled = true
                    mediaPlaybackRequiresUserGesture = false
                    //  allowContentAccess = true
                }
                loadUrl(media.uri)
            }
            webView.setInitialScale(100)
            webView.setBackgroundColor(Color.TRANSPARENT)


            val layoutParams = FrameLayout.LayoutParams(
                media.width,
                media.height
            ).apply {
                gravity = Gravity.CENTER
            }

            frame.addView(webView, layoutParams)
        }

        override fun destroy() {
            try {
                webView.destroy();
            } catch(e: Throwable) {}
        }
    }

    companion object {
        const val LOG_TAG = "PopupView"

        internal fun scaledHeight(targetWidth: Int, sourceWidth: Int, sourceHeight: Int): Int {
            if (targetWidth <= 0 || sourceWidth <= 0 || sourceHeight <= 0) {
                return ViewGroup.LayoutParams.WRAP_CONTENT
            }
            return ((targetWidth.toFloat() / sourceWidth) * sourceHeight).toInt().coerceAtLeast(1)
        }

        fun build(context: Context, popup: PopupProps): PopupView
        {
            return when (popup.media) {
                is PopupProps.Media.Web -> Web(context, popup, popup.media)
                is PopupProps.Media.Video -> Video(context, popup, popup.media)
                is PopupProps.Media.Image -> Image(context, popup, popup.media)
                is PopupProps.Media.Bitmap -> Bitmap(context, popup, popup.media)
                is PopupProps.Media.Mjpeg -> Mjpeg(context, popup, popup.media)
                else -> Default(context, popup)
            }
        }
    }
}
