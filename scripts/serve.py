"""Preview dist/ on this computer; Python standard library only."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import webbrowser


class PreviewHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def port_number(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=port_number, default=8765)
    parser.add_argument("--open", action="store_true", help="open the preview in a browser")
    args = parser.parse_args()
    site = Path(__file__).resolve().parent.parent / "dist"
    if not (site / "index.html").is_file():
        parser.error(f"website not found: {site / 'index.html'}")
    handler = partial(PreviewHandler, directory=str(site))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as error:
        print(f"Cannot start preview on port {args.port}: {error}", file=sys.stderr)
        print("Try: python scripts/serve.py --port 8766", file=sys.stderr)
        return 1
    address = f"http://127.0.0.1:{args.port}/"
    print(f"Preview: {address}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.2, webbrowser.open, args=(address,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
