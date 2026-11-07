# Changelog

## 3.8.0 — 2026-11-07 (PC and Android)

- **Library → Find duplicates.** Lists the same song several times in the folder (same title
  in two formats, remix next to the original). Each file can be listened to (the file
  itself) and deleted (two clicks; the last copy of a song cannot be deleted from there).
- **Another version already in the folder** is shown in the window before downloading
  (the server used to skip it silently as the same song); tick it to keep both versions.
- **The window opens for every list**: search “Download all”, the multi-line list, a
  followed playlist's Check button, an artist's new releases (the automatic check of all
  follows stays silent).

## 3.7.0 — 2026-11-06 (PC and Android)

- **Check before downloading (playlist, artist, Discover).** A window lists the tracks
  already in the folder (left out unless ticked, which replaces the file) and the songs
  present in several versions (remix, remaster, live…), each with a Listen button.
- **Keeping two versions of a song works.** A remix kept next to the original is no longer
  dropped from the queue or skipped by the server as a duplicate.
- **Hardening:** re-tagging, lyrics reading and format conversion only accept audio
  files (conversion could delete the original of any file the page pointed to).
- **Accessibility:** windows are announced as dialogs to screen readers; the new window
  closes with Escape and puts the keyboard on its main button; Listen buttons name the track.

## 3.6.2 — 2026-11-04 (PC and Android) — security

Found by a security review of the app; fixed:

- **Local server usable by other web pages (PC and Android).** A web page open in any
  browser could reach the app's server through DNS rebinding (a domain resolving to
  127.0.0.1): list and read the library, edit tags, delete files (conversion with
  original removed), change the Spotify/YouTube app keys, and on PC make
  `/api/ouvrir-dossier` open any path (`os.startfile` can run a program). On Android
  any other app could do the same. Every `/api/` call now needs a random token known
  only to the MusicFlow page (on Android it never leaves the app), and the `Host`
  header must be the local address. `/api/ouvrir-dossier` only opens local folders.
- **Android: outside pages could use the app bridge.** A link that opened another site
  inside the app gave that site `window.Android` (install an APK, move or delete files).
  Other sites now open in the browser, and the bridge only answers MusicFlow's page.
- **Android: update file checks.** Only `MusicFlow.apk` files from this GitHub
  repository's releases are downloaded; the file must be MusicFlow, newer, and signed
  with the same key before the installer opens.
- **Android: file names and paths from the page** are checked (no `../`, only the app's
  own downloads can be moved); external links limited to web and music apps.
- **Android backup off**: `config.json` holds the app keys and sign-in tokens.
- **PC: port kept by Edge after closing MusicFlow.** Selenium starts Edge with inherited
  handles on Windows, so the browser kept port 5090 and the next launch showed a page
  that never loaded; the server socket is no longer inherited.
- **Artist/Discover: “Clear” also unticked the Albums/EP/Singles/No duplicates
  filters**; it now clears the ticked songs only.
- Remote values (thumbnail, duration, stream address, file path, links) are escaped
  or checked before being put in the page; security headers on every response
  (`X-Frame-Options: DENY`, `nosniff`, `no-referrer`).

## 3.6.1 — 2026-11-02 (PC and Android)

- **Progress bar** while an update downloads, in the update window and in the
  Settings → Version card (moving bar when the size is unknown).
- **What's new** button in Settings → Version.
- Updates are also checked when the app comes back on screen after a while
  (still at most every 6 hours).

## 3.6.0 — 2026-11-01 (PC and Android)

### New: updates inside the app

- When a new version is published on GitHub, a window offers **Install** or
  **Later**. On Android, *Install* downloads the APK inside the app and opens the
  system installer (the user confirms; the first time Android asks to allow
  MusicFlow to install apps). On PC, it downloads the new `.exe`.
- *Later* hides the window for that version; a **Version** card at the top of
  Settings keeps an *Update to x.y.z* button (or *Check for updates*), and a dot on
  the Settings button shows that an update is waiting.

