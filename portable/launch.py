from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: launch.py <application.py>")
    application = Path(sys.argv[1]).expanduser().resolve()
    if not application.is_file():
        raise SystemExit(f"Mapperatorinpainter application was not found: {application}")
    sys.path.insert(0, str(application.parent))
    sys.argv = [str(application)]
    runpy.run_path(str(application), run_name="__main__")


if __name__ == "__main__":
    main()
