# M4 — Safe iteration

## Result

M4 is complete. Each open beatmapset session now keeps an independent revision
timeline for every difficulty. Successful regeneration commits only the active
`.osu`; audio, backgrounds, samples, videos, and other assets are never duplicated
for history.

## Revision transaction

```text
validated generation
        ↓
parent session records `.osu` bytes + settings
        ↓
Revision N becomes current
        ↓
Undo ⇄ Redo restore atomically
```

The original file is Revision 0. Undo and redo validate their bounds, restore via
an atomic replacement, and remain local to the selected difficulty. Generating
after an undo discards the abandoned redo branch. A failed or cancelled generation
still restores the pre-generation snapshot and creates no revision.

Revision metadata records the interval, actual seed, descriptors, negative
descriptors, target/calculated difficulty, mapper ID, year, temperature, CFG,
top-p, lookback/lookahead, hitsound conditioning, timing context, and LoRA path.
The Inpaint tab shows a compact history with the range, seed, difficulty, and
descriptors.

## Seed workflow

- **Random** chooses a seed in Mapperatorinator's existing `0..65536` range.
- The inference compiler resolves a blank seed before starting the worker.
- The actual resolved seed is written back into the field and remains there.
- Repeating generation without changing controls reproduces the same request.
- Every successful revision records that actual seed and its conditioning settings.

## Dirty-state protection

Dirty state is based on the current revision versus the last opened/exported
revision for every difficulty. Undoing back to Revision 0 therefore becomes clean;
undoing away from an exported revision becomes dirty. A successful export marks
the current revisions as saved.

Opening another map or pressing **Close map** offers the three required outcomes:

1. Save — export, then close only after export succeeds.
2. Discard — explicitly close and clean the temporary session.
3. Cancel — retain the current session and all revisions.

The backend independently refuses to close a dirty session unless it has been
exported or the request explicitly authorizes discard. Browser/window close also
uses the standard unsaved-change warning.

## Verification

```text
python -m unittest \
  tests.test_inpainting_session \
  tests.test_inpainting_ui \
  tests.test_partial_inpainting_backend \
  tests.test_inpainting_web_ui

Ran 26 tests ... OK
```

Coverage includes revision commit metadata, undo/redo, redo-branch truncation,
returning to a clean original revision, export baselines, dirty-close rejection,
explicit discard, shared-server job completion, and the M4 UI controls.
