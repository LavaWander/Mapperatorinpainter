# Mapperatorinator codebase orientation

This note records the initial read-only inspection of the Mapperatorinator build copied into this project. It is not an M1 completion report: the partial-generation acceptance test has not yet been run.

## Repository layout

- `Mapperatorinator/web-ui.py` — Flask + pywebview desktop GUI backend and application entry point.
- `Mapperatorinator/template/index.html` — current single Generate form.
- `Mapperatorinator/static/app.js` — form behavior, validation/autofill, descriptor selection, job progress, cancellation, and config import/export.
- `Mapperatorinator/config.py` and `Mapperatorinator/configs/inference/*.yaml` — typed inference configuration and model-version defaults.
- `Mapperatorinator/inference.py` — argument compilation, model/client setup, inference orchestration, and output writing.
- `Mapperatorinator/osuT5/osuT5/inference/preprocessor.py` — audio loading and window segmentation.
- `Mapperatorinator/osuT5/osuT5/inference/processor.py` — reference parsing, conditioning/context construction, model decoding, and interval trimming.
- `Mapperatorinator/osuT5/osuT5/inference/postprocessor.py` — event-to-`.osu` conversion, partial merge, file writing, and the current minimal `.osz` export.
- `Mapperatorinator/osuT5/osuT5/dataset/osu_parser.py` — conversion of reference beatmaps into model events, including compound objects, timing, kiai, SV, and hitsound state.

The copied `Mapperatorinator/` directory originally contained Git metadata from its upstream clone. That nested `.git` directory was removed after confirming that `Mapperatorinator-Extended` is authoritative. The outer repository can now track the imported source files directly; the upstream history remains available from the original GitHub repository.

## Current full-generation path

```text
pywebview window
  → template/index.html + static/app.js
  → POST /start_inference in web-ui.py
  → Hydra model config converted to InferenceConfig
  → compile_args() validates/derives paths and metadata
  → web-ui-owned InferenceClient server is ensured
  → per-job multiprocessing worker calls inference.main(cfg)
  → worker connects to the already-running model server
  → Preprocessor.load()/segment()
  → Processor.generate()
  → optional diffusion position generation
  → Postprocessor.generate()
  → Postprocessor.write_result() or export_osz()
  → .osu or minimal .osz
```

The primary model is already designed to stay loaded across GUI jobs. `web-ui.py` owns `InferenceClient` instances keyed by model server address; job workers use `cfg.use_server = True` and connect to those servers. Inpainting should reuse this ownership and job mechanism instead of starting another Mapperatorinator process or loading another primary model.

One caveat: optional diffusion position models are loaded inside each job's `inference.main()`. V32 currently sets `generate_positions: false`, so its normal path does not do this.

## GUI-to-configuration mapping

`POST /start_inference` composes the selected Hydra config, converts it to `InferenceConfig`, then replaces fields with submitted form values. The existing form already includes:

- `.osu`, audio, output, LoRA, and background paths;
- model, mode, difficulty, year, mapper ID, descriptors, and negative descriptors;
- inherited/overridable beatmap metadata and difficulty settings;
- seed, CFG scale, temperature, and top-p;
- start/end times in raw milliseconds;
- `add_to_beatmap`, `overwrite_reference_beatmap`, `.osz` export, hitsounded conditioning, super timing, and supported input contexts.

The form does not currently expose lookback/lookahead or the other lower-level sampler/timing controls present in `InferenceConfig`.

## Reference-map derivation

When `beatmap_path` is provided, `compile_paths()` parses it and derives the audio path from `AudioFilename` and the output directory from the `.osu` parent directory when those paths were omitted.

`compile_args_from_beatmap()` fills only configuration values that the caller left as `None`. It derives:

- mode, beatmap ID, calculated star difficulty;
- HP, CS, OD, AR, slider multiplier, and slider tick rate;
- detected hitsounded status;
- mania key count/hold ratio and applicable scroll-speed ratio;
- BPM/offset, metadata, background, and preview time.

Year is not derived. Although `generation_config_from_beatmap()` can derive mapper/descriptors when given a tokenizer, `compile_args_from_beatmap()` currently calls it without one, so mapper/style descriptors are not automatically inherited into the main generation conditioning at that stage.

