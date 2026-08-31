from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Collection, Iterator, Mapping, Optional

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


@dataclass(frozen=True)
class BeatmapRevision:
    """One session-local `.osu` snapshot and its generation settings."""

    revision: int
    content: bytes
    created_at: str
    metadata: dict[str, Any]

    def payload(self, *, current: bool) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "current": current,
        }


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
        generation_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        self.source_archive = source_archive
        self.working_directory = working_directory
        self.difficulties = difficulties
        self.source_sha256 = source_sha256
        self.generation_provenance = dict(generation_provenance or {})
        self.created_at = datetime.now(timezone.utc)
        self.session_identifier = self.created_at.strftime("%Y%m%d-%H%M%S")
        self.active_difficulty = next(
            (difficulty for difficulty in difficulties if difficulty.supported),
            difficulties[0],
        )
        self._revision_counter = 0
        self._revisions: dict[str, list[BeatmapRevision]] = {}
        self._revision_cursors: dict[str, int] = {}
        self._saved_revisions: dict[str, int] = {}
        for difficulty in difficulties:
            original = BeatmapRevision(
                revision=0,
                content=difficulty.path.read_bytes(),
                created_at=datetime.now(timezone.utc).isoformat(),
                metadata={"kind": "original"},
            )
            self._revisions[difficulty.relative_path] = [original]
            self._revision_cursors[difficulty.relative_path] = 0
            self._saved_revisions[difficulty.relative_path] = original.revision
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
        is_directory = source.is_dir()
        if not is_directory:
            if source.suffix.lower() != ".osz":
                raise BeatmapsetOpenError(f"Beatmapset must be an .osz file or song folder: {source}")
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
            if is_directory:
                cls._copy_song_folder(source, working_directory)
            else:
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

    @classmethod
    def adopt_generated_workspace(
        cls,
        source_result: str | Path,
        working_directory: str | Path,
        *,
        provenance: Mapping[str, Any] | None = None,
        supported_modes: Collection[int] | None = None,
    ) -> "BeatmapsetSession":
        """Adopt a job-owned generated workspace without copying or archiving it."""
        source = Path(source_result).expanduser().resolve()
        workspace = Path(working_directory).expanduser().resolve()
        if not source.is_file():
            raise BeatmapsetOpenError(f"Generated result no longer exists: {source}")
        if not workspace.is_dir() or not workspace.name.startswith("session-"):
            raise BeatmapsetOpenError(f"Generated workspace is invalid: {workspace}")
        difficulties = cls._discover_difficulties(
            workspace,
            frozenset(cls.DEFAULT_SUPPORTED_MODES if supported_modes is None else supported_modes),
        )
        return cls(
            source_archive=source,
            working_directory=workspace,
            difficulties=difficulties,
            source_sha256=cls._sha256(source),
            generation_provenance=provenance,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        paths = (
            sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.relative_to(path).as_posix().casefold())
            if path.is_dir()
            else [path]
        )
        for item in paths:
            if path.is_dir():
                digest.update(item.relative_to(path).as_posix().encode("utf-8"))
                digest.update(b"\0")
            with item.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse_flag and attributes & reparse_flag)

    @classmethod
    def _copy_song_folder(cls, source: Path, destination: Path) -> None:
        """Copy a loose osu! song folder without following links outside it."""
        try:
            for current_root, directory_names, file_names in os.walk(source, followlinks=False):
                current = Path(current_root)
                if cls._is_link_or_reparse_point(current) and current != source:
                    raise UnsafeArchiveError(f"Song folder contains a linked directory: {current}")
                relative_root = current.relative_to(source)
                target_root = destination / relative_root
                target_root.mkdir(parents=True, exist_ok=True)

                for directory_name in directory_names:
                    directory = current / directory_name
                    if cls._is_link_or_reparse_point(directory):
                        raise UnsafeArchiveError(f"Song folder contains a linked directory: {directory}")
                for file_name in file_names:
                    source_file = current / file_name
                    if cls._is_link_or_reparse_point(source_file) or not source_file.is_file():
                        raise UnsafeArchiveError(f"Song folder contains a linked or special file: {source_file}")
                    shutil.copy2(source_file, target_root / file_name)
        except (OSError, ValueError) as exc:
            if isinstance(exc, UnsafeArchiveError):
                raise
            raise BeatmapsetOpenError(f"Could not copy song folder {source}: {exc}") from exc

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

    @property
    def dirty(self) -> bool:
        """Whether any difficulty differs from the last exported/session-open state."""
        self._ensure_open()
        return any(
            revisions[self._revision_cursors[relative_path]].revision
            != self._saved_revisions[relative_path]
            for relative_path, revisions in self._revisions.items()
        )

    def _difficulty_relative_path(self, path: str | Path | None = None) -> str:
        if path is None:
            return self.active_difficulty.relative_path
        resolved = Path(path).resolve()
        try:
            relative_path = resolved.relative_to(self.working_directory).as_posix()
        except ValueError as exc:
            raise ValueError(f"Revision path is outside the session: {resolved}") from exc
        if relative_path not in self._revisions:
            raise ValueError(f"Revision path is not a discovered difficulty: {relative_path}")
        return relative_path

    @staticmethod
    def _restore_revision(path: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}.",
                suffix=".revision",
                dir=path.parent,
                delete=False,
            ) as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
                temporary_path = Path(temporary_file.name)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def record_revision(
        self,
        *,
        path: str | Path | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BeatmapRevision:
        """Commit the current `.osu` as a new revision and discard any redo branch."""
        self._ensure_open()
        relative_path = self._difficulty_relative_path(path)
        difficulty_path = self.working_directory / Path(relative_path)
        Beatmap.from_path(difficulty_path)

        cursor = self._revision_cursors[relative_path]
        revisions = self._revisions[relative_path][:cursor + 1]
        self._revision_counter += 1
        revision = BeatmapRevision(
            revision=self._revision_counter,
            content=difficulty_path.read_bytes(),
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata=dict(metadata or {"kind": "modified"}),
        )
        revisions.append(revision)
        self._revisions[relative_path] = revisions
        self._revision_cursors[relative_path] = len(revisions) - 1
        return revision

    def mark_dirty(self) -> None:
        """Compatibility helper for callers that modify a working `.osu` directly."""
        self.record_revision()

    def undo(self) -> BeatmapRevision:
        self._ensure_open()
        relative_path = self.active_difficulty.relative_path
        cursor = self._revision_cursors[relative_path]
        if cursor == 0:
            raise ValueError("There is no earlier revision to undo to.")
        cursor -= 1
        revision = self._revisions[relative_path][cursor]
        self._restore_revision(self.active_difficulty.path, revision.content)
        self._revision_cursors[relative_path] = cursor
        return revision

    def redo(self) -> BeatmapRevision:
        self._ensure_open()
        relative_path = self.active_difficulty.relative_path
        cursor = self._revision_cursors[relative_path]
        revisions = self._revisions[relative_path]
        if cursor >= len(revisions) - 1:
            raise ValueError("There is no later revision to redo.")
        cursor += 1
        revision = revisions[cursor]
        self._restore_revision(self.active_difficulty.path, revision.content)
        self._revision_cursors[relative_path] = cursor
        return revision

    def revision_payload(self) -> dict[str, Any]:
        self._ensure_open()
        relative_path = self.active_difficulty.relative_path
        revisions = self._revisions[relative_path]
        cursor = self._revision_cursors[relative_path]
        return {
            "current_revision": revisions[cursor].revision,
            "can_undo": cursor > 0,
            "can_redo": cursor < len(revisions) - 1,
            "items": [revision.payload(current=index == cursor) for index, revision in enumerate(revisions)],
        }

    @property
    def current_revision(self) -> BeatmapRevision:
        """Return the active difficulty's currently selected revision."""
        self._ensure_open()
        relative_path = self.active_difficulty.relative_path
        return self._revisions[relative_path][self._revision_cursors[relative_path]]

    @staticmethod
    def _filename_component(value: Any, *, fallback: str = "unknown") -> str:
        """Make user/map metadata safe and compact inside a Windows filename."""
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value or "").strip())
        text = re.sub(r"\s+", " ", text).strip(" .-")
        return text or fallback

    def suggested_export_name(self) -> str:
        """Build a descriptive, stable filename for the active inpaint revision."""
        beatmap = self.validate_active_difficulty()
        revision = self.current_revision
        metadata = revision.metadata
        identity = (
            f"{self._filename_component(beatmap.artist)} - "
            f"{self._filename_component(beatmap.title)} "
            f"[{self._filename_component(beatmap.version, fallback='Difficulty')}]"
        )
        details: list[str] = []
        difficulty = metadata.get("difficulty")
        if difficulty is not None:
            try:
                details.append(f"{float(difficulty):.1f}star")
            except (TypeError, ValueError):
                pass
        details.extend((f"S{self.session_identifier}", f"R{revision.revision:03d}"))

        if metadata.get("kind") == "original":
            details.append("original")
        else:
            seed = metadata.get("seed")
            if seed is not None:
                details.append(f"seed-{self._filename_component(seed)}")
            descriptors = []
            for descriptor in metadata.get("descriptors") or []:
                leaf = str(descriptor).rsplit("/", 1)[-1]
                safe = self._filename_component(leaf, fallback="")
                if safe:
                    descriptors.append(safe)
            if descriptors:
                descriptor_text = "+".join(descriptors)
                details.append(descriptor_text[:60].rstrip(" .-+"))

        filename = f"{identity}__{'__'.join(details)}.osz"
        # Leave comfortable room for the output directory and collision suffix.
        if len(filename) > 210:
            filename = f"{filename[:-4][:206].rstrip(' .-_')}.osz"
        return filename

    def next_export_path(self, output_directory: str | Path) -> Path:
        """Choose a unique automatic destination without overwriting prior exports."""
        directory = Path(output_directory).expanduser().resolve()
        candidate = directory / self.suggested_export_name()
        if not candidate.exists():
            return candidate
        for copy_number in range(2, 10_000):
            suffixed = candidate.with_name(f"{candidate.stem}__{copy_number:02d}{candidate.suffix}")
            if not suffixed.exists():
                return suffixed
        raise ExportError(f"Could not choose a unique export filename in {directory}")

    def mark_exported(self) -> None:
        self._ensure_open()
        for relative_path, revisions in self._revisions.items():
            self._saved_revisions[relative_path] = revisions[self._revision_cursors[relative_path]].revision

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

        self.mark_exported()
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
