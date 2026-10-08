""" creat training data for AIS messages, by adding messages to metadata.txt and generating corresponding audio files in wavs/ directory"""

from os import path, getcwd, mkdir, sep
import wave
import pyaudio
import keyboard


TRAINING_DATA_DIR = 'training_data'
SIMPLE_TEXT = True # No special words/characters in text
WORKING_DIR = getcwd()

if not path.exists(TRAINING_DATA_DIR):
    mkdir(TRAINING_DATA_DIR)

AUDIO_DATA_DIR  = TRAINING_DATA_DIR + sep + 'wavs'

if not path.exists(TRAINING_DATA_DIR + sep + 'wavs'):
    mkdir(TRAINING_DATA_DIR + sep + 'wavs')

FN = 'metadata.txt'

# set the audio parameters
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS  = 1
SAMPLE_RATE  = 22050

def get_audio():
    """initialize PyAudio object"""
    p = pyaudio.PyAudio()
    # open stream object as input & output
    stream = p.open(format=FORMAT,
                    channels = CHANNELS  ,
                    rate=SAMPLE_RATE,
                    input=True,
                    output=True,
                    frames_per_buffer=CHUNK)
    return p, stream

def audio_stop(p):
    """terminate pyaudio object"""
    p.terminate()

def record_audio(stream):
    """
    Docstring for recording

    :param stream: Description
    :param record_seconds: Description
    """
    frames = []
    continue_recording = True
    while continue_recording:
        data = stream.read(CHUNK)
        # if you want to hear your voice while recording
        #stream.write(data)
        print('.',end=' ')
        frames.append(data)
        if keyboard.is_pressed('q'):  # if key 'q' is pressed
            print('You Pressed A Key!')
            break  # finishing the loop
    stream.stop_stream()
    stream.close()
    print("Finished recording.")
    return frames


def read_messages():
    """ read messages from metadata.txt and generate corresponding audio files in wavs/ directory"""
    message_number = 0
    try:
        f = open(TRAINING_DATA_DIR + sep + FN, 'r', encoding="utf-8")
        for l in f.readlines():
            message_number += 1
            l = l.split('|')[1] # get text from line, TO DO: split original and normalized text
            print(str(message_number),l, end='\n'*2)
            p, stream = get_audio()
            frames = record_audio(stream)
            soundfile = 'audio' + str(message_number)+'.wav'
            wf = wave.open(AUDIO_DATA_DIR + sep + soundfile, "wb")
            wf.setnchannels(CHANNELS)  # Mono
            wf.setsampwidth(p.get_sample_size(FORMAT))
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b''.join(frames))
            wf.close()


    except FileNotFoundError as er:
        print(er)

if __name__ == '__main__':
    read_messages()