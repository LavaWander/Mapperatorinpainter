# M7 — Unified Generate-to-Inpaint workflow

## Result

M7 is complete. Every successful full-map Generate job now offers **Open in
Inpaint** on its completed progress card.

```text
normal Generate inference
          │
          ├── normal configured output (unchanged)
          │
          └── generated .osu + referenced audio/background
                         ↓
             job-owned session workspace
                         ↓
                  Open in Inpaint
                         ↓
          existing Inpaint session/revision workflow
```

The handoff uses the structured return value from the existing inference entry
point; it does not scrape logs, export an intermediate `.osz`, or re-extract an
archive. The job workspace becomes Revision 0 of a normal clean Inpaint session,
so regeneration, preview, undo/redo, automatic output, manual export, and dirty
state protection continue through the implementations built for M2–M6.

## State transferred

- Generated difficulty and inherited beatmap metadata/assets.
- Model selection, seed, target difficulty, positive/negative descriptors,
  mapper ID, and year.
- Temperature, CFG, top-p, lookback, lookahead, LoRA, timing context, and
  hitsound conditioning.
- A default editable interval of the first ten seconds (or the map length when
  shorter).

The model is not reloaded during handoff. Subsequent Inpaint generation follows
the existing shared inference-server path and therefore reuses the already
loaded compatible backend.

## Existing source workflows

The external source controls remain intact. Inpaint can still open either an
arbitrary `.osz` file or an osu! song folder; both continue to create isolated
working copies and retain source-file protection.

## Lifecycle and failure behavior

- Closing a completed Generate card without opening it discards its temporary
  handoff workspace.
- Application shutdown cleans pending and running handoff workspaces.
- Once adopted, the normal Inpaint session cleanup owns the workspace.
- Asset paths are traversal-checked before audio or background files are copied.
- Failure to prepare a handoff reports that **Open in Inpaint** is unavailable
  without changing a successful normal Generate result into a failed job.

## Verification

- Complete Python regression suite: 46 tests passing.
- Focused handoff/web suite: 17 tests passing.
- Main UI JavaScript syntax check passes.
- `git diff --check` passes.
- Tests verify direct workspace adoption, clean Revision 0 state, provenance
  transfer, safe asset paths, workspace disposal, external `.osz` opening, and
  external song-folder opening.

## Manual acceptance path

1. Run `Run Mapperatorinpainter.bat` and generate a full map normally.
2. Confirm its normal Generate output is still written as configured.
3. Click **Open in Inpaint** on the completed job card.
4. Confirm the Inpaint tab opens the generated difficulty with a 00:00–00:10
   interval and the generation controls populated.
5. Regenerate an interval and use Preview, undo/redo, and export.
6. Separately use the Inpaint source picker to open an external `.osz` and an
   external song folder.
