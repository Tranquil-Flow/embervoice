"""Include the loopback UI and bundled notices in wheel installs."""

from pathlib import Path
from shutil import copy2
from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        targets = [root / name for name in ("model-files.json", "LICENSE", "NOTICE")]
        for folder in ("web", "anchors", "legal"):
            targets.extend(path for path in (root / folder).rglob("*") if path.is_file() and not path.is_symlink())
        for path in targets:
            if path.name.startswith(".") or path.suffix in (".pyc", ".py"):
                continue
            destination = Path(self.build_lib) / path.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy2(path, destination)


setup(cmdclass={"build_py": BuildPy})
