from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Collection, Iterator, Optional

from slider import Beatmap


class BeatmapsetOpenError(ValueError):
    """Raised when an `.osz` cannot be opened as a usable beatmapset."""


class UnsafeArchiveError(BeatmapsetOpenError):
    """Raised when an archive member could escape or subvert the session root."""


class AssetResolutionError(BeatmapsetOpenError):
    """Raised when a selected difficulty references an unsafe or missing asset."""


class ExportError(ValueError):
    """Raised when a working beatmapset cannot be exported safely."""


def _milliseconds(value: timedelta) -> int:
    return round(value.total_seconds() * 1_000)


def _beatmap_length(beatmap: Beatmap) -> Optional[int]:
    hit_objects = beatmap.hit_objects(stacking=False)
    if not hit_objects:
        return None

    return max(
        _milliseconds(getattr(hit_object, "end_time", hit_object.time))
        for hit_object in hit_objects
    )


@dataclass(frozen=True)
class BeatmapDifficulty:
    """Metadata needed to choose a working difficulty without mutating it."""

    relative_path: str
    path: Path
    version: str
    mode: int
    mapper: str
    approach_rate: float
    overall_difficulty: float
    circle_size: float
    hp_drain_rate: float
    length_ms: Optional[int]
    supported: bool


class BeatmapsetSession:
    """Owns one extracted, mutable working copy of an immutable `.osz`."""

    DEFAULT_SUPPORTED_MODES = frozenset({0, 1, 2, 3})

    def __init__(
        self,
        *,
        source_archive: Path,
        working_directory: Path,
        difficulties: tuple[BeatmapDifficulty, ...],
        source_sha256: str,
    ) -> None:
        self.source_archive = source_archive
        self.working_directory = working_directory
        self.difficulties = difficulties
        self.source_sha256 = source_sha256
        self.active_difficulty = next(
            (difficulty for difficulty in difficulties if difficulty.supported),
            difficulties[0],
        )
        self.dirty = False
        self._closed = False

    @classmethod
    def open(
        cls,
        source_archive: str | Path,
        *,
        temp_root: str | Path | None = None,
        supported_modes: Collection[int] | None = None,
    ) -> "BeatmapsetSession":
        source = Path(source_archive).expanduser().resolve()
        if source.suffix.lower() != ".osz":
            raise BeatmapsetOpenError(f"Beatmapset must have an .osz extension: {source}")
        if not source.is_file():
            raise BeatmapsetOpenError(f"Beatmapset archive not found: {source}")
        if not zipfile.is_zipfile(source):
            raise BeatmapsetOpenError(f"Beatmapset archive is not a readable zip file: {source}")

        session_parent = (
            Path(temp_root).expanduser().resolve()
            if temp_root is not None
            else Path(tempfile.gettempdir()) / "mapperatorinator"
        )
        session_parent.mkdir(parents=True, exist_ok=True)
        working_directory = Path(tempfile.mkdtemp(prefix="session-", dir=session_parent)).resolve()

        try:
            cls._extract_archive(source, working_directory)
            difficulties = cls._discover_difficulties(
                working_directory,
                frozenset(cls.DEFAULT_SUPPORTED_MODES if supported_modes is None else supported_modes),
            )
            return cls(
                source_archive=source,
                working_directory=working_directory,
                difficulties=difficulties,
                source_sha256=cls._sha256(source),
            )
        except Exception:
            shutil.rmtree(working_directory, ignore_errors=True)
            raise

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_member_parts(member: zipfile.ZipInfo) -> tuple[str, ...]:
        raw_name = member.filename.replace("\\", "/")
        if not raw_name or "\x00" in raw_name:
            raise UnsafeArchiveError("Archive contains an empty or invalid member name.")

        path = PurePosixPath(raw_name)
        if path.is_absolute() or raw_name.startswith(("/", "\\")):
            raise UnsafeArchiveError(f"Archive member uses an absolute path: {member.filename}")

        parts = tuple(part for part in path.parts if part not in ("", "."))
        if not parts or any(part == ".." for part in parts):
            raise UnsafeArchiveError(f"Archive member traverses outside the session: {member.filename}")
        if any(":" in part for part in parts):
            raise UnsafeArchiveError(f"Archive member uses a drive-qualified or alternate-stream path: {member.filename}")

        unix_mode = member.external_attr >> 16
        if stat.S_ISLNK(unix_mode):
            raise UnsafeArchiveError(f"Archive member is a symbolic link: {member.filename}")

        return parts

    @classmethod
    def _extract_archive(cls, source: Path, destination: Path) -> None:
        extracted_targets: set[str] = set()
        try:
            with zipfile.ZipFile(source, "r") as archive:
                for member in archive.infolist():
                    parts = cls._safe_member_parts(member)
                    target = destination.joinpath(*parts).resolve()
                    try:
                        target.relative_to(destination)
                    except ValueError as exc:
                        raise UnsafeArchiveError(
                            f"Archive member escapes the session: {member.filename}"
                        ) from exc

                    target_key = os.path.normcase(str(target))
                    if target_key in extracted_targets:
                        raise UnsafeArchiveError(f"Archive contains a duplicate path: {member.filename}")
                    extracted_targets.add(target_key)

                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as source_file, target.open("xb") as target_file:
                        shutil.copyfileobj(source_file, target_file)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            if isinstance(exc, UnsafeArchiveError):
                raise
            raise BeatmapsetOpenError(f"Could not extract beatmapset archive {source}: {exc}") from exc

    @classmethod
    def _discover_difficulties(
        cls,
        working_directory: Path,
        supported_modes: frozenset[int],
    ) -> tuple[BeatmapDifficulty, ...]:
        osu_paths = sorted(
            (path for path in working_directory.rglob("*") if path.is_file() and path.suffix.lower() == ".osu"),
            key=lambda path: path.relative_to(working_directory).as_posix().casefold(),
        )
        if not osu_paths:
            raise BeatmapsetOpenError("Beatmapset archive contains no .osu difficulties.")

        difficulties = []
        for path in osu_paths:
            relative_path = path.relative_to(working_directory).as_posix()
            try:
                beatmap = Beatmap.from_path(path)
            except Exception as exc:
                raise BeatmapsetOpenError(f"Could not parse difficulty {relative_path}: {exc}") from exc

            mode = int(beatmap.mode)
            difficulties.append(BeatmapDifficulty(
                relative_path=relative_path,
                path=path,
                version=beatmap.version,
                mode=mode,
                mapper=beatmap.creator,
                approach_rate=float(beatmap.approach_rate),
                overall_difficulty=float(beatmap.overall_difficulty),
                circle_size=float(beatmap.circle_size),
                hp_drain_rate=float(beatmap.hp_drain_rate),
                length_ms=_beatmap_length(beatmap),
                supported=mode in supported_modes,
            ))

        return tuple(difficulties)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Beatmapset session is closed.")

    def select_difficulty(self, relative_path: str | Path) -> BeatmapDifficulty:
        self._ensure_open()
        normalized = str(relative_path).replace("\\", "/")
        for difficulty in self.difficulties:
            if difficulty.relative_path == normalized:
                if not difficulty.supported:
                    raise BeatmapsetOpenError(
                        f"Difficulty mode {difficulty.mode} is not supported: {difficulty.relative_path}"
                    )
                self.active_difficulty = difficulty
                return difficulty
        raise BeatmapsetOpenError(f"Difficulty not found in session: {relative_path}")

    def _resolve_asset(self, asset_name: str | None, *, required: bool, label: str) -> Optional[Path]:
        self._ensure_open()
        if not asset_name:
            if required:
                raise AssetResolutionError(
                    f"Selected difficulty has no {label} filename: {self.active_difficulty.relative_path}"
                )
            return None

        normalized_name = asset_name.replace("\\", os.sep).replace("/", os.sep)
        candidate = (self.active_difficulty.path.parent / normalized_name).resolve()
        try:
            candidate.relative_to(self.working_directory)
        except ValueError as exc:
            raise AssetResolutionError(
                f"Selected difficulty references {label} outside the beatmapset: {asset_name}"
            ) from exc

        if not candidate.is_file():
            if required:
                raise AssetResolutionError(
                    f"Selected difficulty {label} file is missing: {asset_name}"
                )
            return None
        return candidate

    def resolve_audio(self) -> Path:
        beatmap = Beatmap.from_path(self.active_difficulty.path)
        resolved = self._resolve_asset(beatmap.audio_filename, required=True, label="audio")
        assert resolved is not None
        return resolved

    def resolve_background(self) -> Optional[Path]:
        beatmap = Beatmap.from_path(self.active_difficulty.path)
        return self._resolve_asset(beatmap.background, required=False, label="background")

    def mark_dirty(self) -> None:
        self._ensure_open()
        self.dirty = True

    def validate_active_difficulty(self) -> Beatmap:
        self._ensure_open()
        try:
            return Beatmap.from_path(self.active_difficulty.path)
        except Exception as exc:
            raise BeatmapsetOpenError(
                f"Working difficulty is not a valid .osu file: {self.active_difficulty.relative_path}: {exc}"
            ) from exc

    def iter_working_files(self) -> Iterator[Path]:
        self._ensure_open()
        for path in sorted(
            (path for path in self.working_directory.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(self.working_directory).as_posix().casefold(),
        ):
            if path.is_symlink():
                raise ExportError(f"Working beatmapset contains a symbolic link: {path}")
            yield path

    def export(self, destination: str | Path, *, overwrite: bool = False) -> Path:
        self._ensure_open()
        self.validate_active_difficulty()

        output = Path(destination).expanduser().resolve()
        if output.suffix.lower() != ".osz":
            raise ExportError(f"Export destination must have an .osz extension: {output}")
        if output == self.source_archive:
            raise ExportError("The immutable source archive cannot be overwritten during a session.")
        if output.exists() and not overwrite:
            raise ExportError(f"Export destination already exists: {output}")

        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(
                temporary_output,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for path in self.iter_working_files():
                    archive.write(path, path.relative_to(self.working_directory).as_posix())
            os.replace(temporary_output, output)
        except Exception as exc:
            temporary_output.unlink(missing_ok=True)
            if isinstance(exc, ExportError):
                raise
            raise ExportError(f"Could not export beatmapset to {output}: {exc}") from exc

        return output

    def source_is_unchanged(self) -> bool:
        self._ensure_open()
        return self._sha256(self.source_archive) == self.source_sha256

    def cleanup(self) -> None:
        if self._closed:
            return
        working_directory = self.working_directory.resolve()
        if working_directory.name.startswith("session-") and working_directory.is_dir():
            shutil.rmtree(working_directory)
        self._closed = True

    close = cleanup

    def __enter__(self) -> "BeatmapsetSession":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cleanup()
