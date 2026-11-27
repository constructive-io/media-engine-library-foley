#!/usr/bin/env python3
"""Is this folder a library? The part of the answer that needs nothing installed.

    python3 scripts/check.py            # this library
    python3 scripts/check.py FOLDER     # another one

It reads media-library.json and says everything wrong with it: a voice with
no consent, a clip that is not there, a path that leaves the library, a bank
that plays a file the library does not hold, a recording that was left in by
mistake. It needs Python 3.9 and nothing else, so it can run on every push.

It does not listen to the clips. `me library check`, from Constructive Media,
does everything this does and measures each clip as well: run that before a
release.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

MANIFEST = "media-library.json"
KINDS = ("own", "permission", "licensed", "synthetic")
SOUND = (".flac", ".wav", ".mp3", ".m4a", ".ogg", ".opus", ".aiff")
PICTURE = (".png", ".jpg", ".jpeg", ".webp")
FOOTAGE = (".mp4", ".mov", ".mkv", ".webm")
#: A clip to clone from is half a minute at most. A file past this is likely the recording it was cut from.
CLIP_BYTES = 6_000_000

NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
KNOWN = {"library", "version", "license", "summary", "homepage", "voices", "banks", "sounds", "faces"}
OF_A_VOICE = {"persona", "name", "emotion", "reference", "said", "profile", "engine", "seed", "consent", "tags"}
#: `file: rain.flac` in a bank, with or without quotes.
PLAYED = re.compile(r"""\bfile:\s*(?:"([^"]+)"|'([^']+)'|([^\s,}]+))""")


