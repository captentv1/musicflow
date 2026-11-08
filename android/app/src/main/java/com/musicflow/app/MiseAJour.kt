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

/**
 * App update downloaded by Android's download manager: it goes on when the user leaves or
 * closes the app (even after the app is killed), with the system's own progress notification.
 * When it ends with the app closed, a notification offers to install it; the APK stays in the
 * app's folder and the Version card offers it at the next launch.
 */
object MiseAJour {
    private const val PREFS = "maj"
    private const val CLE_ID = "id"
    private const val CANAL = "musicflow_maj"
    private const val NOTIF_ID = 4242
    const val EXTRA_INSTALLER = "installer_maj"

    fun fichier(ctx: Context): File =
        File(ctx.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: File(ctx.filesDir, "Download"), "MusicFlow-maj.apk")

    private fun dm(ctx: Context) = ctx.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
    fun idEnCours(ctx: Context): Long = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getLong(CLE_ID, -1L)
    private fun oublier(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove(CLE_ID).apply()

    fun lancer(ctx: Context, url: String): Long {
        val ancien = idEnCours(ctx)
        if (ancien >= 0) dm(ctx).remove(ancien)
        val f = fichier(ctx)
        f.parentFile?.mkdirs(); f.delete()
        val req = DownloadManager.Request(Uri.parse(url))
            .setTitle("MusicFlow update")
            .setDescription("Downloading the new version")
            .setMimeType("application/vnd.android.package-archive")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE)
            .setDestinationUri(Uri.fromFile(f))
        val id = dm(ctx).enqueue(req)
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putLong(CLE_ID, id).apply()
        return id
    }

    /** Status of the current download: (DownloadManager status, percentage or -1), null if none. */
    fun etat(ctx: Context): Pair<Int, Int>? {
        val id = idEnCours(ctx)
        if (id < 0) return null
        dm(ctx).query(DownloadManager.Query().setFilterById(id)).use { c ->
            if (!c.moveToFirst()) { oublier(ctx); return null }
            val statut = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
            val lu = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR))
            val total = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES))
            return statut to (if (total > 0) (lu * 100 / total).toInt() else -1)
        }
    }

    /** Download finished (successfully or not): it is no longer followed. */
    fun termine(ctx: Context) = oublier(ctx)

    /** The downloaded file must be MusicFlow itself, newer, and signed with the same key. */
    fun verifier(ctx: Context, apk: File) {
        val pm = ctx.packageManager
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) PackageManager.GET_SIGNING_CERTIFICATES else 0
        val nouveau = pm.getPackageArchiveInfo(apk.path, flags)
        if (nouveau == null || nouveau.packageName != ctx.packageName) {
            apk.delete(); throw Exception("this file is not MusicFlow")
        }
        val actuel = pm.getPackageInfo(ctx.packageName, flags)
        if (PackageInfoCompat.getLongVersionCode(nouveau) <= PackageInfoCompat.getLongVersionCode(actuel)) {
            apk.delete(); throw Exception("this version is not newer")
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val signes = nouveau.signingInfo?.apkContentsSigners
            val miens = actuel.signingInfo?.apkContentsSigners
            if (signes != null && miens != null && signes.none { s -> miens.any { it == s } }) {
                apk.delete(); throw Exception("signature differs from the installed app")
            }
        }
    }

    /** versionName of a downloaded update ready to install, or "" (an old one is deleted). */
    fun prete(ctx: Context): String {
        val f = fichier(ctx)
        if (!f.exists() || idEnCours(ctx) >= 0) return ""
        return try { verifier(ctx, f); ctx.packageManager.getPackageArchiveInfo(f.path, 0)?.versionName ?: "" }
        catch (e: Exception) { "" }
    }

    fun notifierPrete(ctx: Context, version: String) {
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(NotificationChannel(CANAL, "MusicFlow updates", NotificationManager.IMPORTANCE_DEFAULT))
        }
        val ouvrir = PendingIntent.getActivity(ctx, 0,
            Intent(ctx, MainActivity::class.java).putExtra(EXTRA_INSTALLER, true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        nm.notify(NOTIF_ID, NotificationCompat.Builder(ctx, CANAL)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("MusicFlow $version is ready")
            .setContentText("Tap to install the update")
            .setContentIntent(ouvrir).setAutoCancel(true).build())
    }

    fun effacerNotification(ctx: Context) =
        (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(NOTIF_ID)
}

/**
 * End of the update download, sent by Android even when MusicFlow is closed. The broadcast
 * proves nothing by itself: the id must be ours and the file is checked (MusiFlow, newer,
 * same signature) before anything is offered.
 */
class MajTermineeReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L)
        if (id < 0 || id != MiseAJour.idEnCours(ctx)) return
        if (MainActivity.majSuivieParLaPage) return   // the open app follows it and installs itself
        val etat = MiseAJour.etat(ctx)
        MiseAJour.termine(ctx)
        if (etat?.first != DownloadManager.STATUS_SUCCESSFUL) return
        val version = try {
            val f = MiseAJour.fichier(ctx)
            MiseAJour.verifier(ctx, f)
            ctx.packageManager.getPackageArchiveInfo(f.path, 0)?.versionName ?: ""
        } catch (e: Exception) { return }
        MiseAJour.notifierPrete(ctx, version)
    }
}
