# Mapperatorinpainter portable testing release

## One user-facing package

Mapperatorinpainter has one distribution for testing:

```text
Mapperatorinpainter-<version>-Portable.zip
```

The user extracts it and runs `Run Mapperatorinpainter.bat`. It does not use a
system Python installation or an existing Mapperatorinator checkout.

```text
Mapperatorinpainter/
├── Run Mapperatorinpainter.bat
├── Install Danser Preview.bat
├── .portable-install.json
├── Mapperatorinator/          application files replaced by updates
├── portable/                  update/dependency preparation
├── runtime/
│   ├── python.exe             embedded Python 3.10
│   ├── Lib/site-packages/     PyTorch and application dependencies
│   └── ffmpeg/bin/            portable FFmpeg
├── model_cache/               retained across application updates
├── .tools/                    optional Danser installation
└── inpaint_output/            retained user output
```

The runtime contains PyTorch 2.10 with CUDA 13.0 libraries. The target computer
still needs a compatible 64-bit Windows installation and NVIDIA driver, but it
does not need Python, Git, FFmpeg, the CUDA Toolkit, or Mapperatorinator to be
installed globally.

## Startup and updates

On every portable launch, the launcher:

1. checks non-draft releases at `LavaWander/Mapperatorinpainter` (including
   testing prereleases);
2. asks before installing a newer release;
3. downloads GitHub's source archive for that release;
4. transactionally replaces the `Mapperatorinator` application directory and
   rolls it back if replacement fails;
5. retains `runtime`, `model_cache`, `.tools`, `inpaint_output`, and prior logs;
6. verifies the private Python dependency fingerprint and `pip check` result;
7. installs or repairs libraries inside `runtime` when needed; and
8. starts the application with the portable Python and FFmpeg paths.

An unavailable update check is non-fatal. The installed release starts normally.
There is no `Update.bat` and no separately named update download.

Ordinary application releases do not require rebuilding or freezing an
executable. Publishing a GitHub release makes its source archive available to
the launcher. Rebuild the large portable package only when Python, PyTorch,
FFmpeg, or another bundled runtime dependency changes. The existing base
portable download remains usable because it updates itself before launch.

## Danser

Danser is not a separate download. On the first portable launch without a
complete Danser runtime, the launcher asks whether to run the included verified
`Install Danser Preview.bat`.

If the user declines, the embedded preview remains fully functional. Clicking
the high-fidelity Danser action reports:

```text
Danser 0.11.0 preview is not available. Run 'Install Danser Preview.bat', then
restart Mapperatorinpainter.
```

The installer refuses to operate outside a portable release. It installs only
to `.tools/danser-0.11.0`, recognizes an already complete installation, and
checks the official archive's pinned SHA-256 before extraction.

## Building the base portable package

The maintainer build script downloads the official Python 3.10.11 embedded
runtime, installs the pinned CUDA 13.0 PyTorch and Mapperatorinpainter packages,
adds a portable FFmpeg build, writes the release marker, and creates the zip.

From PowerShell at the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\packaging\Build-Portable.ps1 `
  -Version v0.1.0-testing
```

The result is written to `dist/`, which is ignored by Git. Building requires an
internet connection and substantial free disk space. It is intentionally a
maintainer operation, not something the user runs.

### Current local proof build

The first testing artifact was built and launcher-smoked on 2026-09-01:

```text
dist/Mapperatorinpainter-0.1.0-testing-Portable.zip
Size:    2,476,011,801 bytes (2.306 GiB)
SHA-256: 586199FFD038C718581471ABC19FDB95FEAF721D952AB3501C0DCC1C9989E548
```

The extracted runtime passed dependency preparation, `pip check`, imports of
Torch/Torchaudio/Transformers/Flask/slider/rosu-pp/pywebview, the Qt 5 WebEngine
backend, bundled FFmpeg execution, and an actual hidden launch through
`Run Mapperatorinpainter.bat`. It reports PyTorch `2.10.0+cu130` and CUDA runtime
`13.0`. The test process tree was stopped after startup; model inference and a
second physical Windows machine remain manual release acceptance checks.

## Release discipline

- The version passed to the build script should equal its GitHub release tag.
- Publish application changes as a GitHub release so portable installations see
  them; a pushed commit alone is not treated as a release.
- When `portable/portable-requirements.txt`, Python, PyTorch, or FFmpeg changes,
  build and smoke-test a new base portable archive.
- Test extraction and first launch on a clean Windows account before linking a
  portable archive publicly.
