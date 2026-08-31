from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from slider import Beatmap


class GeneratedHandoffError(ValueError):
    """Raised when a completed Generate result cannot become an Inpaint workspace."""


def _safe_asset_target(workspace: Path, asset_name: str, label: str) -> Path:
    normalized = (asset_name or "").strip().strip('"').replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or path.is_absolute() or any(part == ".." or ":" in part for part in parts):
        raise GeneratedHandoffError(f"Generated map references an unsafe {label} path: {asset_name}")
    target = workspace.joinpath(*parts).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise GeneratedHandoffError(f"Generated map references {label} outside its workspace: {asset_name}") from exc
    return target


def _copy_referenced_asset(source: str | Path | None, target: Path, label: str) -> None:
    if source is None:
        if label == "audio":
            raise GeneratedHandoffError("Generated map has no source audio path.")
        return
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise GeneratedHandoffError(f"Generated map {label} is missing: {source_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source_path != target:
        shutil.copy2(source_path, target)


def materialize_generated_workspace(
    workspace: str | Path,
    *,
    osu_content: str,
    result_path: str | Path,
    audio_path: str | Path | None,
    background_path: str | Path | None,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a session-ready folder directly from an inference result."""
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    osu_path = root / "generated.osu"
    osu_path.write_text(osu_content, encoding="utf-8")

    try:
        beatmap = Beatmap.from_path(osu_path)
    except Exception as exc:
        raise GeneratedHandoffError(f"Generated result is not a valid .osu file: {exc}") from exc

    audio_target = _safe_asset_target(root, beatmap.audio_filename, "audio")
    _copy_referenced_asset(audio_path, audio_target, "audio")

    background_target = None
    if beatmap.background:
        background_target = _safe_asset_target(root, beatmap.background, "background")
        _copy_referenced_asset(background_path, background_target, "background")

    return {
        "workspace": str(root),
        "osu_path": str(osu_path),
        "audio_path": str(audio_target),
        "background_path": str(background_target) if background_target else None,
        "result_path": str(Path(result_path).expanduser().resolve()),
        "difficulty": beatmap.version,
        "title": beatmap.title,
        "artist": beatmap.artist,
        "provenance": dict(provenance),
    }
