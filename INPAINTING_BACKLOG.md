# Mapperatorinator Interactive Inpainting — Backlog

> Before implementing a backlog item, inspect the existing Mapperatorinator implementation for equivalent or adjacent functionality. Prefer adapting/reusing existing code over creating parallel implementations. Do not refactor unrelated code unless necessary for the item being implemented. Complete and test one milestone before beginning the next.

## 0. Project constraints and principles

- [ ] **0.1 Preserve existing Mapperatorinator functionality**

  - Existing full-map generation must continue working.
  - Reuse the existing model loading, inference, preprocessing, postprocessing, configuration, and GUI infrastructure wherever practical.
  - Do **not** create a second Mapperatorinator/model process for inpainting.
  - The model should be loaded once and shared between full generation and inpainting.
  - Avoid modifying model architecture or weights.
  - Treat Mapperatorinator's existing partial-remapping functionality as the inference backend rather than reimplementing it.

- [ ] **0.2 Separate inference from workflow**

  - Inpainting is primarily a new workflow/UI around existing inference.
  - Keep `.osz` session management separate from Mapperatorinator inference logic where practical.
  - Keep future previewer integration behind an abstraction; Danser should not become a dependency of core inference.

- [ ] **0.3 Protect user files**

  - Never destructively modify the source `.osz`.
  - Work against a temporary/session copy.
  - Explicit save/export writes a new `.osz` or intentionally overwrites a user-selected destination.
  - Failed generations must not leave the working `.osu` corrupted.
  - Preserve unknown/unmodified beatmap files and assets.

## Phase 1 — Understand the existing codebase

### 1.1 Trace existing full generation

- [ ] Identify GUI entry point.
- [ ] Identify model initialization/loading.
- [ ] Identify how GUI parameters become inference configuration.
- [ ] Identify inference entry point.
- [ ] Identify preprocessing pipeline.
- [ ] Identify postprocessing pipeline.
- [ ] Identify output `.osu` creation.
- [ ] Identify existing `.osz` export functionality.

**Deliverable:** short internal developer note showing:

```text
GUI
 ↓
configuration
 ↓
model/inference
 ↓
postprocessor
 ↓
.osu/.osz
```

### 1.2 Trace existing partial generation

Find exactly how existing parameters are handled:

- [ ] `beatmap_path`
- [ ] `start_time`
- [ ] `end_time`
- [ ] `add_to_beatmap`
- [ ] `overwrite_reference_beatmap`
- [ ] context options
- [ ] descriptors
- [ ] difficulty
- [ ] mapper/style conditioning
- [ ] seed
- [ ] temperature
- [ ] CFG
- [ ] timing context
- [ ] hitsound-related options

Determine:

- [ ] Which parameters are required.
- [ ] Which can be derived from the reference `.osu`.
- [ ] Which should be exposed in Inpaint.
- [ ] Which should normally remain inherited/hidden.
- [ ] Exactly how objects inside the requested interval are replaced.
- [ ] What happens to objects crossing an interval boundary.
- [ ] What happens to timing points/effect points.
- [ ] Whether the original reference file is mutated.

**Acceptance criterion:** manually invoke existing partial generation without the new GUI and successfully replace a known interval.

## Phase 2 — Session / `.osz` handling

### 2.1 Open beatmapset

- [ ] Add ability to select an `.osz`.
- [ ] Validate that the archive is readable.
- [ ] Create unique temporary working directory.
- [ ] Extract `.osz` contents once.

Example:

```text
temp/
└── mapperatorinator/
    └── session-<id>/
        ├── audio.mp3
        ├── bg.jpg
        ├── Easy.osu
        ├── Hard.osu
        └── Insane.osu
```

- [ ] Prevent zip-slip/path-traversal during extraction.
- [ ] Preserve nested asset directories if present.

### 2.2 Discover difficulties

- [ ] Find all `.osu` files.
- [ ] Parse enough metadata to display:
  - version/difficulty name
  - mode
  - mapper
  - AR
  - OD
  - CS
  - HP
  - length if conveniently available
