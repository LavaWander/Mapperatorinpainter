# M2 — `.osz` workflow proof

**Status:** Complete on 2026-08-29. This is a hard stopping point; M3 has not started.

## Outcome

The new workflow successfully performed:

```text
open source.osz
  → validate and safely extract once
  → discover two difficulties
  → select Expert.osu
  → resolve nested audio/background
  → regenerate 2000–4000 ms through existing v32 inference
  → validate and commit working .osu
  → export complete modified.osz
  → reopen and validate modified.osz
```

The immutable source archive retained this SHA-256 before and after the workflow:

```text
3E0FADFD32930BB8B74F2298F286F974FC865A37F64E9C85CA010D9F43E7F427
```

## Implementation

### Session/archive layer

`Mapperatorinator/inpainting/session.py` provides `BeatmapsetSession`:

- validates `.osz`/zip readability;
- creates a unique `mapperatorinator/session-*` working directory;
- extracts each archive member once without `extractall()`;
- rejects absolute, traversal, drive-qualified, alternate-stream, duplicate, and symbolic-link entries;
- preserves nested directories and file bytes;
- discovers and parses every `.osu` difficulty;
- exposes version, mode, mapper, AR, OD, CS, HP, map-derived length, and supported status;
- selects a difficulty by archive-relative path;
- resolves Unicode and relative audio/background paths while enforcing the session boundary;
- tracks dirty state and source SHA-256;
- cleans the owned temporary directory on context exit;
- atomically exports all working files with archive-root-relative names;
- refuses to overwrite the immutable source archive.

### Inference transaction layer

`Mapperatorinator/inpainting/workflow.py`:

- validates the interval;
- clones the caller's existing `InferenceConfig`;
- points `beatmap_path`, audio, and output at the working session;
- sets the proven partial-remap flags;
- accepts an injected inference runner so the GUI can later supply its shared backend;
- snapshots only the active `.osu`;
- validates the resulting `.osu` with the existing `slider` parser;
- marks the session dirty only after success;
- atomically restores the previous valid file after inference or validation failure.

There is no second Mapperatorinator implementation or subprocess. The manual proof calls the existing `inference.main` function directly. M3 can inject the GUI-owned inference-server path without changing session ownership.

## Automated verification

Command:

```powershell
python -m unittest discover -s tests -v
```

Result: 12 tests passed (3 M1 and 9 M2).

M2 coverage includes:

- malformed/non-zip archive;
- archive with no difficulties;
- zip-slip with forward and backward separators;
- absolute/drive/alternate-stream member names;
- duplicate archive paths;
- symbolic-link entries;
- nested directories and Unicode audio filename;
- metadata discovery and supported-mode selection;
- missing and out-of-session assets;
- source immutability;
- success transaction and dirty state;
- rollback after runner failure;
- rollback after invalid generated `.osu`;
- empty/reversed/negative intervals;
- preservation of every non-active file;
- no accidental session-directory nesting;
- reopening the exported archive.

## Model-backed round-trip

The committed manual harness is:

```text
Mapperatorinator/tests/manual_m2_proof.py
```

Proof command:

```powershell
python tests/manual_m2_proof.py `
  test/m2-proof/source.osz `
  test/m2-proof/modified.osz `
  --difficulty Expert.osu `
  --start-time 2000 `
  --end-time 4000 `
  --seed 12345 `
  --model v32
```

The proof archive contained:

```text
Expert.osu
maps/Easy.osu
assets/audio.wav
images/bg.jpg
samples/custom-hit.wav
video/story.mp4
nested/unknown.bin
```

The exported archive contained the same member set at archive root. All six files other than `Expert.osu` were byte-identical. `Expert.osu` changed and parsed with object start times:

```text
900, 1200, 1500, 3000, 3400, 3800, 4100 ms
```

The reopened export discovered both `Expert.osu` and `maps/Easy.osu`, resolved `assets/audio.wav` and `images/bg.jpg`, and passed zip CRC validation. The temporary session directory was removed normally.

## Scope boundary

M2 deliberately does not include:

- an Inpaint GUI tab;
- repeated regeneration UI;
- shared-server GUI wiring;
- undo/redo or revision history;
- dirty-close prompts or Save As dialogs;
- stale-session recovery after crashes;
- preview or Danser integration.

Those remain in M3 and later milestones. The backend boundary is now ready for the M2/M3 usage checkpoint.
