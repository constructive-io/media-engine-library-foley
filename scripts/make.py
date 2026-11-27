#!/usr/bin/env python3
"""Make every sound in this library. Nothing here is a recording.

    python3 scripts/make.py                 # make all of sounds/
    python3 scripts/make.py RAIN THUNDER    # make some
    python3 scripts/make.py --list          # what there is
    python3 scripts/make.py --verify        # measure what is in sounds/: levels, loop seams, size
    python3 scripts/make.py --hash          # a hash of each file's samples, to compare two builds

Each sound is a function below that adds sines and shaped noise together.
The random numbers come from a seed made of the sound's name, so the same
script makes the same samples every time.

It needs numpy, and the `ffmpeg` program to write FLAC. Where the system
Python has no numpy:

    uv run --no-project --with numpy --python 3.12 python scripts/make.py

Loops are made to be periodic: noise is shaped in the frequency domain over
exactly the loop's length, and anything placed near the end runs round into
the start. The last sample is followed by the first with nothing to hear.
"""

import argparse
import hashlib
import math
import re
import subprocess
import sys
import zlib
from pathlib import Path

try:
    import numpy as np
except ImportError:
    sys.exit("make.py needs numpy. Run it as:\n"
             "  uv run --no-project --with numpy --python 3.12 python scripts/make.py")

RATE = 48_000
HERE = Path(__file__).resolve().parent.parent
SOUNDS = HERE / "sounds"
BANKS = HERE / "banks"

#: What each kind of sound is brought to, and how long it may be, in seconds.
KINDS = {
    "ambience": {"measure": "lufs", "target": -26.0, "loop": True, "least": 10.0, "most": 12.0},
    "bed":      {"measure": "lufs", "target": -22.0, "loop": True, "least": 12.0, "most": 16.0},
    "one-shot": {"measure": "peak", "target": -3.0, "loop": False, "least": 0.2, "most": 4.0},
    "stinger":  {"measure": "peak", "target": -3.0, "loop": False, "least": 2.0, "most": 4.0},
}
#: No sample of any file goes above this, in dBFS.
CEILING = -1.0
#: The whole of sounds/ stays under this many bytes.
MOST_BYTES = 20_000_000

MADE = {}


class Sound:
    def __init__(self, name, kind, seconds, what, make):
        self.name, self.kind, self.seconds, self.what, self.make = name, kind, seconds, what, make

    @property
    def file(self):
        return SOUNDS / f"{self.name.lower()}.flac"

    @property
    def loop(self):
        return KINDS[self.kind]["loop"]


def sound(name, kind, seconds, what):
    """Register the function below as the maker of a sound."""
    def register(make):
        MADE[name] = Sound(name, kind, seconds, what, make)
        return make
    return register


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def seeded(name):
    """Random numbers that are the same every time for the same name."""
    return np.random.default_rng([zlib.crc32(name.encode("utf-8")), 2026])


def count(seconds):
    return int(round(seconds * RATE))


def times(n):
    return np.arange(n) / RATE


def quick(n):
    """The next length at or past n that a Fourier transform is fast for."""
    while True:
        rest = n
        for factor in (2, 3, 5):
            while rest % factor == 0:
                rest //= factor
        if rest == 1:
            return n
        n += 1


def lowpass(f, cut, order=2):
    return 1.0 / np.sqrt(1.0 + (f / cut) ** (2 * order))


def highpass(f, cut, order=2):
    return 1.0 / np.sqrt(1.0 + (cut / np.maximum(f, 1e-6)) ** (2 * order))


def band(f, low, high, order=2):
    return highpass(f, low, order) * lowpass(f, high, order)


def resonance(f, centre, q):
    """One peak at `centre`, narrower as q rises."""
    x = np.maximum(f, 1e-6) / centre
    return 1.0 / np.sqrt(1.0 + (q * (x - 1.0 / x)) ** 2)


def noise(n, rng, shape=None, channels=1):
    """Noise with a chosen spectrum, at an RMS of 1. It is periodic over n samples."""
    f = np.fft.rfftfreq(n, 1.0 / RATE)
    curve = None if shape is None else shape(f)
    made = []
    for _ in range(channels):
        spectrum = np.fft.rfft(rng.standard_normal(n))
        if curve is not None:
            spectrum = spectrum * curve
        spectrum[0] = 0.0
        one = np.fft.irfft(spectrum, n)
        made.append(one / np.sqrt(np.mean(one * one)))
    return made[0] if channels == 1 else np.stack(made, axis=1)


def drift(n, rng, rate, low=0.0, high=1.0):
    """A slow random curve between low and high that ends where it begins."""
    f = np.fft.rfftfreq(n, 1.0 / RATE)
    spectrum = np.fft.rfft(rng.standard_normal(n)) * np.exp(-(f / rate) ** 2)
    spectrum[0] = 0.0
    curve = np.fft.irfft(spectrum, n)
    curve = (curve - curve.min()) / (curve.max() - curve.min())
    return low + (high - low) * curve


def swept(n, rng, centre, low=100.0, high=8000.0, q=6.0, width=0.5, channels=2):
    """Noise through a resonance that moves. `centre` is its place in Hz at every sample.

    A third-octave row of fixed bands, each one heard only while the centre is near it.
    """
    where = np.log2(centre)
    out = np.zeros((n, channels))
    for step in range(int(math.ceil(math.log2(high / low) * 3)) + 1):
        middle = low * 2.0 ** (step / 3.0)
        weight = np.exp(-0.5 * ((where - math.log2(middle)) / width) ** 2)
        one = noise(n, rng, lambda f: resonance(f, middle, q), channels)
        out += (one if channels > 1 else one[:, None]) * weight[:, None]
    return out


def level(samples, decibels):
    """The same sound at an RMS of `decibels`."""
    rms = math.sqrt(float(np.mean(np.square(samples))))
    return samples * (10.0 ** (decibels / 20.0) / rms) if rms > 0 else samples


def pan(mono, where):
    """One channel placed between left (-1) and right (1), at even power."""
    angle = (np.asarray(where) + 1.0) * np.pi / 4.0
    return np.stack([mono * np.cos(angle), mono * np.sin(angle)], axis=1)


def put(buffer, event, at, gain=1.0, wrap=False):
    """Add `event` to `buffer` from sample `at`. With wrap, what passes the end goes on from the start."""
    n, m, at = len(buffer), len(event), int(at)
    if not wrap:
        first, last = max(at, 0), min(n, at + m)
        if last > first:
            buffer[first:last] += gain * event[first - at:last - at]
        return
    at %= n
    done = 0
    while done < m:
        take = min(m - done, n - at)
        buffer[at:at + take] += gain * event[done:done + take]
        done, at = done + take, 0


def convolve(samples, kernel, wrap):
    """Samples through a kernel. With wrap, round the end into the start; without, cut at the end."""
    n = len(samples)
    size = n if wrap else quick(n + len(kernel))
    if samples.ndim == 2 and kernel.ndim == 1:
        kernel = kernel[:, None]
    out = np.fft.irfft(np.fft.rfft(samples, size, axis=0) * np.fft.rfft(kernel, size, axis=0), size, axis=0)
    return out[:n]


def reverb(samples, rng, seconds, mix, wrap, bright=5000.0):
    """A room made of fading noise, added to the sound. One channel stays one channel."""
    m = count(seconds)
    channels = 1 if samples.ndim == 1 else 2
    echo = noise(m, rng, lambda f: band(f, 200.0, bright, 1), channels)
    fall = np.exp(-6.91 * times(m) / seconds)
    start = count(0.012)
    fall[:start] *= np.linspace(0.0, 1.0, start) ** 2
    echo = echo * (fall if channels == 1 else fall[:, None])
    echo = echo / np.sqrt(np.sum(echo * echo, axis=0))
    return samples + mix * convolve(samples, echo, wrap)


def fade(samples, start=0.002, end=0.02):
    """Ease the first and last moments to nothing."""
    out = np.array(samples, dtype=float)
    for length, edge in ((count(start), 1), (count(end), -1)):
        if length <= 0:
            continue
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(length) / length)
        if edge < 0:
            ramp = ramp[::-1]
        part = slice(0, length) if edge > 0 else slice(len(out) - length, len(out))
        out[part] = out[part] * (ramp if out.ndim == 1 else ramp[:, None])
    return out


def ring(pitch, tau, seconds=None, attack=0.0):
    """A sine that dies away: one mode of something struck."""
    n = count(seconds if seconds else tau * 9.0)
    t = times(n)
    out = np.sin(2 * np.pi * pitch * t) * np.exp(-t / tau)
    return fade(out, attack, min(0.004, n / RATE / 4))


