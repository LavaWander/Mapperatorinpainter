# M5 — Danser interval preview

## Result

M5 is complete. The Inpaint tab can launch the active osu!standard difficulty
in Danser 0.11.0 around the current regeneration range. Preview padding defaults
to three seconds before and after and is stored separately from the regeneration
interval, so previewing never changes what the model replaces.

```text
selected .osu + interval + preview padding
                    ↓
viewer-neutral Previewer interface
                    ↓
isolated Danser song source (active difficulty only)
                    ↓
exact MD5 selection + -start/-end seeker
```

Automatic preview after regeneration is intentionally not part of M5; it remains
an M6 item.

## Danser 0.11.0 findings

The integration was based on the tagged 0.11.0 source and verified against the
official Windows release and a real multi-difficulty `.osz`.

- Danser has no CLI option for opening an arbitrary loose `.osu` path. It scans
  `.osu` files under child directories of `General.OsuSongsDir` into `danser.db`.
- A normal database scan detects a changed `.osu` using its modification time.
  `-nodbcheck` skips already-known mapset directories, so it must not be used for
  iterative inpainting previews.
- `-md5` reliably selects one difficulty, but identical copies in multiple
  session directories can collide. Mapperatorinpainter therefore creates a
  private Songs root containing only the active difficulty for each session.
- Assets are hard-linked into the private staging directory when possible and
  copied as a fallback. Only the active `.osu` is copied on every launch. The
  source `.osz`, working `.osu`, other difficulties, and assets are never edited
  by the previewer.
- `-start` and `-end` are seconds and provide the required seeker range. Danser
  fades/stops playback at the requested end but leaves its window open; pressing
  Escape closes it. Launching another preview terminates the earlier preview
  process first.
- Danser 0.11.0 exposes no supported IPC for retargeting an already-running
  instance. M5 uses one process per preview and retains the staged source across
  repeated launches in the same Inpaint session.
- Danser preview is available only for osu!standard (mode 0), matching Danser's
  supported visualization ruleset.
- On the development machine, a one-difficulty staged source scanned quickly and
  reached playback in roughly a few seconds; first-run graphics initialization
  dominates the latency.

## Isolation and installation

Danser stores its database beside a portable Windows executable. Pointing a
regular Danser installation at a temporary source could therefore rewrite that
installation's database. Mapperatorinpainter deliberately auto-detects only a
dedicated copy at:

```text
.tools/danser-0.11.0/danser-cli.exe
```

`Install Danser Preview.bat` downloads the official pinned Windows archive,
verifies SHA-256
`749B2E66E36C3E2217910923802F08DE9BC1C0858FCB6FFAE861A6787FB21EEE`, and extracts
it into the ignored `.tools` directory. An advanced user can instead set
`MAPPERATORINPAINTER_DANSER` to a dedicated Danser CLI path or directory.

Each launch uses a separate `mapperatorinpainter` settings profile and a runtime
settings patch that selects the private Songs root, disables update checks and
Discord presence, uses a 960×540 window, removes the long lead-in/seizure warning,
and hides the results screen. Inpaint session close and application shutdown both
terminate the owned preview and remove its staging directory.

## Verification

- Adapter unit tests cover exact start/end arguments, MD5 selection, exclusion of
  other `.osu` difficulties, Unicode/nested assets, settings isolation, changed
  map MD5 refresh, previous-process termination, and staging cleanup.
- Python compilation and JavaScript syntax checks pass.
- The official Danser 0.11.0 Windows build was launched against
  `UNDEAD CORPORATION - Everything will freeze (Ekoro) [Lunatic]` from an isolated
  session. Danser selected that exact staged path, loaded its audio/storyboard,
  and opened the requested 0–5 second interval configuration.
- The full Mapperatorinator suite could not be rerun in this shell because the
  project Python environment used for M4 is no longer present on `PATH`; the new
  dependency-free adapter tests ran successfully with the bundled Python runtime.

