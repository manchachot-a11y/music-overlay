import requests
import re
from PyQt6.QtCore import QThread, pyqtSignal
import sys
from PyQt6.QtWidgets import QApplication
from collections import namedtuple, OrderedDict
import os
import json

LyricLine = namedtuple("LyricLine", ['timestamp', 'content'])

class LyricsCache:
    def __init__(self, capacity=500, filename="lyrics_cache.json"):
        self.capacity = capacity
        self.filename = filename
        self.cache = self._load_from_disk()

    def _load_from_disk(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return OrderedDict(data)
            except (json.JSONDecodeError, IOError):
                return OrderedDict()
        return OrderedDict()
    
    def save_to_disk(self):
        with open(self.filename, 'w', encoding='utf-8') as f:
            json.dump(list(self.cache.items()), f, indent=4)
    
    def get(self, song_name):
        if song_name not in self.cache:
            return None
        self.cache.move_to_end(song_name)
        return self.cache[song_name]
    
    def put(self, dict_key, track_id, lyrics, force_overwrite=False):
        if dict_key in self.cache:
            self.cache.move_to_end(dict_key)
            if not force_overwrite and self.cache[dict_key].get('id') != track_id:
                return

        self.cache[dict_key] = {
            "id": track_id,
            "lyrics": lyrics
        }

        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

    

class LyricsThread(QThread):
    lyrics_loaded = pyqtSignal(list, object)

    def __init__(self):
        super().__init__()
        self.cache = LyricsCache()
        self.track = ""
        self.artist = ""
        self.duration = 0.0
        self.saved_id = None
        self.cached_results =[]
        self.current_result_idx = 0
        self.direction = 1

    def fetch(self, track, artist, duration=0.0, saved_id=None, force_fetch=False):
        self.cached_results = [] 
        self.current_result_idx = 0
        self.force_fetch = force_fetch

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
            dict_key = f"{current_track}::{current_artist}"

            if not self.cached_results and not self.force_fetch:
                cached_data = self.cache.get(dict_key)
                if cached_data:
                    print(f"CACHE HIT: {dict_key}")
                    self.saved_id = cached_data['id'] 
                    parsed = self.parse_lrc(cached_data['lyrics'])
                    self.lyrics_loaded.emit(parsed, cached_data['id'])
                    break
            
            base_url = "https://lrclib.net/api/search"
            params = {"track_name": current_track, "artist_name": current_artist}
            
            try:
                self._emit_fetching(self.force_fetch)
                response = requests.get(base_url, params=params)#, timeout=10)
                
                # if the user skipped the song during the network delay, loop back
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

                        if self.force_fetch:
                            self.cycle_version(self.direction)    
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
        self.direction = direction
        if not self.cached_results:
            print("Re-fetching Lyrics")
            self.fetch(track=self.track, artist=self.artist, duration=self.duration, saved_id=self.saved_id, force_fetch=True)
            return
        self.current_result_idx = (self.current_result_idx + direction) % len(self.cached_results)
        print(f"Lyrics: Switched to version {self.current_result_idx + 1} of {len(self.cached_results)}")
        self._emit_current()

    def _emit_fetching(self, refetching=False):
        if not refetching:
            self.lyrics_loaded.emit([LyricLine(0.0, "Fetching Lyrics...")], None)
        else:
            self.lyrics_loaded.emit([LyricLine(0.0, "Re-Fetching Lyrics...")], None)

    def _emit_current(self):
        if not self.cached_results: return
        result = self.cached_results[self.current_result_idx]
        lrc = result.get('syncedLyrics')
        lrc_id = result.get('id')

        dict_key = f"{self.track}::{self.artist}"

        self.cache.put(dict_key, lrc_id, lrc, force_overwrite=False)
        self.cache.save_to_disk()

        parsed = self.parse_lrc(lrc)
        self.lyrics_loaded.emit(parsed, lrc_id)

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

    def whitelist_current(self):
        if not self.cached_results:
            return

        result = self.cached_results[self.current_result_idx]
        lrc = result.get('syncedLyrics')
        lrc_id = result.get('id')
        dict_key = f"{self.track}::{self.artist}"

        self.cache.put(dict_key, lrc_id, lrc, force_overwrite=True)
        self.cache.save_to_disk()
        print(f"Cache Updated: Whitelisted {lrc_id} for {dict_key}")
    
if __name__ == "__main__":
    app = QApplication(sys.argv)

    def on_lyrics_loaded(lyrics, lyric_id):
        print(lyrics[:5])
        app.quit()

    thread = LyricsThread()
    thread.lyrics_loaded.connect(on_lyrics_loaded)

    thread.fetch(track=input("Track: "), artist=input("Artist: "))

    sys.exit(app.exec())