def burst(rng, seconds, shape, tau, attack=0.0003):
    """A moment of shaped noise that dies away."""
    n = count(seconds)
    made = noise(max(quick(n), 8192), rng, shape)[:n] * np.exp(-times(n) / tau)
    return fade(made, attack, min(0.004, seconds / 4))


def glide(start, end, seconds, bend=1.0, vibrato=(0.0, 0.0)):
    """A whistled note that slides from one pitch to another, with its edges eased."""
    n = count(seconds)
    u = np.arange(n) / n
    pitch = start * (end / start) ** (u ** bend)
    pitch = pitch * (1.0 + vibrato[1] * np.sin(2 * np.pi * vibrato[0] * times(n)))
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    shape = np.sin(np.pi * u) ** 0.7
    return shape * (np.sin(phase) + 0.18 * np.sin(2 * phase) + 0.05 * np.sin(3 * phase))


def whole(pitch, seconds):
    """The nearest pitch that fits a whole number of cycles into the loop."""
    return round(pitch * seconds) / seconds


_STEPS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def hz(note):
    """`A4` is 440 Hz. A sharp is `#`, a flat is `b`."""
    letter, mark, octave = re.fullmatch(r"([A-G])([#b]?)(-?\d)", note).groups()
    step = _STEPS[letter] + {"#": 1, "b": -1, "": 0}[mark] + 12 * (int(octave) + 1)
    return 440.0 * 2.0 ** ((step - 69) / 12.0)


def pad(pitch, seconds, rng, attack=1.0, release=1.5, bright=1.7, top=5000.0, spread=0.6):
    """A soft held note: three voices a little out of tune, left, middle and right."""
    n = count(seconds + release)
    t = times(n)
    out = np.zeros((n, 2))
    for cents, where in ((-7.0, -spread), (0.0, 0.0), (6.0, spread)):
        f = pitch * 2.0 ** (cents / 1200.0)
        voice, k = np.zeros(n), 1
        while k * f < top and k <= 12:
            voice += np.sin(2 * np.pi * k * f * t + rng.uniform(0, 2 * np.pi)) / k ** bright
            k += 1
        out += pan(voice, where)
    return fade(out, attack, release) / 3.0


def pluck(pitch, tau, bright=1.0, top=9000.0, seconds=None):
    """A plucked note: every overtone dies faster than the one below it."""
    n = count(seconds if seconds else min(tau * 7.0, 6.0))
    t = times(n)
    out, k = np.zeros(n), 1
    while k * pitch < top and k <= 16:
        out += np.sin(2 * np.pi * k * pitch * t) / k ** bright * np.exp(-t * (1.0 + 0.5 * (k - 1)) / tau)
        k += 1
    return fade(out, 0.003, 0.02)


def bass(pitch, tau):
    n = count(tau * 6.0)
    t = times(n)
    out = np.sin(2 * np.pi * pitch * t) + 0.35 * np.sin(4 * np.pi * pitch * t) + 0.12 * np.sin(6 * np.pi * pitch * t)
    return fade(out * np.exp(-t / tau), 0.006, 0.02)


def thump(low=46.0, drop=80.0, bend=0.028, tau=0.11, seconds=0.5):
    """A soft drum: a sine that falls in pitch as it dies."""
    n = count(seconds)
    t = times(n)
    phase = 2 * np.pi * (low * t + drop * bend * (1.0 - np.exp(-t / bend)))
    return fade(np.sin(phase) * np.exp(-t / tau), 0.002, 0.03)


# ---------------------------------------------------------------------------
# Loudness, as ITU-R BS.1770 measures it
# ---------------------------------------------------------------------------

def weighted(samples):
    """The K weighting of BS.1770, applied in the frequency domain."""
    n = len(samples)
    z = np.exp(-2j * np.pi * np.fft.rfftfreq(n))
    shelf = (1.53512485958697 - 2.69169618940638 * z + 1.19839281085285 * z * z) / (
        1.0 - 1.69065929318241 * z + 0.73248077421585 * z * z)
    high = (1.0 - 2.0 * z + z * z) / (1.0 - 1.99004745483398 * z + 0.99007225036621 * z * z)
    return np.fft.irfft(np.fft.rfft(samples, axis=0) * (shelf * high)[:, None], n, axis=0)


def lufs(samples):
    """Integrated loudness of (n, channels) samples, gated as the standard says."""
    heard = weighted(samples)
    size, hop = count(0.4), count(0.1)
    if len(heard) < size:
        return float("-inf")
    total = np.concatenate([[0.0], np.cumsum(np.sum(heard * heard, axis=1))])
    starts = np.arange(0, len(heard) - size + 1, hop)
    power = (total[starts + size] - total[starts]) / size
    loud = -0.691 + 10 * np.log10(np.maximum(power, 1e-12))
    kept = power[loud > -70.0]
    if not len(kept):
        return float("-inf")
    gate = -0.691 + 10 * np.log10(np.mean(kept)) - 10.0
    kept = power[(loud > -70.0) & (loud > gate)]
    return -0.691 + 10 * np.log10(np.mean(kept))


def decibels(value):
    return 20 * math.log10(max(float(value), 1e-9))


# ---------------------------------------------------------------------------
# Ambience: loops that end where they begin
# ---------------------------------------------------------------------------

@sound("RAIN", "ambience", 10, "Steady rain, with nearer drops in it")
def rain(rng, n):
    wash = noise(n, rng, lambda f: band(f, 700.0, 8000.0, 1) * (1.0 + resonance(f, 4000.0, 0.8))
                 * lowpass(f, 12000.0, 3), 2)
    roof = noise(n, rng, lambda f: band(f, 90.0, 500.0, 2), 2)
    drops = np.zeros((n, 2))
    for pitch, each_second in ((1300, 25), (1900, 45), (2700, 70), (3800, 90), (5200, 90), (7000, 60)):
        number = each_second * n // RATE
        at = rng.integers(0, n, number)
        size = rng.lognormal(0.0, 0.7, number)
        side = (rng.uniform(-1.0, 1.0, number) + 1.0) * np.pi / 4.0
        t = times(count(0.012))
        drop = np.sin(2 * np.pi * pitch * t) * np.exp(-t / (3.0 / pitch + 0.0004))
        for channel, share in ((0, np.cos(side)), (1, np.sin(side))):
            train = np.zeros(n)
            np.add.at(train, at, size * share)
            drops[:, channel] += convolve(train, drop, wrap=True)
    swell = drift(n, rng, 0.3, 0.88, 1.0)
    return (level(wash, 0.0) + level(drops, -4.0) + level(roof, -9.0)) * swell[:, None]


@sound("WIND", "ambience", 12, "Wind that rises and falls in gusts")
def wind(rng, n):
    gust = np.clip(0.8 * drift(n, rng, 0.35) + 0.25 * drift(n, rng, 0.9), 0.0, 1.0)
    howl = swept(n, rng, 220.0 * 2.0 ** (2.4 * gust), low=110.0, high=3600.0, q=6.0, width=0.45)
    howl = howl * (0.25 + gust ** 1.5)[:, None]
    rumble = noise(n, rng, lambda f: band(f, 30.0, 180.0, 2), 2) * (0.5 + 0.5 * gust)[:, None]
    air = noise(n, rng, lambda f: band(f, 1500.0, 7000.0, 1) * lowpass(f, 10000.0, 3), 2) * (0.1 + gust ** 2)[:, None]
    return level(howl, 0.0) + level(rumble, -6.0) + level(air, -16.0)


@sound("SEA", "ambience", 12, "Waves that break on a shore and hiss back down it")
def sea(rng, n):
    seconds = n / RATE
    t = times(n)
    low = noise(n, rng, lambda f: band(f, 50.0, 350.0, 2), 2)
    middle = noise(n, rng, lambda f: band(f, 300.0, 1800.0, 2), 2)
    high = noise(n, rng, lambda f: band(f, 1800.0, 9000.0, 1) * lowpass(f, 12000.0, 3), 2)
    far = noise(n, rng, lambda f: band(f, 40.0, 600.0, 1) * lowpass(f, 3000.0, 2), 2)

    def swell(at, rise, fall):
        since = (t - at + seconds / 2.0) % seconds - seconds / 2.0
        return np.where(since < 0, np.exp(since / rise), np.exp(-since / fall))

    out = level(far, -17.0)
    for at, size, side in ((1.2, 1.0, -0.2), (5.4, 0.7, 0.25), (9.0, 0.88, 0.0)):
        wave = (low * swell(at, 0.9, 1.0)[:, None] * 1.0
                + middle * swell(at + 0.15, 0.7, 1.4)[:, None] * 0.7
                + high * swell(at + 0.5, 0.5, 2.3)[:, None] * 0.45)
        out = out + size * wave * np.array([1.0 - side, 1.0 + side])
    return out


