"""MusicFlow on iPhone: starts the shared Python engine (the same Flask server as the
Android app, app_mobile.py) on 127.0.0.1:5090 and shows its page in a WebView."""
import sys
import threading
import time
import urllib.request
from pathlib import Path

import toga
from toga.style import Pack

PORT = 5090
URL = f"http://127.0.0.1:{PORT}/"


class MusicFlow(toga.App):
    def startup(self):
        moteur = Path(__file__).resolve().parent / "engine"
        sys.path.insert(0, str(moteur))
        import app_mobile

        # Documents/MusicFlow: shown in the Files app (UIFileSharingEnabled)
        dossier = Path.home() / "Documents" / "MusicFlow"
        config = Path(self.paths.config)
        config.mkdir(parents=True, exist_ok=True)
        app_mobile.configure(str(dossier), str(config / "config.json"))
        threading.Thread(target=app_mobile.start_server, daemon=True).start()

        self.vue = toga.WebView(style=Pack(flex=1))
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = self.vue
        self.main_window.show()
        threading.Thread(target=self._attendre_serveur, daemon=True).start()

    def _attendre_serveur(self):
        """The page is opened once the server answers (Python starts in ~1-3 s)."""
        for _ in range(120):
            try:
                urllib.request.urlopen(URL, timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        self.loop.call_soon_threadsafe(setattr, self.vue, "url", URL)


def main():
    return MusicFlow("MusicFlow", "io.github.captentv1.musicflow")
