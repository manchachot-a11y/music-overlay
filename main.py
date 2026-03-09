import sys
import json
import os
import ctypes
import numpy as np
import threading
from collections import deque
from PyQt6.QtWidgets import QApplication, QWidget, QSizeGrip, QMenu
from PyQt6.QtGui import QPainter, QColor, QFont, QPixmap, QLinearGradient, QPainterPath, QPen, QBrush, QFontMetrics, QRadialGradient, QTransform
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QRect, QRectF, QVariantAnimation, QTimer, QAbstractAnimation, QEasingCurve
# imports
from lyrics_engine import LyricsThread
import datetime

# win11 blur structs
class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_uint)
    ]

# win comp attrib
class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.POINTER(ACCENT_POLICY)),
        ("SizeOfData", ctypes.c_size_t)
    ]

# beat detector
class BeatDetector:
    # ctor
    def __init__(self, history_size=25):
        self.history_bass = []
        self.history_broad = []
        self.history_size = history_size
        self.sens_bass = 1.35
        self.sens_broad = 2.0  
        
    # detect beat (bass/broad)
    def is_beat(self, fft_data):
        current_bass = np.mean(fft_data[1:5])
        current_broad = np.mean(fft_data[5:150])

        if len(self.history_bass) < self.history_size:
            self.history_bass.append(current_bass)
            self.history_broad.append(current_broad)
            return False, False
            
        avg_bass = np.mean(self.history_bass)
        avg_broad = np.mean(self.history_broad)
        
        bass_hit = current_bass > (avg_bass * self.sens_bass) and current_bass > 0.05
        broad_hit = current_broad > (avg_broad * self.sens_broad) and current_broad > 0.08
        
        self.history_bass.append(current_bass)
        self.history_bass.pop(0)
        self.history_broad.append(current_broad)
        self.history_broad.pop(0)
        
        return bass_hit, broad_hit


# audio capture thread
class AudioThread(QThread):
    audio_signal = pyqtSignal(list, bool, bool)
    silence_signal = pyqtSignal(bool)
    audio_tick = pyqtSignal(float)

    FRAME_DURATION = 1024 / 48000

    def __init__(self):
        super().__init__()
        self.detector = BeatDetector()
        self.running = True
        self.rolling_peak = 2.0
        self.decay_rate = 0.95
        self.eq_curve = np.linspace(1, 1, 150)
        self.silence_frames = 0
        self.window = np.hanning(1024)

        # ring buffer between capture and processing
        self._raw_buffer = deque(maxlen=8)  # holds up to 8 unprocessed frames
        self._buffer_lock = threading.Lock()
        self._data_ready = threading.Event()

    def run(self):
        import soundcard as sc

        # tight capture loop on os thread
        capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        capture_thread.start()

        # Processing loop — runs in the QThread worker, consumes from deque
        while self.running:
            if not self._data_ready.wait(timeout=0.1):
                continue
            self._data_ready.clear()

            while True:
                with self._buffer_lock:
                    if not self._raw_buffer:
                        break
                    data = self._raw_buffer.popleft()

                self._process_frame(data)

    def _capture_loop(self):
        import soundcard as sc
        import warnings
        try:
            from soundcard import SoundcardRuntimeWarning
        except ImportError:
            SoundcardRuntimeWarning = RuntimeWarning

        # Elevate this OS thread priority so Windows scheduler doesn't starve it
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(),
                2  # THREAD_PRIORITY_HIGHEST
            )
        except Exception:
            pass

        default_speaker_name = str(sc.default_speaker().name)
        loopback_mic = sc.get_microphone(id=default_speaker_name, include_loopback=True)

        with loopback_mic.recorder(samplerate=48000) as mic:
            while self.running:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    data = mic.record(numframes=1024)

                had_discontinuity = any(
                    issubclass(w.category, SoundcardRuntimeWarning) for w in caught
                )

                with self._buffer_lock:
                    self._raw_buffer.append((data, had_discontinuity))
                self._data_ready.set()

    def _process_frame(self, payload):
        data, had_discontinuity = payload

        peak_amplitude = np.max(np.abs(data))
        if peak_amplitude < 0.0001 and not had_discontinuity:
            self.silence_frames += 1
        else:
            self.silence_frames = 0

        is_silent = (self.silence_frames > 5)
        self.silence_signal.emit(is_silent)

        if not had_discontinuity and not is_silent:
            self.audio_tick.emit(self.FRAME_DURATION)

        fft_data = np.abs(np.fft.rfft(data[:, 0] * self.window))
        vis_data = fft_data[1:151] * self.eq_curve
        current_peak = np.percentile(vis_data, 98)

        if current_peak > self.rolling_peak:
            self.rolling_peak = current_peak
        else:
            self.rolling_peak *= self.decay_rate

        effective_peak = max(self.rolling_peak, 1.0)
        normalized_bars = (vis_data / effective_peak).tolist()

        is_bass, is_broad = self.detector.is_beat(fft_data)
        self.audio_signal.emit(normalized_bars, bool(is_bass), bool(is_broad))

    def stop(self):
        self.running = False
        self._data_ready.set()  # unblock the processing loop
        self.wait(2000)

