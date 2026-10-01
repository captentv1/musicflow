"""OAuth Google (YouTube Data API v3) + création de playlist.

La connexion se fait exclusivement via accounts.google.com — l'utilisateur
saisit son mot de passe sur le site officiel de Google, jamais dans MusicFlow.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import secrets_store as store

REDIRECT_URI = "http://127.0.0.1:5090/auth/youtube/callback"
SCOPES = "https://www.googleapis.com/auth/youtube"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"


def _request(method, url, headers=None, data=None, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers = {**(headers or {}), "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YouTube API {e.code}: {detail}") from e


def build_authorize_url(state: str) -> str:
    creds = store.get_provider("youtube")
    params = {
        "client_id": creds["client_id"],
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(code: str):
    creds = store.get_provider("youtube")
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read())

    store.set_provider_fields(
        "youtube",
        access_token=tok["access_token"],
        refresh_token=tok.get("refresh_token", creds.get("refresh_token", "")),
        expires_at=time.time() + tok.get("expires_in", 3600) - 30,
    )
    me = _request(
        "GET",
        f"{API_BASE}/channels",
        headers={"Authorization": f"Bearer {tok['access_token']}"},
        params={"part": "snippet", "mine": "true"},
    )
    items = me.get("items") or []
    name = items[0]["snippet"]["title"] if items else "Compte YouTube"
    store.set_provider_fields("youtube", user={"name": name})


def _refresh_if_needed():
    creds = store.get_provider("youtube")
    if not creds.get("refresh_token"):
        raise RuntimeError("Non connecté à YouTube.")
    if creds.get("access_token") and time.time() < float(creds.get("expires_at") or 0):
        return creds["access_token"]

    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        tok = json.loads(resp.read())

    store.set_provider_fields(
        "youtube", access_token=tok["access_token"], expires_at=time.time() + tok.get("expires_in", 3600) - 30
    )
    return tok["access_token"]


def _auth_headers():
    return {"Authorization": f"Bearer {_refresh_if_needed()}"}


def create_playlist(title: str, description: str = "Créée avec MusicFlow") -> dict:
    return _request(
        "POST",
        f"{API_BASE}/playlists",
        headers=_auth_headers(),
        params={"part": "snippet,status"},
        data={
            "snippet": {"title": title, "description": description},
            "status": {"privacyStatus": "private"},
        },
    )


def add_video(playlist_id: str, video_id: str):
    _request(
        "POST",
        f"{API_BASE}/playlistItems",
        headers=_auth_headers(),
        params={"part": "snippet"},
        data={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    )