@sound("FIRE", "ambience", 10, "A wood fire: a low roar, a hiss, and crackles")
def fire(rng, n):
    roar = noise(n, rng, lambda f: band(f, 40.0, 260.0, 2), 2) * drift(n, rng, 2.5, 0.55, 1.0)[:, None]
    hiss = noise(n, rng, lambda f: band(f, 1500.0, 9000.0, 1), 2) * (drift(n, rng, 6.0, 0.15, 1.0) ** 2)[:, None]
    pools = {centre: noise(n, rng, lambda f: band(f, centre / 1.5, centre * 1.5, 2))
             for centre in (500.0, 1100.0, 2200.0, 4000.0, 6500.0)}
    centres = list(pools)
    crackle = np.zeros((n, 2))
    at = 0
    for _ in range(16 * n // RATE):
        # Crackles come in runs: most land near the one before.
        at = (at + int(rng.uniform(0.004, 0.09) * RATE)) % n if rng.random() < 0.45 else int(rng.integers(0, n))
        centre = centres[int(rng.choice(len(centres), p=(0.1, 0.22, 0.3, 0.25, 0.13)))]
        length = count(rng.uniform(0.002, 0.014))
        piece = np.take(pools[centre], int(rng.integers(0, n)) + np.arange(length), mode="wrap")
        snap = fade(piece * np.exp(-times(length) / (length / RATE / 4.0)), 0.0002, 0.001)
        put(crackle, pan(snap, rng.uniform(-0.7, 0.7)), at, min(rng.lognormal(0.0, 0.5), 2.2), wrap=True)
    return level(roar, 0.0) + level(hiss, -11.0) + level(crackle, -10.0)


@sound("ROOM-TONE", "ambience", 10, "The quiet of an empty room, with air moving somewhere")
def room_tone(rng, n):
    tone = noise(n, rng, lambda f: highpass(f, 35.0, 2) / (1.0 + f / 160.0) * lowpass(f, 3500.0, 3), 2)
    vent = noise(n, rng, lambda f: resonance(f, 190.0, 3.0) + 0.3 * resonance(f, 900.0, 1.5), 2)
    return (level(tone, 0.0) + level(vent, -12.0)) * drift(n, rng, 0.2, 0.93, 1.0)[:, None]


@sound("NIGHT", "ambience", 12, "Crickets at night, near and far")
def night(rng, n):
    seconds = n / RATE
    out = np.zeros((n, 2))

    def chirp(pitch, pulses):
        length, gap = count(0.011), count(0.017)
        made = np.zeros(pulses * gap + length)
        t = times(length)
        for pulse in range(pulses):
            f = pitch * (1.0 + 0.008 * pulse)
            one = np.sin(np.pi * t / t[-1]) ** 2 * (np.sin(2 * np.pi * f * t) + 0.1 * np.sin(4 * np.pi * f * t))
            made[pulse * gap:pulse * gap + length] += one * (0.6 + 0.4 * pulse / max(pulses - 1, 1))
        return made

    for pitch, each_loop, pulses, side, near in ((4500, 27, 3, -0.6, 1.0), (4850, 23, 4, 0.5, 0.8),
                                                 (4150, 30, 3, 0.15, 0.55), (5150, 25, 3, -0.2, 0.4),
                                                 (4300, 21, 4, 0.8, 0.3), (4700, 29, 3, -0.85, 0.25)):
        period = n / each_loop
        rests = rng.integers(0, each_loop)          # each cricket stops for a while, then goes on
        for number in range(each_loop):
            if (number - rests) % each_loop < each_loop // 5:
                continue
            at = int(number * period + rng.normal(0.0, 0.03) * period)
            put(out, pan(chirp(pitch * rng.normal(1.0, 0.003), pulses), side), at, near * rng.uniform(0.85, 1.0),
                wrap=True)

    t = times(n)
    far = np.zeros((n, 2))
    for channel, pitch, beat in ((0, 6200.0, 43.0), (1, 6850.0, 38.0), (0, 7400.0, 51.0), (1, 5900.0, 47.0)):
        ticks = (0.5 - 0.5 * np.cos(2 * np.pi * whole(beat, seconds) * t)) ** 3
        far[:, channel] += noise(n, rng, lambda f: resonance(f, pitch, 45.0)) * ticks * drift(n, rng, 0.4, 0.5, 1.0)
    air = noise(n, rng, lambda f: band(f, 100.0, 1200.0, 1) * lowpass(f, 4000.0, 3), 2)
    return level(out, 0.0) + level(far, -15.0) + level(air, -20.0)


@sound("BIRDS", "ambience", 12, "Small birds calling in the open, some near, some far")
def birds(rng, n):
    def tsip():
        made = np.zeros(count(0.6))
        for number in range(int(rng.integers(3, 6))):
            put(made, glide(rng.uniform(5000, 5600), rng.uniform(3400, 3900), 0.055, 0.8), count(0.095 * number))
        return made

    def whistle():
        made = np.zeros(count(0.7))
        first = rng.uniform(2300, 2700)
        put(made, glide(first, first * 1.28, 0.22, 1.4), 0)
        put(made, glide(first * 1.26, first * 1.04, 0.32, 0.7, vibrato=(28.0, 0.012)), count(0.27), 0.9)
        return made

    def warble():
        made = np.zeros(count(1.1))
        at, last = 0.0, rng.uniform(3000, 4400)
        for _ in range(int(rng.integers(6, 11))):
            to = rng.uniform(2800, 4800)
            length = rng.uniform(0.06, 0.11)
            put(made, glide(last, to, length, rng.uniform(0.6, 1.6)), count(at), rng.uniform(0.7, 1.0))
            at, last = at + length * 0.92, to
        return made

    def trill():
        made = np.zeros(count(1.0))
        base = rng.uniform(3600, 4200)
        for number in range(int(rng.integers(9, 14))):
            put(made, glide(base, base * 1.22, 0.036, 1.0), count(number / 14.0), 0.6 + 0.4 * math.sin(number / 4.0))
        return made

    calls = np.zeros((n, 2))
    order = [tsip, whistle, warble, trill, tsip, warble, whistle, tsip, trill, warble, whistle, tsip, warble, tsip,
             trill, whistle]
    for number, call in enumerate(order):
        at = (number + rng.uniform(0.1, 0.9)) * n / len(order)
        near = rng.choice((1.0, 0.7, 0.45, 0.3, 0.2))
        put(calls, pan(call(), rng.uniform(-0.8, 0.8)), at, near, wrap=True)
    calls = reverb(calls, rng, 1.1, 0.35, wrap=True, bright=7000.0)
    leaves = noise(n, rng, lambda f: band(f, 250.0, 5000.0, 1) * lowpass(f, 8000.0, 3), 2) * drift(n, rng, 0.5, 0.6, 1.0)[:, None]
    return level(calls, 0.0) + level(leaves, -14.0)


@sound("HUM", "ambience", 10, "A low machine hum, steady, with a slow beat in it")
def hum(rng, n):
    seconds = n / RATE
    t = times(n)
    out = np.zeros((n, 2))
    for pitch, size in ((50.0, 1.0), (100.0, 0.7), (100.3, 0.3), (150.0, 0.38), (150.2, 0.15), (200.0, 0.24),
                        (250.0, 0.13), (300.0, 0.1), (400.0, 0.05), (500.0, 0.03)):
        phase = rng.uniform(0, 2 * np.pi)
        for channel in (0, 1):           # the two sides nearly in step, so that their sum is still the hum
            out[:, channel] += size * np.sin(2 * np.pi * whole(pitch, seconds) * t + phase + rng.uniform(-0.5, 0.5))
    motor = noise(n, rng, lambda f: band(f, 80.0, 600.0, 1) * lowpass(f, 3000.0, 3), 2)
    whirr = noise(n, rng, lambda f: resonance(f, 1150.0, 20.0), 2) * drift(n, rng, 0.6, 0.7, 1.0)[:, None]
    return level(out, 0.0) + level(motor, -13.0) + level(whirr, -24.0)


@sound("MURMUR", "ambience", 12, "A room of people too far off to follow: bursts of shaped noise, no speech")
def murmur(rng, n):
    seconds = n / RATE
    f = np.fft.rfftfreq(n, 1.0 / RATE)
    crowd = np.zeros((n, 2))
    for _ in range(22):
        source = np.fft.rfft(rng.standard_normal(n))
        chest = rng.uniform(110.0, 240.0)
        mouths = []
        for _ in range(3):
            first, second = rng.uniform(350.0, 800.0), rng.uniform(1000.0, 2300.0)
            shape = (0.9 * resonance(f, chest, 4.0) + resonance(f, first, 5.0) + 0.6 * resonance(f, second, 7.0)
                     + 0.12 * resonance(f, 3000.0, 4.0)) * lowpass(f, 4000.0, 2)
            one = np.fft.irfft(source * shape, n)
            mouths.append(one / np.sqrt(np.mean(one * one)))
        voice = np.zeros(n)
        at, end = rng.uniform(0.0, seconds), 0.0
        start = at
        while at - start < seconds - 2.2:
            for _ in range(int(rng.integers(3, 10))):          # a run of bursts, then a rest
                length = rng.uniform(0.09, 0.26)
                shape = fade(np.ones(count(length)), 0.03, min(0.07, length / 2))
                piece = np.take(mouths[int(rng.integers(0, 3))], count(at) + np.arange(len(shape)), mode="wrap")
                put(voice, piece * shape, count(at), rng.uniform(0.5, 1.0), wrap=True)
                at += length + rng.uniform(0.02, 0.08)
                if at - start >= seconds - 2.2:
                    break
            at += rng.uniform(0.4, 1.8)
        crowd += pan(voice, rng.uniform(-0.9, 0.9)) * rng.uniform(0.3, 1.0)
    crowd = reverb(crowd, rng, 0.9, 0.6, wrap=True, bright=3500.0)

    clinks = np.zeros((n, 2))
    for at in sorted(rng.uniform(0.0, seconds, 5)):
        pitch = rng.uniform(2300.0, 4600.0)
        clink = burst(rng, 0.3, lambda g: resonance(g, pitch, 60.0), 0.05)
        put(clinks, pan(clink, rng.uniform(-0.7, 0.7)), count(at), rng.uniform(0.4, 1.0), wrap=True)
    clinks = reverb(clinks, rng, 0.9, 0.5, wrap=True)
    floor = noise(n, rng, lambda g: band(g, 60.0, 400.0, 1) * lowpass(g, 2500.0, 3), 2)
    return level(crowd, 0.0) + level(clinks, -24.0) + level(floor, -16.0)


# ---------------------------------------------------------------------------
# One-shots
# ---------------------------------------------------------------------------

@sound("THUNDER", "one-shot", 4.0, "A crack of thunder and the rumble after it")
def thunder(rng, n):
    t = times(n)
    low = noise(n, rng, lambda f: band(f, 25.0, 120.0, 2), 2)
    middle = noise(n, rng, lambda f: band(f, 100.0, 600.0, 2), 2)
    high = noise(n, rng, lambda f: band(f, 500.0, 3500.0, 1), 2)

    def roll(strikes, rise, tau):
        shape = np.zeros(n)
        for at, size in strikes:
            since = np.maximum(t - at, 0.0)
            shape += size * (1.0 - np.exp(-since / rise)) * np.exp(-since / tau) * (t >= at)
        return shape[:, None]

    strikes = ((0.04, 1.0), (0.3, 0.55), (0.72, 0.75), (1.35, 0.45), (2.0, 0.3))
    out = (low * roll(strikes, 0.05, 0.95) + 0.8 * middle * roll(strikes, 0.02, 0.5)
           + 0.9 * high * roll((strikes[0], strikes[2]), 0.002, 0.11))
    return fade(out, 0.002, 0.9)


@sound("KNOCK", "one-shot", 0.7, "Three knocks on a wooden door")
def knock(rng, n):
    out = np.zeros(n)
    for at, size in ((0.02, 1.0), (0.2, 0.85), (0.375, 0.95)):
        bend = rng.normal(1.0, 0.015)
        for pitch, tau, share in ((165, 0.035, 1.0), (247, 0.028, 0.7), (410, 0.018, 0.5), (720, 0.01, 0.3),
                                  (1150, 0.006, 0.2)):
            put(out, ring(pitch * bend, tau), count(at), size * share)
        put(out, burst(rng, 0.03, lambda f: lowpass(f, 2500.0, 2), 0.004), count(at), size * 0.5)
    return out


@sound("DOOR", "one-shot", 0.9, "A door pushed shut: the latch, then the thud")
def door(rng, n):
    out = np.zeros(n)

    def latch(at, size, pitches):
        for pitch, tau in pitches:
            put(out, ring(pitch, tau), count(at), size)
        put(out, burst(rng, 0.02, lambda f: highpass(f, 2000.0, 2), 0.003), count(at), size)

    latch(0.03, 0.22, ((2800, 0.004), (4300, 0.003)))
    for pitch, tau, share in ((82, 0.09, 1.0), (138, 0.06, 0.6), (230, 0.035, 0.35)):
        put(out, ring(pitch, tau, attack=0.003), count(0.09), share)
    put(out, burst(rng, 0.25, lambda f: lowpass(f, 900.0, 2), 0.03, 0.001), count(0.09), 0.6)
    put(out, burst(rng, 0.5, lambda f: band(f, 300.0, 1200.0, 2), 0.08, 0.002), count(0.09), 0.2)
    latch(0.125, 0.3, ((1900, 0.006), (3300, 0.004)))
    return reverb(pan(out, 0.0), rng, 0.5, 0.35, wrap=False, bright=3500.0)


@sound("BELL", "one-shot", 4.0, "One stroke of a large bell, left to ring")
def bell(rng, n):
    # The partials of a bell are not whole multiples of one pitch. This is a long-known additive
    # recipe for one: (ratio to the strike pitch, shift in Hz, size, share of the length).
    partials = ((0.56, 0.0, 1.0, 1.0), (0.56, 1.0, 0.67, 0.9), (0.92, 0.0, 1.0, 0.65), (0.92, 1.7, 1.8, 0.55),
                (1.19, 0.0, 2.67, 0.325), (1.7, 0.0, 1.67, 0.35), (2.0, 0.0, 1.46, 0.25), (2.74, 0.0, 1.33, 0.2),
                (3.0, 0.0, 1.33, 0.15), (3.76, 0.0, 1.0, 0.1), (4.07, 0.0, 1.33, 0.075))
    t = times(n)
    out = np.zeros(n)
    for ratio, shift, size, share in partials:
        out += size * np.sin(2 * np.pi * (392.0 * ratio + shift) * t) * np.exp(-6.91 * t / (4.6 * share))
    put(out, burst(rng, 0.03, lambda f: band(f, 1500.0, 7000.0, 1), 0.004), 0, 2.0)
    return fade(out, 0.001, 0.25)


@sound("CHIME", "one-shot", 2.8, "Four metal chimes struck one after another")
def chime(rng, n):
    out = np.zeros((n, 2))
    for at, note, side, size in ((0.02, "A5", -0.4, 1.0), (0.17, "E6", 0.3, 0.8), (0.41, "C#6", -0.1, 0.9),
                                 (0.58, "F#6", 0.5, 0.7)):
        pitch = hz(note)
        # A tube rings at these multiples of its lowest pitch.
        tube = sum(share * ring(pitch * ratio, tau, 2.2) for ratio, tau, share in
                   ((1.0, 0.42, 1.0), (2.756, 0.2, 0.45), (5.404, 0.09, 0.25), (8.933, 0.04, 0.12)))
        put(out, pan(tube, side), count(at), size)
    return fade(out, 0.001, 0.2)


@sound("CLICK", "one-shot", 0.2, "A button pressed")
def click(rng, n):
    out = np.zeros(n)
    put(out, burst(rng, 0.02, lambda f: band(f, 1500.0, 7000.0, 2), 0.0012, 0.0001), count(0.005))
    put(out, ring(1900, 0.004), count(0.005), 0.6)
    put(out, ring(3600, 0.0025), count(0.005), 0.3)
    return out


@sound("TICK", "one-shot", 0.25, "One tick of a clock")
def tick(rng, n):
    out = np.zeros(n)
    for at, size in ((0.005, 1.0), (0.027, 0.35)):
        put(out, ring(4200, 0.0035), count(at), size)
        put(out, ring(6100, 0.002), count(at), size * 0.5)
        put(out, ring(2300, 0.006), count(at), size * 0.4)
        put(out, burst(rng, 0.01, lambda f: highpass(f, 3000.0, 2), 0.0008, 0.0001), count(at), size * 0.5)
    put(out, ring(620, 0.02), count(0.005), 0.15)
    return out


@sound("WHOOSH", "one-shot", 1.0, "Something passing quickly, left to right")
def whoosh(rng, n):
    u = np.arange(n) / n
    near = np.exp(-((u - 0.55) / 0.22) ** 2)
    body = swept(n, rng, 300.0 * 2.0 ** (3.3 * near), low=200.0, high=6400.0, q=5.0, width=0.5, channels=1)[:, 0]
    air = noise(n, rng, lambda f: band(f, 3000.0, 9000.0, 1))
    mono = level(body, 0.0) * near ** 1.2 + level(air, -12.0) * near ** 2
    return fade(pan(mono, -0.7 + 1.4 * u), 0.02, 0.05)


@sound("POP", "one-shot", 0.25, "A soft pop, like a cork or a bubble")
def pop(rng, n):
    length = count(0.2)
    t = times(length)
    pitch = 520.0 + 580.0 * np.exp(-t / 0.006)
    tone = np.sin(2 * np.pi * np.cumsum(pitch) / RATE) * np.exp(-t / 0.022)
    out = np.zeros(n)
    put(out, fade(tone, 0.0005, 0.01), count(0.005))
    put(out, burst(rng, 0.01, lambda f: band(f, 800.0, 5000.0, 1), 0.0015, 0.0001), count(0.005), 0.35)
    return out


@sound("BEEP", "one-shot", 0.35, "A plain short tone")
def beep(rng, n):
    length = count(0.27)
    t = times(length)
    tone = np.sin(2 * np.pi * 1000 * t) + 0.08 * np.sin(2 * np.pi * 2000 * t) + 0.03 * np.sin(2 * np.pi * 3000 * t)
    out = np.zeros(n)
    put(out, fade(tone, 0.006, 0.012), count(0.01))
    return out


@sound("DING", "one-shot", 2.0, "A small desk bell, struck once")
def ding(rng, n):
    out = np.zeros(n)
    for pitch, tau, share in ((1568.0, 0.42, 1.0), (1571.5, 0.38, 0.5), (1568.0 * 2.76, 0.16, 0.45),
                              (1568.0 * 5.4, 0.06, 0.2)):
        put(out, ring(pitch, tau, 1.95), count(0.005), share)
    put(out, burst(rng, 0.02, lambda f: band(f, 2000.0, 9000.0, 1), 0.002), count(0.005), 0.4)
    return fade(out, 0.0, 0.15)


@sound("ERROR", "one-shot", 0.6, "Two low buzzes, the second lower: something went wrong")
def error(rng, n):
    out = np.zeros(n)
    for at, pitch, seconds in ((0.01, 233.1, 0.16), (0.23, 174.6, 0.26)):
        t = times(count(seconds))
        tone = sum(np.sin(2 * np.pi * pitch * k * t) / k * float(lowpass(np.array(pitch * k), 2500.0, 2))
                   for k in range(1, 40, 2) if pitch * k < 8000)
        put(out, fade(tone, 0.005, 0.012), count(at))
    return out


def _bar(pitch, tau, seconds):
    """A soft struck bar: nearly a sine, with a little above it."""
    return (ring(pitch, tau, seconds, 0.004) + 0.25 * ring(2 * pitch, tau * 0.6, seconds, 0.004)
            + 0.08 * ring(3 * pitch, tau * 0.4, seconds, 0.004))


@sound("SUCCESS", "one-shot", 1.0, "Three quick rising notes: it worked")
def success(rng, n):
    out = np.zeros(n)
    for at, note, tau in ((0.01, "C6", 0.09), (0.11, "E6", 0.09), (0.21, "G6", 0.2)):
        put(out, _bar(hz(note), tau, min(tau * 8, 0.78)), count(at))
    return fade(out, 0.0, 0.05)


@sound("NOTIFICATION", "one-shot", 1.2, "Two soft notes, the second higher: something arrived")
def notification(rng, n):
    out = np.zeros(n)
    for at, note, tau, size in ((0.01, "E5", 0.16, 0.9), (0.17, "A5", 0.26, 1.0)):
        pitch = hz(note)
        t = times(count(min(tau * 7, 1.0)))
        # The second sine bends the first for a moment, which gives the note a soft knock at its start.
        tone = np.sin(2 * np.pi * pitch * t + 1.4 * np.exp(-t / 0.05) * np.sin(4 * np.pi * pitch * t))
        put(out, fade(tone * np.exp(-t / tau), 0.004, 0.02), count(at), size)
    return fade(out, 0.0, 0.05)


@sound("HEARTBEAT", "one-shot", 1.8, "Two beats of a heart")
def heartbeat(rng, n):
    out = np.zeros(n)
    for beat in (0.03, 0.93):
        for at, low, drop, tau, size in ((0.0, 44.0, 22.0, 0.06, 1.0), (0.3, 54.0, 26.0, 0.045, 0.7)):
            one = thump(low, drop, 0.04, tau, 0.45)
            thud = burst(rng, 0.45, lambda f: band(f, 40.0, 260.0, 2), tau, 0.008)
            put(out, fade(np.tanh(1.8 * one) + 0.45 * thud, 0.008, 0.03), count(beat + at), size)
    return out


@sound("FOOTSTEPS", "one-shot", 2.4, "Four steps on a hard floor")
def footsteps(rng, n):
    out = np.zeros(n)
    for step in range(4):
        at = 0.06 + 0.56 * step + rng.normal(0.0, 0.012)
        bend = (1.0, 0.94)[step % 2] * rng.normal(1.0, 0.02)
        size = (1.0, 0.9)[step % 2] * rng.uniform(0.9, 1.0)
        put(out, ring(95 * bend, 0.03, attack=0.002), count(at), size)
        put(out, ring(170 * bend, 0.02, attack=0.002), count(at), size * 0.6)
        put(out, burst(rng, 0.08, lambda f: band(f, 200.0, 1800.0, 2), 0.012), count(at), size * 0.9)
        put(out, burst(rng, 0.15, lambda f: band(f, 400.0, 3000.0, 2), 0.025, 0.004), count(at + 0.11), size * 0.4)
        put(out, ring(130 * bend, 0.02, attack=0.003), count(at + 0.11), size * 0.25)
    return reverb(out, rng, 0.35, 0.3, wrap=False, bright=3000.0)


@sound("PAGE-TURN", "one-shot", 0.9, "A page of a book turned over")
def page_turn(rng, n):
    t = times(n)
    paper = noise(n, rng, lambda f: band(f, 1800.0, 11000.0, 1), 2)
    body = noise(n, rng, lambda f: band(f, 150.0, 600.0, 2), 2)
    crinkle = np.abs(noise(n, rng, lambda f: lowpass(f, 140.0, 2))) ** 2
    crinkle = crinkle / crinkle.max()
    swish = np.where(t < 0.4, (t / 0.4) ** 2, np.exp(-(t - 0.4) / 0.035))
    settle = 0.25 * np.exp(-np.maximum(t - 0.56, 0.0) / 0.08) * (t >= 0.56)
    out = paper * ((swish + settle) * (0.35 + 0.65 * crinkle))[:, None] + 0.25 * body * swish[:, None]
    flap = burst(rng, 0.2, lambda f: band(f, 300.0, 2500.0, 2), 0.03, 0.003)
    put(out, pan(flap, -0.2), count(0.55), 1.6)
    side = np.array([1.0, 0.0]) * (0.6 - 0.2 * t / t[-1])[:, None] + np.array([0.0, 1.0]) * (0.4 + 0.2 * t / t[-1])[:, None]
    return fade(out * side * 2.0, 0.03, 0.08)


@sound("TYPING", "one-shot", 2.6, "A few words typed on a keyboard")
def typing(rng, n):
    out = np.zeros((n, 2))
    at = 0.04
    for key in range(15):
        space = key in (5, 10)
        side = 0.0 if space else rng.uniform(-0.35, 0.35)
        size = rng.uniform(0.75, 1.0) * (1.15 if space else 1.0)
        down = (burst(rng, 0.03, lambda f: band(f, 1200.0, 6000.0, 2), 0.0025, 0.0001)
                + 0.5 * ring(rng.uniform(1800, 2600), 0.005, 0.03)
                + (1.0 if space else 0.7) * ring(260.0 if space else rng.uniform(380, 520), 0.02 if space else 0.012, 0.03))
        up = burst(rng, 0.02, lambda f: band(f, 2000.0, 8000.0, 2), 0.002, 0.0001) + 0.2 * ring(3000, 0.003, 0.02)
        put(out, pan(down, side), count(at), size)
        put(out, pan(up, side), count(at + rng.uniform(0.06, 0.1)), size * 0.3)
        at += rng.uniform(0.1, 0.19) + (0.16 if space else 0.0)
    return out


@sound("SHUTTER", "one-shot", 0.3, "A camera shutter opening and closing")
def shutter(rng, n):
    out = np.zeros(n)

    def clack(at, size, pitches, bright):
        put(out, burst(rng, 0.04, lambda f: band(f, 800.0, bright, 2), 0.004, 0.0001), count(at), size * 0.9)
        for pitch, tau, share in pitches:
            put(out, ring(pitch, tau), count(at), size * share)

    clack(0.01, 0.85, ((1400, 0.008, 0.5), (3100, 0.005, 0.4), (240, 0.015, 0.4)), 6000.0)
    length = count(0.066)
    whirr = noise(8192, rng, lambda f: band(f, 1500.0, 4500.0, 2))[:length]
    whirr = whirr * (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 220 * times(length))))
    put(out, fade(whirr, 0.003, 0.003), count(0.013), 0.1)
    clack(0.08, 1.0, ((1700, 0.007, 0.5), (3600, 0.004, 0.45), (300, 0.012, 0.4)), 8000.0)
    clack(0.108, 0.28, ((2100, 0.005, 0.5), (4200, 0.003, 0.4)), 8000.0)
    return out


