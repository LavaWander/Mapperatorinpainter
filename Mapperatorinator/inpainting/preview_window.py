from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from slider import Beatmap


DEFAULT_SELECTION_MS = 10_000
DEFAULT_PADDING_MS = 3_000
SAMPLE_SET_NAMES = {1: "normal", 2: "soft", 3: "drum"}


@dataclass(frozen=True)
class PreviewWindowSnapshot:
    session_id: str | None
    selection_start: int
    selection_end: int
    padding_before: int
    padding_after: int
    cursor: int
    configuration_revision: int


class PreviewWindowController:
    """Thread-safe state shared by the editor and persistent preview window."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session_id: str | None = None
        self._selection_start = 0
        self._selection_end = DEFAULT_SELECTION_MS
        self._padding_before = DEFAULT_PADDING_MS
        self._padding_after = DEFAULT_PADDING_MS
        self._cursor = 0
        self._configuration_revision = 0

    @staticmethod
    def _clamp(value: int, lower: int, upper: int) -> int:
        return max(lower, min(int(value), upper))

    @classmethod
    def _normalize_selection(cls, start: int, end: int, length_ms: int | None) -> tuple[int, int]:
        upper = max(1, int(length_ms)) if length_ms is not None else max(int(end), 1)
        normalized_start = cls._clamp(start, 0, max(0, upper - 1))
        normalized_end = cls._clamp(end, normalized_start + 1, upper)
        return normalized_start, normalized_end

    def configure(
        self,
        *,
        session_id: str,
        selection_start: int,
        selection_end: int,
        padding_before: int,
        padding_after: int,
        length_ms: int | None,
    ) -> PreviewWindowSnapshot:
        if padding_before < 0 or padding_after < 0:
            raise ValueError("Preview padding cannot be negative.")
        if padding_before > 30_000 or padding_after > 30_000:
            raise ValueError("Preview padding cannot exceed 30 seconds.")
        start, end = self._normalize_selection(selection_start, selection_end, length_ms)
        with self._lock:
            changed_session = session_id != self._session_id
            self._session_id = session_id
            self._selection_start = start
            self._selection_end = end
            self._padding_before = int(padding_before)
            self._padding_after = int(padding_after)
            if changed_session:
                self._cursor = max(0, start - self._padding_before)
            elif length_ms is not None:
                self._cursor = self._clamp(self._cursor, 0, max(0, int(length_ms) - 1))
            self._configuration_revision += 1
            return self._snapshot_unlocked()

    def copy_boundary(
        self,
        boundary: Literal["start", "end"],
        timestamp: int,
        *,
        length_ms: int,
    ) -> PreviewWindowSnapshot:
        if boundary not in {"start", "end"}:
            raise ValueError("Preview boundary must be start or end.")
        upper = max(1, int(length_ms))
        position = self._clamp(timestamp, 0, upper)
        with self._lock:
            start = self._selection_start
            end = self._selection_end
            if boundary == "start":
                start = min(position, upper - 1)
                if start >= end:
                    end = min(upper, start + min(DEFAULT_SELECTION_MS, max(1, upper - start)))
            else:
                end = max(1, position)
                if end <= start:
                    start = max(0, end - min(DEFAULT_SELECTION_MS, end))
            self._selection_start, self._selection_end = self._normalize_selection(start, end, upper)
            self._cursor = self._clamp(position, 0, max(0, upper - 1))
            self._configuration_revision += 1
            return self._snapshot_unlocked()

    def set_cursor(self, timestamp: int, *, length_ms: int) -> PreviewWindowSnapshot:
        with self._lock:
            self._cursor = self._clamp(timestamp, 0, max(0, int(length_ms) - 1))
            return self._snapshot_unlocked()

    def clear_session(self, session_id: str) -> PreviewWindowSnapshot:
        with self._lock:
            if self._session_id == session_id:
                self._session_id = None
                self._cursor = 0
                self._configuration_revision += 1
            return self._snapshot_unlocked()

    def snapshot(self) -> PreviewWindowSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> PreviewWindowSnapshot:
        return PreviewWindowSnapshot(
            session_id=self._session_id,
            selection_start=self._selection_start,
            selection_end=self._selection_end,
            padding_before=self._padding_before,
            padding_after=self._padding_after,
            cursor=self._cursor,
            configuration_revision=self._configuration_revision,
        )


def _milliseconds(value) -> int:
    return round(value.total_seconds() * 1_000)


def _density_from_hitobjects(hit_objects, length_ms: int, bins: int) -> list[float]:
    if bins < 2:
        raise ValueError("Density needs at least two bins.")
    if length_ms <= 0:
        return [0.0] * bins

    counts = [0.0] * bins
    for hit_object in hit_objects:
        timestamp = _milliseconds(hit_object.time)
        if timestamp < 0:
            continue
        index = min(bins - 1, int(timestamp / length_ms * bins))
        counts[index] += 1.0

    if not any(counts):
        return [0.0] * bins

    smoothed = []
    for index, value in enumerate(counts):
        total = value
        weight = 1.0
        for offset, neighbor_weight in ((-2, 0.2), (-1, 0.55), (1, 0.55), (2, 0.2)):
            neighbor = index + offset
            if 0 <= neighbor < bins:
                total += counts[neighbor] * neighbor_weight
                weight += neighbor_weight
        smoothed.append(total / weight)

    peak = max(smoothed)
    if peak <= 0:
        return [0.0] * bins
    return [round(math.sqrt(value / peak), 4) for value in smoothed]


def _approach_preempt(approach_rate: float) -> int:
    if approach_rate < 5:
        return round(1_200 + 600 * (5 - approach_rate) / 5)
    return round(1_200 - 750 * (approach_rate - 5) / 5)


def _sample_slider_curve(hit_object) -> list[list[float]]:
    sample_count = max(16, min(160, math.ceil(float(hit_object.length) / 6)))
    points = []
    for index in range(sample_count + 1):
        point = hit_object.curve(index / sample_count)
        points.append([round(float(point.x), 2), round(float(point.y), 2)])
    return points


def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _hitsound_samples(
    beatmap: Beatmap,
    time,
    hitsound: int,
    addition: str,
    *,
    edge_sets: str | None = None,
) -> list[dict]:
    """Resolve osu! hitSample semantics into sample names and fallback kinds."""
    parts = (addition or "0:0:0:0:").split(":")
    parts.extend([""] * (5 - len(parts)))
    normal_set = _safe_int(parts[0])
    addition_set = _safe_int(parts[1])
    sample_index = _safe_int(parts[2])
    volume = _safe_int(parts[3])
    custom_filename = parts[4].strip()

    if edge_sets:
        edge_parts = edge_sets.split(":")
        if edge_parts:
            normal_set = _safe_int(edge_parts[0])
        if len(edge_parts) > 1:
            addition_set = _safe_int(edge_parts[1])

    if beatmap.timing_points:
        timing_point = beatmap.timing_point_at(time + timedelta(milliseconds=5))
        timing_sample_set = int(timing_point.sample_type or 0)
        timing_sample_index = int(timing_point.sample_set or 0)
        timing_volume = int(timing_point.volume)
    else:
        timing_sample_set = 0
        timing_sample_index = 0
        timing_volume = 100

    beatmap_sample_set = {
        "normal": 1,
        "soft": 2,
        "drum": 3,
    }.get(str(beatmap.sample_set).casefold(), 1)
    timing_sample_set = timing_sample_set if timing_sample_set in SAMPLE_SET_NAMES else beatmap_sample_set
    normal_set = normal_set if normal_set in SAMPLE_SET_NAMES else timing_sample_set
    addition_set = addition_set if addition_set in SAMPLE_SET_NAMES else normal_set
    sample_index = sample_index or timing_sample_index
    volume = max(0, min(100, volume or timing_volume))

    if custom_filename:
        return [{
            "kind": "custom",
            "candidate": custom_filename,
            "volume": volume,
            "use_map_asset": True,
        }]

    suffix = "" if sample_index in {0, 1} else str(sample_index)
    kinds = [("normal", normal_set)]
    if hitsound & 2:
        kinds.append(("whistle", addition_set))
    if hitsound & 4:
        kinds.append(("finish", addition_set))
    if hitsound & 8:
        kinds.append(("clap", addition_set))
    return [
        {
            "kind": kind,
            "candidate": f"{SAMPLE_SET_NAMES[sample_set]}-hit{kind}{suffix}.wav",
            "volume": volume,
            "use_map_asset": sample_index != 0,
        }
        for kind, sample_set in kinds
    ]


def _hitsound_event(
    beatmap: Beatmap,
    time,
    hitsound: int,
    addition: str,
    event_type: str,
    *,
    edge_sets: str | None = None,
) -> dict:
    return {
        "time": _milliseconds(time),
        "type": event_type,
        "samples": _hitsound_samples(
            beatmap,
            time,
            int(hitsound),
            addition,
            edge_sets=edge_sets,
        ),
    }


def preview_map_data(path: str | Path, length_ms: int, *, bins: int = 240) -> dict:
    """Parse one difficulty into compact data for the embedded previewer.

    The existing ``slider`` dependency remains the source of truth for object
    timing, stacking, slider duration, and curve geometry. The browser only
    receives a renderer-friendly representation of those parsed objects.
    """
    beatmap = Beatmap.from_path(Path(path))
    hit_objects = beatmap.hit_objects(stacking=True)
    objects = []
    hitsound_events = []
    combo_index = 0
    combo_number = 0

    for index, hit_object in enumerate(hit_objects):
        if index == 0:
            combo_number = 1
        elif hit_object.new_combo:
            combo_index = (combo_index + 1 + int(hit_object.combo_skip)) % 4
            combo_number = 1
        else:
            combo_number += 1

        kind = type(hit_object).__name__.lower()
        item = {
            "type": kind,
            "time": _milliseconds(hit_object.time),
            "x": round(float(hit_object.position.x), 2),
            "y": round(float(hit_object.position.y), 2),
            "combo": combo_number,
            "color": combo_index,
            "new_combo": bool(hit_object.new_combo),
        }
        if kind in {"slider", "spinner", "holdnote"}:
            item["end_time"] = _milliseconds(hit_object.end_time)
        if kind == "slider":
            item.update({
                "path": _sample_slider_curve(hit_object),
                "repeat": int(hit_object.repeat),
            })
            span_count = max(1, int(hit_object.repeat))
            duration = hit_object.end_time - hit_object.time
            for edge_index in range(span_count + 1):
                edge_time = hit_object.time + duration * edge_index / span_count
                edge_sound = hit_object.edge_sounds[edge_index] if edge_index < len(hit_object.edge_sounds) else 0
                edge_sets = hit_object.edge_additions[edge_index] if edge_index < len(hit_object.edge_additions) else None
                if edge_index == 0:
                    event_type = "slider_head"
                elif edge_index == span_count:
                    event_type = "slider_end"
                else:
                    event_type = "slider_repeat"
                hitsound_events.append(_hitsound_event(
                    beatmap,
                    edge_time,
                    edge_sound,
                    hit_object.addition,
                    event_type,
                    edge_sets=edge_sets,
                ))
        elif kind == "spinner":
            hitsound_events.append(_hitsound_event(
                beatmap,
                hit_object.end_time,
                hit_object.hitsound,
                hit_object.addition,
                "spinner_end",
            ))
        elif kind == "circle":
            hitsound_events.append(_hitsound_event(
                beatmap,
                hit_object.time,
                hit_object.hitsound,
                hit_object.addition,
                "circle",
            ))
        objects.append(item)

    circle_size = float(beatmap.circle_size)
    approach_rate = float(beatmap.approach_rate)
    return {
        "density": _density_from_hitobjects(hit_objects, length_ms, bins),
        "object_count": len(hit_objects),
        "metadata": {
            "title": beatmap.title,
            "artist": beatmap.artist,
        },
        "scene": {
            "width": 512,
            "height": 384,
            "circle_radius": round(54.4 - 4.48 * circle_size, 3),
            "approach_preempt": _approach_preempt(approach_rate),
            "objects": objects,
            "hitsounds": sorted(hitsound_events, key=lambda event: event["time"]),
        },
    }


def hitobject_density(path: str | Path, length_ms: int, *, bins: int = 240) -> tuple[list[float], int]:
    """Return parser-backed, normalized hitobject-onset density for a timeline."""
    parsed = preview_map_data(path, length_ms, bins=bins)
    return parsed["density"], parsed["object_count"]
