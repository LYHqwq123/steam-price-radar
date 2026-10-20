"""Build a double-clickable preview from the real, collected price snapshot."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def package(output: Path) -> Path:
    site = ROOT / "dist"
    html = (site / "index.html").read_text(encoding="utf-8")
    css = (site / "assets/style.css").read_text(encoding="utf-8")
    js = (site / "assets/app.js").read_text(encoding="utf-8")
    data = json.loads((site / "data/prices.json").read_text(encoding="utf-8"))
    index_path = site / "data/catalog-index.json"
    catalog = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"games": []}
    manifest_path = site / "data/price-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    shards = {}
    if isinstance(manifest, dict):
        for entry in manifest.get("shards", []):
            if not isinstance(entry, dict) or entry.get("available") is not True:
                continue
            file = entry.get("file")
            if not isinstance(file, str) or not re.fullmatch(r"price-shards/price-[0-9]{2}\.json", file):
                raise ValueError("Price manifest contains an unsafe shard path.")
            shard_path = site / "data" / file
            if shard_path.exists():
                shards[file] = json.loads(shard_path.read_text(encoding="utf-8"))
    # Data is never interpreted as markup, including text returned by a store.
    def encoded(value):
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    encoded_data = encoded(data)
    encoded_catalog = encoded(catalog)
    encoded_manifest = encoded(manifest)
    encoded_shards = encoded(shards)
    html, css_count = re.subn(
        r'<link\b[^>]*href=["\'](?:\./)?assets/style\.css["\'][^>]*>',
        lambda _: "<style>\n" + css + "\n</style>",
        html,
        flags=re.I,
    )
    script = ("<script>window.PRICE_RADAR_BOOTSTRAP=" + encoded_data
              + ";window.__CATALOG_DATA__=" + encoded_catalog
              + ";window.__PRICE_MANIFEST__=" + encoded_manifest
              + ";window.__PRICE_SHARDS__=" + encoded_shards
              + ";</script>\n<script>\n" + js.replace("</script", "<\\/script") + "\n</script>")
    html, js_count = re.subn(
        r'<script\b[^>]*src=["\'](?:\./)?assets/app\.js["\'][^>]*>\s*</script>',
        lambda _: script,
        html,
        flags=re.I,
    )
    if css_count != 1 or js_count != 1:
        raise ValueError("Expected exactly one local stylesheet and one app script.")
    icon_path = site / "assets/favicon.svg"
    if icon_path.exists():
        icon = "data:image/svg+xml;base64," + base64.b64encode(icon_path.read_bytes()).decode("ascii")
        html = re.sub(r'(["\'])(?:\./)?assets/favicon\.svg\1', lambda _: '"' + icon + '"', html)
    # The original page's defer attribute does not defer an inline script.
    # Place the bootstrap and app after the document contents for both modes.
    html = html.replace(script, "")
    html = re.sub(r"</body>", lambda _: script + "\n</body>", html, count=1, flags=re.I)
    # Keep the home links in this file rather than opening its parent directory.
    html = html.replace('href="./"', 'href="#top"')
    html = html.replace("<body>", '<body id="top">', 1)
    html = re.sub(r"(?m)^[ \t]+$", "", html)
    output.write_text(html, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "打开预览.html")
    args = parser.parse_args()
    path = package(args.output.resolve())
    print(f"Preview ready: {path}")


if __name__ == "__main__":
    main()
