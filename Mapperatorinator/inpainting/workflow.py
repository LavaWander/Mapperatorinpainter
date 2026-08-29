from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from slider import Beatmap

from config import InferenceConfig
from .session import BeatmapsetSession


class GenerationTransactionError(RuntimeError):
    """Raised when interval regeneration fails and the working file is restored."""


class GenerationValidationError(GenerationTransactionError):
    """Raised when inference leaves an invalid working `.osu`."""


InferenceRunner = Callable[[InferenceConfig], Any]


def build_inpainting_config(
    base_config: InferenceConfig,
    session: BeatmapsetSession,
    *,
    start_time: int,
    end_time: int,
) -> InferenceConfig:
    """Translate session state into the existing partial-inference configuration."""
    if start_time < 0:
        raise ValueError("Inpainting start time must be non-negative.")
    if end_time <= start_time:
        raise ValueError("Inpainting end time must be greater than start time.")

    config = copy.deepcopy(base_config)
    config.beatmap_path = str(session.active_difficulty.path)
    config.audio_path = str(session.resolve_audio())
    config.output_path = str(session.working_directory)
    config.start_time = int(start_time)
    config.end_time = int(end_time)
    config.add_to_beatmap = True
    config.overwrite_reference_beatmap = True
    config.export_osz = False
    return config


def restore_snapshot(path: Path, content: bytes) -> None:
    """Atomically restore a previously captured `.osu` snapshot."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".restore",
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


def regenerate_interval(
    session: BeatmapsetSession,
    config: InferenceConfig,
    inference_runner: InferenceRunner,
) -> Any:
    """Run existing inference as a transaction against the active working `.osu`."""
    active_path = session.active_difficulty.path
    snapshot = active_path.read_bytes()

    try:
        result = inference_runner(config)
        try:
            Beatmap.from_path(active_path)
        except Exception as exc:
            raise GenerationValidationError(
                f"Generated working difficulty did not parse: {active_path}: {exc}"
            ) from exc
    except Exception as exc:
        restore_snapshot(active_path, snapshot)
        if isinstance(exc, GenerationTransactionError):
            raise
        raise GenerationTransactionError(
            f"Interval generation failed; restored {active_path.name}: {exc}"
        ) from exc

    session.mark_dirty()
    return result
