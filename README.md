# foley

<p align="center" width="100%">
  <img height="250" src="https://raw.githubusercontent.com/constructive-io/constructive/refs/heads/main/assets/outline-logo.svg" alt="Constructive" />
</p>

<p align="center" width="100%">
  <a href="https://github.com/constructive-io/media-library-foley/actions/workflows/check.yml">
    <img height="20" src="https://github.com/constructive-io/media-library-foley/actions/workflows/check.yml/badge.svg" alt="Check status" />
  </a>
  <a href="https://github.com/constructive-io/media-library-foley/blob/main/LICENSE"><img height="20" src="https://img.shields.io/badge/license-CC0--1.0-blue.svg" alt="CC0 1.0 license" /></a>
</p>

<p align="center" width="100%">
  Sound effects, ambience loops and short music beds. Every one is made by a script.
</p>

A library of sounds for [Constructive Media](https://github.com/constructive-io/constructive-media).
There are 36 of them: 9 ambience loops, 21 one-shots, 4 music beds and 2 stingers.

None of them is a recording. [`scripts/make.py`](./scripts/make.py) builds each
one from sines and shaped noise. Run it and you get the same files again.

There are no voices and no faces here.

## Use it

`me` is the command of Constructive Media. The name is short for "media engine".

Add the library by name, once an index lists it:

```sh
me library add foley
```

Or add it by its address:

```sh
me library add constructive-io/media-library-foley
```

Then pull a bank into a `.conv` script and name a sound in a cue:

```
@include weather
@include foley
---
[ambience RAIN]
ANNA: It has rained since morning.
[THUNDER]
[KNOCK]
BEN: Somebody is at the door.
[ambience off fade-out:3]
```

Sound names are in capitals. Each sound knows its own track, so `[RAIN]` alone
also works.

## The banks

There are five banks. Include the ones you need.

| Bank | Holds |
| --- | --- |
| `weather` | RAIN, WIND, SEA, NIGHT, BIRDS, THUNDER |
| `rooms` | ROOM-TONE, HUM, MURMUR, FIRE |
| `interface` | CLICK, POP, BEEP, DING, CHIME, ERROR, SUCCESS, NOTIFICATION, WHOOSH |
| `foley` | KNOCK, DOOR, BELL, TICK, HEARTBEAT, FOOTSTEPS, PAGE-TURN, TYPING, SHUTTER |
| `music` | CALM, TENSE, BRIGHT, PULSE, OPEN, CLOSE, RISER, IMPACT |

Each sound plays on a track that every script already has:

- Ambience loops play on `ambience`.
- Music beds play on `music`.
- One-shots play on `foreground`.
- The stingers, RISER and IMPACT play on `transition`.

Each bank also gives every sound a `level`. Change it in a cue if you want it
louder or quieter: `[THUNDER level:-16]`.

## The sounds

All files are FLAC, 48 kHz, 16-bit. The whole `sounds/` folder is about 18 MB.

### Ambience

These loop. The end of each file runs into its start with no click and no gap.
They are stereo.

| Name | Kind | Length | What it is |
| --- | --- | --- | --- |
| RAIN | loop | 10 s | Steady rain, with nearer drops in it |
| WIND | loop | 12 s | Wind that rises and falls in gusts |
| SEA | loop | 12 s | Waves that break on a shore and hiss back down it |
| NIGHT | loop | 12 s | Crickets at night, near and far |
| BIRDS | loop | 12 s | Small birds calling in the open, some near, some far |
| ROOM-TONE | loop | 10 s | The quiet of an empty room, with air moving somewhere |
| HUM | loop | 10 s | A low machine hum, steady, with a slow beat in it |
| MURMUR | loop | 12 s | A room of people too far off to follow. It is bursts of shaped noise. There is no speech in it |
| FIRE | loop | 10 s | A wood fire: a low roar, a hiss, and crackles |

### One-shots

These play once.

| Name | Kind | Length | What it is |
| --- | --- | --- | --- |
| THUNDER | one-shot, stereo | 4.0 s | A crack of thunder and the rumble after it |
| KNOCK | one-shot, mono | 0.7 s | Three knocks on a wooden door |
| DOOR | one-shot, stereo | 0.9 s | A door pushed shut: the latch, then the thud |
| BELL | one-shot, mono | 4.0 s | One stroke of a large bell, left to ring |
| CHIME | one-shot, stereo | 2.8 s | Four metal chimes struck one after another |
| CLICK | one-shot, mono | 0.2 s | A button pressed |
| TICK | one-shot, mono | 0.25 s | One tick of a clock |
| WHOOSH | one-shot, stereo | 1.0 s | Something passing quickly, left to right |
| POP | one-shot, mono | 0.25 s | A soft pop, like a cork or a bubble |
| BEEP | one-shot, mono | 0.35 s | A plain short tone |
| DING | one-shot, mono | 2.0 s | A small desk bell, struck once |
| ERROR | one-shot, mono | 0.6 s | Two low buzzes, the second lower: something went wrong |
| SUCCESS | one-shot, mono | 1.0 s | Three quick rising notes: it worked |
| NOTIFICATION | one-shot, mono | 1.2 s | Two soft notes, the second higher: something arrived |
| HEARTBEAT | one-shot, mono | 1.8 s | Two beats of a heart |
| FOOTSTEPS | one-shot, mono | 2.4 s | Four steps on a hard floor |
| PAGE-TURN | one-shot, stereo | 0.9 s | A page of a book turned over |
| TYPING | one-shot, stereo | 2.6 s | A few words typed on a keyboard |
| SHUTTER | one-shot, mono | 0.3 s | A camera shutter opening and closing |
| RISER | one-shot, stereo | 3.5 s | A rising rush that builds and stops short |
| IMPACT | one-shot, stereo | 2.8 s | A deep hit with a long tail |

### Music

The beds loop like the ambience does. Each stays in one key. They are stereo.

| Name | Kind | Length | What it is |
| --- | --- | --- | --- |
| CALM | bed, loop | 16 s | Slow soft chords in C major with a few plucked notes over them. 60 beats a minute |
| TENSE | bed, loop | 16 s | A low held D, thin strings in D minor and a steady muted pulse. 90 beats a minute |
| BRIGHT | bed, loop | 16 s | A quick plucked pattern over four chords in G major, with a light beat. 120 beats a minute |
| PULSE | bed, loop | 16 s | Chords that pump on every half beat over a steady low drum, in A minor. 120 beats a minute |
| OPEN | stinger | 3.4 s | Plucked notes climbing into a C major chord: something begins |
| CLOSE | stinger | 3.6 s | Plucked notes stepping down to a low C: something ends |

The tunes were written for this library. They copy no other music.

## Levels

- Ambience loops are at -26 LUFS.
- Music beds are at -22 LUFS.
- One-shots and stingers peak at -3 dBFS and fade at both ends.

Nothing clips. No file has a DC offset.

## Rebuild the sounds

You need `numpy` and the `ffmpeg` program.

```sh
python3 scripts/make.py                 # make every sound again
python3 scripts/make.py RAIN THUNDER    # make only these
python3 scripts/make.py --list          # list the sounds
python3 scripts/make.py --verify        # measure every file: level, loop seam, size
python3 scripts/make.py --hash          # a hash of each file's samples
```

If your Python has no `numpy`, use `uv`:

```sh
uv run --no-project --with numpy --python 3.12 python scripts/make.py
```

The random numbers come from seeds fixed in the script. Two runs on one
machine give the same samples. Check it with `--hash` before and after.
Another machine or another version of numpy may differ in the last bit.

To change a sound, change its function in `scripts/make.py` and run the script.
Do not edit a file in `sounds/` by hand.

## Check the library

```sh
python3 scripts/check.py     # is it a library; needs nothing installed
me library check .           # the same check, by the engine
```

## License

The sounds are under [CC0 1.0](./LICENSE). You may use them for anything. You
do not have to ask and you do not have to give credit.

The script that makes them, `scripts/make.py`, is offered under the same terms.