@sound("RISER", "one-shot", 3.5, "A rising rush that builds and stops short")
def riser(rng, n):
    t = times(n)
    u = t / t[-1]
    rush = swept(n, rng, 180.0 * 2.0 ** (5.5 * u ** 1.3), low=150.0, high=12000.0, q=5.0, width=0.6)
    pitch = 110.0 * 2.0 ** (2.0 * u ** 1.2)
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    tone = sum(np.sin(k * phase + rng.uniform(0, 2 * np.pi)) / k for k in range(1, 9))
    shake = 1.0 - 0.4 * u * (0.5 + 0.5 * np.sin(2 * np.pi * np.cumsum(4.0 + 12.0 * u) / RATE))
    out = level(rush, 0.0) + level(pan(tone * shake, 0.0), -8.0)
    return fade(out * (0.05 + u ** 2.2)[:, None], 0.05, 0.012)


@sound("IMPACT", "one-shot", 2.8, "A deep hit with a long tail")
def impact(rng, n):
    t = times(n)
    at = count(0.01)
    sub = np.tanh(2.0 * thump(42.0, 60.0, 0.06, 0.5, 2.6))
    body = burst(rng, 1.6, lambda f: band(f, 60.0, 500.0, 2), 0.22, 0.002)
    crack = burst(rng, 0.2, lambda f: band(f, 500.0, 7000.0, 1), 0.018, 0.0003)
    dry = np.zeros(n)
    put(dry, sub, at, 1.0)
    put(dry, body, at, 0.7)
    put(dry, crack, at, 0.8)
    air = np.zeros(n)
    put(air, body, at, 0.5)
    put(air, crack, at, 0.8)
    tail = reverb(pan(air, 0.0), rng, 2.4, 1.0, wrap=False, bright=2500.0) - pan(air, 0.0)
    return fade(pan(dry, 0.0) + 0.6 * tail, 0.002, 0.4)