# windows media API thread
class MediaThread(QThread):
    media_signal = pyqtSignal(str, str, bytes, str, float)
    playback_state_signal = pyqtSignal(bool) 
    position_signal = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.running = True
        self.audio_is_silent = True
        self._pending_tick = 0.0  # No lock — GIL is sufficient for a float
        self._loop = None

    @pyqtSlot(bool)
    def set_audio_silence(self, is_silent):
        self.audio_is_silent = is_silent

    @pyqtSlot(float)
    def on_audio_tick(self, duration):
        # Only advance when audio is confirmed playing
        if not self.audio_is_silent:
            self._pending_tick += duration

    def run(self):
        import asyncio
        import datetime
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus
        )
        from winrt.windows.storage.streams import Buffer

        async def fetch_media():
            LYRIC_LEAD = 0.3
            current_title = ""
            current_thumb_size = 0
            last_known_is_playing = True
            
            internal_pos = 0.0
            last_seen_update_time = None
            expected_timeline_update_after = None
            current_duration = 0.0

            last_os_target = None
            last_os_target_time = None 

            manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()

            while self.running:
                try:
                    session = manager.get_current_session()
                    if session:
                        playback_info = session.get_playback_info()
                        is_playing = (playback_info.playback_status == GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING)
                        
                        if is_playing != last_known_is_playing:
                            self.playback_state_signal.emit(is_playing)
                            last_known_is_playing = is_playing

                        app_id = session.source_app_user_model_id if session.source_app_user_model_id else ""
                        info = await session.try_get_media_properties_async()
                        
                        if info.title:
                            title_changed = (info.title != current_title)
                            image_bytes = b""
                            if info.thumbnail:
                                stream = await info.thumbnail.open_read_async()
                                if stream.size > 0:
                                    buffer = Buffer(stream.size)
                                    await stream.read_async(buffer, stream.size, 0)
                                    image_bytes = bytes(buffer)
                            thumb_changed = (len(image_bytes) != current_thumb_size)
                            
                            if title_changed or thumb_changed:
                                is_first_boot = (current_title == "")  # Check before updating current_title
                                
                                current_title = info.title
                                current_thumb_size = len(image_bytes)
                                artist_name = info.artist if info.artist else "Unknown Artist"
                                
                                timeline = session.get_timeline_properties()
                                current_duration = timeline.end_time.total_seconds() if timeline else 0.0
                                
                                self.media_signal.emit(info.title, artist_name, image_bytes, app_id, current_duration)
                                
                                if title_changed:
                                    if not is_first_boot:
                                        # Track skip — engage limbo lock to ignore stale ghost data
                                        expected_timeline_update_after = datetime.datetime.now(datetime.timezone.utc)
                                        last_seen_update_time = None
                                        internal_pos = 0.0
                                        self._pending_tick = 0.0
                                        self.position_signal.emit(0.0)
                                    else:
                                        # Startup — bypass limbo and let the OS snap fire immediately
                                        expected_timeline_update_after = None
                                        last_seen_update_time = None
                                        self._pending_tick = 0.0

                        timeline = session.get_timeline_properties()
                        if timeline:
                            current_update = timeline.last_updated_time
                            base_pos = timeline.position.total_seconds()
                            
                            old_timeline = False
                            if expected_timeline_update_after:
                                if current_update < expected_timeline_update_after:
                                    old_timeline = True
                                else:
                                    expected_timeline_update_after = None

                            if not old_timeline and current_update != last_seen_update_time:
                                last_seen_update_time = current_update

                                now = datetime.datetime.now(datetime.timezone.utc)
                                age_of_update = max(0.0, (now - current_update).total_seconds())
                                os_target = (base_pos + age_of_update if is_playing else base_pos) + LYRIC_LEAD

                                print(f"OS UPDATE: target={os_target:.3f} internal={internal_pos:.3f} drift={os_target - internal_pos:.3f}")

                                drift = os_target - internal_pos
                                if internal_pos == 0.0 or abs(drift) > 2.0:
                                    internal_pos = os_target
                                    self._pending_tick = 0.0
                                    last_os_target = None  # Hard snap
                                else:
                                    last_os_target = os_target
                                    last_os_target_time = datetime.datetime.now(datetime.timezone.utc)

                            # 3. Drain hardware ticks 
                            else:
                                tick = self._pending_tick
                                self._pending_tick = 0.0
                                internal_pos += tick

                                # Continuously nudge toward last known OS target every loop iteration
                                if not old_timeline and last_os_target is not None and is_playing:
                                    # Project where os_target should be now based on time since recieved
                                    age = (datetime.datetime.now(datetime.timezone.utc) - last_os_target_time).total_seconds()
                                    projected_target = last_os_target + age
                                    
                                    drift = projected_target - internal_pos
                                    if abs(drift) > 0.05:
                                        internal_pos += drift * 0.05  # Small factor every 50ms so it accumulates fast
                                    elif abs(drift) <= 0.05:
                                        last_os_target = None

                            if current_duration > 0:
                                internal_pos = min(internal_pos, current_duration)
                                
                            self.position_signal.emit(internal_pos)

                except Exception:
                    if last_known_is_playing:
                        self.playback_state_signal.emit(False)
                        last_known_is_playing = False

                await asyncio.sleep(0.05)

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(fetch_media())
        except RuntimeError as e:
            if "Event loop stopped before Future completed" not in str(e):
                raise  # re-raise anything unexpected
        finally:
            self._loop.close()

    def stop(self):
        self.running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.wait(2000)


# sizegrip throttle
class ThrottledSizeGrip(QSizeGrip):
    def __init__(self, parent=None):
        super().__init__(parent)
        # timer for drag throttling
        from PyQt6.QtCore import QElapsedTimer
        self.drag_timer = QElapsedTimer()
        self.drag_timer.start()

    def mouseMoveEvent(self, event):
        # throttle mouse moves <8ms
        if self.drag_timer.elapsed() < 8:
            return 
        self.drag_timer.restart()
        super().mouseMoveEvent(event)

