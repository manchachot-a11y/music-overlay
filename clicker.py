import soundcard as sc
import soundfile as sf
import numpy as np

import keyboard
import threading
import time

click_audio_path = 'drum-hitclap.wav'
clack_audio_path = 'drum-hitnormal.wav'

class Clicker:
    def __init__(self):
        self.default_speaker = sc.default_speaker()

        #keyboard.hook(self.keyboard_callback)

    def run_clicker(self):
        self.click_audio_data, self.click_samplerate = sf.read(click_audio_path)
        self.clack_audio_data, self.clack_samplerate = sf.read(clack_audio_path)
        

        print(f"playing audio file {click_audio_path}")
        print(f"Sample rate: {self.click_samplerate}")
        print(f"Audio shape {self.click_audio_data.shape}")

        default_speaker = sc.default_speaker()
        #speaker = default_speaker.player(samplerate=48000, blocksize=256, exclusive_mode=True) 
        print(f"Default speaker {default_speaker.name}")
        
        
        #default_speaker.play(data=self.click_audio_data,samplerate=self.click_samplerate)
        #default_speaker.play(data=self.clack_audio_data,samplerate=self.clack_samplerate)
        print("Done")


    def click(self):
        self.default_speaker.play(data=self.click_audio_data, samplerate=self.click_samplerate, blocksize=128)

    def clack(self):
        self.default_speaker.play(data=self.clack_audio_data, samplerate=self.clack_samplerate, blocksize=128)

    def keyboard_callback(self, event):
        if(event.event_type == keyboard.KEY_DOWN):
            if(event.name == 'c'):
                print("c")
                threading.Thread(target=self.click, daemon=True).start()
            if(event.name =='z'):
                threading.Thread(target=self.clack, daemon=True).start()
                print("z")



def main():
    clicker = Clicker()
    clicker.run_clicker()
    keyboard.hook(clicker.keyboard_callback)
    keyboard.wait('esc')

if __name__ == "__main__":
    main()
