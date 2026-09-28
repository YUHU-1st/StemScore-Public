from __future__ import annotations

from pathlib import Path
from typing import Any

import mido


ROLE_PROGRAMS = {
    "lead_vocal": 53,
    "harmony_vocal": 52,
    "bass": 33,
    "guitar": 25,
    "piano": 0,
    "other": 88,
}


def _absolute_seconds(track: mido.MidiTrack, ticks_per_beat: int) -> list[tuple[float, mido.Message]]:
    tempo = 500000
    seconds = 0.0
    events: list[tuple[float, mido.Message]] = []
    for message in track:
        seconds += mido.tick2second(message.time, ticks_per_beat, tempo)
        if message.type == "set_tempo":
            tempo = message.tempo
        else:
            events.append((seconds, message.copy(time=0)))
    return events


def normalize_single_track(path: Path, role: str, bpm: float) -> dict[str, Any]:
    source = mido.MidiFile(path)
    merged = mido.merge_tracks(source.tracks)
    events = _absolute_seconds(merged, source.ticks_per_beat)
    ticks_per_beat = 480
    tempo = mido.bpm2tempo(max(30.0, min(300.0, bpm)))
    grid_ticks = ticks_per_beat // 4
    result = mido.MidiFile(type=0, ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    result.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=role, time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    if role in ROLE_PROGRAMS:
        track.append(mido.Message("program_change", program=ROLE_PROGRAMS[role], channel=0, time=0))
    normalized_events: list[tuple[int, mido.Message]] = []
    active_notes: dict[int, int] = {}
    note_count = 0
    pitches: list[int] = []
    for seconds, message in events:
        if message.is_meta and message.type in {"track_name", "end_of_track"}:
            continue
        absolute_tick = round(mido.second2tick(seconds, ticks_per_beat, tempo))
        if message.type in {"note_on", "note_off"}:
            absolute_tick = round(absolute_tick / grid_ticks) * grid_ticks
            is_note_on = message.type == "note_on" and message.velocity > 0
            if is_note_on:
                active_notes[message.note] = absolute_tick
            else:
                start_tick = active_notes.pop(message.note, None)
                if start_tick is not None:
                    absolute_tick = max(absolute_tick, start_tick + grid_ticks)
        if hasattr(message, "channel"):
            message.channel = 0
        normalized_events.append((absolute_tick, message))
        if message.type == "note_on" and message.velocity > 0:
            note_count += 1
            pitches.append(message.note)
    normalized_events.sort(
        key=lambda item: (
            item[0],
            0 if item[1].type == "note_off" or (item[1].type == "note_on" and item[1].velocity == 0) else 1,
        )
    )
    previous_tick = 0
    for absolute_tick, message in normalized_events:
        message.time = max(0, absolute_tick - previous_tick)
        previous_tick = absolute_tick
        track.append(message)
    track.append(mido.MetaMessage("end_of_track", time=0))
    result.save(path)
    return {
        "notes": note_count,
        "pitch_min": min(pitches) if pitches else None,
        "pitch_max": max(pitches) if pitches else None,
        "bpm": round(bpm, 4),
        "ticks_per_beat": ticks_per_beat,
        "quantized_grid": "1/16",
    }


def write_empty_midi(path: Path, role: str, bpm: float) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    ticks_per_beat = 480
    tempo = mido.bpm2tempo(max(30.0, min(300.0, bpm)))
    midi = mido.MidiFile(type=0, ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=role, time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    if role in ROLE_PROGRAMS:
        track.append(mido.Message("program_change", program=ROLE_PROGRAMS[role], channel=0, time=0))
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi.save(path)
    return {
        "notes": 0,
        "pitch_min": None,
        "pitch_max": None,
        "bpm": round(bpm, 4),
        "ticks_per_beat": ticks_per_beat,
    }


def write_drum_midi(path: Path, notes: list[dict[str, Any]], bpm: float) -> dict[str, Any]:
    ticks_per_beat = 480
    tempo = mido.bpm2tempo(max(30.0, min(300.0, bpm)))
    events: list[tuple[int, mido.Message]] = []
    for note in notes:
        start = round(mido.second2tick(float(note["time"]), ticks_per_beat, tempo))
        duration = max(1, round(mido.second2tick(float(note.get("duration", 0.08)), ticks_per_beat, tempo)))
        pitch = int(note["note"])
        velocity = max(1, min(127, int(note["velocity"])))
        events.append((start, mido.Message("note_on", note=pitch, velocity=velocity, channel=9, time=0)))
        events.append((start + duration, mido.Message("note_off", note=pitch, velocity=0, channel=9, time=0)))
    events.sort(key=lambda item: (item[0], 0 if item[1].type == "note_off" else 1))
    midi = mido.MidiFile(type=0, ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name="drums", time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    previous = 0
    for absolute, message in events:
        message.time = max(0, absolute - previous)
        previous = absolute
        track.append(message)
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi.save(path)
    return {"notes": len(notes), "bpm": round(bpm, 4), "ticks_per_beat": ticks_per_beat}


def merge_midi_files(sources: list[tuple[str, Path]], target: Path, bpm: float) -> dict[str, Any]:
    ticks_per_beat = 480
    tempo = mido.bpm2tempo(max(30.0, min(300.0, bpm)))
    output = mido.MidiFile(type=1, ticks_per_beat=ticks_per_beat)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="StemScore conductor", time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    output.tracks.append(conductor)
    total_notes = 0
    for index, (role, path) in enumerate(sources):
        midi = mido.MidiFile(path)
        merged = mido.merge_tracks(midi.tracks)
        events = _absolute_seconds(merged, midi.ticks_per_beat)
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=role, time=0))
        if role != "drums" and role in ROLE_PROGRAMS:
            track.append(mido.Message("program_change", program=ROLE_PROGRAMS[role], channel=index % 9, time=0))
        previous = 0
        for seconds, message in events:
            if message.is_meta:
                continue
            absolute = round(mido.second2tick(seconds, ticks_per_beat, tempo))
            message.time = max(0, absolute - previous)
            previous = absolute
            if hasattr(message, "channel"):
                message.channel = 9 if role == "drums" else index % 9
            if message.type == "note_on" and message.velocity > 0:
                total_notes += 1
            track.append(message)
        track.append(mido.MetaMessage("end_of_track", time=0))
        output.tracks.append(track)
    output.save(target)
    return {"tracks": len(sources), "notes": total_notes, "bpm": round(bpm, 4)}

