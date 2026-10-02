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
 * Service de premier plan (foreground) affichant une notification persistante pendant un
 * téléchargement/transfert — sans lui, Android peut suspendre le réseau ou tuer le process
 * quelques minutes après que l'app passe en arrière-plan (mise en veille, changement d'app).
 * Démarré/arrêté depuis le JS de la page via window.Android.startDownloadService()/stopDownloadService().
 * Tapoter la notification rouvre l'app (bouton « voir le téléchargement »).
 */
class DownloadForegroundService : Service() {

    companion object {
        private const val CHANNEL_ID = "musicflow_downloads"
        private const val NOTIF_ID = 1001
        const val EXTRA_TEXT = "text"
        const val EXTRA_PERCENT = "percent" // -1 = pas de barre de progression (indéterminé)
    }

    // Sans ces verrous, le processeur et le Wi-Fi s'endorment écran éteint : le téléchargement
    // s'arrêtait au bout de ~2 minutes en arrière-plan.
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForeground(NOTIF_ID, buildNotification("Téléchargement en cours…", -1))
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
            .addAction(0, "Voir le téléchargement", pendingIntent)

        if (percent in 0..100) builder.setProgress(100, percent, false)
        return builder.build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "Téléchargements MusicFlow", NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }
}