# ---------------------------------------------------------------------------
# Music: loops in one key each, and two stingers
#
# Every tempo here has a whole number of beats in two seconds. A player that
# overlaps the end of a loop with its start by two seconds, as Constructive
# Media does on its music track, stays on the beat.
# ---------------------------------------------------------------------------

@sound("CALM", "bed", 16, "Slow soft chords in C major with a few plucked notes over them")
def calm(rng, n):
    beat = RATE                    # 60 beats a minute, four bars of four
    pads, keys = np.zeros((n, 2)), np.zeros((n, 2))
    chords = (("C3", "G3", "E4", "B4"), ("A2", "G3", "C4", "E4"), ("F2", "A3", "C4", "E4"), ("G2", "D3", "C4", "D4"))
    for bar, chord in enumerate(chords):
        for note in chord:
            put(pads, pad(hz(note), 3.2, rng, attack=1.4, release=2.4), bar * 4 * beat, wrap=True)
    # The chord swells in first; the plucked notes come in half a beat after each bar begins.
    notes = ((0, 0.5, "E5"), (0, 2.0, "G5"), (0, 3.0, "B4"), (1, 0.5, "C5"), (1, 2.0, "E5"), (1, 3.0, "G4"),
             (2, 0.5, "A4"), (2, 2.0, "C5"), (2, 3.0, "E5"), (3, 0.5, "D5"), (3, 2.0, "C5"), (3, 3.5, "G4"))
    for number, (bar, at, note) in enumerate(notes):
        one = pluck(hz(note), 0.9, bright=1.6, top=6000.0)
        put(keys, pan(one, (-0.25, 0.25)[number % 2]), int((bar * 4 + at) * beat), (1.0, 0.8, 0.7)[number % 3],
            wrap=True)
    return reverb(level(pads, 0.0) + level(keys, -7.0), rng, 2.6, 0.35, wrap=True)


