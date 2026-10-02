"""Local storage of OAuth credentials (Client ID/Secret + tokens).

Everything stays on the user's disk, in config.json next to the app.
Nothing is ever sent anywhere except the official Spotify/Google APIs
for the OAuth code exchange. This file must never be shared/committed.
"""
import json
import threading
from pathlib import Path

import os as _os, sys as _sys
# .exe build (PyInstaller): data goes to %APPDATA%\MusicFlow (the exe folder is temporary)
DATA_DIR = (Path(_os.environ.get("APPDATA", Path.home())) / "MusicFlow") if getattr(_sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
_LOCK = threading.Lock()

_DEFAULT = {
    "spotify": {
        "client_id": "",
        "client_secret": "",
        "access_token": "",
        "refresh_token": "",
        "expires_at": 0,
        "user": None,
    },
    "youtube": {
        "client_id": "",
        "client_secret": "",
        "access_token": "",
        "refresh_token": "",
        "expires_at": 0,
        "user": None,
    },
}


def _read() -> dict:
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(_DEFAULT))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return json.loads(json.dumps(_DEFAULT))
    for provider, defaults in _DEFAULT.items():
        data.setdefault(provider, json.loads(json.dumps(defaults)))
        for k, v in defaults.items():
            data[provider].setdefault(k, v)
    return data


def _write(data: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        import os
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass  # best-effort on Windows


def get_provider(provider: str) -> dict:
    with _LOCK:
        return _read().get(provider, {}).copy()


def set_provider_fields(provider: str, **fields):
    with _LOCK:
        data = _read()
        data.setdefault(provider, json.loads(json.dumps(_DEFAULT[provider])))
        data[provider].update(fields)
        _write(data)


def clear_tokens(provider: str):
    set_provider_fields(
        provider, access_token="", refresh_token="", expires_at=0, user=None
    )


def has_app_credentials(provider: str) -> bool:
    p = get_provider(provider)
    return bool(p.get("client_id") and p.get("client_secret"))


def is_connected(provider: str) -> bool:
    p = get_provider(provider)
    return bool(p.get("refresh_token") or p.get("access_token"))
