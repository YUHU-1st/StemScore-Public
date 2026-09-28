from pathlib import Path
import math
import struct
import wave

import mido


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(123), time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    midi.tracks.append(conductor)
    for name, note in (("Piano", 60), ("Bass", 36)):
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        track.append(mido.Message("program_change", program=0, time=0))
        track.append(mido.Message("note_on", note=note, velocity=100, time=0))
        track.append(mido.Message("note_off", note=note, velocity=0, time=1920))
        midi.tracks.append(track)
    midi.save(ROOT / "probe.mid")

    with wave.open(str(ROOT / "probe.wav"), "wb") as wav:
        wav.setparams((2, 2, 44100, 44100, "NONE", "not compressed"))
        frames = bytearray()
        for index in range(44100):
            value = int(0.15 * 32767 * math.sin(2 * math.pi * 440 * index / 44100))
            frames.extend(struct.pack("<hh", value, value))
        wav.writeframes(frames)


if __name__ == "__main__":
    main()
