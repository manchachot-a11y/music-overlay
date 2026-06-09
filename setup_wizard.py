import sys
import os
import json
import shutil
import subprocess
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QStackedWidget, QProgressBar
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve, QRect
from PyQt6.QtGui import QPainter, QColor, QLinearGradient, QFont, QPainterPath, QPen

CONFIG_FILE = "ui_config.json"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0 # no terminal window popup


def get_asset_path(relative):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(__file__), relative)

def is_setup_complete():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f).get("setup_complete", False)
    except Exception:
        pass
    return False

def mark_setup_complete(music_app):
    data = {}
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
    except Exception:
        pass
    data["setup_complete"] = True
    data["music_app"] = music_app
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f)


class SpicetifyInstallThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def run(self):
        try:
            def log_step(msg):
                print(f"[DEBUG: Spicetify] {msg}")
                self.progress.emit(msg)

            log_step("verbose install...")
            local = os.environ.get("LOCALAPPDATA", "")
            spicetify_exe = os.path.join(local, "spicetify", "spicetify.exe")

            if not os.path.exists(spicetify_exe) and not shutil.which("spicetify"):
                log_step("CLI not found. Downloading via PowerShell...")
                
                ps_command = (
                    "iwr -useb https://raw.githubusercontent.com/spicetify/spicetify-cli/master/install.ps1 | iex; "
                    "if ($LASTEXITCODE -ne 0) { throw 'PS Install Failed' }"
                )
                
                process = subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=CREATE_NO_WINDOW
                )
                
                stdout, stderr = process.communicate()
                print(f"[POWERSHELL STDOUT]:\n{stdout}")
                if process.returncode != 0:
                    print(f"[POWERSHELL ERROR]:\n{stderr}")
                    self.finished.emit(False, f"PS Error: {stderr[:100]}")
                    return

            spicetify_cmd = spicetify_exe if os.path.exists(spicetify_exe) else shutil.which("spicetify")
            log_step(f"Using binary: {spicetify_cmd}")

            log_step("Writing JS extension file...")
            ext_dir = os.path.join(local, "spicetify", "Extensions")
            os.makedirs(ext_dir, exist_ok=True)
            
            from spicetify_extension import SPICETIFY_EXTENSION_JS
            with open(os.path.join(ext_dir, "music_overlay.js"), "w") as f:
                f.write(SPICETIFY_EXTENSION_JS)

            log_step("Checking for Spicetify CLI updates...")
            upgrade_proc = subprocess.Popen(
                [spicetify_cmd, "upgrade"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=CREATE_NO_WINDOW
            )
            out, err = upgrade_proc.communicate()
            print(f"[Upgrade Output]: {out}")
            
            # adblocker stuff
            adblock_url = "https://raw.githubusercontent.com/rxri/spicetify-extensions/main/adblock/adblock.js"
            adblock_path = os.path.join(ext_dir, "adblock.js")

            log_step("Downloading adblockify...")
            subprocess.run(["powershell", "-Command", f"Invoke-WebRequest -UseBasicParsing '{adblock_url}' -OutFile '{adblock_path}'"], creationflags=CREATE_NO_WINDOW)

            commands = [
                (["restore"], "Cleaning up old patch"),
                (["config", "extensions", "music_overlay.js"], "Registering extensions"),
                (["config", "extensions", "adblock.js",], "Blocking ads"),
                (["backup", "apply"], "Applying fresh patch")
            ]

            log_step("Killing Spotify process...")
            subprocess.run(["taskkill", "/F", "/IM", "Spotify.exe"], 
                           capture_output=True, creationflags=CREATE_NO_WINDOW)

            for cmd_args, desc in commands:
                log_step(f"Action: {desc}...")
                proc = subprocess.Popen(
                    [spicetify_cmd] + cmd_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=CREATE_NO_WINDOW
                )
                out, err = proc.communicate()
                
                print(f"{desc} Output")
                print(out)
                if proc.returncode != 0:
                    print(f"{desc} ERROR")
                    print(err)
                    self.finished.emit(False, f"{desc} failed: {err[:100]}")
                    return

            log_step("all steps completed successfully.")
            self.finished.emit(True, "")

        except Exception as e:
            import traceback
            print("[CRITICAL INSTALL ERROR]")
            traceback.print_exc()
            self.finished.emit(False, str(e))

class GlassWidget(QWidget):

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, QColor(20, 20, 25, 80))
        grad.setColorAt(1.0, QColor(8, 8, 12, 100)) 

        painter.setBrush(grad)
        painter.setPen(QPen(QColor(255, 255, 255, 18), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 15, 15)

def label(text, size=12, bold=False, color="#ffffff", align=Qt.AlignmentFlag.AlignCenter):
    l = QLabel(text)
    w = QFont.Weight.Bold if bold else QFont.Weight.Normal
    l.setFont(QFont("Segoe UI", size, w))
    l.setStyleSheet(f"color: {color}; background: transparent;")
    l.setAlignment(align)
    l.setWordWrap(True)
    return l

def btn(text, primary=False):
    b = QPushButton(text)
    b.setFont(QFont("Segoe UI", 10))
    b.setFixedHeight(34)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setStyleSheet("""
            QPushButton {
                background: rgba(100, 210, 255, 180);
                color: #000;
                border: none;
                border-radius: 8px;
                padding: 0 20px;
                font-weight: bold;
            }
            QPushButton:hover { background: rgba(130, 225, 255, 200); }
            QPushButton:disabled { background: rgba(80, 80, 80, 120); color: #666; }
        """)
    else:
        b.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,15);
                color: #ccc;
                border: 1px solid rgba(255,255,255,25);
                border-radius: 8px;
                padding: 0 20px;
            }
            QPushButton:hover { background: rgba(255,255,255,28); color: #fff; }
        """)
    return b


class WelcomePage(QWidget):
    choice = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(14)

        lay.addWidget(label("Welcome to music-overlay", 15, bold=True))
        lay.addWidget(label("What music app do you use?", 10, color="#aaaaaa"))
        lay.addSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        for text, key in [("Spotify", "spotify"), ("YouTube Music", "ytmusic"), ("Both", "both")]:
            b = btn(text, primary=(key == "spotify"))
            b.clicked.connect(lambda _, k=key: self.choice.emit(k))
            row.addWidget(b)
        lay.addLayout(row)

        lay.addSpacing(4)
        already = QLabel("I have already set this up")
        already.setFont(QFont("Segoe UI", 8))
        already.setStyleSheet("color: #555; text-decoration: underline; background: transparent;")
        already.setAlignment(Qt.AlignmentFlag.AlignCenter)
        already.setCursor(Qt.CursorShape.PointingHandCursor)
        already.mousePressEvent = lambda e: self.choice.emit("already_done")
        lay.addWidget(already)


class SpotifyPage(QWidget):
    done = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._thread = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(10)

        lay.addWidget(label("Setting up Spotify", 14, bold=True))
        self.body = label("Close Spotify, then click Install.", 10, color="#aaaaaa")
        lay.addWidget(self.body)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setFixedHeight(4)
        self.bar.hide()
        self.bar.setStyleSheet("""
            QProgressBar { background: rgba(255,255,255,15); border: none; border-radius: 2px; }
            QProgressBar::chunk { background: rgba(100,210,255,200); border-radius: 2px; }
        """)
        lay.addWidget(self.bar)

        self.status = label("", 9, color="#888888")
        self.status.hide()
        lay.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.skip_btn = btn("Skip")
        self.skip_btn.clicked.connect(lambda: self.done.emit(False))
        self.install_btn = btn("Install", primary=True)
        self.install_btn.clicked.connect(self._install)
        row.addWidget(self.skip_btn)
        row.addWidget(self.install_btn)
        lay.addLayout(row)

        already = QLabel("Already installed")
        already.setFont(QFont("Segoe UI", 8))
        already.setStyleSheet("color: #555; text-decoration: underline; background: transparent;")
        already.setAlignment(Qt.AlignmentFlag.AlignCenter)
        already.setCursor(Qt.CursorShape.PointingHandCursor)
        already.mousePressEvent = lambda e: self.done.emit(True)
        lay.addWidget(already)

    def _install(self):
        self.install_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.bar.show()
        self.status.show()

        self._thread = SpicetifyInstallThread()
        self._thread.progress.connect(lambda m: self.status.setText(m))
        self._thread.finished.connect(self._on_done)
        self._thread.start()

    def _on_done(self, success, error):
        self.bar.hide()
        if success:
            mark_setup_complete("spotify")
            self.status.setText("Done! Restart Spotify to finish.")
            self.status.setStyleSheet("color: #6cf; background: transparent;")
            QTimer.singleShot(1800, lambda: self.done.emit(True))
        else:
            self.status.setText(f"✗ {error}")
            self.status.setStyleSheet("color: #f88; background: transparent;")
            self.install_btn.setEnabled(True)
            self.skip_btn.setEnabled(True)


class YTMusicPage(QWidget):
    done = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(10)

        lay.addWidget(label("Setting up YouTube Music", 14, bold=True))
        lay.addWidget(label(
            "Click 'Copy Path', then in Chrome go to chrome://extensions,\n"
            "enable Developer Mode, click Load unpacked, and paste the path.",
            10, color="#aaaaaa"
        ))

        self.status = label("", 9, color="#6cf")
        self.status.hide()
        lay.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.skip_btn = btn("Skip")
        self.skip_btn.clicked.connect(lambda: self.done.emit(False))
        self.copy_btn = btn("Copy Path", primary=True)
        self.copy_btn.clicked.connect(self._copy_path)
        self.done_btn = btn("Done")
        self.done_btn.clicked.connect(lambda: self.done.emit(True))
        row.addWidget(self.skip_btn)
        row.addWidget(self.copy_btn)
        row.addWidget(self.done_btn)
        lay.addLayout(row)

        already = QLabel("Already installed")
        already.setFont(QFont("Segoe UI", 8))
        already.setStyleSheet("color: #555; text-decoration: underline; background: transparent;")
        already.setAlignment(Qt.AlignmentFlag.AlignCenter)
        already.setCursor(Qt.CursorShape.PointingHandCursor)
        already.mousePressEvent = lambda e: self.done.emit(True)
        lay.addWidget(already)

    def _copy_path(self):
        from PyQt6.QtWidgets import QApplication
        from ytmusic_extension import MANIFEST_JSON, CONTENT_JS

        ext_dst = os.path.join(os.environ.get("APPDATA", ""), "music-overlay", "ytmusic-extension")
        os.makedirs(ext_dst, exist_ok=True)
        with open(os.path.join(ext_dst, "manifest.json"), "w") as f:
            f.write(MANIFEST_JSON)
        with open(os.path.join(ext_dst, "content.js"), "w") as f:
            f.write(CONTENT_JS)

        QApplication.clipboard().setText(ext_dst)

        # Open Chrome
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        ]
        chrome = next((p for p in chrome_paths if os.path.exists(p)), None)
        if chrome:
            subprocess.Popen([chrome, "--new-window"])

        mark_setup_complete("ytmusic")


class FinishPage(QWidget):
    done = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 28)
        lay.setSpacing(14)

        lay.addWidget(label("You're all set!", 15, bold=True))
        lay.addWidget(label("Open your music app and start playing.", 10, color="#aaaaaa"))

        row = QHBoxLayout()
        go = btn("Let's go", primary=True)
        go.clicked.connect(self.done.emit)
        row.addStretch()
        row.addWidget(go)
        row.addStretch()
        lay.addLayout(row)

class SetupWizard(GlassWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(480, 160)

        self.music_app = None
        self._drag_pos = None

        # Apply Win11 blur
        try:
            import ctypes
            from main import ACCENT_POLICY, WINDOWCOMPOSITIONATTRIBDATA

            hwnd = int(self.winId())
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 34, ctypes.byref(ctypes.c_uint(0xFFFFFFFE)), 4)

            accent = ACCENT_POLICY()
            accent.AccentState = 3  # ACCENT_ENABLE_ACRYLICBLURBEHIND

            data = WINDOWCOMPOSITIONATTRIBDATA()
            data.Attribute = 19  # WCA_ACCENT_POLICY
            data.Data = ctypes.pointer(accent)
            data.SizeOfData = ctypes.sizeof(accent)

            ctypes.windll.user32.SetWindowCompositionAttribute(
                hwnd, ctypes.pointer(data))
        except Exception:
            pass

        # Close button
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.move(self.width() - 30, 8)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: rgba(255,255,255,80);
                border: none;
                font-size: 11px;
                border-radius: 12px;
            }
            QPushButton:hover {
                background: rgba(255,255,255,20);
                color: white;
            }
        """)
        self.close_btn.raise_()

        # Stack
        self.stack = QStackedWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        self.p_welcome = WelcomePage()
        self.p_spotify = SpotifyPage()
        self.p_ytmusic = YTMusicPage()
        self.p_finish = FinishPage()

        self.stack.addWidget(self.p_welcome)   # 0
        self.stack.addWidget(self.p_spotify)   # 1
        self.stack.addWidget(self.p_ytmusic)   # 2
        self.stack.addWidget(self.p_finish)    # 3

        self.p_welcome.choice.connect(self._on_choice)
        self.p_spotify.done.connect(self._on_spotify_done)
        self.p_ytmusic.done.connect(self._on_ytmusic_done)
        self.p_finish.done.connect(self._finish)

        # Center on screen
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            screen.center().x() - self.width() // 2,
            screen.center().y() - self.height() // 2
        )

    def _finish(self):
        self.close()
        if __name__ == "__main__":
            QApplication.instance().quit()
    
    def _on_choice(self, app):
        if app == "already_done":
            mark_setup_complete("unknown")
            self.stack.setCurrentIndex(3)  # go straight to finish
            return
        self.music_app = app
        if app == "spotify":
            self.stack.setCurrentIndex(1)
        elif app == "ytmusic":
            self.stack.setCurrentIndex(2)
        else:
            self.stack.setCurrentIndex(1)

    def _on_spotify_done(self, success):
        mark_setup_complete(self.music_app)
        if self.music_app == "both":
            self.stack.setCurrentIndex(2)
        else:
            self.stack.setCurrentIndex(3)

    def _on_ytmusic_done(self, success):
        mark_setup_complete(self.music_app)
        self.stack.setCurrentIndex(3)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


def run_setup_if_needed():
    if is_setup_complete():
        return False
    wizard = SetupWizard()
    wizard.show()
    wizard.exec() if hasattr(wizard, 'exec') else None
    return True


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = SetupWizard()
    w.show()
    w.destroyed.connect(app.quit)
    sys.exit(app.exec())