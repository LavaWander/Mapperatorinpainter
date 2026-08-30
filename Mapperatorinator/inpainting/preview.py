from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


DANSER_VERSION = "0.11.0"
DANSER_PROFILE = "mapperatorinpainter"
DANSER_ENVIRONMENT_VARIABLE = "MAPPERATORINPAINTER_DANSER"


class PreviewError(ValueError):
    """Raised when a beatmap interval cannot be opened in a previewer."""


@dataclass(frozen=True)
class PreviewLaunch:
    """Details of one asynchronously launched preview."""

    beatmap_path: Path
    start_time: int
    end_time: int
    process_id: int
    viewer: str


class Previewer(Protocol):
    """Viewer-neutral interface used by the Inpaint workflow."""

    def preview(self, beatmap_path: str | Path, start_time: int, end_time: int) -> PreviewLaunch:
        ...

    def close(self) -> None:
        ...

    def stop(self) -> None:
        ...


def find_danser_executable(project_root: str | Path) -> Path | None:
    """Find the dedicated Danser CLI without consulting a normal Danser install."""
    configured = os.environ.get(DANSER_ENVIRONMENT_VARIABLE, "").strip()
    candidates = []
    if configured:
        configured_path = Path(configured).expanduser()
        candidates.append(
            configured_path / "danser-cli.exe" if configured_path.is_dir() else configured_path
        )

    root = Path(project_root).expanduser().resolve()
    candidates.extend((
        root / ".tools" / f"danser-{DANSER_VERSION}" / "danser-cli.exe",
        root / ".tools" / "danser" / "danser-cli.exe",
    ))
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


class DanserPreviewer:
    """Preview one working difficulty through an isolated Danser 0.11.0 song source.

    Danser does not accept a loose `.osu` path. Before each launch this adapter
    mirrors the beatmapset into a private Songs directory, includes only the
    active difficulty, and selects that exact file by its content MD5.
    """

    def __init__(
        self,
        *,
        executable: str | Path,
        beatmapset_root: str | Path,
        temp_root: str | Path | None = None,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.executable = Path(executable).expanduser().resolve()
        self.beatmapset_root = Path(beatmapset_root).expanduser().resolve()
        if not self.executable.is_file():
            raise PreviewError(f"Danser CLI was not found: {self.executable}")
        if not self.beatmapset_root.is_dir():
            raise PreviewError(f"Beatmapset working directory was not found: {self.beatmapset_root}")

        preview_parent = (
            Path(temp_root).expanduser().resolve()
            if temp_root is not None
            else Path(tempfile.gettempdir()) / "mapperatorinpainter-previews"
        )
        preview_parent.mkdir(parents=True, exist_ok=True)
        self.preview_root = Path(tempfile.mkdtemp(prefix="preview-", dir=preview_parent)).resolve()
        self.songs_root = self.preview_root / "Songs"
        self.mapset_root = self.songs_root / "working-mapset"
        self.log_path = self.preview_root / "danser-preview.log"
        self._process_factory = process_factory
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _validate_interval(start_time: int, end_time: int) -> None:
        if start_time < 0:
            raise PreviewError("Preview start time cannot be negative.")
        if end_time <= start_time:
            raise PreviewError("Preview end time must be after its start time.")

    @staticmethod
    def _md5(path: Path) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _link_or_copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    def _stage(self, beatmap_path: Path) -> Path:
        try:
            relative_beatmap = beatmap_path.relative_to(self.beatmapset_root)
        except ValueError as exc:
            raise PreviewError(f"Preview beatmap is outside the working beatmapset: {beatmap_path}") from exc
        if beatmap_path.suffix.lower() != ".osu" or not beatmap_path.is_file():
            raise PreviewError(f"Preview beatmap is not a readable .osu file: {beatmap_path}")

        if self.mapset_root.exists():
            shutil.rmtree(self.mapset_root)
        self.mapset_root.mkdir(parents=True)

        for source in self.beatmapset_root.rglob("*"):
            if not source.is_file() or source.suffix.lower() == ".osu":
                continue
            relative = source.relative_to(self.beatmapset_root)
            self._link_or_copy(source, self.mapset_root / relative)

        staged_beatmap = self.mapset_root / relative_beatmap
        staged_beatmap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(beatmap_path, staged_beatmap)
        return staged_beatmap

    def _ensure_profile(self) -> None:
        settings_directory = self.executable.parent / "settings"
        settings_directory.mkdir(parents=True, exist_ok=True)
        profile = settings_directory / f"{DANSER_PROFILE}.json"
        if not profile.exists():
            profile.write_text("{}\n", encoding="utf-8")

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def preview(self, beatmap_path: str | Path, start_time: int, end_time: int) -> PreviewLaunch:
        """Launch Danser at an exact millisecond interval and return immediately."""
        self._validate_interval(start_time, end_time)
        path = Path(beatmap_path).expanduser().resolve()

        with self._lock:
            if self._closed:
                raise PreviewError("This preview session is already closed.")
            self._stop_process()
            staged_beatmap = self._stage(path)
            self._ensure_profile()

            patch = {
                "General": {
                    "OsuSongsDir": str(self.songs_root),
                    "DiscordPresenceOn": False,
                },
                "Graphics": {
                    "Fullscreen": False,
                    "WindowWidth": 960,
                    "WindowHeight": 540,
                },
                "Gameplay": {"ShowResultsScreen": False},
                "Playfield": {
                    "LeadInTime": 0,
                    "LeadInHold": 0,
                    "FadeOutTime": 0.25,
                    "SeizureWarning": {"Enabled": False},
                },
            }
            command = [
                str(self.executable),
                f"-settings={DANSER_PROFILE}",
                f"-md5={self._md5(staged_beatmap)}",
                f"-start={start_time / 1000:g}",
                f"-end={end_time / 1000:g}",
                "-quickstart",
                "-noupdatecheck",
                f"-sPatch={json.dumps(patch, ensure_ascii=False, separators=(',', ':'))}",
            ]
            popen_options = {
                "cwd": str(self.executable.parent),
                "stdin": subprocess.DEVNULL,
                "stderr": subprocess.STDOUT,
            }
            if os.name == "nt":
                popen_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                popen_options["start_new_session"] = True

            with self.log_path.open("ab") as log_file:
                popen_options["stdout"] = log_file
                try:
                    self._process = self._process_factory(command, **popen_options)
                except OSError as exc:
                    raise PreviewError(f"Could not launch Danser: {exc}") from exc

            # Catch missing runtime files and other immediate startup failures
            # before the UI reports a successful preview launch.
            time.sleep(0.15)
            if self._process.poll() is not None:
                exit_code = self._process.returncode
                self._process = None
                details = ""
                try:
                    lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    panic_line = next((line for line in reversed(lines) if line.startswith("panic:")), "")
                    details = f" {panic_line}" if panic_line else ""
                except OSError:
                    pass
                raise PreviewError(f"Danser exited during startup (code {exit_code}).{details}")

            return PreviewLaunch(
                beatmap_path=path,
                start_time=start_time,
                end_time=end_time,
                process_id=self._process.pid,
                viewer=f"Danser {DANSER_VERSION}",
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stop_process()
            preview_root = self.preview_root
            if preview_root.name.startswith("preview-") and preview_root.is_dir():
                shutil.rmtree(preview_root, ignore_errors=True)
            self._closed = True

    def stop(self) -> None:
        """Stop the current renderer while retaining this session's staging area."""
        with self._lock:
            if not self._closed:
                self._stop_process()

    def __enter__(self) -> "DanserPreviewer":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
