"""Package the reviewed local site without browser profiles or temporary files."""

from __future__ import annotations

from pathlib import Path
import zipfile

from package_preview import ROOT, package


def main() -> None:
    package(ROOT / "打开预览.html")
    destination = ROOT.parent / "steam-price-radar-本地预览版.zip"
    number = 2
    while destination.exists():
        destination = ROOT.parent / f"steam-price-radar-本地预览版-{number}.zip"
        number += 1
    excluded = {".qa", ".git", "__pycache__", ".pytest_cache", "node_modules"}
    entries = [path for path in ROOT.rglob("*") if path.is_file()
               and not excluded.intersection(path.relative_to(ROOT).parts)
               and path.suffix not in {".pyc", ".tmp", ".zip"}]
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(entries):
            archive.write(path, Path(ROOT.name) / path.relative_to(ROOT))
    with zipfile.ZipFile(destination) as archive:
        corrupt = archive.testzip()
        if corrupt:
            raise RuntimeError(f"ZIP validation failed: {corrupt}")
        if not any("/.github/workflows/" in name for name in archive.namelist()):
            raise RuntimeError("ZIP is missing its GitHub Actions workflows")
    print(f"Packaged {len(entries)} files, {destination.stat().st_size} bytes")


if __name__ == "__main__":
    main()