- [ ] Present difficulties in a selector.
- [ ] Reject/disable unsupported modes appropriately.

### 2.3 Resolve assets

For selected difficulty:

- [ ] Resolve `AudioFilename`.
- [ ] Verify audio exists.
- [ ] Resolve background when available.
- [ ] Handle Unicode filenames.
- [ ] Handle relative paths safely.
- [ ] Give useful errors for missing assets.

### 2.4 Working-copy semantics

Maintain separately:

```text
source archive
     │
     ├── immutable source
     │
     └── working directory
             │
             └── mutable .osu
```

- [ ] Never edit source archive during the session.
- [ ] Track whether working copy has unsaved changes.
- [ ] Allow session cleanup.
- [ ] Clean temporary data on normal exit.
- [ ] Gracefully handle stale temp sessions after crashes if necessary.

## Phase 3 — Inpaint GUI

### 3.1 Add top-level mode/tab

Existing:

```text
[ Generate ]
```

Becomes conceptually:

```text
[ Generate ] [ Inpaint ]
```

- [ ] Preserve Generate behavior.
- [ ] Both modes use the same loaded model/backend.
- [ ] Switching tabs does not reload model.

### 3.2 Source section

Inpaint UI:

```text
Beatmapset    example.osz       [Browse]
Difficulty   Expert ▼
Audio        song.mp3
Length       03:42
```

- [ ] Browse/open `.osz`.
- [ ] Select difficulty.
- [ ] Display useful metadata.
- [ ] Changing difficulty changes active working `.osu`.

### 3.3 Interval selection

Initial implementation:

```text
Start     01:23.450
End       01:31.200
```

Support:

- [ ] `MM:SS`
- [ ] `MM:SS.mmm`
- [ ] optionally raw seconds

Validation:

- [ ] start ≥ 0
- [ ] end > start
- [ ] end ≤ sensible map/audio boundary
- [ ] reject empty intervals
- [ ] normalize entered timestamps for display

Do **not** build a fancy timeline yet.

### 3.4 Generation parameters

Expose parameters that actually make sense when replacing part of an existing map.

Primary controls:

- [ ] descriptors
- [ ] target difficulty
- [ ] mapper/style conditioning where supported
- [ ] year where supported
- [ ] seed
- [ ] timing context
- [ ] hitsound behavior

Advanced:

- [ ] temperature
- [ ] CFG
- [ ] lookback
- [ ] lookahead
- [ ] other meaningful inference controls already provided by Mapperatorinator

Avoid duplicating values that should naturally come from the existing beatmap.

### 3.5 Inherited properties

Display where useful, but default to reference map values:

- [ ] mode
- [ ] CS
- [ ] AR
- [ ] OD
- [ ] HP
- [ ] slider multiplier
- [ ] audio
- [ ] metadata
- [ ] existing timing

Clearly distinguish:

**Inherited from map** vs. **Generation conditioning**.

## Phase 4 — Inpainting execution

### 4.1 Build inference request

Translate UI state into existing Mapperatorinator configuration.

Conceptually:

```text
beatmap_path = working_copy.osu
start_time = selected_start
end_time = selected_end
add_to_beatmap = true
...
```

- [ ] Do not shell out to another full Mapperatorinator instance if avoidable.
- [ ] Call existing inference components directly/shared through the application's normal backend.
- [ ] Reuse already-loaded model.

### 4.2 Safe generation transaction

Before generation:

- [ ] Snapshot current working `.osu`.

Generate replacement.

On success:

- [ ] Validate output parses.
- [ ] Commit new version to working copy.
- [ ] Mark session dirty.
- [ ] Refresh UI metadata where necessary.

On failure:

- [ ] Restore previous valid `.osu`.
- [ ] Surface useful error.
- [ ] Do not destroy previous successful generation.

### 4.3 Boundary behavior

Test deliberately:

