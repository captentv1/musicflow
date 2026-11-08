package com.musicflow.app

import android.app.DownloadManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import androidx.core.app.NotificationCompat
import androidx.core.content.pm.PackageInfoCompat
import java.io.File

object UpdateManager {
    private const val PREFS = "maj"
    private const val KEY_ID = "id"
    private const val UPDATE_CHANNEL = "musicflow_maj"
    private const val NOTIF_ID = 4242
    const val EXTRA_INSTALLER = "installer_maj"

    fun file(ctx: Context): File =
        File(ctx.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: File(ctx.filesDir, "Download"), "MusicFlow-maj.apk")

    private fun dm(ctx: Context) = ctx.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
    fun currentId(ctx: Context): Long = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getLong(KEY_ID, -1L)
    private fun forget(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove(KEY_ID).apply()

    fun startDownload(ctx: Context, url: String): Long {
        val previousId = currentId(ctx)
        if (previousId >= 0) dm(ctx).remove(previousId)
        val f = file(ctx)
        f.parentFile?.mkdirs(); f.delete()
        val req = DownloadManager.Request(Uri.parse(url))
            .setTitle("MusicFlow update")
            .setDescription("Downloading the new version")
            .setMimeType("application/vnd.android.package-archive")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE)
            .setDestinationUri(Uri.fromFile(f))
        val id = dm(ctx).enqueue(req)
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putLong(KEY_ID, id).apply()
        return id
    }

    fun state(ctx: Context): Pair<Int, Int>? {
        val id = currentId(ctx)
        if (id < 0) return null
        dm(ctx).query(DownloadManager.Query().setFilterById(id)).use { c ->
            if (!c.moveToFirst()) { forget(ctx); return null }
            val status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
            val downloaded = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR))
            val total = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES))
            return status to (if (total > 0) (downloaded * 100 / total).toInt() else -1)
        }
    }

    fun finished(ctx: Context) = forget(ctx)

    // Security: only install MusicFlow itself, newer, signed with the same key.
    fun verify(ctx: Context, apk: File) {
        val pm = ctx.packageManager
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) PackageManager.GET_SIGNING_CERTIFICATES else 0
        val newInfo = pm.getPackageArchiveInfo(apk.path, flags)
        if (newInfo == null || newInfo.packageName != ctx.packageName) {
            apk.delete(); throw Exception("this file is not MusicFlow")
        }
        val current = pm.getPackageInfo(ctx.packageName, flags)
        if (PackageInfoCompat.getLongVersionCode(newInfo) <= PackageInfoCompat.getLongVersionCode(current)) {
            apk.delete(); throw Exception("this version is not newer")
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val signers = newInfo.signingInfo?.apkContentsSigners
            val mine = current.signingInfo?.apkContentsSigners
            if (signers != null && mine != null && signers.none { s -> mine.any { it == s } }) {
                apk.delete(); throw Exception("signature differs from the installed app")
            }
        }
    }

    fun readyVersion(ctx: Context): String {
        val f = file(ctx)
        if (!f.exists() || currentId(ctx) >= 0) return ""
        return try { verify(ctx, f); ctx.packageManager.getPackageArchiveInfo(f.path, 0)?.versionName ?: "" }
        catch (e: Exception) { "" }
    }

    fun notifyReady(ctx: Context, version: String) {
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(NotificationChannel(UPDATE_CHANNEL, "MusicFlow updates", NotificationManager.IMPORTANCE_DEFAULT))
        }
        val openIntent = PendingIntent.getActivity(ctx, 0,
            Intent(ctx, MainActivity::class.java).putExtra(EXTRA_INSTALLER, true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        nm.notify(NOTIF_ID, NotificationCompat.Builder(ctx, UPDATE_CHANNEL)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("MusicFlow $version is ready")
            .setContentText("Tap to install the update")
            .setContentIntent(openIntent).setAutoCancel(true).build())
    }

    fun clearNotification(ctx: Context) =
        (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(NOTIF_ID)
}

class UpdateDownloadReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L)
        if (id < 0 || id != UpdateManager.currentId(ctx)) return
        if (MainActivity.updateFollowedByPage) return
        val state = UpdateManager.state(ctx)
        UpdateManager.finished(ctx)
        if (state?.first != DownloadManager.STATUS_SUCCESSFUL) return
        val version = try {
            val f = UpdateManager.file(ctx)
            UpdateManager.verify(ctx, f)
            ctx.packageManager.getPackageArchiveInfo(f.path, 0)?.versionName ?: ""
        } catch (e: Exception) { return }
        UpdateManager.notifyReady(ctx, version)
    }
}
