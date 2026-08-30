# M6 — Persistent instant preview and fast interval selection

## Result

M6 is complete with the agreed hybrid preview architecture.

```text
working .osu + resolved audio
          │
          ├── existing slider parser ──→ compact playfield scene
          │                                  │
          └── session-only audio route ──────┤
                                             ↓
                                  persistent preview window
                                  (instant seek + rendering)

preview cursor ──→ optional Danser launch for a high-fidelity check
```

The preview window is opened once from the Inpaint tab and remains independent
of generation work. It follows the selected session, difficulty, current
revision, undo, and redo. A revision-keyed scene cache prevents reparsing the
same map on every state poll.

## Embedded preview

- Uses Mapperatorinator's existing `slider` dependency for hitobject timing,
  stacking, slider duration, and slider curve geometry.
- Streams only the selected session's resolved audio. Flask conditional file
  responses support byte ranges, allowing the embedded audio element to seek
  without loading or importing the map into osu!.
- Renders an osu!standard-oriented lightweight playfield with circles, approach
  circles, combo numbers/colors, slider bodies and balls, repeats, and spinners.
- Keeps audio loaded while an `.osu` revision changes, so generation and
  undo/redo refresh the objects rather than restarting playback infrastructure.
- Clicking or dragging the density waveform seeks immediately. Arrow keys seek
  by one second, or five seconds while Shift is held.
- Displays the exact cursor timestamp and map length. `Copy to Start` and
  `Copy to End` update the main Inpaint fields through the preview controller and
  `BroadcastChannel`.
- `Space` plays/pauses, `[` and `]` copy interval boundaries, `R` requests
  regeneration, and `Ctrl+Z` / `Ctrl+Shift+Z` request undo/redo. Shortcuts are
  ignored while a form control has focus.
- Changed revisions can automatically return to the padded selection start and
  resume playback after the user has initiated audio once. This respects the
  embedded browser's media autoplay restriction.

This renderer is deliberately a workflow preview, not an osu! or Danser clone.
It does not currently reproduce skins, storyboards, videos, continuous slider
loops/ticks, or every stable rendering quirk.

## Preview follow-up

The first user-test pass produced three focused corrections:

- The native preview opens at 1360×1000 and the playfield canvas is taller. Its
  transform now reserves more than three circle radii around every playfield
  edge, so hit circles and their largest approach circles are not clipped.
- Repeating sliders show reverse arrows at the applicable endpoints. Sliders
  with multiple reverses also show an `×N` badge, and both endpoints are marked
  when the path reverses at both ends.
- Hitsounds are scheduled at circle clicks, slider heads, every slider repeat
  edge, final slider ends, and spinner ends. The parser supplies timing-point
  sample set/index/volume plus object/edge overrides. Existing beatmap samples
  are streamed from the isolated session through a traversal-safe endpoint;
  normal/soft/drum samples that would ordinarily come from the user's osu! skin
  use lightweight built-in preview sounds. Hitsounds can be disabled in the
  preview window and the preference is remembered.

## Danser role

Danser 0.11.0 remains available through `Open cursor in Danser`, with a separate
stop button. Danser is no longer required for the persistent window, seeker,
audio playback, selection controls, or automatic revision refresh. Its role is
to provide an optional high-fidelity visual and hitsound check.

## Verification

- The complete Python suite passes: 36 tests.
- JavaScript syntax checks pass for the main UI and preview window.
- Endpoint tests cover revision refresh, parsed scene loading, selection-copy
  synchronization, CSRF protection, and byte-range audio responses.
- Parser tests cover density, circles, sliders, spinners, slider curves, and
  object durations.
- A real pywebview smoke test used
  `UNDEAD CORPORATION - Everything will freeze (Ekoro) [Lunatic]`: the window
  loaded 961 objects, fully decoded its 197.695-second audio track, rendered the
  preview controls, and reached `Ready` without importing the map into osu!.

## Manual acceptance path

1. Run `Run Mapperatorinpainter.bat` and open an `.osz` in Inpaint.
2. Click `Open Preview`; leave the preview window open.
3. Click or drag on the density waveform and verify immediate audio seeking.
4. Use `Copy to Start` and `Copy to End` and verify the main fields change.
5. Regenerate, undo, redo, and change difficulty; verify the open preview updates.
6. Optionally click `Open cursor in Danser` to compare the lightweight preview
   with the high-fidelity renderer.
