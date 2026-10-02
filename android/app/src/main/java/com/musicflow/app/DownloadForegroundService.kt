package com.musicflow.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.net.wifi.WifiManager
import androidx.core.app.NotificationCompat

/**
 * Foreground service showing a persistent notification during a
 * download/transfer: without it, Android may suspend the network or kill the process
 * a few minutes after the app goes to the background (sleep, app switch).
 * Started/stopped from the page's JS via window.Android.startDownloadService()/stopDownloadService().
 * Tapping the notification reopens the app (“View download” button).
 */
class DownloadForegroundService : Service() {

    companion object {
        private const val CHANNEL_ID = "musicflow_downloads"
        private const val NOTIF_ID = 1001
        const val EXTRA_TEXT = "text"
        const val EXTRA_PERCENT = "percent" // -1 = no progress bar (indeterminate)
    }

    // Without these locks, the CPU and Wi-Fi fall asleep with the screen off: the download
    // used to stop after ~2 minutes in the background.
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIF_ID, buildNotification("Downloading…", -1))
        try {
            wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MusicFlow:telechargement").apply {
                    setReferenceCounted(false); acquire(6 * 60 * 60 * 1000L) // 6 h maximum
                }
        } catch (e: Exception) { }
        try {
            @Suppress("DEPRECATION")
            wifiLock = (applicationContext.getSystemService(WIFI_SERVICE) as WifiManager)
                .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "MusicFlow:wifi").apply {
                    setReferenceCounted(false); acquire()
                }
        } catch (e: Exception) { }
    }

    override fun onDestroy() {
        try { wakeLock?.takeIf { it.isHeld }?.release() } catch (e: Exception) { }
        try { wifiLock?.takeIf { it.isHeld }?.release() } catch (e: Exception) { }
        super.onDestroy()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val text = intent?.getStringExtra(EXTRA_TEXT)
        if (!text.isNullOrBlank()) {
            val percent = intent?.getIntExtra(EXTRA_PERCENT, -1) ?: -1
            val nm = getSystemService(NotificationManager::class.java)
            nm.notify(NOTIF_ID, buildNotification(text, percent))
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(text: String, percent: Int): android.app.Notification {
        val openAppIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M)
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        else PendingIntent.FLAG_UPDATE_CURRENT
        val pendingIntent = PendingIntent.getActivity(this, 0, openAppIntent, flags)

        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("MusicFlow")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setContentIntent(pendingIntent)
            .addAction(0, "View download", pendingIntent)

        if (percent in 0..100) builder.setProgress(100, percent, false)
        return builder.build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "MusicFlow downloads", NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }
}
