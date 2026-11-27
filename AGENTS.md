# foley — agent guide

A library of sounds for [Constructive Media](https://github.com/constructive-io/constructive-media):
ambience loops, one-shots and short music beds. [`media-library.json`](./media-library.json)
names what is in it. [README.md](./README.md) lists every sound.

## Rules

- **Every sound comes from `scripts/make.py`.** A file in `sounds/` is what the script
  wrote. To change a sound, change its function and run the script. Do not edit a
  `.flac` by hand.
- **Never add a recording.** No microphone, no download, no sample from anywhere, no
  speech. A new sound is a new function in `scripts/make.py`, built from sines and
  shaped noise with fixed seeds.
- **Nothing copied.** A music bed is written here, in one key, and follows no existing
  tune.
- **Loops are exact.** An ambience loop or a bed is periodic over its own length. Make
  it that way; do not paper over a seam with a fade.
- **Keep the levels.** Ambience at -26 LUFS, beds at -22 LUFS, one-shots and stingers
  peaking at -3 dBFS with a fade at each end. 48 kHz, 16-bit FLAC. All of `sounds/`
  under 20 MB.
- **A sound is in one bank.** Names are in capitals, track names in lower case. Use the
  tracks every script has: `ambience`, `music`, `foreground`, `transition`. A bank
  uses only the keys the `.sounds` format has.
- **No voices, no faces.** This library holds sounds only.
- **Paths stay inside.** A bank names a file in `sounds/` by its name. Nothing names a
  path on one machine.
- **Rebuild and check before a commit.** Run the four commands below. Report what they
  printed, and that nobody listened unless somebody did.

## Commands

```sh
python3 scripts/make.py                   # make every sound again; needs numpy and ffmpeg
python3 scripts/make.py NAME...           # make some
python3 scripts/make.py --list            # what there is
python3 scripts/make.py --verify          # measure every file: level, loop seam, size, banks
python3 scripts/make.py --hash            # a hash of each file's samples, to compare two builds
python3 scripts/check.py                  # is it a library; needs nothing installed
me library check .                        # the same check, by the engine
```

Where the system Python has no numpy:

```sh
uv run --no-project --with numpy --python 3.12 python scripts/make.py
```

## Add a sound

1. Write a function in `scripts/make.py` under `@sound(NAME, kind, seconds, what)`. The
   kind is `ambience`, `bed`, `one-shot` or `stinger`. Take random numbers only from
   the `rng` the function is given.
2. Run `python3 scripts/make.py NAME`, then `python3 scripts/make.py --verify`.
3. Add one line for it to one bank in `banks/`, and one row to the table in the README.
4. Run `python3 scripts/check.py`.
