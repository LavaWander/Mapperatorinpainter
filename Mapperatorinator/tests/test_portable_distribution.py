from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[2]


class PortableDistributionTests(unittest.TestCase):
    def test_launcher_uses_one_private_runtime_and_integrated_update_check(self) -> None:
        launcher = (PROJECT_ROOT / "Run Mapperatorinpainter.bat").read_text(encoding="utf-8")
        self.assertIn("runtime\\python.exe", launcher)
        self.assertIn(".portable-install.json", launcher)
        self.assertIn("Prepare-Portable.ps1", launcher)
        self.assertIn("portable\\launch.py", launcher)
        self.assertIn("Install optional Danser", launcher)
        self.assertFalse((PROJECT_ROOT / "Update.bat").exists())

    def test_update_preserves_runtime_and_uses_github_releases(self) -> None:
        updater = (PROJECT_ROOT / "portable" / "Prepare-Portable.ps1").read_text(encoding="utf-8")
        self.assertIn("api.github.com/repos/$repository/releases", updater)
        self.assertIn("zipball_url", updater)
        self.assertIn(".update-backup-", updater)
        self.assertNotIn('Move-Item -LiteralPath (Join-Path $root "runtime")', updater)

    def test_portable_dependency_repair_does_not_require_git(self) -> None:
        requirements = (PROJECT_ROOT / "portable" / "portable-requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("git+", requirements)
        self.assertIn("OliBomby/slider/archive/", requirements)

    def test_danser_installer_requires_a_portable_release(self) -> None:
        installer = (PROJECT_ROOT / "Install Danser Preview.bat").read_text(encoding="utf-8")
        self.assertIn(".portable-install.json", installer)
        self.assertIn("runtime\\python.exe", installer)
        self.assertIn("danser-core.dll", installer)
        self.assertIn("assets.dpak", installer)


if __name__ == "__main__":
    unittest.main()
