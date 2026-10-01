"""Stdlib bootstrap for the launcher; stdout contains only resolved JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tiancheng_mcp.runtime_config import launcher_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--pass-env", action="append", default=[])
    args = parser.parse_args()
    if args.pass_env:
        from tiancheng_mcp.service import TianChengService
        TianChengService._validate_passthrough_env(args.pass_env)
    print(json.dumps(launcher_config(ROOT, args.local), ensure_ascii=False))


if __name__ == "__main__":
    main()