### New: welcome on first launch

- First launch only (on Android stored in the app settings, so clearing the cache
  does not show it again): what MusicFlow is, what's inside, theme choice, default
  format, then one guided tour that switches tab by itself. The **?** button still
  explains only the current tab.

### Interface

- **Artist** and **Discover**: a bar at the bottom of the screen shows how many
  songs are ticked, with *Download* and *Clear* (the button used to be far above
  the list). The Artist card explains how to pick songs; *Artist radio* button.
- **Shortcuts** at the top of Search: top charts, Spotify radio, artist, playlist.
- **Spotify radio** block has a visible title; the tutorial now covers radio,
  Discover and picking songs by artist.
- Status messages written off screen show up as a notice at the bottom.

### Fixed

- **Format reset to Opus**: a format picked in Search, Link or Artist was not
  saved. It is now the default everywhere and kept after a restart.
- Diagnostics showed "MusicFlow 3.0" instead of the real version.
- First launch: a tab tutorial could start before the welcome, which then never
  showed; tab tutorials now wait for the welcome.
- **Android: download stuck on “Conversion…”** when FFmpeg hangs: the conversion is
  now stopped after 5 minutes. If the downloaded file is already plain audio (M4A,
  Opus, MP3) it is kept with its tags instead of failing, and once FFmpeg has hung
  the next downloads take the M4A stream directly (no 5-minute wait per track).
- **Android: download stuck right after “Lyrics added”**: the saturation check
  (FFmpeg analysis) had no time limit; it now gives up after 60 s.

## 3.5.1 — 2026-10-31 (PC and Android)

Found by a full audit: every server route, real downloads in the 6 formats,
interface flows at phone and desktop width, and the APK on an Android 14
emulator (GitHub Actions, `ci/test-apk` branch).

### New: Spotify radio on Android

- Same feature and same API as on PC (`POST /api/spotify-radio`), found by the
  app's hidden WebView (`SpotifyRecherche.radio`) instead of Edge: track/artist
  page → “…” menu → radio item, with the playlist search (`<query> radio`,
  Spotify-made playlists `37i9dQZF1E…`) as a fallback. Page waits sized for slow
  phones (25 s per page, 60 s overall).
- The *Spotify radio* block is now shown in the *Link* tab of the app.

### New: SoundCloud when YouTube refuses a download

- YouTube's “Sign in to confirm you're not a bot” check refuses every client on
  some networks (VPN, mobile carriers, data centers) and the download failed. The
  same song is now taken from SoundCloud, scored like a YouTube video (no covers,
  no 30 s previews); tags, HD cover art and lyrics are applied as usual.

### Fixed

- **Android: Spotify search returned the previous results.** The hidden WebView
  kept the page being left until the new one replaced it, and the reading scripts
  read that old page (a search returned the previous search, a radio lookup the
  previous radio). The page being left is now marked (`window.__mfAncienne`) and
  ignored.
- **Wrong album in the tags.** Live versions, remixes and compilations were picked
  (“Dynamite” → a live concert album, “Despacito” → “2018 Award Nominees”,
  “Djadja” → a remix single). They now win only when nothing else matches, and
  Deezer is asked when iTunes only has those.
- **Country charts took up to ~50 s** (the Apple feed could hang 25 s per try):
  8 s timeout, 1 h cache, last good chart served if the feed fails.
- **Spotify search unavailable** (browser still starting) now falls back to Deezer
  before YouTube: clean titles instead of “Song (Clip Officiel) - Channel”.
- **Phone layout:** the radio field was ~250 px tall and its help text broke
  mid-word; the scroll arrows covered the Download buttons of the result cards
  (they now show only while scrolling); “No live / remix / karaoke” wrapped on
  4 lines.
- Unrecognized-link message lists every supported service; inline favicon (no
  404 in the console); larger checkboxes and favorite buttons for touch.

### Faster

