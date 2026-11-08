"""Spotify OAuth (Authorization Code) + playlist creation.

Sign-in happens only through accounts.spotify.com — the user types
their password on Spotify's official site, never in MusicFlow.
"""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import secrets_store as store

REDIRECT_URI = "http://127.0.0.1:5090/auth/spotify/callback"
SCOPES = "playlist-modify-public playlist-modify-private user-read-email"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"


def _request(method, url, headers=None, data=None, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            headers = {**(headers or {}), "Content-Type": "application/json"}
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Spotify API {e.code}: {detail}") from e


def build_authorize_url(state: str) -> str:
    creds = store.get_provider("spotify")
    params = {
        "client_id": creds["client_id"],
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "show_dialog": "true",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _basic_auth_header():
    creds = store.get_provider("spotify")
    raw = f"{creds['client_id']}:{creds['client_secret']}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def exchange_code(code: str):
    data = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    ).encode("utf-8")
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(TOKEN_URL, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read())

    store.set_provider_fields(
        "spotify",
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token", store.get_provider("spotify").get("refresh_token", "")),
        expires_at=time.time() + tok.get("expires_in", 3600) - 30,
    )
    me = _request("GET", f"{API_BASE}/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
    store.set_provider_fields(
        "spotify", user={"id": me.get("id"), "name": me.get("display_name") or me.get("id")}
    )


def _refresh_if_needed():
    creds = store.get_provider("spotify")
    if not creds.get("refresh_token"):
        raise RuntimeError("Not connected to Spotify.")
    if creds.get("access_token") and time.time() < float(creds.get("expires_at") or 0):
        return creds["access_token"]

    data = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]}
    ).encode("utf-8")
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(TOKEN_URL, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read())

    store.set_provider_fields(
        "spotify",
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token", creds["refresh_token"]),
        expires_at=time.time() + tok.get("expires_in", 3600) - 30,
    )
    return tok["access_token"]


def _auth_headers():
    return {"Authorization": f"Bearer {_refresh_if_needed()}"}


# ---------------------------------------------------------------------------
# Client Credentials (app-only, no user sign-in) — only used
# to read public data (tracks of a playlist/album) to get around
# the ~50-100 track limit of Spotify's preview page without an account.
# ---------------------------------------------------------------------------

_APP_TOKEN = {"access_token": "", "expires_at": 0.0}


def _app_access_token() -> str | None:
    creds = store.get_provider("spotify")
    if not creds.get("client_id") or not creds.get("client_secret"):
        return None
    if _APP_TOKEN["access_token"] and time.time() < _APP_TOKEN["expires_at"]:
        return _APP_TOKEN["access_token"]

    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    req = urllib.request.Request(TOKEN_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            tok = json.loads(resp.read())
    except urllib.error.HTTPError:
        return None  # invalid Client ID/Secret -> fall back to embed scraping

    _APP_TOKEN["access_token"] = tok["access_token"]
    _APP_TOKEN["expires_at"] = time.time() + tok.get("expires_in", 3600) - 30
    return _APP_TOKEN["access_token"]


def _best_available_headers():
    """Broadest token available: the signed-in user's first
    (also sees their private playlists), otherwise an app-only Client Credentials token
    (PUBLIC playlists only), otherwise None."""
    creds = store.get_provider("spotify")
    if creds.get("refresh_token"):
        try:
            return {"Authorization": f"Bearer {_refresh_if_needed()}"}
        except Exception:
            pass  # user token invalid/expired -> try Client Credentials
    token = _app_access_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return None


def fetch_full_tracklist(kind: str, spotify_id: str):
    """Fetches ALL tracks of a playlist/album through the official API (paginated).
    Returns (name, tracks), or None if no access is possible (no Client ID/Secret
    configured, or private playlist without user sign-in) — the caller then falls back
    to scraping the public preview page (limited but always available)."""
    headers = _best_available_headers()
    if not headers:
        return None
    base = f"{API_BASE}/{kind}s/{spotify_id}"

    try:
        meta = _request("GET", base, headers=headers, params={"fields": "name"})
        name = meta.get("name") or ""

        tracks = []
        url = f"{base}/tracks?limit=50"
        while url:
            page = _request("GET", url, headers=headers)
            for it in page.get("items") or []:
                track = it.get("track") if kind == "playlist" else it
                if track and track.get("id"):
                    tracks.append(track)
            url = page.get("next")
        return name, tracks
    except Exception:
        return None  # playlist private/inaccessible with this token -> fall back to scraping


def search_track_uri(title: str, artist: str) -> str | None:
    q = f"{title} {artist}".strip()
    result = _request(
        "GET", f"{API_BASE}/search", headers=_auth_headers(), params={"q": q, "type": "track", "limit": 1}
    )
    items = (result.get("tracks") or {}).get("items") or []
    return items[0]["uri"] if items else None


def search_track(title: str, artist: str = "") -> dict | None:
    """Looks up the matching track on Spotify (official name, artist, cover art).

    Uses the app-only token (Client ID/Secret) if configured — no need to be
    signed in. Used to fix a title/cover coming from YouTube ("Lyrics",
    video thumbnail…) with the real Spotify metadata.
    """
    q = f"{title} {artist}".strip()
    if not q:
        return None
    headers = _best_available_headers()
    if not headers:
        return None
    try:
        result = _request(
            "GET", f"{API_BASE}/search", headers=headers, params={"q": q, "type": "track", "limit": 1}
        )
    except Exception:
        return None
    items = (result.get("tracks") or {}).get("items") or []
    if not items:
        return None
    t = items[0]
    images = ((t.get("album") or {}).get("images")) or []
    return {
        "title": t.get("name") or "",
        "artist": ", ".join(a.get("name", "") for a in t.get("artists") or []),
        "cover": images[0]["url"] if images else "",
    }


def create_playlist(name: str, description: str = "Created with MusicFlow") -> dict:
    creds = store.get_provider("spotify")
    user_id = (creds.get("user") or {}).get("id")
    if not user_id:
        raise RuntimeError("Spotify account not connected.")
    return _request(
        "POST",
        f"{API_BASE}/users/{user_id}/playlists",
        headers=_auth_headers(),
        data={"name": name, "description": description, "public": False},
    )


def add_tracks(playlist_id: str, uris: list[str]):
    for i in range(0, len(uris), 100):
        chunk = uris[i : i + 100]
        _request(
            "POST",
            f"{API_BASE}/playlists/{playlist_id}/tracks",
            headers=_auth_headers(),
            data={"uris": chunk},
        )
