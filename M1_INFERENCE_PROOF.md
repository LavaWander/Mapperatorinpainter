# M1 — Existing partial-inference proof

**Status:** Complete on 2026-08-29. This is a hard stopping point; M2 has not started.

## Outcome

Mapperatorinator v32 successfully replaced a known interval in an existing `.osu` through the unmodified inference backend. The output parsed successfully, objects outside the interval were preserved, and the reference file was not modified.

No production inference or GUI code was changed for M1. The only code added is focused characterization coverage under `Mapperatorinator/tests/`.

## Environment

- Windows, Python 3.10.9
- PyTorch 2.10.0+cu130
- Transformers 4.57.3
- NVIDIA GeForce RTX 3060, 12 GB
- Locally cached `OliBomby/Mapperatorinator-v32/gamemode=0` checkpoint

## Existing execution path

```text
GUI / Hydra CLI
  ↓
InferenceConfig + compile_args()
  ↓
Preprocessor.load() / segment()
  ↓
Processor.generate()
  ↓
Postprocessor.generate() / add_to_beatmap()
  ↓
write_result() / export_osz()
```

The desktop GUI already keeps a long-lived inference server and uses per-job workers as clients. That server is the shared-model backend the future Inpaint workflow should reuse.

## Characterization tests

Command:

```powershell
python -m unittest discover -s tests -v
```

Result: 3 tests passed.

The tests establish that `Postprocessor.add_to_beatmap()`:

- returns a parseable beatmap without writing the reference file;
- treats both interval boundaries as inclusive;
- selects hit objects by their start time;
- retains sliders/spinners starting before the interval even when they end inside it;
- replaces objects starting inside the interval even when they end after it;
- replaces timing/effect points by offset inside the interval;
- sorts merged objects and timing points;
- adds a timing-state reconciliation point at the interval start when needed.

Fixtures:

- `Mapperatorinator/tests/fixtures/partial_reference.osu`
- `Mapperatorinator/tests/fixtures/partial_generated.osu`

Test module:

- `Mapperatorinator/tests/test_partial_inpainting_backend.py`

## Model-backed proof

The proof used a disposable copy of the reference fixture, a generated 12-second WAV, and this existing CLI path:

```powershell
python inference.py `
  beatmap_path=test/m1-proof/working.osu `
  output_path=test/m1-proof `
  start_time=2000 `
  end_time=4000 `
  add_to_beatmap=true `
  overwrite_reference_beatmap=false `
  export_osz=false `
  seed=12345 `
  in_context=[TIMING]
```

Observed configuration derivation included mode 0, calculated difficulty 4.21, HP 5, CS 4, OD 8, AR 9, slider multiplier 1.4, slider tick rate 1, BPM 150, metadata, and detected hitsounded status.

Reference SHA-256 before and after inference:

```text
C13A8E98690106386C1288EA923D329DDFD23477CC900DB9A8C845CDC304C033
```

Reference objects inside 2000–4000 ms:

```text
Circle 2000
Circle 3000
Slider 3500
Spinner 3800
Circle 4000
```

Generated/merged objects inside 2000–4000 ms:

```text
Circle 3000
Circle 3400
Circle 3800
```

Objects outside the interval were identical before and after:

```text
Circle 900
Spinner 1200–2500
Slider 1500–3286 (approximately; duration is timing-derived)
Circle 4100
```

The output contained sorted timing offsets at 0, 1000, 3000, 3400, and 4100 ms and parsed successfully with the same `slider` library used by Mapperatorinator.

## Parameter conclusions for Inpaint

### Required workflow values

- working-copy `beatmap_path`;
- valid `start_time` and `end_time` in milliseconds;
- `add_to_beatmap=true`;
- `overwrite_reference_beatmap` should target only a transaction-protected working copy;
- an audio file resolvable from `AudioFilename` or supplied explicitly.

The current backend technically permits missing/one-sided start/end values and does not fully validate interval ordering or audio bounds. The Inpaint workflow must validate those before inference.

### Naturally inherited from the reference

- audio path and output directory when omitted;
- mode and beatmap ID;
- calculated star difficulty;
- HP, CS, OD, AR;
- slider multiplier/tick rate;
- detected hitsounded status;
- mode-specific key/hold/scroll values;
- BPM/offset, metadata, background, and preview time.

Year is not derivable. Mapper/descriptors are not automatically inherited into the main conditioning because the initial beatmap compilation does not receive the loaded tokenizer.

### Appropriate Inpaint controls

- descriptors and negative descriptors;
- target difficulty;
- mapper/style ID and year where supported;
- seed;
- timing context and model-supported hitsound behavior;
- temperature and CFG;
- advanced lookback/lookahead and existing sampler controls.

Mode, difficulty settings, slider settings, audio, metadata, and timing should normally remain inherited unless a later validated use case requires an override.

## Existing mutation and export behavior

- `add_to_beatmap=true, overwrite_reference_beatmap=false` writes a UUID-named result and leaves the reference untouched.
- `add_to_beatmap=true, overwrite_reference_beatmap=true, export_osz=false` writes directly to the reference path and is not transactional.
- Current `.osz` export packages only the generated `.osu`, audio, and optional background. It does not preserve an existing beatmapset.

These findings support the backlog design: M2 needs a separate working-copy/session transaction around the existing backend, not a replacement inference implementation.
