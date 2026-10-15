# musslop → Godot 4 example

A minimal adaptive-music player for Godot 4 that reads the `manifest.json`
produced by musslop's **Files → Loops (zip)** export.

```text
res://music/tavern/
  manifest.json
  01_Intro.wav
  02_Verse.wav
  02_Verse_tail.wav      (optional, when "transition tail" was on)
  ...
```

## Use

1. In musslop, prepare the track (sections, loop flags, scenes) and export
   **Loops (zip)**. Unzip it into your Godot project, e.g. `res://music/tavern/`.
2. Import the WAVs (Godot imports `.wav` automatically). In the Import dock you
   may set **Loop Mode: Disabled** — the script handles looping itself so that
   transitions can land on boundaries.
3. Add `MusslopPlayer.gd` to a `Node` and set `manifest_path` in the inspector.
4. Call from gameplay:

```gdscript
$Music.play_loop(0)                 # start looping loop #0
$Music.cue(2, MusslopPlayer.Cue.NATURAL)  # switch to loop #2 at the loop end
$Music.cue(3, MusslopPlayer.Cue.SOON)     # ...at the next transition point (phrase)
$Music.cue(1, MusslopPlayer.Cue.NOW)      # ...immediately with a crossfade
$Music.cue_scene("Combat")          # use a scene saved in musslop (its loop + cue mode)
$Music.cancel_cue()                 # stay in the current loop
$Music.fade_out(2.0)
```

## What the manifest gives you

| Field | Meaning |
|---|---|
| `loops[i].file` | WAV file for the section |
| `loops[i].loop` | `false` for build-ups — play once, then move on |
| `loops[i].loop_start_sample` | where a repeat restarts (0 unless a loop-start marker was set) |
| `loops[i].transition_points_sec` | phrase boundaries inside the loop (every `phrase_bars` bars, default 4) for `SOON` cues; bar length is `beats_per_bar` |
| `loops[i].tail_file` | the audio right after the loop end — play it over the next loop's start |
| `loops[i].stinger` | `cymbal` / `boom` / `riser` — an accent you may play on entry |
| `scenes[]` | musslop scenes: `loop_index`, `cue`, `layers`, `hotkey` |

The script uses two `AudioStreamPlayer`s (A/B) and schedules the swap by
polling `get_playback_position()` in `_process`, which is accurate to one
frame. For sample-accurate scheduling use `AudioServer.get_time_to_next_mix()`
or an `AudioStreamPlaylist` — this example keeps things readable.

Layers (stems) are not part of this example: export the stems separately and
drive a second set of players with `scenes[].layers` if you need vertical mixing.
