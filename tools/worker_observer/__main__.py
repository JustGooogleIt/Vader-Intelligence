"""Run from checkout: python -m tools.worker_observer --help."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .boundary import Unavailable, decode
from .observer import Observer, preview


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--status", help="Workspace-relative worker status JSON")
    parser.add_argument(
        "--assess",
        action="store_true",
        help="Explicit external opt-in; interactive preview required",
    )
    parser.add_argument("command", choices=("discover", "inspect"))
    args = parser.parse_args()
    try:
        with Path(args.config).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise Unavailable("config_too_large")
        observer = Observer(decode(raw))
        observation = observer.inspect(args.worker, capture=args.command == "inspect")
        if args.status:
            observation["local_status"] = observer.status(observation, args.status)
        digest = None
        if (
            args.assess
            and observer.config.get("allow_external", False)
            and observation["identity"] == "matched"
            and observation["terminal"] is not None
            and not observation.get("local_status", {}).get("valid")
        ):
            if not sys.stdin.isatty():
                raise Unavailable("interactive_preview_required")
            snapshot = preview(observation)
            print(
                "Selected metadata/text will be sent externally to TypeSafe. No secret-redaction guarantee.",
                file=sys.stderr,
            )
            print(snapshot.decode(), file=sys.stderr)
            print("Type SEND to share this exact snapshot: ", end="", file=sys.stderr, flush=True)
            if sys.stdin.readline().strip() == "SEND":
                digest = hashlib.sha256(snapshot).hexdigest()
        observation["advisory"] = observer.assess(
            observation, opt_in=args.assess, approved_digest=digest
        )
        print(json.dumps(observation, ensure_ascii=True))
        return 0 if observation["identity"] == "matched" else 1
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(
            json.dumps(
                {
                    "identity": "unknown",
                    "reason": str(error)
                    if isinstance(error, Unavailable)
                    else "invalid_configuration",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
