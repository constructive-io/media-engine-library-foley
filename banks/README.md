# banks

A bank is a file ending `.sounds` that a script pulls in by name:

```
@include weather
```

Each line under `@sounds` names one sound and the file it plays:

```
RAIN:    { file: rain.flac, loop: true, track: ambience, level: -34 }
THUNDER: { file: thunder.flac, track: foreground, level: -20 }
```

A `file:` is a name inside [`sounds/`](../sounds), never a path on a machine.
A `level:` is the loudness the sound is brought to, in decibels, from -80 to 0.
A short sharp sound is mostly quiet, so its number is low.

| Bank | Holds | Track |
| --- | --- | --- |
| `weather` | rain, wind, sea, night, birds, thunder | `ambience`; thunder on `foreground` |
| `rooms` | room tone, hum, murmur, fire | `ambience` |
| `interface` | presses, tones and small signals | `foreground` |
| `foley` | knocks, steps, paper, keys | `foreground` |
| `music` | four beds, two stingers, a riser, an impact | `music`; the one-shots on `transition` |

A sound is in one bank only.
