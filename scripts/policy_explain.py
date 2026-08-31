"""Read-only access-policy validator used by the local PowerShell TUI.

This helper intentionally prints only the structured policy decision.  It does
not read target contents and is not part of the MCP stdio process.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tiancheng_mcp.policy import AccessPolicy


def main() -> int:
    parser = argparse.ArgumentParser(description="Explain a TianCheng access-policy decision")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--operation", default="read")
    args = parser.parse_args()
    policy = AccessPolicy.load(Path(args.policy), Path(args.workspace))
    print(json.dumps(policy.explain(args.path, args.operation).as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
