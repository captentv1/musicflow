"""MusicFlow on iPhone: starts the shared Python engine (the same Flask server as the
Android app, app_mobile.py) on 127.0.0.1:5090 and shows its page in a WebView."""
import asyncio
import sys
import threading
import urllib.request
from pathlib import Path

import toga
from toga.style import Pack

PORT = 5090
URL = f"http://127.0.0.1:{PORT}/"


def _serveur_pret() -> bool:
    try:
        urllib.request.urlopen(URL, timeout=2)
        return True
    except Exception:
        return False


class MusicFlow(toga.App):
    def startup(self):
        try:
            self._demarrer()
        except Exception:
            # A failure here leaves a black screen with no message: keep the reason
            # in Documents/MusicFlow/startup-error.txt (visible in the Files app).
            import traceback
            erreur = Path.home() / "Documents" / "MusicFlow" / "startup-error.txt"
            erreur.parent.mkdir(parents=True, exist_ok=True)
            erreur.write_text(traceback.format_exc(), encoding="utf-8")
            raise

    def _demarrer(self):
        moteur = Path(__file__).resolve().parent / "engine"
        sys.path.insert(0, str(moteur))
        import app_mobile

        # Documents/MusicFlow: shown in the Files app (UIFileSharingEnabled)
        dossier = Path.home() / "Documents" / "MusicFlow"
        config = Path(self.paths.config)
        config.mkdir(parents=True, exist_ok=True)
        app_mobile.configure(str(dossier), str(config / "config.json"))
        threading.Thread(target=app_mobile.start_server, daemon=True).start()
        print("[MusicFlow] server thread started", flush=True)

        self.vue = toga.WebView(style=Pack(flex=1))
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = self.vue
        self.main_window.show()
        self._habiller()
        # UI changes must happen on the app's own loop, not from a background thread:
        # setting the URL from a thread left the WebView black on iOS.
        self.loop.create_task(self._ouvrir_page())

    def _habiller(self):
        """The page has its own top bar: the white iOS navigation bar ("MusicFlow")
        only doubled it. Hidden, and the area behind the status bar made dark like
        the page. Best effort: Toga's native attributes vary between versions."""
        try:
            from rubicon.objc import ObjCClass
            sombre = ObjCClass("UIColor").colorWithRed(0.07, green=0.05, blue=0.13, alpha=1.0)
            impl = self.main_window._impl
            for nom in ("navigation_controller", "controller", "native_controller"):
                ctrl = getattr(impl, nom, None)
                if ctrl is not None and hasattr(ctrl, "setNavigationBarHidden_animated_"):
                    ctrl.setNavigationBarHidden(True, animated=False)
                if ctrl is not None and hasattr(ctrl, "view"):
                    ctrl.view.backgroundColor = sombre
            native = getattr(impl, "native", None)
            if native is not None:
                native.backgroundColor = sombre
        except Exception as exc:
            print(f"[MusicFlow] window styling skipped: {exc}", flush=True)

    async def _ouvrir_page(self):
        for _ in range(240):
            if await self.loop.run_in_executor(None, _serveur_pret):
                break
            await asyncio.sleep(0.5)
        print("[MusicFlow] server ready, opening the page", flush=True)
        self.vue.url = URL


def main():
    return MusicFlow("MusicFlow", "io.github.captentv1.musicflow")
