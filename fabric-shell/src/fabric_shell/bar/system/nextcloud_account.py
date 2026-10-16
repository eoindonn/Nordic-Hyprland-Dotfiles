"""The Nextcloud desktop client's account, shared by the calendar and Deck.

Signs in with the client's own app password, read from the keyring at runtime
and kept only in memory, so nothing extra is stored and signing the client out
signs us out too.
"""

import base64
import configparser
import threading
import urllib.error
import urllib.request
from pathlib import Path

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret

CLIENT_CONFIG = Path.home() / ".config" / "Nextcloud" / "nextcloud.cfg"
TIMEOUT = 20  # s per request


class AuthError(RuntimeError):
    pass


class NextcloudAccount:
    def __init__(self):
        self.url, self.user = self._read_config()
        self._auth: str | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.url and self.user)

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> bytes:
        """Blocking HTTP request to `path` on the server; call from a worker thread."""
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=body,
            method=method,
            headers={"Authorization": self.auth_header(), **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                with self._lock:
                    self._auth = None  # re-read next time: the client may have signed in again
                raise AuthError("Nextcloud rejected the desktop client's login") from e
            raise

    def auth_header(self) -> str:
        with self._lock:
            if self._auth is None:
                self._auth = self._load_auth()
            return self._auth

    @staticmethod
    def _read_config() -> tuple[str, str]:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(CLIENT_CONFIG)
            accounts = parser["Accounts"]
        except (configparser.Error, KeyError):
            return "", ""
        url = accounts.get("1\\url", "").rstrip("/")
        user = accounts.get("1\\dav_user", "") or accounts.get("1\\webflow_user", "")
        return url, user

    def _load_auth(self) -> str:
        """Basic auth header from the desktop client's app password."""
        service = Secret.Service.get_sync(Secret.ServiceFlags.OPEN_SESSION | Secret.ServiceFlags.LOAD_COLLECTIONS)
        items = service.search_sync(
            None,
            {"server": "Nextcloud"},
            Secret.SearchFlags.ALL | Secret.SearchFlags.LOAD_SECRETS | Secret.SearchFlags.UNLOCK,
            None,
        )
        prefix = f"{self.user}_app-password:{self.url}/"
        for item in items:
            attrs = item.get_attributes()
            if not attrs.get("user", "").startswith(prefix) or item.get_secret() is None:
                continue
            secret = item.get_secret().get_text() or ""
            if attrs.get("type") == "base64":
                secret = base64.b64decode(secret).decode()
            return "Basic " + base64.b64encode(f"{self.user}:{secret}".encode()).decode()
        raise AuthError("Nextcloud desktop client isn't signed in")