Difficulty is recommended but not structurally required by the config; a reference map normally supplies it. Seed is optional and becomes a random integer in the inclusive implementation range beginning at zero and ending at `2**16` when omitted.

## Existing partial-remapping behavior

The documented invocation is conceptually:

```text
beatmap_path=<reference.osu>
start_time=<milliseconds>
end_time=<milliseconds>
add_to_beatmap=true
```

`start_time` and `end_time` are optional integer milliseconds. There is currently no explicit validation that both are present, ordered, non-negative, or within the audio duration. The backend also does not explicitly validate that `add_to_beatmap` has a reference path; downstream code assumes one.

Partial inference reuses the normal pipeline:

1. `Preprocessor.segment()` keeps only audio windows needed around the interval, accounting for model lookback/lookahead.
2. `Processor` supplies reference events before the start as partial output context, trims generated events to roughly the selected interval with 10 ms leniency, and appends reference events outside the interval.
3. `Postprocessor.generate()` constructs a complete `.osu` string.
4. `Postprocessor.add_to_beatmap()` parses that result and the original reference, then performs the final merge.

The final merge uses inclusive interval membership by **object start time** and timing-point offset:

- original hit objects are removed when `start <= object.time <= end`;
- generated hit objects are added when `start <= object.time <= end`;
- original timing points are removed when `start <= timing.offset <= end`;
- generated timing points are added in the same interval;
- hit objects and timing points are then sorted;
- timing/SV/volume/kiai state is reconciled at the interval start when possible.

Consequences to verify experimentally in M1:

- A slider or spinner that starts before the interval is retained by the final merge even if it ends inside the interval.
- An object that starts inside the interval is replaced/removed even if it ends after the interval.
- Event-level trimming can create incomplete generated compound objects at boundaries; the postprocessor generally warns about or drops incomplete compound objects. Exact slider/spinner/hold behavior needs the requested boundary corpus rather than assumption.
- Timing-point parent relationships and state restoration around both boundaries need explicit tests.

The reference file is only mutated when both `add_to_beatmap` and `overwrite_reference_beatmap` are true and normal `.osu` output is selected. Otherwise a UUID-named `.osu` is written to the output directory. This existing overwrite path is not transactional.

## Timing, context, and hitsounds

Available context types are `NONE`, `TIMING`, `KIAI`, `MAP`, `GD`, `NO_HS`, and (internally/model-dependently) `SV`. Model-version capability rules in the JavaScript hide or disable unsupported choices; V32's current UI advertises only `TIMING` as selectable input context.

The parser and postprocessor already represent timing points, kiai, SV, new combos, hitsounds, sample sets/additions, and volume. The `hitsounded` field is conditioning, while the `NO_HS` context is the specialized hitsound-generation route on models that support it. A future Inpaint hitsound control should map onto these existing semantics rather than introduce a separate hitsound merger.

## Current output and `.osz` behavior

`Postprocessor.write_result()` writes UTF-8 with BOM. With `overwrite_reference_beatmap=true`, it writes directly to the reference path; otherwise output uses a generated UUID filename.

The current `export_osz()` creates a new zip containing only:

- one generated `.osu` string;
- the selected audio file at archive root;
- the optional background at archive root.

It does not open an existing `.osz`, retain other difficulties, preserve videos/custom samples/unknown assets, or reproduce nested directories. Phase 2/6 session packaging therefore needs a beatmapset-preserving workflow layer, kept separate from inference as specified in the backlog.

## Natural extension seams

1. Add `.osz` session ownership, extraction, revision snapshots, dirty state, and export as a separate workflow module.
2. Keep the active difficulty as a working `.osu` path and build the existing `InferenceConfig` partial-remap request from it.
3. Submit inpainting through the same web-UI-owned model server and job/progress infrastructure.
4. Wrap the existing direct-overwrite path in session-level snapshot/validate/commit/restore semantics; never point it at the source archive.
5. Keep preview behind its own interface and out of `inference.py`.

## Known gaps before M1 can be declared complete

- No representative `.osu` test fixture or model-backed partial inference has been run yet.
- Exact compound-object boundary behavior has only been traced statically.
- Exact timing/effect-point behavior needs a deliberately constructed fixture.
- The existing tests are mostly training/model scripts; no focused automated coverage for `Postprocessor.add_to_beatmap()` or `.osz` preservation was found.