@sound("TENSE", "bed", 16, "A low held D, thin strings in D minor and a steady muted pulse")
def tense(rng, n):
    seconds = n / RATE
    beat = RATE * 2 // 3           # 90 beats a minute, six bars of four
    bar = 4 * beat
    t = times(n)
    drone = np.zeros((n, 2))
    for note, size in (("D2", 1.0), ("D3", 0.3), ("D1", 0.5)):
        for channel in (0, 1):
            for k in range(1, 7):
                f = whole(hz(note) * k * (1.0, 1.003)[channel], seconds)
                drone[:, channel] += size * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi)) / k ** 1.8
    drone = drone * (0.8 + 0.2 * np.sin(2 * np.pi * t / 8.0))[:, None]

    strings = np.zeros((n, 2))
    chords = (("A4", "D5", "F5"), ("A4", "D5", "F5"), ("Bb4", "D5", "F5"), ("Bb4", "D5", "F5"), ("Bb4", "D5", "G5"),
              ("A4", "C#5", "E5"))
    for number, chord in enumerate(chords):
        for note in chord:
            put(strings, pad(hz(note), bar / RATE - 0.4, rng, attack=0.7, release=1.3, bright=1.2, top=7000.0),
                number * bar, wrap=True)
    strings = strings * (1.0 + 0.22 * np.sin(2 * np.pi * 6.0 * t))[:, None]

    pulse, beats, ticks = np.zeros((n, 2)), np.zeros((n, 2)), np.zeros((n, 2))
    accents = (1.0, 0.5, 0.7, 0.5, 0.9, 0.5, 0.7, 0.6)
    for eighth in range(48):
        at = eighth * beat // 2
        put(pulse, pan(pluck(hz("D3"), 0.14, bright=1.2, top=1800.0), 0.0), at, accents[eighth % 8], wrap=True)
        if eighth % 2:
            put(ticks, pan(burst(rng, 0.08, lambda f: band(f, 5000.0, 9000.0, 2), 0.012), (-0.4, 0.4)[eighth // 2 % 2]),
                at, wrap=True)
        if eighth % 8 in (0, 5):
            put(beats, pan(thump(), 0.0), at, (1.0, 0.6)[eighth % 8 > 0], wrap=True)
    wet = reverb(level(strings, -5.0) + level(pulse, 0.0) + level(ticks, -22.0), rng, 2.2, 0.3, wrap=True)
    return wet + level(drone, -6.0) + level(beats, -4.0)


@sound("BRIGHT", "bed", 16, "A quick plucked pattern over four chords in G major, with a light beat")
def bright(rng, n):
    beat = RATE // 2               # 120 beats a minute, eight bars of four
    bar = 4 * beat
    chords = (("G2", ("G4", "B4", "D5", "G5"), ("G3", "B3", "D4")),
              ("D2", ("F#4", "A4", "D5", "F#5"), ("F#3", "A3", "D4")),
              ("E2", ("E4", "G4", "B4", "E5"), ("E3", "G3", "B3")),
              ("C2", ("E4", "G4", "C5", "E5"), ("E3", "G3", "C4")))
    patterns = ((0, 1, 2, 3, 2, 1, 2, 1), (3, 2, 1, 2, 0, 1, 2, 3))
    accents = (1.0, 0.6, 0.8, 0.6, 0.9, 0.6, 0.8, 0.65)
    arp, pads, low, beats, shaker = (np.zeros((n, 2)) for _ in range(5))
    for number in range(8):
        root, tones, triad = chords[number % 4]
        start = number * bar
        for eighth, tone in enumerate(patterns[number // 4]):
            at = start + eighth * beat // 2
            put(arp, pan(pluck(hz(tones[tone]), 0.35, bright=1.1), (-0.3, 0.3)[eighth % 2]), at, accents[eighth],
                wrap=True)
            shake = burst(rng, 0.06, lambda f: band(f, 6000.0, 12000.0, 2), 0.014, 0.004)
            put(shaker, pan(shake, 0.2), at, (0.6, 1.0)[eighth % 2], wrap=True)
        for note in triad:
            put(pads, pad(hz(note), 1.7, rng, attack=0.3, release=0.7), start, wrap=True)
        for eighth, size in ((0, 1.0), (4, 0.8), (7, 0.6)):
            put(low, pan(bass(hz(root), 0.22), 0.0), start + eighth * beat // 2, size, wrap=True)
        for at in (0, 2):
            put(beats, pan(thump(), 0.0), start + at * beat, wrap=True)
    wet = reverb(level(arp, 0.0) + level(pads, -8.0) + level(shaker, -18.0), rng, 1.6, 0.25, wrap=True)
    return wet + level(low, -3.0) + level(beats, -5.0)


@sound("PULSE", "bed", 16, "Chords that pump on every half beat over a steady low drum, in A minor")
def pulse(rng, n):
    beat = RATE // 2               # 120 beats a minute, eight bars of four
    bar = 4 * beat
    chords = (("A1", ("A3", "C4", "E4")), ("F1", ("A3", "C4", "F4")), ("C2", ("G3", "C4", "E4")),
              ("G1", ("G3", "B3", "D4")))
    stabs, low, beats, hats, held = (np.zeros((n, 2)) for _ in range(5))
    for number, (root, triad) in enumerate(chords):
        start = number * 2 * bar
        put(held, pad(hz("E5"), 3.0, rng, attack=1.0, release=1.6, bright=2.0), start, wrap=True)
        for eighth in range(16):
            at = start + eighth * beat // 2
            for index, note in enumerate(triad):
                put(stabs, pan(pluck(hz(note), 0.18, bright=0.8, top=5000.0), (-0.5, 0.0, 0.5)[index]), at,
                    (0.5, 1.0)[eighth % 2], wrap=True)
            put(low, pan(bass(hz(root) * 2, 0.16), 0.0), at, (1.0, 0.7)[eighth % 2], wrap=True)
            if eighth % 2:
                put(hats, pan(burst(rng, 0.05, lambda f: band(f, 7000.0, 12000.0, 2), 0.01), -0.2), at, wrap=True)
            else:
                put(beats, pan(thump(), 0.0), at, wrap=True)
    wet = reverb(level(stabs, -2.0) + level(held, -12.0) + level(hats, -18.0), rng, 1.4, 0.2, wrap=True)
    return wet + level(low, -6.0) + level(beats, -4.0)


@sound("OPEN", "stinger", 3.4, "Plucked notes climbing into a C major chord: something begins")
def opening(rng, n):
    out = np.zeros((n, 2))
    for number, note in enumerate(("C4", "G4", "C5", "E5", "G5")):
        put(out, pan(pluck(hz(note), 0.5, bright=1.2), -0.4 + 0.2 * number), count(0.02 + 0.1 * number),
            0.7 + 0.06 * number)
    for note in ("C3", "G3", "E4", "G4", "D5"):
        put(out, pad(hz(note), 1.0, rng, attack=0.12, release=1.6) * 2.2, count(0.52))
    put(out, pan(pluck(hz("C6"), 0.7, bright=1.4), 0.1), count(0.52), 0.8)
    return fade(reverb(out, rng, 2.0, 0.35, wrap=False), 0.002, 0.6)


@sound("CLOSE", "stinger", 3.6, "Plucked notes stepping down to a low C: something ends")
def closing(rng, n):
    out = np.zeros((n, 2))
    for number, (at, note) in enumerate(((0.02, "G5"), (0.14, "E5"), (0.28, "D5"), (0.44, "C5"))):
        put(out, pan(pluck(hz(note), 0.45, bright=1.2), 0.4 - 0.25 * number), count(at), 0.85 - 0.05 * number)
    for note in ("C3", "G3", "E4"):
        put(out, pad(hz(note), 0.9, rng, attack=0.08, release=2.0) * 2.2, count(0.64))
    put(out, pan(pluck(hz("C2"), 1.1, bright=1.3, top=2500.0), 0.0), count(0.64), 1.3)
    put(out, pan(pluck(hz("C4"), 0.8, bright=1.4), 0.0), count(0.64), 0.7)
    return fade(reverb(out, rng, 2.2, 0.35, wrap=False), 0.002, 0.7)


# ---------------------------------------------------------------------------
# From samples to a file
# ---------------------------------------------------------------------------

def clean(samples, loop):
    """Take out anything below hearing, so the sound has no offset from zero."""
    n = len(samples)
    size = n if loop else quick(n + RATE // 2)
    f = np.fft.rfftfreq(size, 1.0 / RATE)
    low = (1j * f / 12.0) / (1.0 + 1j * f / 12.0)          # a filter that only looks back, so nothing sounds early
    return np.fft.irfft(np.fft.rfft(samples, size, axis=0) * (low * low)[:, None], size, axis=0)[:n]


def render(one):
    """The finished samples of a sound, as (n, channels) between -1 and 1."""
    kind = KINDS[one.kind]
    n = count(one.seconds)
    samples = np.asarray(one.make(seeded(one.name), n), dtype=float)
    if samples.ndim == 1:
        samples = samples[:, None]
    if len(samples) != n:
        raise SystemExit(f"{one.name}: made {len(samples)} samples, not {n}")
    samples = clean(samples, kind["loop"])
    if not kind["loop"]:
        samples = fade(samples, 0.002, 0.015)
    if kind["measure"] == "lufs":
        samples = samples * 10 ** ((kind["target"] - lufs(samples)) / 20.0)
    else:
        samples = samples * (10 ** (kind["target"] / 20.0) / np.abs(samples).max())
    if decibels(np.abs(samples).max()) > CEILING:
        raise SystemExit(f"{one.name}: at its loudness its peak is {decibels(np.abs(samples).max()):.1f} dBFS, "
                         f"over {CEILING}. Make its loudest moments smaller.")
    return samples


def write(one, samples):
    """Round to 16 bits with a little noise under it, and hand the result to ffmpeg."""
    rng = seeded(one.name + " dither")
    whole_numbers = np.round(samples * 32767.0 + rng.random(samples.shape) - rng.random(samples.shape))
    data = np.clip(whole_numbers, -32768, 32767).astype("<i2").tobytes()
    SOUNDS.mkdir(exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(RATE),
                    "-ac", str(samples.shape[1]), "-i", "pipe:0", "-map_metadata", "-1", "-fflags", "+bitexact",
                    "-flags:a", "+bitexact", "-c:a", "flac", "-compression_level", "12", "-sample_fmt", "s16",
                    str(one.file)], input=data, check=True)


def build(names):
    for name in names:
        one = MADE[name]
        samples = render(one)
        write(one, samples)
        print(f"{one.name:<13} {one.kind:<9} {one.seconds:>5.2f} s  {samples.shape[1]} ch  "
              f"peak {decibels(np.abs(samples).max()):6.1f} dBFS  {one.file.stat().st_size / 1e3:7.0f} kB")


# ---------------------------------------------------------------------------
# Measuring what is in sounds/
# ---------------------------------------------------------------------------

def decode(path):
    """A file's samples as whole numbers, (n, channels), and what ffmpeg says the file is."""
    said = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"],
                          capture_output=True, check=True)
    about = re.search(r"Audio: (\w+), (\d+) Hz, (\w+), (\w+)", said.stderr.decode("utf-8", "replace"))
    codec, rate, layout, form = about.groups()
    channels = {"mono": 1, "stereo": 2}[layout]
    return np.frombuffer(said.stdout, dtype="<i2").reshape(-1, channels), (codec, int(rate), form)


def measure(path):
    """What ffmpeg's ebur128 and astats filters say of a file."""
    said = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                           "astats=measure_perchannel=none,ebur128=framelog=verbose", "-f", "null", "-"],
                          capture_output=True, check=True).stderr.decode("utf-8", "replace")

    def number(pattern):
        found = re.findall(pattern, said)
        return float(found[-1]) if found and found[-1] not in ("-inf", "nan") else float("-inf")

    return {"lufs": number(r"\bI:\s+(-?[\d.]+|-inf|nan) LUFS"), "peak": number(r"Peak level dB: (-?[\d.]+|-inf)"),
            "rms": number(r"RMS level dB: (-?[\d.]+|-inf)"), "dc": number(r"DC offset: (-?[\d.]+)")}


def bands(frame):
    """A 50 ms frame as the level, in dB, of 24 bands from 50 Hz to 16 kHz."""
    window = np.hanning(len(frame))[:, None]
    power = np.sum(np.abs(np.fft.rfft(frame * window, axis=0)) ** 2, axis=1)
    f = np.fft.rfftfreq(len(frame), 1.0 / RATE)
    edges = 50.0 * (16000.0 / 50.0) ** (np.arange(25) / 24.0)
    return np.array([10 * np.log10(np.sum(power[(f >= low) & (f < high)]) + 1e-12)
                     for low, high in zip(edges[:-1], edges[1:])])


def seam(samples):
    """How the end of a loop meets its start, against how the loop moves inside itself.

    Two things are compared with the same thing between neighbours inside the loop:
    the jump from one sample to the next, and the change in spectrum from one 50 ms
    frame to the next. A third frame is taken across the seam itself, half from the
    end and half from the start, where a click would show.
    """
    x = samples.astype(float) / 32768.0
    steps = np.abs(np.diff(x, axis=0)).max(axis=1)
    jump = float(np.abs(x[0] - x[-1]).max())
    size = count(0.05)
    half = size // 2
    round_again = np.concatenate([x, x[:size]])
    spectra = [bands(round_again[start:start + size]) for start in range(0, len(x), half)]
    change = [float(np.mean(np.abs(spectra[i] - spectra[i + 2]))) for i in range(len(spectra) - 2)]
    overlap = [float(np.mean(np.abs(spectra[i] - spectra[i + 1]))) for i in range(len(spectra) - 2)]
    # The frame that starts half a frame before the end is the one across the seam.
    last, first, across = spectra[-2], spectra[0], spectra[-1]
    return {
        "jump": jump, "jump_inside": float(steps.max()), "jump_usual": float(np.percentile(steps, 99)),
        "change": float(np.mean(np.abs(last - first))), "change_inside": max(change[::2]),
        "change_usual": float(np.median(change[::2])),
        "across": max(float(np.mean(np.abs(last - across))), float(np.mean(np.abs(across - first)))),
        "across_inside": max(overlap),
    }


def verify():
    problems = []
    total = 0
    in_banks = {}
    for bank in sorted(BANKS.glob("*.sounds")):
        for name, file in re.findall(r"^([A-Z][A-Z0-9_-]*):\s*\{[^}]*\bfile:\s*([^\s,}]+)", bank.read_text(), re.M):
            if name in in_banks:
                problems.append(f"{name} is in two banks: {in_banks[name]} and {bank.stem}")
            in_banks[name] = bank.stem
            if name in MADE and file != MADE[name].file.name:
                problems.append(f"{name} in bank {bank.stem} plays {file}, not {MADE[name].file.name}")
    for path in sorted(SOUNDS.glob("*.flac")):
        if path.stem.upper() not in MADE:
            problems.append(f"sounds/{path.name} is not made by this script")
    print(f"{'sound':<13} {'kind':<9} {'s':>6} {'ch':>2} {'LUFS':>6} {'peak':>6} {'RMS':>6} {'DC':>8} {'kB':>6}  seam")
    for one in MADE.values():
        kind = KINDS[one.kind]
        if not one.file.is_file():
            problems.append(f"{one.name}: sounds/{one.file.name} is not there")
            continue
        if one.name not in in_banks:
            problems.append(f"{one.name}: no bank names it")
        samples, (codec, rate, form) = decode(one.file)
        said = measure(one.file)
        size = one.file.stat().st_size
        total += size
        seconds = len(samples) / RATE

        def wrong(what):
            problems.append(f"{one.name}: {what}")

        if (codec, rate, form) != ("flac", RATE, "s16"):
            wrong(f"it is {codec} at {rate} Hz as {form}, not 16-bit flac at {RATE} Hz")
        if not kind["least"] <= seconds <= kind["most"]:
            wrong(f"it is {seconds:.2f} s long; a {one.kind} is {kind['least']} to {kind['most']} s")
        if abs(said["dc"]) > 0.0005:
            wrong(f"it sits {said['dc']} away from zero")
        if said["peak"] > CEILING:
            wrong(f"its peak is {said['peak']} dBFS, over {CEILING}")
        if kind["measure"] == "lufs" and abs(said["lufs"] - kind["target"]) > 1.0:
            wrong(f"its loudness is {said['lufs']} LUFS, not within 1 of {kind['target']}")
        if kind["measure"] == "peak" and abs(said["peak"] - kind["target"]) > 0.5:
            wrong(f"its peak is {said['peak']} dBFS, not within 0.5 of {kind['target']}")
        note = ""
        if kind["loop"]:
            met = seam(samples)
            if samples.shape[1] != 2:
                wrong("a loop is in stereo")
            if met["jump"] > met["jump_inside"]:
                wrong(f"the last sample jumps {met['jump']:.5f} to the first; inside the loop the most is "
                      f"{met['jump_inside']:.5f}")
            if met["change"] > met["change_inside"]:
                wrong(f"the last 50 ms differ from the first by {met['change']:.1f} dB; inside the loop the most "
                      f"is {met['change_inside']:.1f}")
            if met["across"] > met["across_inside"]:
                wrong(f"the frame across the seam differs from its neighbours by {met['across']:.1f} dB; inside "
                      f"the loop the most is {met['across_inside']:.1f}")
            note = (f"jump {met['jump']:.4f} (inside: usual {met['jump_usual']:.4f}, most {met['jump_inside']:.4f})  "
                    f"last/first 50 ms {met['change']:.1f} dB (inside: usual {met['change_usual']:.1f}, "
                    f"most {met['change_inside']:.1f})  across {met['across']:.1f} dB (most {met['across_inside']:.1f})")
        else:
            edge = int(max(np.abs(samples[0]).max(), np.abs(samples[-1]).max()))
            if edge > 8:
                wrong(f"it starts or ends {edge} steps from zero; a one-shot is faded at both ends")
            note = f"ends within {edge} of 32768 from zero"
        # Under 0.4 s there is no loudness to measure: the standard needs a whole block.
        loud = f"{said['lufs']:>6.1f}" if said["lufs"] > -70.0 else "     -"
        print(f"{one.name:<13} {one.kind:<9} {seconds:>6.2f} {samples.shape[1]:>2} {loud} "
              f"{said['peak']:>6.1f} {said['rms']:>6.1f} {said['dc']:>8.5f} {size / 1e3:>6.0f}  {note}")
    print(f"sounds/: {total / 1e6:.1f} MB in {len(MADE)} files")
    if total > MOST_BYTES:
        problems.append(f"sounds/ is {total / 1e6:.1f} MB, over {MOST_BYTES / 1e6:.0f}")
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"ok: {len(MADE)} sounds measured")
    return 0


def hashes(names):
    """A SHA-256 of each file's decoded samples. Two builds made the same sound if these agree."""
    for name in names:
        samples, _ = decode(MADE[name].file)
        print(f"{hashlib.sha256(samples.tobytes()).hexdigest()}  {MADE[name].file.name}")
    return 0


def main(arguments):
    parser = argparse.ArgumentParser(description="Make the sounds of this library. Every one is synthesised here.")
    parser.add_argument("names", nargs="*", metavar="NAME", help="the sounds to make (default: all of them)")
    parser.add_argument("--list", action="store_true", help="list the sounds and stop")
    parser.add_argument("--verify", action="store_true", help="measure the files in sounds/ and say what is off")
    parser.add_argument("--hash", action="store_true", help="print a hash of each file's samples")
    asked = parser.parse_args(arguments)
    names = [name.upper() for name in asked.names] or list(MADE)
    unknown = [name for name in names if name not in MADE]
    if unknown:
        parser.error(f"no sound called {', '.join(unknown)}. There are: {', '.join(MADE)}")
    if asked.list:
        for name in names:
            print(f"{name:<13} {MADE[name].kind:<9} {MADE[name].seconds:>5.2f} s  {MADE[name].what}")
        return 0
    if asked.verify:
        return verify()
    if asked.hash:
        return hashes(names)
    build(names)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