- *Listen* then *Download* (or a second *Listen*) on the same track no longer
  redoes the YouTube lookups: matches and stream URLs are kept 1 h
  (second listen: ~10 s → instant).

### Tested

- PC: every server route, real downloads in MP3 128/320, Opus, FLAC, AAC and WAV
  (tags, cover art, lyrics, `.lrc` checked in each file), 6 parallel downloads,
  pause/stop, duplicates, interface flows at 412 px and 1280 px: no JavaScript
  error, nothing overflowing.
- Android 14 emulator: app start, searches, links, 4/4 radios, discover, artist,
  every tab, interface in Chrome at phone size. Downloads there go through the
  SoundCloud fallback (YouTube blocks GitHub's servers); the conversion did not
  finish within the test's 15 min — the emulator runs the ARM ffmpeg through
  translation, so this could not be checked there.

## 3.5.0 — 2026-10-31 (PC; the lyrics fix also goes into the next APK)

### New: Spotify radio (PC)

Import the radio Spotify builds around a song or an artist (about 50 tracks).

- **Where**: *From a link* tab → **Spotify radio** block.
- **Input**: a search text (`Blinding Lights The Weeknd`, `Amr Diab`) or a Spotify
  track/artist link. A pasted link selects *Song radio* / *Artist radio* by itself.
- **How**: the automated Edge browser (`spotify_scan.py`, same profile as the full
  scan) opens the track or artist page and clicks *Go to song/artist radio* in its
  “…” menu. The label depends on the account language, so it is matched on the word
  “radio” in several scripts (Latin, Arabic, Persian, Cyrillic, Japanese, Korean,
  Chinese). No sign-in is needed.
  - The menu is tried twice (it sometimes opens before its items load).
  - Fallback: search the playlists `<query> radio` and keep the ones made by
    Spotify (id starting with `37i9dQZF1E`).
- **Result**: the radio playlist link is put in the link field and analyzed like any
  playlist (track list, selection, download, follow).
- **API**: `POST /api/spotify-radio` with `{"query": str, "kind": "track" | "artist"}`
  → `{"url": str, "source": "menu" | "search"}`, `404` if no radio is found,
  `500` if Edge/Selenium is unavailable.
- **Android**: hidden (there is no Edge to drive).
- Typical time: 10–40 s.

### Fixed: tracks saved without lyrics (`paroles.py`, PC and Android)

- LRCLIB often answers `503`/`429` under load; the error was swallowed and the
  track was saved “without lyrics”. Requests are now retried (4 tries, growing
  delay); a `404` still means “not found”.
- Titles are cleaned before the lookup: `(feat. …)`, `[…]`, `- Remastered 2011`,
  `- Radio Edit`, `- Live`…; only the main artist is kept.
- Lookup order, from the most precise to the broadest: exact `/api/get` (with the
  duration) → `/api/search` by title + artist → free-text search → title only →
  [lyrics.ovh](https://lyrics.ovh) as a second source (plain text only).
- Every candidate is scored (title, artist, duration ±3 s / ±15 s, synced lyrics
  preferred) so a broad search does not embed the lyrics of another song.
- Comparison works for every script (accents removed, Unicode case folding).
- Test set of 24 tracks in French, Arabic (Arabic script and Latin transliteration),
  Korean, Japanese, Russian, Italian, Spanish, German, Hindi and English:
  **11 → 22** found. Still missing: two Arabic classics that neither source has.
- Synced lyrics go into the file and into a `.lrc`, as before.

### Fixed: interface

- PC: the *Destination folder* field was squashed to ~25 px in Search, Link and
  Library; the *Quality* list took the whole row and overflowed the card
  (`.row2` children now have `min-width: 0`).

### Fixed: Spotify browser

- The search browser stays open between searches and locks the Edge profile, so
  the full playlist scan and the Spotify sign-in window failed with
  `DevToolsActivePort file doesn't exist`. Both now close it first
  (`spotify_scan.fermer_recherche()`).
