"""Run the M2 `.osz` workflow against the existing model backend.

This is intentionally a manual proof harness rather than a second application
entry point. The future GUI will call the same session and workflow APIs while
using its already-owned inference server.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf

from inference import main as run_inference
from inpainting.session import BeatmapsetSession
from inpainting.workflow import build_inpainting_config, regenerate_interval
from osuT5.osuT5.event import ContextType


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--difficulty", required=True, help="Archive-relative .osu path")
    parser.add_argument("--start-time", type=int, required=True, help="Milliseconds")
    parser.add_argument("--end-time", type=int, required=True, help="Milliseconds")
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--model", default="v32")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    cli = parse_args()
    config_directory = Path(__file__).resolve().parents[1] / "configs" / "inference"
    with initialize_config_dir(version_base="1.1", config_dir=str(config_directory)):
        config = OmegaConf.to_object(compose(config_name=cli.model))

    config.seed = cli.seed
    config.in_context = [ContextType.TIMING]
    config.use_server = False

    with BeatmapsetSession.open(cli.source) as session:
        session.select_difficulty(cli.difficulty)
        request = build_inpainting_config(
            config,
            session,
            start_time=cli.start_time,
            end_time=cli.end_time,
        )
        regenerate_interval(session, request, run_inference)
        exported = session.export(cli.destination, overwrite=cli.overwrite)
        result = {
            "source": str(session.source_archive),
            "source_unchanged": session.source_is_unchanged(),
            "working_difficulty": session.active_difficulty.relative_path,
            "session_dirty": session.dirty,
            "exported": str(exported),
            "exported_size": exported.stat().st_size,
        }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
