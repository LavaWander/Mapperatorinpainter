from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping, Sequence

from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf
from slider import Beatmap

from config import InferenceConfig
from osuT5.osuT5.event import ContextType

from .session import BeatmapDifficulty, BeatmapsetSession
from .workflow import build_inpainting_config


_CLOCK_TIMESTAMP = re.compile(r"^(?P<minutes>\d+):(?P<seconds>\d{1,2})(?:\.(?P<milliseconds>\d{1,3}))?$")
_RAW_SECONDS = re.compile(r"^\d+(?:\.\d{1,3})?$")


def parse_timestamp_ms(value: str) -> int:
    """Parse MM:SS[.mmm] or raw seconds into milliseconds."""
    text = (value or "").strip()
    clock_match = _CLOCK_TIMESTAMP.fullmatch(text)
    if clock_match:
        seconds = int(clock_match.group("seconds"))
        if seconds >= 60:
            raise ValueError("Timestamp seconds must be less than 60.")
        milliseconds = (clock_match.group("milliseconds") or "").ljust(3, "0")
        return (int(clock_match.group("minutes")) * 60 + seconds) * 1_000 + int(milliseconds or 0)

    if _RAW_SECONDS.fullmatch(text):
        whole_seconds, _, fraction = text.partition(".")
        return int(whole_seconds) * 1_000 + int(fraction.ljust(3, "0") or 0)

    raise ValueError("Use MM:SS, MM:SS.mmm, or raw seconds.")


def format_timestamp_ms(value: int | None) -> str:
    if value is None:
        return "—"
    if value < 0:
        raise ValueError("Timestamp cannot be negative.")
    minutes, remainder = divmod(int(value), 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _split_descriptors(values: Sequence[str] | str | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    result = [part.strip() for value in values for part in value.split(",") if part.strip()]
    return result or None


def _optional_int(value: str | None) -> int | None:
    return None if value is None or not value.strip() else int(value)


def _optional_float(value: str | None) -> float | None:
    return None if value is None or not value.strip() else float(value)


def compose_inpainting_config(
    *,
    config_dir: str | Path,
    model_name: str,
    session: BeatmapsetSession,
    values: Mapping[str, str],
    descriptors: Sequence[str] | None = None,
    negative_descriptors: Sequence[str] | None = None,
) -> InferenceConfig:
    """Apply Inpaint controls to the normal inference config and M2 request builder."""
    with initialize_config_dir(version_base="1.1", config_dir=str(Path(config_dir).resolve())):
        config = OmegaConf.to_object(compose(config_name=model_name))

    start_time = parse_timestamp_ms(values.get("start_time", ""))
    end_time = parse_timestamp_ms(values.get("end_time", ""))
    map_length = session.active_difficulty.length_ms
    if map_length is not None and end_time > map_length:
        raise ValueError(
            f"End time {format_timestamp_ms(end_time)} exceeds the selected map length "
            f"{format_timestamp_ms(map_length)}."
        )

    config = build_inpainting_config(config, session, start_time=start_time, end_time=end_time)
    config.difficulty = _optional_float(values.get("difficulty"))
    config.mapper_id = _optional_int(values.get("mapper_id"))
    config.year = _optional_int(values.get("year"))
    config.seed = _optional_int(values.get("seed"))
    for field in ("cfg_scale", "temperature", "top_p", "lookback", "lookahead"):
        value = _optional_float(values.get(field))
        if value is not None:
            setattr(config, field, value)
    config.lora_path = values.get("lora_path") or None
    config.descriptors = _split_descriptors(descriptors)
    config.negative_descriptors = _split_descriptors(negative_descriptors)

    config.in_context = [ContextType.TIMING] if values.get("timing_context") == "true" else []
    hitsounds = values.get("hitsounds", "inherit")
    if hitsounds not in {"inherit", "yes", "no"}:
        raise ValueError("Hitsound behavior must be inherit, yes, or no.")
    config.hitsounded = None if hitsounds == "inherit" else hitsounds == "yes"
    return config


def difficulty_payload(difficulty: BeatmapDifficulty) -> dict:
    return {
        "relative_path": difficulty.relative_path,
        "version": difficulty.version,
        "mode": difficulty.mode,
        "mapper": difficulty.mapper,
        "ar": difficulty.approach_rate,
        "od": difficulty.overall_difficulty,
        "cs": difficulty.circle_size,
        "hp": difficulty.hp_drain_rate,
        "length_ms": difficulty.length_ms,
        "length": format_timestamp_ms(difficulty.length_ms),
        "supported": difficulty.supported,
    }


def session_payload(session_id: str, session: BeatmapsetSession) -> dict:
    beatmap = Beatmap.from_path(session.active_difficulty.path)
    audio = session.resolve_audio()
    background = session.resolve_background()
    return {
        "session_id": session_id,
        "source_archive": str(session.source_archive),
        "source_name": session.source_archive.name,
        "working_directory": str(session.working_directory),
        "dirty": session.dirty,
        "generation_provenance": dict(session.generation_provenance),
        "revisions": session.revision_payload(),
        "active_difficulty": difficulty_payload(session.active_difficulty),
        "difficulties": [difficulty_payload(item) for item in session.difficulties],
        "assets": {
            "audio": audio.name,
            "audio_path": str(audio),
            "background": background.name if background else None,
        },
        "metadata": {
            "title": beatmap.title,
            "artist": beatmap.artist,
            "slider_multiplier": float(beatmap.slider_multiplier),
            "slider_tick_rate": float(beatmap.slider_tick_rate),
        },
    }
