"""Local storage of OAuth credentials (Client ID/Secret + tokens) — mobile version.

The config.json path is injected by MainActivity through init(): on Android
it points to the app's private storage (context.filesDir), unreachable by other apps.
"""
import json
import threading
from pathlib import Path

_LOCK = threading.Lock()
CONFIG_PATH: Path | None = None

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


def init(config_path: str):
    global CONFIG_PATH
    CONFIG_PATH = Path(config_path)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def _read() -> dict:
    if not CONFIG_PATH or not CONFIG_PATH.exists():
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
    set_provider_fields(provider, access_token="", refresh_token="", expires_at=0, user=None)


def has_app_credentials(provider: str) -> bool:
    p = get_provider(provider)
    return bool(p.get("client_id") and p.get("client_secret"))


def is_connected(provider: str) -> bool:
    p = get_provider(provider)
    return bool(p.get("refresh_token") or p.get("access_token"))
