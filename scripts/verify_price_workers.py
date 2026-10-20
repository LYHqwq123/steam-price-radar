"""Reject a publication when a current-run worker artifact is absent or empty."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def verify(artifacts, worker_count=8):
    if not isinstance(artifacts, list) or not isinstance(worker_count, int) or worker_count < 1:
        raise ValueError("expected an artifact list and a positive worker count")
    found = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        name = artifact.get("name")
        if not isinstance(name, str) or not name.startswith("price-shards-"):
            continue
        suffix = name.removeprefix("price-shards-")
        if not suffix.isascii() or not suffix.isdigit() or suffix != str(int(suffix)):
            raise ValueError("invalid worker artifact name")
        worker = int(suffix)
        if not 0 <= worker < worker_count or worker in found:
            raise ValueError("unexpected or duplicated worker artifact")
        size = artifact.get("size_in_bytes")
        if artifact.get("expired") is not False or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError(f"worker {worker} artifact is expired or empty")
        found.add(worker)
    missing = set(range(worker_count)) - found
    if missing:
        raise ValueError("missing current-run worker artifacts: " + ", ".join(map(str, sorted(missing))))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.artifacts.read_text(encoding="utf-8"))
        workers = verify(payload, args.workers)
    except (OSError, ValueError, TypeError) as error:
        print(f"Refusing to publish old baseline: {error}", file=sys.stderr)
        return 1
    print(f"Verified all {len(workers)} nonempty current-run worker artifacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
