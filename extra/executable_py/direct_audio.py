""" direct audio output using PyAudio """
from pathlib import Path

from pydub import AudioSegment
import numpy as np
import matplotlib.pyplot as plt
import pyaudio
import json

ASSET_DIR = Path(__file__).resolve().parent.parent
SAMPLE_RATE = 44100  # Standard CD-quality sample rate
CHUNKSIZE = 1024

# Load your audio file (MP3, WAV, etc.)
AUDIO_PATH = ASSET_DIR / "getallen_44100.wav"
audio = AudioSegment.from_file(AUDIO_PATH)
audio = audio.set_channels(1)
samples = np.array(audio.get_array_of_samples()).astype(np.float32)
samples /= np.max(np.abs(samples))

# Get sample rate (frames per second)
sample_rate = audio.frame_rate

# Extract raw audio waarschuwing as a NumPy array
WARNING_PATH = ASSET_DIR / "waarschuwing.wav"
waarschuwing_segment = AudioSegment.from_file(WARNING_PATH) -3
waarschuwing = waarschuwing_segment.set_channels(1)
waarschuwing = np.array(waarschuwing_segment.get_array_of_samples()).astype(np.float32)
waarschuwing /= np.max(np.abs(waarschuwing))


# Print summary
#print(f"Audio duration: {len(samples)/sample_rate:.2f} seconds")
#print(f"Sample rate: {sample_rate} Hz")

# Plot waveform using matplotlib
def plot_waveform(samples, sample_rate = SAMPLE_RATE):
    plt.figure(figsize=(12, 4))
    plt.plot(samples, color='slateblue')
    plt.title("Original Audio Waveform")
    plt.xlabel("Sample Index")
    plt.ylabel("Amplitude")
    plt.grid(True)
    plt.tight_layout()
    plt.show()

# Initialize PyAudio
p = pyaudio.PyAudio()

"""
for i in range(p.get_device_count()):

    dev = p.get_device_info_by_index(i)
    print(f"Device {i}: {dev['name']} (Output: {dev['maxOutputChannels'] > 0})")
"""
stream = p.open(
    format=pyaudio.paInt16,
    channels=1,
    rate=SAMPLE_RATE,
    output=True,
    output_device_index=1  # Uncomment to use a specific device
)

getallen= samples[SAMPLE_RATE*1:]  # delet 1 second
honderdtallen = samples[CHUNKSIZE*5550:]  # keep 1 second
num_chunks = len(honderdtallen ) // CHUNKSIZE + 1
print(f"Total chunks: {num_chunks}")
SHOW_WAVEFORM = True

def create_audio_chunks(stream, seg, index):
    """ Play a chunk of audio waarschuwing from start to end """
    #for (start,end) in index:
    b = index[0] * CHUNKSIZE
    e = index[1] * CHUNKSIZE
    chunk = seg[b:e]
    print(f"Playing segment index {index[0]}..{index[1]}")

    if SHOW_WAVEFORM:
        plot_waveform(chunk)

    return (chunk * 32767).astype(np.int16).tobytes()


index_warn = [ (0, 108), (108, 150), (150, 176), (176, 225), (225, 330), (330, 390), (390, 460), \
        (460, 505),(505, 588),(588,638),(638, 732), (732, 770), (770, 800)]

index_0 =[(7,34),(50,72),(88,110),(130,150),(170,200),(210,250),(255,295),(295,325),(340,370),(382,410),(425,450),\
        (460,490),(503,535),(540,570),(590,620),(632,670),(680,720),(730,770),(780,820),(830,870),(880,920),\
        (930,970),(980,1020),(1020,1070),(1085,1120),(1120,1170),(1170,1220),(1235,1280),(1300,1335),(1351,1390),(1408,1445),\
        (1455,1490),(1510,1550),(1560,1610),(1620,1665),(1675,1720),(1730,1775),(1783,1828),(1840,1885),(1895,1940),(1948,1985),\
        (2000,2050),(2050,2100),(2105,2150),(2162,2210),(2225,2270),(2280,2325),(2330,2375),(2400,2435),(2455,2490),(2510,2545),\
        (2563,2600),(2610,2655),(2670,2710 ),(2730,2765),(2785,2820),(2850,2890),(2910,2950),(2975,3010),(3035,3075),(3100,3135),\
        (3152,3195),(3208,3250),(3270,3310),(3338,3380),(3400,3450),(3470,3500),(3530,3570),(3600,3640),(3660,3710),(3720,3750),\
        (3785,3820),(3850,3885),(3910,3960),(3970,4010),(4035,4075),(4098,4135), (4158,4198),(4220,4260),(4282,4330),(4340,4370),\
        (4390,4430),(4450,4490),(4500,4540),(4555,4600),(4610,4660),(4670,4710),(4730,4760),(4790,4820),(4840,4880),(4900,4930),\
        (4970,5000),(5028,5060),(5085,5120),(5140,5190),(5200,5250),(5260,5315),(5320,5370),(5388,5430),(5440,5490),(5510,5535)]

index_100 = [(7,34),(55,85),(110,140),(160,190),(210,250),(270,300),(320,365),(380,425),(445,475),(490,520),\
            (540,570),(590,630),(640,690),(700,750),(750,800),(800,850),(860,910),(920,970),(970,1020),(1030,1080),(1090,1150),\
            (1155,1200),(1210,1260),(1270,1330),(1340,1400),(1410,1470),(1490,1555),(1570,1620),(1630,1680),(1700,1760)]

def create_warning(num):
    return create_audio_chunks(stream, waarschuwing, index_warn[num])

def create_number(num):
    return create_audio_chunks(stream, getallen, index_0[num])

def create_number_100(num):
    return create_audio_chunks(stream, honderdtallen, index_100[num])

i = 13
#for i in range(13):
stream.write(create_number(i) )

##SHOW_WAVEFORM = False


stream.stop_stream()
stream.close()
p.terminate()


with open(ASSET_DIR / "warn.json", "w", encoding="utf-8") as f:
    json.dump(index_warn, f)

with open(ASSET_DIR / "index_0.json", "w", encoding="utf-8") as f:
    json.dump(index_0, f)
"""
with open(ASSET_DIR / "index_100.json", "w", encoding="utf-8") as f:
    json.dump(index_100, f)

with open(ASSET_DIR / "index_0.json", "r", encoding="utf-8") as f:
    index_0_ = json.load(f)
print(index_0,'\n'*2)

with open(ASSET_DIR / "index_100.json", "r", encoding="utf-8") as f:
    index_100_ = json.load(f)
print(index_100,'\n'*2)

with open(ASSET_DIR / "warn.json", "r", encoding="utf-8") as f:
    index_warn_ = json.load(f)
print(index_warn_,'\n'*2)
"""