# M3 — Usable Inpaint tab

## Result

M3 is complete. Mapperatorinator now has a top-level **Inpaint** tab that opens an
`.osz` into the M2 working-copy session, selects a difficulty, replaces a validated
time interval, and exports the current beatmapset. Generate remains on its original
form and both modes use the existing UI-owned inference server.

## Reused implementation

The Inpaint route composes the normal Hydra inference configuration, calls the
existing `compile_args`, and then calls the existing `_ensure_inference_server`.
The long-lived `InferenceClient` remains keyed and owned by `web-ui.py`; Inpaint does
not launch another Mapperatorinator/model service. Its worker calls the M2
`regenerate_interval` transaction with the normal `inference.main` runner.

```text
Generate form ─┐
               ├─ compile_args → shared InferenceClient/model server
Inpaint form ──┘                         ↓
                             short per-generation worker
```

## UI scope

- Source `.osz`, difficulty selector, audio, length, and inherited map metadata.
- `MM:SS`, `MM:SS.mmm`, and raw-second interval entry with normalized display.
- Descriptors, negative descriptors, target difficulty, mapper ID, year, seed,
  timing context, and hitsound behavior.
- Temperature, CFG, top-p, lookback, lookahead, and optional LoRA controls.
- Repeated regeneration retains the extracted session, difficulty, interval,
  controls, and actual seed.
- Export delegates to the M2 archive implementation and never targets the source.

Undo/redo, revision history, random-seed controls, and dirty-close prompts remain M4.

## Verification

Automated suite:

```text
python -m unittest \
  tests.test_inpainting_session \
  tests.test_partial_inpainting_backend \
  tests.test_inpainting_ui \
  tests.test_inpainting_web_ui

Ran 20 tests ... OK
```

The endpoint test submits two jobs against one session and asserts that the same
working directory remains active, both configs use the server backend, the seed is
stable, and no second extraction occurs.

Manual browser/model proof used cached Mapperatorinator v32 and the M2 proof archive:

1. Opened `source.osz`, selected Expert, and displayed inherited metadata.
2. Regenerated `00:02.000–00:04.000` with seed `32`.
3. Regenerated the same interval again without reopening or changing the session.
4. Both jobs completed; the first reported 17.4 tok/s and the reused-server run
   reported 164.9 tok/s.
5. Server output contained exactly one model-load event across both jobs.
6. The twice-modified working `Expert.osu` parsed successfully with five objects.

The source `.osz` remained immutable. Browser inspection showed no new JavaScript
errors; existing missing-descriptor-translation warnings are unrelated to M3.