- [ ] circle immediately before start
- [ ] circle immediately after end
- [ ] slider beginning before start and ending inside selection
- [ ] slider beginning inside and ending after selection
- [ ] spinner crossing boundary
- [ ] timing point inside selection
- [ ] inherited timing
- [ ] kiai/effect points
- [ ] new combo state
- [ ] hitsound/sample state

Document what Mapperatorinator currently does rather than silently inventing behavior.

If behavior is undesirable, file it as a separate improvement rather than contaminating the initial implementation.

## Phase 5 — Iterative workflow

This is where the feature becomes worthwhile.

### 5.1 Regenerate repeatedly

The expected workflow must be:

```text
select interval
     ↓
set descriptors/settings
     ↓
GENERATE
     ↓
inspect
     ↓
change seed/settings
     ↓
GENERATE AGAIN
```

No:

- reopening `.osz`
- reloading model
- reselecting difficulty
- reentering interval
- reextracting files

### 5.2 Undo

Implement at minimum:

```text
Generation 0 — original
Generation 1
Generation 2
Generation 3
```

- [ ] Undo last regeneration.
- [ ] Redo if reasonably easy.
- [ ] Keep snapshots session-local.
- [ ] Don't copy audio/assets for every revision; only version the `.osu`.

This is high-value. Don't leave it until the end.

### 5.3 Seed workflow

- [ ] Random seed button.
- [ ] Display actual seed used.
- [ ] Preserve seed after generation.
- [ ] Allow exact regeneration with same seed/settings.
- [ ] Record seed with revision metadata if possible.

### 5.4 Generation history

Nice intermediate structure:

```text
Revision 4
Range: 01:23.450–01:31.200
Seed: 38271942
Descriptors: stream, intense
Difficulty: 6.2
```

Doesn't initially need a fancy UI, but store enough information that one can be added later.

## Phase 6 — Save/export

### 6.1 Export `.osz`

When requested:

- [ ] Package current working directory.
- [ ] Include all original assets.
- [ ] Include modified `.osu`.
- [ ] Preserve other difficulties unchanged.
- [ ] Produce valid `.osz`.
- [ ] Avoid accidentally nesting the session directory inside archive.

Correct:

```text
map.osz
├── audio.mp3
├── bg.jpg
└── difficulty.osu
```

Wrong:

```text
map.osz
└── session-123/
    ├── audio.mp3
    └── difficulty.osu
```

### 6.2 Save As

- [ ] Default to a new filename.
- [ ] Don't overwrite original unless explicitly requested.
- [ ] Remember sensible output directory during session.

### 6.3 Dirty-state protection

On closing/opening another map with unsaved generations:

- [ ] Warn about unsaved changes.
- [ ] Save / Discard / Cancel.

## Phase 7 — Preview integration abstraction

**Do not integrate Danser yet.**

First create the interface that a viewer will eventually use.

### 7.1 Preview command

Have one conceptual operation:

```text
preview(
    beatmap_path,
    start_time,
    end_time
)
```

For now it can:

- [ ] reveal working directory, or
- [ ] invoke a configurable external command, or
- [ ] simply remain unavailable until viewer integration.

The important part is that Inpaint code doesn't directly depend on Danser internals.

### 7.2 Preview interval

Store sensible preview padding separately from regeneration range:

```text
regeneration:
01:23.450 ───────── 01:31.200

preview:
01:20.450 ───────────────── 01:34.200
```

Default perhaps ±3 seconds.

This lets you judge **transitions**, not merely the generated objects.

## Phase 8 — Danser integration

Only start this after Phases 1–6 work reliably.

### 8.1 Investigate Danser input behavior

Determine experimentally:

- [ ] Can Danser directly open arbitrary loose `.osu`?
- [ ] If not, how does its beatmap scanner work?
- [ ] Can a session directory be added to its song sources?
- [ ] Can changed `.osu` files be detected without restarting Danser?
- [ ] Can the currently selected difficulty be addressed reliably?
- [ ] Startup latency.
- [ ] Rescan latency.
- [ ] Whether an already-running Danser instance can be reused.

**Do not design around assumptions here. Test it.**

