import requests
import re
from PyQt6.QtCore import QThread, pyqtSignal
import sys
from PyQt6.QtWidgets import QApplication
from collections import namedtuple

LyricLine = namedtuple("LyricLine", ['timestamp', 'content'])

class LyricsThread(QThread):
    lyrics_loaded = pyqtSignal(list, object) 

    def __init__(self):
        super().__init__()
        self.track = ""
        self.artist = ""
        self.duration = 0.0
        self.saved_id = None
        self.cached_results =[]
        self.current_result_idx = 0

    def fetch(self, track, artist, duration=0.0, saved_id=None):
        self.track = track
        self.artist = artist
        self.duration = duration
        self.saved_id = saved_id
        
        # Start the thread only if it isn't already running
        if not self.isRunning():
            self.start()

    def run(self):
        # The loop ensures that if the track changes while downloading, it loops again
        while True:
            current_track = self.track
            current_artist = self.artist
            current_duration = self.duration
            current_id = self.saved_id
            
            base_url = "https://lrclib.net/api/search"
            params = {"track_name": current_track, "artist_name": current_artist}
            
            try:
                response = requests.get(base_url, params=params, timeout=5)
                
                # If the user skipped the song during the network delay, loop back!
                if self.track != current_track:
                    continue
                    
                if response.status_code == 200:
                    results = response.json()
                    if results:
                        self.cached_results = [r for r in results if r.get('syncedLyrics')]
                        
                        if not self.cached_results:
                            print("Lyrics: No synced matches found.")
                            self._emit_failed()
                            break
                            
                        if current_id:
                            match = next((r for r in self.cached_results if str(r.get('id')) == str(current_id)), None)
                            if match:
                                self.current_result_idx = self.cached_results.index(match)
                            else:
                                self._sort_and_pick_best(current_duration)
                        else:
                            self._sort_and_pick_best(current_duration)
                            
                        self._emit_current()
                        break
                
                # failure
                if self.track == current_track:
                    print("Lyrics: Search returned no results.")
                    self._emit_failed()
                    break
                    
            except Exception as e:
                print(f"Lyrics Error: {e}")
                if self.track == current_track:
                    self._emit_failed()
                    break

    def _sort_and_pick_best(self, current_duration):
        if current_duration > 0:
            self.cached_results.sort(key=lambda x: abs(x.get('duration', 0) - current_duration))
        self.current_result_idx = 0

    def cycle_version(self, direction=1):
        if not self.cached_results:
            return
        self.current_result_idx = (self.current_result_idx + direction) % len(self.cached_results)
        print(f"Lyrics: Switched to version {self.current_result_idx + 1} of {len(self.cached_results)}")
        self._emit_current()

    def _emit_current(self):
        result = self.cached_results[self.current_result_idx]
        parsed = self.parse_lrc(result.get('syncedLyrics'))
        self.lyrics_loaded.emit(parsed, result.get('id'))

    def _emit_failed(self):
        self.lyrics_loaded.emit([LyricLine(0.0, "No Lyrics Available")], None)

    def parse_lrc(self, lrc_string):
        lyrics_list =[]
        pattern = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)')

        for line in lrc_string.splitlines():
            match = pattern.match(line.strip())
            if match:
                minutes = int(match.group(1))
                seconds = float(match.group(2))
                text = match.group(3).strip()
                
                if not text:
                    text = "♪"
                    
                total_time = (minutes * 60) + seconds
                lyrics_list.append(LyricLine(total_time, text))

        return lyrics_list
    
if __name__ == "__main__":
    app = QApplication(sys.argv)

    def on_lyrics_loaded(lyrics, lyric_id):
        print(lyrics[:5])
        app.quit()

    thread = LyricsThread()
    thread.lyrics_loaded.connect(on_lyrics_loaded)

    thread.fetch(track=input("Track: "), artist=input("Artist: "))

    sys.exit(app.exec())