def check(folder):
    """Everything wrong with the library in `folder`: (name, voices, problems, notes)."""
    folder = Path(folder).expanduser()
    problems, notes = [], []
    written = folder / MANIFEST
    if not written.is_file():
        return None, 0, [f"{folder} is not a library: it has no {MANIFEST}"], []
    try:
        manifest = json.loads(written.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, 0, [f"{written} cannot be read as JSON: {error}"], []
    if not isinstance(manifest, dict):
        return None, 0, [f"{written} should hold an object, in braces"], []
    folder = folder.resolve()

    name = manifest.get("library")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        problems.append('"library" is the library\'s name: lower case letters and numbers, joined by hyphens')
        name = None
    for key in sorted(set(manifest) - KNOWN):
        notes.append(f'"{key}" is not something a library says; it is ignored')

    def within(path):
        try:
            path.relative_to(folder)
        except ValueError:
            return False
        return True

    def inside(given, what):
        if not isinstance(given, str) or not given.strip():
            problems.append(f"{what} should be a path inside the library")
            return None
        path = (folder / given).resolve()
        if Path(given).is_absolute() or not within(path):
            problems.append(f"{what} is `{given}`, which is outside the library: everything it names is inside it")
            return None
        return path

    named = set()      # every file the library names, to tell a stray recording from a clip
    voices, seen = 0, set()
    listed = manifest.get("voices", [])
    if not isinstance(listed, list):
        problems.append('"voices" should be a list')
        listed = []
    for number, given in enumerate(listed, start=1):
        if not isinstance(given, dict):
            problems.append(f"voice {number} should be an object, in braces")
            continue
        persona, emotion = given.get("persona"), given.get("emotion", "neutral")
        who = f"voice {number}"
        if not isinstance(persona, str) or not NAME.fullmatch(persona):
            problems.append(f'{who}: "persona" is who speaks with it, as a script writes them: lower case, with hyphens')
            persona = None
        if not isinstance(emotion, str) or not NAME.fullmatch(emotion):
            problems.append(f'{who}: "emotion" should be one word, such as neutral or whisper')
            emotion = "neutral"
        if persona:
            who = f"{persona}/{emotion}"
            if (persona, emotion) in seen:
                problems.append(f"{who} is there twice: a persona has one voice for an emotion")
            seen.add((persona, emotion))
            voices += 1
        for key in sorted(set(given) - OF_A_VOICE):
            notes.append(f'{who}: "{key}" is not something a voice says; it is ignored')

        said = given.get("said")
        if said is not None and (not isinstance(said, str) or not said.strip()):
            problems.append(f'{who}: "said" should be the words said in the clip')
            said = None
        profile = given.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile.strip()):
            problems.append(f'{who}: "profile" should be the name a speech server knows the voice by')
            profile = None
        reference = None
        if "reference" in given:
            reference = inside(given["reference"], f'{who}: "reference"')
            if reference is not None and not reference.is_file():
                problems.append(f"{who}: its clip, `{given['reference']}`, is not there")
                reference = None
            elif reference is not None and reference.suffix.lower() not in SOUND:
                problems.append(f"{who}: its clip, `{given['reference']}`, is not a kind of sound file known here "
                                f"({', '.join(SOUND)})")
                reference = None
        elif not profile:
            problems.append(f'{who}: a voice is a "reference", a clip to clone from, or a "profile", the name a '
                            "speech server knows one of its own voices by")
        if reference is not None:
            named.add(reference)
            beside = reference.with_suffix(".txt")
            if said is None and beside.is_file():
                said = beside.read_text(encoding="utf-8", errors="replace").strip() or None
            if said is None:
                problems.append(f'{who}: the words said in its clip are not written down. Give them as "said", or '
                                f"in `{beside.relative_to(folder)}`")
            if reference.stat().st_size > CLIP_BYTES:
                notes.append(f"{who}: its clip is {reference.stat().st_size / 1e6:.0f} MB. A clip to clone from is "
                             "half a minute at most; the recording it was cut from is kept somewhere else")

        seed = given.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            problems.append(f'{who}: "seed" should be a whole number')
        engine = given.get("engine")
        if engine is not None and not isinstance(engine, str):
            problems.append(f'{who}: "engine" should be the name of a speech adapter')
        consent = given.get("consent")
        if not isinstance(consent, dict):
            problems.append(f'{who}: it has no "consent". Every voice says whose it is and by what right it is '
                            f'here: {{"by": "who", "kind": "{" | ".join(KINDS)}"}}')
            continue
        if not isinstance(consent.get("by"), str) or not consent["by"].strip():
            problems.append(f'{who}: its consent does not say who gave it, as "by"')
        if consent.get("kind") not in KINDS:
            problems.append(f'{who}: the "kind" of its consent should be one of {", ".join(KINDS)}')
        elif consent["kind"] in ("licensed", "synthetic") and not str(consent.get("note") or "").strip():
            problems.append(f'{who}: consent of the kind `{consent["kind"]}` says more in a "note": '
                            + ("which license, and where the recording is from" if consent["kind"] == "licensed"
                               else "how the voice was made"))
        if "on" in consent:
            try:
                date.fromisoformat(str(consent["on"]))
            except ValueError:
                problems.append(f'{who}: the day its consent was given, "on", should be written like 2026-11-26')

    def folders(key):
        given = manifest.get(key, [])
        if not isinstance(given, list):
            problems.append(f'"{key}" should be a list of folders')
            return []
        found = []
        for one in given:
            path = inside(one, f'"{key}"')
            if path is not None and not path.is_dir():
                problems.append(f'"{key}" names `{one}`, and there is no such folder')
            elif path is not None:
                found.append(path)
        return found

    banks, sounds = folders("banks"), folders("sounds")
    for bank in sorted(file for one in banks for file in one.glob("*.sounds")):
        text = "\n".join(line.split(" #")[0] for line in bank.read_text(encoding="utf-8", errors="replace").splitlines()
                         if not line.lstrip().startswith("#"))
        for quoted, single, bare in PLAYED.findall(text):
            file = quoted or single or bare
            if Path(file).expanduser().is_absolute():
                problems.append(f"bank `{bank.stem}`: it plays `{file}`, a path only this machine has")
            elif not any((one / file).is_file() for one in sounds):
                where = ", ".join(str(one.relative_to(folder)) for one in sounds) or 'no folder: "sounds" names none'
                problems.append(f"bank `{bank.stem}`: it plays `{file}`, which is not in the library's sounds ({where})")

    faces = manifest.get("faces", {})
    if not isinstance(faces, dict):
        problems.append('"faces" should be an object: a picture for a persona')
        faces = {}
    for persona, picture in faces.items():
        path = inside(picture, f'"faces": {persona}')
        if path is None:
            continue
        if not path.is_file():
            problems.append(f"the face of {persona}, `{picture}`, is not there")
        elif path.suffix.lower() not in PICTURE:
            problems.append(f"the face of {persona}, `{picture}`, should be a picture ({', '.join(PICTURE)})")

    # A recording nothing names is most often the one a clip was cut from, left in by mistake.
    for path in sorted(folder.rglob("*")):
        if ".git" in path.relative_to(folder).parts or not path.is_file():
            continue
        ending, where = path.suffix.lower(), path.relative_to(folder)
        if ending in FOOTAGE:
            notes.append(f"`{where}` is footage. A library holds clips of sound and pictures of faces, and no video")
        elif ending in SOUND and path not in named and not any(within_one(path, one) for one in sounds):
            notes.append(f"`{where}` is a recording that no voice and no bank names. If it is what a clip was cut "
                         "from, keep it somewhere else")

    if not voices and not any(list(one.glob("*.sounds")) for one in banks) and not faces and not problems:
        notes.append("it holds no voices, no banks and no faces yet")
    return name, voices, problems, notes


def within_one(path, folder):
    try:
        path.relative_to(folder)
    except ValueError:
        return False
    return True


def main(arguments):
    folder = arguments[0] if arguments else Path(__file__).resolve().parent.parent
    name, voices, problems, notes = check(folder)
    for problem in problems:
        print(problem)
    for note in notes:
        print(f"note: {note}")
    if problems:
        print(f"{len(problems)} problem(s): this is not a library yet", file=sys.stderr)
        return 1
    print(f"ok: {name}, {voices} voice(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