### 8.2 Basic Preview button

Desired behavior:

```text
[ Preview ]
```

launches active difficulty around:

```text
selected_start - padding
        ↓
selected_end + padding
```

### 8.3 Automatic preview

Once basic preview is reliable:

```text
☑ Preview after generation
```

Flow becomes:

```text
Regenerate
   ↓
generation completes
   ↓
viewer reloads
   ↓
playback begins just before changed section
```

That's the payoff.

## Phase 9 — Better interval selection

Only after basic start/end fields work.

### 9.1 Timeline

Display a simple song timeline.

Potentially show hitobject density:

```text
0:00                                         3:42
│──────────────────────────────────────────────│
   ▂▃▂▅▇▇▃▂   ▂▄▇████▆▃    ▂▂▅▇▅▃
                    [██████]
                    selection
```

- [ ] Seek position.
- [ ] Drag range.
- [ ] Numeric start/end stay synchronized.

### 9.2 Keyboard range selection

Useful workflow:

```text
[ = selection start
] = selection end
```

Potential additional shortcuts:

```text
Space = play/pause
R     = regenerate
Ctrl+Z = undo
Ctrl+Shift+Z = redo
```

Don't hijack keys while typing into text fields.

## Phase 10 — Generate → Inpaint handoff

This should eventually eliminate `.osz` round-tripping entirely for maps created in the same session.

After normal generation:

```text
Generation complete

[ Export ] [ Open in Inpaint ]
```

`Open in Inpaint`:

- [ ] Reuse existing generated working files.
- [ ] Switch tab.
- [ ] Select generated difficulty.
- [ ] Populate inherited parameters.
- [ ] Do not reload model.
- [ ] Do not export then re-extract an `.osz`.

## Phase 11 — Reliability tests

Build a small test corpus containing:

- [ ] short map
- [ ] long map
- [ ] Unicode metadata/filenames
- [ ] multiple difficulties
- [ ] lots of custom samples
- [ ] background/video assets
- [ ] unusual timing sections
- [ ] BPM changes
- [ ] inherited timing
- [ ] sliders crossing selected boundaries
- [ ] spinner boundaries
- [ ] map with missing optional assets
- [ ] malformed `.osz`
- [ ] `.osz` containing nested directories

For every test:

```text
open
→ select difficulty
→ regenerate
→ undo
→ regenerate again
→ export
→ reopen exported .osz
```

No corruption.

## Phase 12 — Polish

Only after the workflow works.

- [ ] Progress indicator during inference.
- [ ] Cancel generation if Mapperatorinator supports safe cancellation.
- [ ] Clear model-loading status.
- [ ] Useful errors rather than Python tracebacks in UI.
- [ ] Persist harmless UI preferences.
- [ ] Recent files.
- [ ] Remember last descriptor/settings values where appropriate.
- [ ] Tooltips for obscure inference parameters.
- [ ] Disable impossible actions rather than allowing them to fail.
- [ ] Clean shutdown of model/backend/viewer.
- [ ] Proper temporary-directory cleanup.

## Milestones

Treat these as hard stopping points rather than trying to implement the entire backlog in one pass.

**M1 — Prove inference:** Existing `.osu` → specify interval → Mapperatorinator replaces interval correctly.

**M2 — Prove `.osz` workflow:** Open `.osz` → extract → choose difficulty → regenerate → export valid modified `.osz`.

**M3 — Usable Inpaint tab:** All important conditioning controls exposed, model shared with Generate, repeated regeneration works.

**M4 — Safe iteration:** Undo/redo, seeds, generation history, dirty-state handling.

**M5 — Preview:** Danser launches reliably at the regenerated section.

**M6 — Fast workflow:** automatic preview/reload, timeline selection, keyboard shortcuts.

**M7 — Unified workflow:** Generate → Open in Inpaint → iterate → Export.

The **M2/M3 boundary is an intentional stopping point for real-world use and feedback**. Do not proceed directly into timeline and Danser work before learning from the basic inpainting workflow.

