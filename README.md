<div align="center">

# MusicFlow

**Download your music with the right title, artist, album, cover art and lyrics.**
Android and Windows · free · open source (GPL-3.0) · no account, no ads

[![Download the APK](https://img.shields.io/github/v/release/captentv1/musicflow?label=Download%20APK&style=for-the-badge)](https://github.com/captentv1/musicflow/releases/latest)
[![License GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue?style=for-the-badge)](LICENSE)
![Android 8+](https://img.shields.io/badge/Android-8%2B-3ddc84?style=for-the-badge)

[Website](https://captentv1.github.io/musicflow/) · [Download](https://github.com/captentv1/musicflow/releases/latest) · [Report a bug](https://github.com/captentv1/musicflow/issues) · [🇫🇷 Français](#-français)

</div>

<p align="center">
  <img src="docs/captures/1-recherche.png" width="19%" alt="Search">
  <img src="docs/captures/2-lien.png" width="19%" alt="Playlist from a link">
  <img src="docs/captures/3-artiste.png" width="19%" alt="Artist discography">
  <img src="docs/captures/4-telechargements.png" width="19%" alt="Download queue">
  <img src="docs/captures/8-themes.png" width="19%" alt="16 themes">
</p>

## Features

MusicFlow is a **free music downloader** for **Android** and **Windows**. Paste a **Spotify**, **YouTube**, **Deezer** or **Apple Music** playlist, album or track link, or just type a title, and get **MP3** or **FLAC** files with proper **ID3 tags**, **album art** and **synced lyrics** (LRC). Companion player: [MusicFlow Play](https://github.com/captentv1/musicflow-play).


**Find**
- Search by title (several sources), by artist (full discography), or paste many titles at once
- Playlist, album and track links: several links at a time, without duplicates
- Spotify radio (PC): the ~50 track radio of a song or an artist, from its name or its Spotify link
- Discover: global, per-country and per-genre charts, popular playlists
- Voice search, history, suggestions, filters (no live/remix, duration)

**Download**
- Download queue, several playlists in parallel, pause / resume / stop
- Keeps downloading in the background; pauses without internet and resumes on its own
- Never re-downloads what is already in your folder; duplicate detection
- Picks the best video (duration check, live/karaoke versions skipped), compare versions, “other version” in one tap
- MP3 128/192/320, FLAC, WAV, Opus, AAC; leading and trailing silence removed

**Clean files**
- Title, artist, album, genre, track number, year, ISRC, label
- Cover art up to 1000 px, embedded lyrics + synced `.lrc` file (every language; LRCLIB, lyrics.ovh as backup)
- Library: favorites, tags, tag editor, re-tagging of older files, shrink big files
- Checks: suspicious duration, clipped or nearly silent audio

**Comfort**
- 16 themes (glass style, light, OLED black, automatic), text size, accent color, compact mode
- Interactive tutorial on every screen
- “Share → MusicFlow” from another app (Android)
- Follow playlists and artists: new tracks and new releases detected
- Built-in diagnostics, error log, settings backup and restore

## Install

**Android (8.0 or newer, 64-bit)**
1. Download [`MusicFlow.apk`](https://github.com/captentv1/musicflow/releases/latest/download/MusicFlow.apk).
2. Open it and allow installing from this source.

**Windows 10/11**
1. Download [`MusicFlow.exe`](https://github.com/captentv1/musicflow/releases/latest/download/MusicFlow.exe).
2. Double-click it: MusicFlow opens in your browser. Nothing else to install.
   (If Windows shows “Windows protected your PC”: *More info › Run anyway*.)

**From source (Windows)**
```bash
git clone https://github.com/captentv1/musicflow.git
cd musicflow
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py        # then open http://127.0.0.1:5090
```

## Build the APK

Requirements: JDK 17, Android SDK (platform 36), Python 3.13 (required by Chaquopy for the build).

```bash
cd android
gradlew assembleRelease           # → app/build/outputs/apk/release/app-release.apk
```

The Python 3.13 path is set in `android/app/build.gradle` (`buildPython`).
Release signing reads a keystore kept outside the repository; without it, use `gradlew assembleDebug`.
The whole interface is a single file, `index.html`, copied to `android/app/src/main/assets/` and `android/app/src/main/python/`.

## How it works

A small Python server (Flask) does the work; the interface is a web page, shown in the
browser on PC and inside the app on Android (via [Chaquopy](https://chaquo.com/chaquopy/)).

| Role | Tool |
|---|---|
| Audio download | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| Conversion (Android) | [FFmpegKit](https://github.com/ffmpegkit-maintained/ffmpeg) |
| Tags and covers | [mutagen](https://github.com/quodlibet/mutagen) |
| Album info, artists, charts | Public Deezer API, iTunes Search, Apple Music charts |
| Lyrics | [LRCLIB](https://lrclib.net), [lyrics.ovh](https://lyrics.ovh) as backup |

No API key or account is needed.

## ⚠️ Disclaimer

MusicFlow is a tool for **personal use**. You alone are responsible for what you download:
respect copyright and the terms of service of the services you use.
Only download content you are allowed to copy (public domain, Creative Commons, or content
you own the rights to). This project is not affiliated with any music or video service;
names are mentioned for descriptive purposes only.

## License

[GPL-3.0](LICENSE) — you may use, study, modify and redistribute MusicFlow, provided you keep
the same license and publish your changes.

---

## 🇫🇷 Français

**MusicFlow** est une application **gratuite et open source** pour **Android** et **Windows** qui télécharge votre musique avec le bon titre, l'artiste, l'album, la pochette et les paroles. Collez un lien de playlist, d'album ou de titre **Spotify**, **YouTube**, **Deezer** ou **Apple Music**, ou tapez simplement un titre : vous obtenez des fichiers **MP3** ou **FLAC** bien rangés. Sans compte, sans publicité. Lecteur associé : [MusicFlow Play](https://github.com/captentv1/musicflow-play).

### Fonctionnalités

- **Recherche** par titre, par artiste (discographie complète) ou plusieurs titres d'un coup ; liens de playlists, albums et titres
- **Découvrir** : classements mondiaux, par pays et par genre, playlists populaires ; radio Spotify (PC)
- **File de téléchargement** : plusieurs playlists en parallèle, pause et reprise, continue en arrière-plan et reprend seule après une coupure
- **Jamais de doublon** : ce qui est déjà dans votre dossier n'est pas retéléchargé
- **La bonne version** : vérification de la durée, versions live et karaoké écartées, « autre version » en un geste
- **Formats** : MP3 128/192/320, FLAC, WAV, Opus, AAC ; silences de début et de fin supprimés
- **Fichiers propres** : titre, artiste, album, genre, numéro de piste, année, pochette jusqu'à 1000 px, paroles intégrées et fichier `.lrc` synchronisé
- **Bibliothèque** : favoris, éditeur de tags, suivi de playlists et d'artistes (nouveautés détectées)
- **16 thèmes** (verre, clair, noir OLED, automatique) et tutoriel interactif sur chaque écran

### Installation

**Android (8.0 ou plus récent, 64 bits)**
1. Téléchargez [`MusicFlow.apk`](https://github.com/captentv1/musicflow/releases/latest/download/MusicFlow.apk).
2. Ouvrez-le et autorisez l'installation depuis cette source.

**Windows 10/11**
1. Téléchargez [`MusicFlow.exe`](https://github.com/captentv1/musicflow/releases/latest/download/MusicFlow.exe).
2. Double-cliquez : MusicFlow s'ouvre dans votre navigateur, rien d'autre à installer.
   (Si Windows affiche « Windows a protégé votre ordinateur » : *Informations complémentaires › Exécuter quand même*.)

### Avertissement

MusicFlow est destiné à un **usage personnel**. Vous êtes seul responsable de ce que vous téléchargez : respectez le droit d'auteur et les conditions d'utilisation des services. Ce projet n'est affilié à aucun service de musique ou de vidéo.

---

<sub>Keywords / mots-clés : music downloader, téléchargeur de musique, Spotify playlist downloader, télécharger playlist Spotify, YouTube to MP3, YouTube MP3, Deezer downloader, Apple Music, MP3 320 kbps, FLAC, ID3 tags, album art, pochette, synced lyrics, paroles synchronisées, LRC, yt-dlp GUI, Android APK, Windows, free, gratuit, open source.</sub>
