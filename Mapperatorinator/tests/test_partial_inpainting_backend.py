from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path

from slider import Beatmap, Circle, Slider, Spinner

from config import InferenceConfig
from osuT5.osuT5.inference.postprocessor import Postprocessor


FIXTURES = Path(__file__).parent / "fixtures"
START_TIME = 2_000
END_TIME = 4_000


def milliseconds(value: timedelta) -> int:
    return round(value.total_seconds() * 1_000)


class PartialBeatmapMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        args = InferenceConfig(start_time=START_TIME, end_time=END_TIME)
        self.postprocessor = Postprocessor(args)
        self.reference_path = FIXTURES / "partial_reference.osu"
        self.reference_text = self.reference_path.read_text(encoding="utf-8")
        self.generated_text = (FIXTURES / "partial_generated.osu").read_text(encoding="utf-8")

    def merge(self) -> Beatmap:
        merged = self.postprocessor.add_to_beatmap(
            self.generated_text,
            str(self.reference_path),
        )
        return Beatmap.parse(merged)

    def test_merge_is_non_mutating_and_returns_a_parseable_beatmap(self) -> None:
        merged = self.postprocessor.add_to_beatmap(
            self.generated_text,
            str(self.reference_path),
        )

        self.assertEqual(self.reference_text, self.reference_path.read_text(encoding="utf-8"))
        self.assertIsInstance(Beatmap.parse(merged), Beatmap)

    def test_interval_membership_is_inclusive_and_uses_object_start_time(self) -> None:
        objects = self.merge().hit_objects(stacking=False)
        by_time = {milliseconds(hit_object.time): hit_object for hit_object in objects}

        self.assertEqual([900, 1200, 1500, 2000, 2500, 3000, 4000, 4100], sorted(by_time))

        # Compound objects beginning before the interval survive even when their
        # end extends into it.
        self.assertIsInstance(by_time[1200], Spinner)
        self.assertEqual(2500, milliseconds(by_time[1200].end_time))
        self.assertIsInstance(by_time[1500], Slider)
        self.assertGreater(milliseconds(by_time[1500].end_time), START_TIME)

        # Objects beginning inside the interval come from the generated map.
        self.assertIsInstance(by_time[2000], Circle)
        self.assertEqual((320, 64), tuple(by_time[2000].position))
        self.assertIsInstance(by_time[3000], Slider)
        self.assertGreater(milliseconds(by_time[3000].end_time), END_TIME)

        # Both boundaries are replaced, while the first object after the end is
        # retained from the reference. Generated objects outside the interval
        # are not imported.
        self.assertEqual((416, 192), tuple(by_time[4000].position))
        self.assertEqual((288, 288), tuple(by_time[4100].position))
        self.assertNotIn(4500, by_time)

    def test_timing_points_inside_interval_are_replaced_and_sorted(self) -> None:
        timing_points = self.merge().timing_points
        offsets = [milliseconds(timing_point.offset) for timing_point in timing_points]

        self.assertEqual(offsets, sorted(offsets))
        self.assertIn(1000, offsets)
        self.assertIn(2000, offsets)  # start-state reconciliation point
        self.assertIn(2200, offsets)
        self.assertIn(3200, offsets)
        self.assertIn(3900, offsets)
        self.assertIn(4100, offsets)
        self.assertNotIn(2500, offsets)
        self.assertNotIn(3000, offsets)
        self.assertNotIn(3500, offsets)


if __name__ == "__main__":
    unittest.main()