# main UI window
class MusicOverlay(QWidget):
    def __init__(self):
        super().__init__()
        
        # window flags
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # apply win11 blur
        try:
            hwnd = int(self.winId())
            
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ctypes.c_uint(0xFFFFFFFE)), 4)
            
            accent = ACCENT_POLICY()
            accent.AccentState = 4
            
            data = WINDOWCOMPOSITIONATTRIBDATA()
            data.Attribute = 19 
            data.Data = ctypes.pointer(accent)
            data.SizeOfData = ctypes.sizeof(accent)
            
            ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.pointer(data))
        except Exception:
            pass

        # ui state vars
        self.enable_auto_popup = True 
        self.config_file = "ui_config.json"
        self.audio_data = np.zeros(150) 
        
        self.bar_intensity = 0.0 
        self.bg_intensity = 0.0
        
        self.drag_pos = None
        self.snap_edge = 'left' 
        self.song_title = "Waiting for music..."
        self.song_artist = ""
        self.album_pixmap = QPixmap()
        
        self.sizegrip = ThrottledSizeGrip(self)
        
        self.is_locked = False
        self.scaler_enabled = True

        self.current_color = QColor(255, 255, 255)
        self.color_anim = QVariantAnimation(self)
        self.color_anim.setDuration(1000)
        self.color_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.color_anim.valueChanged.connect(self.update_color)

        self.content_opacity = 1.0
        self.pending_metadata = None
        self.content_anim = QVariantAnimation(self)
        self.content_anim.setDuration(250)
        self.content_anim.setStartValue(0.0)
        self.content_anim.setEndValue(1.0)
        self.content_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.content_anim.valueChanged.connect(self.update_content_opacity)
        self.content_anim.finished.connect(self.on_content_fade_finished)

        self.text_offset = 0.0
        self.scroll_dist = 0.0
        self.scroll_anim = QVariantAnimation(self)
        self.scroll_anim.setEasingCurve(QEasingCurve.Type.Linear) 
        self.scroll_anim.valueChanged.connect(self.update_text_offset)
        self.scroll_anim.finished.connect(self.on_scroll_finished)
        
        self.scroll_timer = QTimer(self)
        self.scroll_timer.setSingleShot(True) 
        self.scroll_timer.timeout.connect(self.start_text_scroll)
        self.scroll_timer.start(10000)

        self.load_position()
        if self.scaler_enabled and not self.is_minimized:
            self.sizegrip.show()
            self.sizegrip.raise_()


        self.is_animating = False
        self.auto_reverse_pending = False
        self.pop_anim = QVariantAnimation(self)
        self.pop_anim.setDuration(500) 
        self.pop_anim.setStartValue(0.0)
        self.pop_anim.setEndValue(1.0)
        self.pop_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.pop_anim.valueChanged.connect(self._animate_pop)
        self.pop_anim.finished.connect(self.on_pop_anim_finished)
        
        self.auto_pop_timer = QTimer(self)
        self.auto_pop_timer.setSingleShot(True)
        self.auto_pop_timer.timeout.connect(self.start_minimize_animation)
        
        self.auto_minimized_by_pause = False
        self.pause_timer = QTimer(self)
        self.pause_timer.setSingleShot(True)
        self.pause_timer.setInterval(3000) 
        self.pause_timer.timeout.connect(self.on_music_paused)

        # start audio thread
        self.audio_thread = AudioThread()
        self.audio_thread.audio_signal.connect(self.update_visualizer)
        self.audio_thread.start()

        
        # start media thread
        self.media_thread = MediaThread()
        
        self.audio_thread.silence_signal.connect(self.media_thread.set_audio_silence)

        self.media_thread.media_signal.connect(self.update_metadata)
        self.media_thread.playback_state_signal.connect(self.handle_playback_state)
        self.media_thread.start()
        
        # lyrics fetcher
        self.lyric_engine = LyricsThread()
        self.lyric_engine.lyrics_loaded.connect(self.on_lyrics_received)
        self.current_lyrics = []

        self.media_thread.position_signal.connect(self.update_playback_position)
        self.current_lyric_index = 0

        self.setMouseTracking(True) 
        self.lyrics_expanded = False
        self.hovering_lyrics_tab = False
        self.base_height = 150
        self.expanded_lyrics_height = 400 
        
        self.lyrics_expand_anim = QVariantAnimation(self)
        self.lyrics_expand_anim.setDuration(500) 
        self.lyrics_expand_anim.setStartValue(0.0)
        self.lyrics_expand_anim.setEndValue(1.0)
        self.lyrics_expand_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.lyrics_expand_anim.valueChanged.connect(self._animate_lyrics_height)
        
        self.smooth_scroll_y = 0.0
        self.lyric_scroll_anim = QVariantAnimation(self)
        self.lyric_scroll_anim.setDuration(400)
        self.lyric_scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.lyric_scroll_anim.valueChanged.connect(self.update_lyric_scroll)

        self.is_lyrics_animating = False
        self.lyrics_expand_anim.finished.connect(self.on_lyrics_anim_finished)

        self.hover_alpha = 0.0
        self.hover_anim = QVariantAnimation(self)
        self.hover_anim.setDuration(250)
        self.hover_anim.valueChanged.connect(self._update_hover_alpha)

        self.lyrics_opacity = 1.0
        self.pending_lyrics = None
        self.lyrics_fade_anim = QVariantAnimation(self)
        self.lyrics_fade_anim.setDuration(400)
        self.lyrics_fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.lyrics_fade_anim.valueChanged.connect(self._update_lyrics_opacity)
        self.lyrics_fade_anim.finished.connect(self._on_lyrics_fade_finished)

        self.brightness_timer = QTimer(self)
        self.brightness_timer.timeout.connect(self.update_background_brightness)
        self.brightness_timer.start(250)

        self.audio_thread.audio_tick.connect(self.media_thread.on_audio_tick)
        
        self._smooth_brightness = 0.5

    # hover alpha
    def _update_hover_alpha(self, val):
        self.hover_alpha = val
        self.update()
    
    def update_background_brightness(self):
        self.current_raw_brightness = self.get_background_brightness()
        # lerp it :O
        self._smooth_brightness += (self.current_raw_brightness - self._smooth_brightness) * 0.15
        self.update()

    # lyrics opacity change
    def _update_lyrics_opacity(self, val):
        self.lyrics_opacity = float(val)
        self.update()

    # swap lyrics after fade
    def _on_lyrics_fade_finished(self):
        if self.lyrics_fade_anim.endValue() == 0.0:
            if getattr(self, 'pending_lyrics', None) is not None:
                self.current_lyrics, self.current_lrc_id = self.pending_lyrics
                self.pending_lyrics = None
                self.current_lyric_index = 0
                self.smooth_scroll_y = 0.0
                
                self.lyrics_fade_anim.stop()
                self.lyrics_fade_anim.setStartValue(0.0)
                self.lyrics_fade_anim.setEndValue(1.0)
                self.lyrics_fade_anim.start()
    
    # sample bg brightness
    def get_background_brightness(self):
        screen = self.screen()
        if not screen: return 0.5
        
        geom = self.geometry()
        # Sample a 100x100 patch from 9 points across the widget
        brightnesses = []
        for fx in [0.2, 0.5, 0.8]:
            for fy in [0.2, 0.5, 0.8]:
                sx = geom.x() + int(geom.width() * fx)
                sy = geom.y() + int(geom.height() * fy)
                pixmap = screen.grabWindow(0, sx, sy, 2, 2)
                img = pixmap.toImage().scaled(1, 1,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                brightnesses.append(img.pixelColor(0, 0).valueF())
        return sum(brightnesses) / len(brightnesses)
    
    def get_secondary_text_color(self, alpha_mult=1.0):
        import math
        norm = min(1.0, getattr(self, '_smooth_brightness', 0.5) / 0.7)
        t = max(0.0, min(1.0, (norm - 0.29) / 0.36))
        curved = (1 - math.cos(t * math.pi)) / 2
        val = int(184 - 133 * curved)
        return QColor(val, val, val, int(180 * alpha_mult))
        
    # context menu
    def contextMenuEvent(self, event):
        context_menu = QMenu(self)
        context_menu.setStyleSheet("""
            QMenu { background-color: #2b2b2b; color: white; border: none; }
            QMenu::item { padding: 5px 20px 5px 20px; }
            QMenu::item:selected { background-color: #555; }
        """)

        whitelist_action = context_menu.addAction("Save Current Lyric Version")
        context_menu.addSeparator()

        next_lyric_action = context_menu.addAction("Next Lyric Version")
        prev_lyric_action = context_menu.addAction("Previous Lyric Version")
        context_menu.addSeparator()

        toggle_scaler_action = context_menu.addAction("Show Scaler")
        toggle_scaler_action.setCheckable(True)
        toggle_scaler_action.setChecked(self.scaler_enabled)

        lock_action = context_menu.addAction("Lock Position")
        lock_action.setCheckable(True)
        lock_action.setChecked(self.is_locked)

        context_menu.addSeparator()
        quit_action = context_menu.addAction("Quit")

        action = context_menu.exec(self.mapToGlobal(event.pos()))

        if action == whitelist_action:
            if self.song_title and getattr(self, 'current_lrc_id', None):
                dict_key = f"{self.song_title}::{self.song_artist}"
                
                if not hasattr(self, 'saved_lyrics'):
                    self.saved_lyrics = {}
                    
                self.saved_lyrics[dict_key] = self.current_lrc_id
                self.save_position() 
                print(f"Whitelisted version {self.current_lrc_id} for {dict_key}")
        elif action == next_lyric_action:
            self.lyric_engine.cycle_version(1)
        elif action == prev_lyric_action:
            self.lyric_engine.cycle_version(-1)
        elif action == toggle_scaler_action:
            self.scaler_enabled = toggle_scaler_action.isChecked()
            if self.scaler_enabled and not self.is_minimized:
                self.sizegrip.show()
                self.sizegrip.raise_()
            else:
                self.sizegrip.hide()
            self.save_position()
            
        elif action == lock_action:
            self.is_locked = lock_action.isChecked()
            self.save_position()
            
        elif action == quit_action:
            self.close()

    @pyqtSlot(bool)
    # playback state
    def handle_playback_state(self, is_playing):
        if is_playing:
            self.pause_timer.stop()
            if self.auto_minimized_by_pause:
                self.auto_minimized_by_pause = False
                self.start_expand_animation(auto_reverse=False)
        else:
            if not self.is_minimized and not self.is_animating:
                self.pause_timer.start()

    # pause handler
    def on_music_paused(self):
        if not self.is_minimized and not self.is_animating:
            self.auto_minimized_by_pause = True
            self.start_minimize_animation()
            
    # color tween
    def update_color(self, color):
        self.current_color = color
        self.update() 
    
    # determine album color
    def extract_dominant_color(self):
        target_color = QColor(220, 220, 220) 
        if not self.album_pixmap.isNull():
            sample_size = 10
            img = self.album_pixmap.toImage().scaled(sample_size, sample_size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            
            best_color = QColor(255, 255, 255)
            max_saturation = -1
            
            for x in range(sample_size):
                for y in range(sample_size):
                    pixel = img.pixelColor(x, y)
                    if pixel.value() > 40 and pixel.saturation() > max_saturation:
                        max_saturation = pixel.saturation()
                        best_color = pixel
                        
            target_color = best_color
            h, s, v, a = target_color.getHsv()
            
            v = max(v, 160) 
            s = min(s, 120) 
            
            target_color.setHsv(h, s, v, a)
            
        self.color_anim.stop()
        self.color_anim.setStartValue(self.current_color)
        self.color_anim.setEndValue(target_color)
        self.color_anim.start()
    
    # scroll offset
    def update_text_offset(self, val):
        self.text_offset = val
        self.update()

    # begin text scroll
    def start_text_scroll(self):
        if self.is_minimized: 
            self.scroll_timer.start(10000)
            return
            
        title_font = QFont("Segoe UI", 14, QFont.Weight.Bold)
        title_fm = QFontMetrics(title_font)
        title_w = title_fm.horizontalAdvance(self.song_title)
        
        artist_font = QFont("Segoe UI", 10)
        artist_fm = QFontMetrics(artist_font)
        artist_w = artist_fm.horizontalAdvance(self.song_artist)
        
        avail_w = self.width() - 120 
        
        if title_w > avail_w or artist_w > avail_w:
            self.scroll_dist = max(title_w, artist_w) + 50 
            duration = int((self.scroll_dist / 30.0) * 1000) 
            self.scroll_anim.setDuration(max(2000, duration))
            self.scroll_anim.setStartValue(0.0)
            self.scroll_anim.setEndValue(float(self.scroll_dist))
            self.scroll_anim.start()
        else:
            self.scroll_timer.start(10000)

    # scroll done
    def on_scroll_finished(self):
        self.text_offset = 0.0
        self.update()
        self.scroll_timer.start(10000) 

    # content fade
    def update_content_opacity(self, val):
        self.content_opacity = val
        self.update()

    @pyqtSlot(list, object)
    # new lyrics arrived
    def on_lyrics_received(self, lyrics_list, lrc_id):
        self.pending_lyrics = (lyrics_list, lrc_id)
        
        if self.lyrics_opacity <= 0.01:
            self._on_lyrics_fade_finished()
        elif self.lyrics_fade_anim.endValue() != 0.0:
            self.lyrics_fade_anim.stop()
            self.lyrics_fade_anim.setStartValue(float(self.lyrics_opacity))
            self.lyrics_fade_anim.setEndValue(0.0)
            self.lyrics_fade_anim.start()
        print(f"\n--- SYNCED LYRICS LOADED (ID: {lrc_id}) ---")

    # content fade finished
    def on_content_fade_finished(self):
        if self.content_anim.direction() == QAbstractAnimation.Direction.Backward:
            if self.pending_metadata:
                title, artist, image_bytes, app_id, duration = self.pending_metadata
                self.song_title = title
                self.song_artist = artist
                
                self.text_offset = 0.0
                self.scroll_anim.stop()
                self.scroll_timer.start(10000)
                
                if image_bytes: self.album_pixmap.loadFromData(image_bytes)
                else: self.album_pixmap = QPixmap()
                
                self.extract_dominant_color()
                
                dict_key = f"{self.song_title}::{self.song_artist}"
                saved_id = getattr(self, 'saved_lyrics', {}).get(dict_key)
                
                if saved_id:
                    print(f"Fetching WHITELISTED lyrics for: {self.song_title} (ID: {saved_id})")
                else:
                    print(f"Fetching lyrics for: {self.song_title} by {self.song_artist}")
                    
                self.lyric_engine.fetch(self.song_title, self.song_artist, duration, saved_id)
                
                is_yt_music = "chrome" in app_id.lower() or "msedge" in app_id.lower()
                
                if self.enable_auto_popup and is_yt_music:
                    if self.is_minimized or (self.is_animating and self.pop_anim.direction() == QAbstractAnimation.Direction.Backward):
                        self.start_expand_animation(auto_reverse=True)
                    elif self.auto_reverse_pending:
                        self.auto_pop_timer.start(2500)
            self.content_anim.setDirection(QAbstractAnimation.Direction.Forward)
            self.content_anim.start()

    @pyqtSlot(str, str, bytes, str, float)
    def update_metadata(self, title, artist, image_bytes, app_id, duration):
        
        # change thumbnail only if it arrived late (every time)
        if title == self.song_title and self.song_title != "Waiting for music...":
            if image_bytes: 
                self.album_pixmap.loadFromData(image_bytes)
                self.extract_dominant_color()  # tween to the new background color
                self.update()
            return  # dont trigger fadeout

        # otherwise new song
        self.pending_metadata = (title, artist, image_bytes, app_id, duration)
        
        # if its already fading undo it
        if self.content_anim.state() == QAbstractAnimation.State.Running:
            if self.content_anim.direction() == QAbstractAnimation.Direction.Forward:
                self.content_anim.setDirection(QAbstractAnimation.Direction.Backward)
        else:
            self.content_anim.setDirection(QAbstractAnimation.Direction.Backward)
            self.content_anim.start()
            
        # fade out old lyrics on track change
        if getattr(self, 'current_lyrics', None):
            self.lyrics_fade_anim.stop()
            self.lyrics_fade_anim.setStartValue(float(self.lyrics_opacity))
            self.lyrics_fade_anim.setEndValue(0.0)
            self.lyrics_fade_anim.start()

    @pyqtSlot(float)
    # playback position update
    def update_playback_position(self, pos_seconds):
        if not getattr(self, 'current_lyrics', None):
            return
            
        if self.current_lyric_index > 0 and pos_seconds < self.current_lyrics[self.current_lyric_index - 1].timestamp:
            self.current_lyric_index = 0
            self.animate_lyric_scroll()
            
        old_index = self.current_lyric_index
        
        while self.current_lyric_index < len(self.current_lyrics):
            line = self.current_lyrics[self.current_lyric_index]
            if pos_seconds >= line.timestamp:
                self.current_lyric_index += 1
            else:
                break
                
        if old_index != self.current_lyric_index:
            self.animate_lyric_scroll()

    # load saved geometry/state
    def load_position(self):
        self.expanded_geometry = QRect(100, 100, 480, 150) 
        self.is_minimized = False
        
        self.base_height = 150
        self.lyric_offset = 250 
        self.lyrics_expanded = False 
        self.saved_lyrics = {}
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r") as f:
                    pos = json.load(f)
                    
                    self.base_height = pos.get("base_h", 150)
                    self.lyric_offset = pos.get("lyric_offset", 250)
                    
                    x, y = pos.get("x", 100), pos.get("y", 100)
                    w = pos.get("w", 480)
                    
                    self.expanded_geometry = QRect(x, y, w, self.base_height)
                    
                    self.is_minimized = pos.get("minimized", False)
                    self.is_locked = pos.get("locked", False)
                    self.scaler_enabled = pos.get("scaler_enabled", True)
                    self.saved_lyrics = pos.get("saved_lyrics", {})
        except Exception:
            pass
            
        self.expanded_lyrics_height = self.base_height + self.lyric_offset
        self.setGeometry(self.expanded_geometry)
        
        if not self.scaler_enabled:
            self.sizegrip.hide()
            
        if self.is_minimized:
            self.is_minimized = False 
            self._apply_minimize()

    # persist geometry/state
    def save_position(self):
        rect = self.expanded_geometry 
        with open(self.config_file, "w") as f:
            json.dump({
                "x": rect.x(), "y": rect.y(), 
                "w": rect.width(), 
                
                "base_h": getattr(self, 'base_height', 150),
                "lyric_offset": getattr(self, 'lyric_offset', 250),
                
                "minimized": self.is_minimized,
                "locked": self.is_locked,
                "scaler_enabled": self.scaler_enabled,
                "saved_lyrics": getattr(self, 'saved_lyrics', {})
            }, f)

    # toggle native rounding
    def _toggle_native_rounding(self, enabled: bool):
        try:
            hwnd = int(self.winId())
            mode = 2 if enabled else 1
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(mode)), 4
            )
        except Exception:
            pass
    
    # apply minimize geometry
    def _apply_minimize(self):
        screen = self.screen().availableGeometry() 
        y_pos = self.expanded_geometry.y()
        
        if self.expanded_geometry.center().x() < screen.center().x():
            self.snap_edge = 'left'
            self.setGeometry(screen.left() - 2, y_pos, 8, 100) 
        else:
            self.snap_edge = 'right'
            self.setGeometry(screen.right() - 6, y_pos, 8, 100) 
            
        self.is_minimized = True
        self._toggle_native_rounding(False) 
        self.sizegrip.hide()

    def resizeEvent(self, event):
        # reposition sizegrip
        self.sizegrip.move(self.width() - 20, self.height() - 20)
        self.sizegrip.resize(20, 20)
        
        is_expanding = getattr(self, 'is_lyrics_animating', False)
        is_dragging = getattr(self, 'is_dragging_lyrics_bar', False)
        
        if not self.is_minimized and not getattr(self, 'is_animating', False) and not is_expanding and not is_dragging:
            self.expanded_geometry = self.geometry()
            
            if not getattr(self, 'lyrics_expanded', False):
                self.base_height = self.height() 
                self.expanded_lyrics_height = self.base_height + getattr(self, 'lyric_offset', 250)
            else:
                self.expanded_lyrics_height = self.height()
                self.lyric_offset = max(50, self.expanded_lyrics_height - getattr(self, 'base_height', 150))
                
        super().resizeEvent(event)

    def leaveEvent(self, event):
        # hover leave
        if getattr(self, 'hovering_lyrics_tab', False):
            self.hovering_lyrics_tab = False
            self.hover_anim.stop()
            self.hover_anim.setStartValue(float(getattr(self, 'hover_alpha', 0.0)))
            self.hover_anim.setEndValue(0.0)
            self.hover_anim.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        # primary mouse press
        if event.button() == Qt.MouseButton.LeftButton:
            hover_rect = QRect(0, self.height() - 25, self.width(), 25)
            if hover_rect.contains(event.pos()) and not self.is_minimized:
                if not getattr(self, 'lyrics_expanded', False):
                    self.toggle_lyrics()
                else:
                    self.lyric_drag_start = event.globalPosition().toPoint()
                    self.lyric_drag_current = self.lyric_drag_start
                    self.is_dragging_lyrics_bar = True
                if self.auto_pop_timer.isActive():
                    self.auto_pop_timer.stop()
                    self.auto_reverse_pending = False
                event.accept()
                return
                
            if self.auto_pop_timer.isActive():
                self.auto_pop_timer.stop()
                self.auto_reverse_pending = False
                event.accept()
                return
            if self.is_animating:
                event.accept()
                return
            if self.is_minimized:
                self.auto_minimized_by_pause = False 
                self.start_expand_animation(auto_reverse=False)
                event.accept()
                return
            minimize_hitbox = QRect(0, 0, 40, 30) 
            if minimize_hitbox.contains(event.pos()):
                self.start_minimize_animation()
                event.accept()
                return
            
            if not self.is_locked:
                self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        hover_rect = QRect(40, self.height() - 25, self.width() - 80, 25)
        
        is_hover = hover_rect.contains(event.pos()) or getattr(self, 'is_dragging_lyrics_bar', False)
        
        if is_hover != getattr(self, 'hovering_lyrics_tab', False):
            self.hovering_lyrics_tab = is_hover
            
            self.hover_anim.stop()
            self.hover_anim.setStartValue(float(getattr(self, 'hover_alpha', 0.0)))
            self.hover_anim.setEndValue(1.0 if is_hover else 0.0)
            self.hover_anim.start()

        if event.buttons() == Qt.MouseButton.LeftButton:
            
            if not hasattr(self, 'drag_timer'):
                from PyQt6.QtCore import QElapsedTimer
                self.drag_timer = QElapsedTimer()
                self.drag_timer.start()
                
            if self.drag_timer.elapsed() < 8: 
                event.accept()
                return
                
            self.drag_timer.restart()

            if getattr(self, 'is_dragging_lyrics_bar', False):
                new_pos = event.globalPosition().toPoint()
                delta = new_pos.y() - self.lyric_drag_current.y()
                
                new_h = max(self.base_height + 50, self.height() + delta)
                self.setGeometry(self.x(), self.y(), self.width(), new_h)
                
                self.lyric_drag_current = new_pos
                event.accept()
                return
            elif self.drag_pos is not None:
                self.move(event.globalPosition().toPoint() - self.drag_pos)
                event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if getattr(self, 'is_dragging_lyrics_bar', False):
                self.is_dragging_lyrics_bar = False
                
                dist = abs(event.globalPosition().toPoint().y() - self.lyric_drag_start.y())
                if dist < 5:
                    self.toggle_lyrics()
                else:
                    self.expanded_lyrics_height = self.height()
                    self.lyric_offset = max(50, self.expanded_lyrics_height - getattr(self, 'base_height', 150))
                    self.save_position() 
                event.accept()
                return
            self.drag_pos = None
            if not self.is_minimized and not self.is_animating:
                self.expanded_geometry = self.geometry()
            self.save_position()
            event.accept()

    @pyqtSlot(list, bool, bool) 
    # visualizer update
    def update_visualizer(self, normalized_bars, is_bass, is_broad):
        if is_bass or is_broad:
            self.bar_intensity = 1.0
            self.bg_intensity = 1.3 if is_broad else 1.0
            self.is_broad_active = is_broad 
        else:
            self.bar_intensity = max(0.0, self.bar_intensity - 0.02)   
            
            decay = 0.005 if getattr(self, 'is_broad_active', False) else 0.015
            self.bg_intensity = max(0.0, self.bg_intensity - decay)
            
            if self.bg_intensity <= 0: 
                self.is_broad_active = False 

        safe_base = getattr(self, 'base_height', 150)
        max_pixel_height = max(10, safe_base - 90)

        for i in range(150):
            new_val = (normalized_bars[i] if i < len(normalized_bars) else 0) * max_pixel_height
            self.audio_data[i] = max(new_val, self.audio_data[i] * 0.85)
        
        self.update()

    # animation keyframes
    def setup_animation_keyframes(self):
        screen = self.screen().geometry()
        if self.expanded_geometry.center().x() < screen.center().x():
            self.snap_edge = 'left'
        else:
            self.snap_edge = 'right'
        self.anim_end_rect = self.expanded_geometry
        self.anim_mid_rect = QRect(self.expanded_geometry.x(), self.expanded_geometry.y(), self.expanded_geometry.width(), 100)
        if self.snap_edge == 'left':
            start_x = screen.left() - self.expanded_geometry.width() + 5
        else:
            start_x = screen.right() - 4
        self.anim_start_rect = QRect(start_x, self.expanded_geometry.y(), self.expanded_geometry.width(), 100)

    # expand animation
    def start_expand_animation(self, auto_reverse=False):
        self.is_animating = True 
        
        self.is_minimized = False 
        self.auto_reverse_pending = auto_reverse
        
        if auto_reverse and getattr(self, 'lyrics_expanded', False):
            self.lyrics_expanded = False
            self.expanded_lyrics_height = self.base_height
            self.expanded_geometry.setHeight(self.base_height)

        self.sizegrip.hide()
        self.setup_animation_keyframes()
        
        self.pop_anim.setDirection(QAbstractAnimation.Direction.Forward)
        if self.pop_anim.state() != QAbstractAnimation.State.Running:
            self.pop_anim.start()

    # minimize animation
    def start_minimize_animation(self):
        self.is_animating = True
        
        self.sizegrip.hide()
        if not self.is_minimized:
            self.expanded_geometry = self.geometry()
            
        self.setup_animation_keyframes()
        
        self.pop_anim.setDirection(QAbstractAnimation.Direction.Backward)
        if self.pop_anim.state() != QAbstractAnimation.State.Running:
            self.pop_anim.start()

    # pop frame animation
    def _animate_pop(self, val):
        start = self.anim_start_rect
        mid = self.anim_mid_rect
        end = self.anim_end_rect
        if val <= 0.5:
            t = val * 2.0
            x = int(start.x() + (mid.x() - start.x()) * t)
            y = int(start.y() + (mid.y() - start.y()) * t)
            w = int(start.width() + (mid.width() - start.width()) * t)
            h = int(start.height() + (mid.height() - start.height()) * t)
        else:
            t = (val - 0.5) * 2.0
            x = int(mid.x() + (end.x() - mid.x()) * t)
            y = int(mid.y() + (end.y() - mid.y()) * t)
            w = int(mid.width() + (end.width() - mid.width()) * t)
            h = int(mid.height() + (end.height() - mid.height()) * t)
        self.setGeometry(x, y, w, h)
        self.setWindowOpacity(0.5 + (0.5 * val)) 

    # pop animation end
    def on_pop_anim_finished(self):
        if self.pop_anim.direction() == QAbstractAnimation.Direction.Forward:
            self.is_animating = False
            self.setGeometry(self.expanded_geometry)
            
            if self.scaler_enabled:
                self.sizegrip.show()
                self.sizegrip.raise_()
                
            #self.save_position()
            if self.auto_reverse_pending:
                self.auto_pop_timer.start(2500) 
        else:
            self.is_animating = False
            self.is_minimized = True
            self.setWindowOpacity(1.0)
            #self.save_position()
            
            screen = self.screen().availableGeometry()
            y_pos = self.expanded_geometry.y()
            
            if self.snap_edge == 'left':
                self.setGeometry(screen.left() - 2, y_pos, 8, 100)
            else:
                self.setGeometry(screen.right() - 6, y_pos, 8, 100)
                
            self._toggle_native_rounding(False) 
            self.sizegrip.hide()

    # toggle lyrics pane
    def toggle_lyrics(self):
        self.lyrics_expanded = not self.lyrics_expanded
        self.is_lyrics_animating = True 

        self.lyric_start_h = float(self.height())
        
        fully_open_h = float(getattr(self, 'base_height', 150) + getattr(self, 'lyric_offset', 250))
        
        if self.lyrics_expanded:
            self.lyric_end_h = fully_open_h
        else:
            self.lyric_end_h = float(getattr(self, 'base_height', 150))

        self.expanded_lyrics_height = fully_open_h

        # fire the 0.0 to 1.0 multiplier
        self.lyrics_expand_anim.stop()
        self.lyrics_expand_anim.setStartValue(0.0)
        self.lyrics_expand_anim.setEndValue(1.0)
        self.lyrics_expand_anim.start()

    def on_lyrics_anim_finished(self):
        self.is_lyrics_animating = False
        if not getattr(self, 'is_minimized', False) and not getattr(self, 'is_animating', False):
            self.expanded_geometry = self.geometry()

    # lyrics height frame
    def _animate_lyrics_height(self, val):
        current_h = self.lyric_start_h + ((self.lyric_end_h - self.lyric_start_h) * val)
        safe_h = int(current_h)
        
        self.setGeometry(self.x(), self.y(), self.width(), safe_h)
        self.expanded_geometry.setHeight(safe_h) 
        
    # scroll to active lyric
    def animate_lyric_scroll(self):
        active_index = max(-1, self.current_lyric_index - 1)
        
        offset = min(15.0 + (active_index * 0), 40.0)
        target_y = 20.0 + (active_index * 50.0) - offset
        target_y = max(0.0, target_y)
        
        self.lyric_scroll_anim.stop()
        self.lyric_scroll_anim.setStartValue(getattr(self, 'smooth_scroll_y', 0.0))
        self.lyric_scroll_anim.setEndValue(float(target_y))
        self.lyric_scroll_anim.start()

    # lyric scroll update
    def update_lyric_scroll(self, val):
        self.smooth_scroll_y = val
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        raw_brightness = getattr(self, 'current_raw_brightness', 0.5)

        norm_brightness = min(1.0, raw_brightness / 0.7)

        base_alpha = int(20 + (90 * norm_brightness))
        
        pulse_top = int(40 * self.bg_intensity)
        pulse_bottom = int(10 * self.bg_intensity)

        if norm_brightness > 0.6:
            pulse_top += int(30 * norm_brightness * self.bg_intensity)
            pulse_bottom += int(10 * norm_brightness * self.bg_intensity)
        
        c = self.current_color
        r_hint, g_hint, b_hint = int(c.red() * 0.15), int(c.green() * 0.15), int(c.blue() * 0.15)
        
        glass_grad = QLinearGradient(0, 0, 0, self.height())
        glass_grad.setColorAt(0.0, QColor(25+r_hint+pulse_top, 25+g_hint+pulse_top, 30+b_hint+pulse_top, base_alpha+pulse_top)) 
        glass_grad.setColorAt(1.0, QColor(5+r_hint+pulse_bottom, 5+g_hint+pulse_bottom, 10+b_hint+pulse_bottom, base_alpha+10+pulse_bottom))

        painter.setBrush(glass_grad)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 15, 15)    
        
        ui_alpha = int(255 * self.content_opacity)
        painter.setPen(QColor(150, 150, 150, int(180 * self.content_opacity)))
        painter.drawLine(15, 15, 27, 15)

        painter.save()
        
        text_rect_width = self.width() - 120
        
        if text_rect_width > 0:
            painter.save()
            clip_rect = QRect(20, 15, text_rect_width, 75)
            painter.setClipRect(clip_rect)

            title_font = QFont("Segoe UI", 14, QFont.Weight.Bold)
            title_fm = QFontMetrics(title_font)
            title_w = title_fm.horizontalAdvance(self.song_title)

            title_grad = QLinearGradient(20, 0, 20 + text_rect_width, 0)
            title_grad.setColorAt(0.0, QColor(255, 255, 255, ui_alpha))
            if title_w > text_rect_width:
                title_grad.setColorAt(0.85, QColor(255, 255, 255, ui_alpha))
                title_grad.setColorAt(1.0, QColor(255, 255, 255, 0)) 
            else:
                title_grad.setColorAt(1.0, QColor(255, 255, 255, ui_alpha))

            painter.setPen(QPen(QBrush(title_grad), 1))
            painter.setFont(title_font)
            
            if title_w > text_rect_width:
                painter.drawText(int(20 - self.text_offset), 45, self.song_title)
                painter.drawText(int(20 - self.text_offset + self.scroll_dist), 45, self.song_title)
            else:
                painter.drawText(20, 45, self.song_title)

            artist_alpha = int(180 * self.content_opacity)
            artist_font = QFont("Segoe UI", 10)
            artist_fm = QFontMetrics(artist_font)
            artist_w = artist_fm.horizontalAdvance(self.song_artist)

            artist_grad = QLinearGradient(20, 0, 20 + text_rect_width, 0)
            artist_grad.setColorAt(0.0, QColor(180, 180, 180, artist_alpha))
            if artist_w > text_rect_width:
                artist_grad.setColorAt(0.85, QColor(180, 180, 180, artist_alpha))
                artist_grad.setColorAt(1.0, QColor(180, 180, 180, 0))
            else:
                artist_grad.setColorAt(1.0, QColor(180, 180, 180, artist_alpha))

            painter.setPen(QPen(QBrush(artist_grad), 1))
            painter.setFont(artist_font)
            
            if artist_w > text_rect_width:
                painter.drawText(int(20 - self.text_offset), 70, self.song_artist)
                painter.drawText(int(20 - self.text_offset + self.scroll_dist), 70, self.song_artist)
            else:
                painter.drawText(20, 70, self.song_artist)
                
            painter.restore()

        if not self.album_pixmap.isNull():
            cover_rect = QRectF(self.width() - 90, 15, 70, 70)
            path = QPainterPath()
            path.addRoundedRect(cover_rect, 8, 8) 
            painter.save()
            painter.setClipPath(path)
            painter.setOpacity(self.content_opacity)
            painter.drawPixmap(cover_rect.toRect(), self.album_pixmap)
            painter.restore()
        else:
            painter.setBrush(QColor(50, 50, 50, int(255 * self.content_opacity)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(self.width() - 90, 15, 70, 70, 8, 8)
            
        if getattr(self, 'expanded_lyrics_height', 0) > self.base_height:
            diff = self.expanded_lyrics_height - self.base_height
            expand_progress = (self.height() - self.base_height) / diff if diff > 0 else 0.0
        else:
            expand_progress = 0.0
            
        expand_progress = max(0.0, min(1.0, expand_progress))

        painter.restore()

        c = self.current_color
        h, s, v, a = c.getHsv()
        
        pulse_amount = int(30 * self.bar_intensity) 
        new_s = min(255, s + pulse_amount)
        final_bar_color = QColor.fromHsv(h, new_s, v)
        alpha = int(180 + (75 * self.bar_intensity)) 
        r, g, b = final_bar_color.red(), final_bar_color.green(), final_bar_color.blue()
        
        gradient = QLinearGradient(0, self.base_height - 20, 0, 75)
        gradient.setColorAt(0.0, QColor(r, g, b, alpha)) 
        gradient.setColorAt(1.0, QColor(r, g, b, 0))   
        
        max_height = self.base_height - 90
        ref_gradient = QLinearGradient(0, self.base_height - 20, 0, self.base_height - 20 + (max_height * 0.2))
        ref_gradient.setColorAt(0.0, QColor(r, g, b, int(alpha * 0.5 * expand_progress))) 
        ref_gradient.setColorAt(1.0, QColor(r, g, b, 0)) 

        painter.setPen(Qt.PenStyle.NoPen)
        bar_spacing = 8
        num_bars = max(1, (self.width() - 40) // bar_spacing)
        chunks = np.array_split(self.audio_data, num_bars)
        
        # bar loop - collapse bands into visual bars, preserve transients
        for i, chunk in enumerate(chunks):
            val = np.max(chunk) if len(chunk) > 0 else 0
            bar_height = min(int(val), max_height) 
            
            painter.setBrush(gradient)
            painter.drawRect(20 + (i * bar_spacing), self.base_height - 20, 5, -bar_height)
            
            # reflection draw faint mirror when expanded
            if expand_progress > 0 and bar_height > 0:
                painter.setBrush(ref_gradient)
                painter.drawRect(20 + (i * bar_spacing), self.base_height - 20, 5, int(bar_height * 0.6))

        if getattr(self, 'expanded_lyrics_height', 0) > self.base_height:
            diff = self.expanded_lyrics_height - self.base_height
            expand_progress = (self.height() - self.base_height) / diff if diff > 0 else 0.0
        else:
            expand_progress = 0.0
            
        expand_progress = max(0.0, min(1.0, expand_progress))

        if self.height() > self.base_height and getattr(self, 'current_lyrics', None) and expand_progress > 0.01:
            painter.save()
            lyrics_rect = QRect(0, self.base_height, self.width(), self.height() - self.base_height)
            painter.setClipRect(lyrics_rect)

            start_y = self.base_height + 20.0 
            
            lyric_font = QFont("Segoe UI", 12, QFont.Weight.Bold)
            painter.setFont(lyric_font)
            line_spacing = 50.0 

            active_index = max(-1, self.current_lyric_index - 1)
            
            center_y = start_y + (active_index * line_spacing) - getattr(self, 'smooth_scroll_y', 0)
            
            max_dist_top = max(1.0, center_y - self.base_height)
            max_dist_bottom = max(1.0, self.height() - center_y)

            for i, line in enumerate(self.current_lyrics):
                # lyrics loop - center weighting for active line, fade edges to focus
                line_y = start_y + (i * line_spacing) - getattr(self, 'smooth_scroll_y', 0)
                
                if line_y < self.base_height - 10 or line_y > self.height() + 50:
                    continue
                
                dist = center_y - line_y
                if dist > 0: 
                    pos_alpha = max(0.0, 1.0 - (abs(dist) / max_dist_top))
                else: 
                    pos_alpha = max(0.0, 1.0 - (abs(dist) / max_dist_bottom))
                    
                pos_alpha = pos_alpha ** 1.5 
                final_alpha_mult = pos_alpha * expand_progress * getattr(self, 'lyrics_opacity', 1.0)
                
                if i == active_index:
                    color = QColor(255, 255, 255, int(255 * final_alpha_mult))
                else:
                    color = self.get_secondary_text_color(final_alpha_mult)

                painter.setPen(color)
                text_rect = QRectF(20, line_y - 20, self.width() - 40, 60)
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, line.content)

            painter.restore()

        if getattr(self, 'hover_alpha', 0.0) > 0.0 and not self.is_minimized:
            painter.save()
            
            hover_rect = QRectF(0, self.height() - 25, self.width(), 25)
            
            cx = self.width() / 2.0
            cy = float(self.height())
            
            rx = max(1.0, (self.width() / 2) - 40.0)
            ry = 25.0
            
            grad = QRadialGradient(cx, cy, rx)
            grad.setFocalPoint(cx, cy)
            
            grad.setColorAt(0.0, QColor(0, 0, 0, int(150 * self.hover_alpha)))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            
            brush = QBrush(grad)
            scale_y = ry / rx
            matrix = QTransform()
            matrix.translate(cx, cy)
            matrix.scale(1.0, scale_y)
            matrix.translate(-cx, -cy)
            brush.setTransform(matrix)
            
            painter.setBrush(brush)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(hover_rect.toRect())
            
            painter.setPen(QColor(255, 255, 255, int(120 * self.hover_alpha)))
            caret_font = QFont("Segoe UI", 12, QFont.Weight.Bold)
            painter.setFont(caret_font)
            
            caret_char = "ʌ" if getattr(self, 'lyrics_expanded', False) or getattr(self, 'is_lyrics_animating', False) else "v"
            painter.drawText(hover_rect, Qt.AlignmentFlag.AlignCenter, caret_char)
            
            painter.restore()
            
    # cleanup
    def closeEvent(self, event):

        if not self.is_minimized:
            self.save_position() 

        self.audio_thread.stop()
        self.media_thread.stop()
        event.accept()
        QApplication.instance().quit()
        import os
        os._exit(0)

# entry
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MusicOverlay()
    window.show()
    sys.exit(app.exec())