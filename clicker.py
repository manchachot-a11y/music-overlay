import soundcard as sc
import soundfile as sf
import numpy as np

import keyboard
import threading
import time

click_audio_path = './drum-hitnormal.wav'
clack_audio_path = './drum-hitnormal.wav'

def run_clicker():
    try:
        click_audio_data, click_samplerate = sf.read(click_audio_path)
        clack_audio_data, clack_samplerate = sf.read(clack_audio_path)
        

        print(f"playing audio file {click_audio_path}")
        print(f"Sample rate: {click_samplerate}")
        print(f"Audio shape {click_audio_data.shape}")

        default_speaker = sc.default_speaker()
        #speaker = default_speaker.player(samplerate=48000, blocksize=256, exclusive_mode=True) 
        print(f"Default speaker {default_speaker.name}")
        
        
        default_speaker.play(data=click_audio_data,samplerate=click_samplerate)
        default_speaker.play(data=clack_audio_data,samplerate=clack_samplerate)
        print("Done")
    except FileNotFoundError:
        print(f"Path {click_audio_path} DNE")
    except Exception as e:
        print(f"error: {e}")


def click():
    default_speaker.play(data=click_audio_data, samplerate=click_samplerate, blocksize=128)

def clack():
    default_speaker.play(data=clack_audio_data, samplerate=clack_samplerate, blocksize=128)

def keyboard_callback(event):
    if(event.event_type == keyboard.KEY_DOWN):
        if(event.name == 'c'):
            print("c")
            threading.Thread(target=click, daemon=True).start()
        if(event.name =='z'):
            threading.Thread(target=clack, daemon=True).start()
            print("z")

keyboard.hook(keyboard_callback)

keyboard.wait('esc')


