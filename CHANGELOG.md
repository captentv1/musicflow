# Changelog

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
