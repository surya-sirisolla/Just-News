"""Make the recap Reel's background tracks: four short instrumentals synthesized from sine waves and
noise, so there is no licence or copyright claim to worry about. Each is 16 bars at 96 BPM (40 s) and
loops seamlessly (the reverb tail wraps around to the start).

The app only plays the files this writes (justnews/assets/music/*.m4a); numpy is needed only here:
    uv run --with numpy python scripts/make_music.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np

SR = 44100
BEAT = 60 / 96
BAR = 4 * BEAT
BARS = 16
LENGTH = BARS * BAR  # 40 s
TAIL = 3.0
OUT = Path(__file__).resolve().parent.parent / "justnews" / "assets" / "music"
rng = np.random.default_rng(7)


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def chord(root: int, kind: str) -> list[int]:
    shape = {"maj": [0, 4, 7], "min": [0, 3, 7], "maj7": [0, 4, 7, 11], "min9": [0, 3, 7, 10, 14],
             "add9": [0, 4, 7, 14]}[kind]
    return [root + s for s in shape]


def t_axis(dur: float) -> np.ndarray:
    return np.arange(int(dur * SR)) / SR


def env(dur: float, attack: float, decay: float, release: float = 0.05) -> np.ndarray:
    t = t_axis(dur)
    e = np.minimum(1, t / attack) * np.exp(-t / decay)
    tail = np.clip((dur - t) / release, 0, 1)
    return e * tail


# --- voices -----------------------------------------------------------------------------------
def keys(f: float, dur: float = 1.6) -> np.ndarray:
    """Soft electric piano: a sine lightly frequency-modulated, the brightness fading first."""
    t = t_axis(dur)
    index = 1.2 * np.exp(-t / 0.25)
    tone = np.sin(2 * np.pi * f * t + index * np.sin(2 * np.pi * f * t))
    return (tone + 0.15 * np.sin(4 * np.pi * f * t)) * env(dur, 0.004, 0.9, 0.3)


def bell(f: float, dur: float = 2.5) -> np.ndarray:
    t = t_axis(dur)
    index = 2.0 * np.exp(-t / 0.6)
    return np.sin(2 * np.pi * f * t + index * np.sin(2 * np.pi * 3.5 * f * t)) * env(dur, 0.003, 1.1, 0.4)


def pluck(f: float, dur: float = 1.8) -> np.ndarray:
    """Karplus-Strong string, vectorised one period at a time."""
    n = int(SR / f)
    out = np.empty(int(dur * SR) + n)
    out[:n] = np.convolve(rng.uniform(-1, 1, n), np.ones(5) / 5, "same") * np.hanning(n)  # a soft pick, not hiss
    for i in range(n, len(out), n):
        prev = out[i - n:i]
        nxt = 0.5 * (prev + np.roll(prev, -1)) * 0.996
        out[i:i + n] = nxt[:len(out) - i]
    return out[:int(dur * SR)] * env(dur, 0.002, 0.8, 0.2)


def pad(notes: list[int], dur: float) -> np.ndarray:
    """Warm pad: three slightly detuned voices per note, a few soft harmonics, slow swell."""
    t = t_axis(dur)
    out = np.zeros_like(t)
    for m in notes:
        for cents in (-7, 0, 7):
            f = hz(m) * 2 ** (cents / 1200)
            phase = rng.uniform(0, 2 * np.pi)
            for h in range(1, 6):
                out += np.sin(2 * np.pi * f * h * t + phase * h) / h ** 1.6
    shape = np.minimum(1, t / 1.2) * np.clip((dur - t) / 1.2, 0, 1)
    return out * shape * (1 + 0.08 * np.sin(2 * np.pi * 0.2 * t)) / (len(notes) * 3)


def bass(f: float, dur: float) -> np.ndarray:
    t = t_axis(dur)
    return (np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)) * env(dur, 0.01, dur * 0.8, 0.08)


def kick(dur: float = 0.35) -> np.ndarray:
    t = t_axis(dur)
    freq = 45 + 70 * np.exp(-t / 0.04)
    return np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t / 0.12)


def tick(dur: float = 0.08, decay: float = 0.015) -> np.ndarray:
    noise = np.diff(np.convolve(rng.uniform(-1, 1, int(dur * SR) + 1), np.ones(4) / 4, "same"))  # 3-8 kHz, no fizz
    return noise * np.exp(-t_axis(dur) / decay)


# --- mixing -----------------------------------------------------------------------------------
class Track:
    def __init__(self):
        self.dry = np.zeros((2, int((LENGTH + TAIL) * SR)))
        self.wet = np.zeros_like(self.dry)

    def add(self, sound: np.ndarray, at: float, gain: float, pan: float = 0.0, reverb: float = 0.3) -> None:
        i = int(at * SR)
        sound = sound[:self.dry.shape[1] - i]
        left, right = gain * np.cos((pan + 1) * np.pi / 4), gain * np.sin((pan + 1) * np.pi / 4)
        for ch, g in ((0, left), (1, right)):
            self.dry[ch, i:i + len(sound)] += g * sound
            self.wet[ch, i:i + len(sound)] += g * reverb * sound

    def mix(self) -> np.ndarray:
        ir_t = t_axis(2.2)
        out = self.dry.copy()
        for ch in range(2):  # a room: decaying noise, a little different per side, softened
            ir = np.convolve(rng.normal(0, 1, len(ir_t)), np.ones(12) / 12, "same") * np.exp(-ir_t / 0.55)
            size = 1 << int(np.ceil(np.log2(out.shape[1] + len(ir))))
            wet = np.fft.irfft(np.fft.rfft(self.wet[ch], size) * np.fft.rfft(ir, size), size)[:out.shape[1]]
            out[ch] += wet / np.abs(ir).sum() * 6
        n = int(LENGTH * SR)
        out[:, :out.shape[1] - n] += out[:, n:]  # wrap the tail so the loop has no seam
        out = out[:, :n]
        out /= np.abs(out).max()
        return np.tanh(out * 1.2) / np.tanh(1.2) * 0.9


def progression(chords: list[tuple[int, str]], bars_each: int = 1):
    """(bar start time, chord notes) for all 16 bars."""
    for b in range(BARS):
        root, kind = chords[(b // bars_each) % len(chords)]
        yield b * BAR, root, chord(root, kind)


# --- the four tracks --------------------------------------------------------------------------
def calm_keys() -> Track:
    """Calm piano: C - G - Am - F, gentle arpeggio, pad and soft bass."""
    tr = Track()
    pattern = [0, 2, 1, 2, 3, 2, 1, 2]
    for start, root, notes in progression([(60, "maj"), (55, "maj"), (57, "min"), (53, "maj")]):
        tr.add(pad(notes, BAR + 1.2), start, 0.30, reverb=0.5)
        tr.add(bass(hz(root - 24), 2 * BEAT), start, 0.45, reverb=0.1)
        tr.add(bass(hz(root - 24), 2 * BEAT), start + 2 * BEAT, 0.35, reverb=0.1)
        tones = notes + [notes[0] + 12]
        for k, step in enumerate(pattern):
            tr.add(keys(hz(tones[step] + 12)), start + k * BEAT / 2, 0.22 - 0.03 * (k % 2),
                   pan=0.3 * np.sin(k), reverb=0.4)
    return tr


def newsroom_pulse() -> Track:
    """Steady newsroom pulse: Am - F - C - G, bass in eighths, soft kick and ticks, keys on the one."""
    tr = Track()
    for start, root, notes in progression([(57, "min"), (53, "maj"), (60, "maj"), (55, "maj")]):
        tr.add(pad(notes, BAR + 1.0), start, 0.22, reverb=0.4)
        for k in range(8):
            tr.add(bass(hz(root - 24), BEAT / 2), start + k * BEAT / 2, 0.40 if k % 2 == 0 else 0.28, reverb=0.05)
            tr.add(tick(), start + k * BEAT / 2 + BEAT / 4, 0.06, pan=0.4, reverb=0.1)
        for beat in (0, 2):
            tr.add(kick(), start + beat * BEAT, 0.55, reverb=0.05)
        tr.add(tick(0.25, 0.08), start + 3 * BEAT, 0.06, pan=-0.3, reverb=0.3)
        for k, m in enumerate(notes):
            tr.add(keys(hz(m + 12), 1.2), start + k * 0.015, 0.14, pan=-0.2, reverb=0.5)
    return tr


def morning_pluck() -> Track:
    """Bright morning: G - D - Em - C, plucked strings and a light shaker."""
    tr = Track()
    pattern = [0, 1, 2, 3, 2, 1, 0, 2]
    for start, root, notes in progression([(55, "maj"), (50, "maj"), (52, "min"), (48, "maj")]):
        tr.add(pad(notes, BAR + 1.0), start, 0.16, reverb=0.5)
        tr.add(bass(hz(root - 12), BAR * 0.9), start, 0.35, reverb=0.1)
        tones = notes + [notes[0] + 12]
        for k, step in enumerate(pattern):
            tr.add(pluck(hz(tones[step] + 12)), start + k * BEAT / 2, 0.30, pan=-0.35 + 0.1 * (k % 4), reverb=0.35)
        for k in range(16):
            tr.add(tick(0.06, 0.01), start + k * BEAT / 4, 0.035 if k % 2 else 0.05, pan=0.45, reverb=0.15)
    return tr


def ambient_glow() -> Track:
    """Slow ambient: Dm9 - Bbmaj7 - Fmaj7 - Cadd9, two bars each, with a few soft bells."""
    tr = Track()
    scale = [62, 64, 65, 69, 72, 74, 76]  # D minor pentatonic-ish, upper register
    for start, root, notes in progression([(50, "min9"), (46, "maj7"), (53, "maj7"), (48, "add9")], bars_each=2):
        if (start / BAR) % 2 == 0:
            tr.add(pad(notes, 2 * BAR + 1.5), start, 0.42, reverb=0.7)
            tr.add(bass(hz(root - 12), 2 * BAR), start, 0.30, reverb=0.2)
        for beat in rng.choice([0, 1.5, 2, 3], size=2, replace=False):
            tr.add(bell(hz(rng.choice(scale) + 12)), start + beat * BEAT, 0.10, pan=rng.uniform(-0.5, 0.5), reverb=0.8)
    return tr


TRACKS = {"calm-keys": calm_keys, "newsroom-pulse": newsroom_pulse, "morning-pluck": morning_pluck,
          "ambient-glow": ambient_glow}


def write(name: str, audio: np.ndarray) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "track.wav"
        with wave.open(str(raw), "wb") as w:
            w.setnchannels(2), w.setsampwidth(2), w.setframerate(SR)
            w.writeframes((audio.T * 32767).astype("<i2").tobytes())
        target = OUT / f"{name}.m4a"
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(raw),
                        "-af", "loudnorm=I=-18:TP=-2:LRA=11", "-ar", str(SR), "-c:a", "aac", "-b:a", "96k",
                        str(target)], check=True)
    return target


if __name__ == "__main__":
    for name in sys.argv[1:] or TRACKS:
        print(write(name, TRACKS[name]().mix()))
