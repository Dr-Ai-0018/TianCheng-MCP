"""Optional launcher-only proxy check; never prints a proxy URL or credential."""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path

import httpx2

from .proxy import ProxySettings


_IP_URLS = {"http": "http://api.ipify.org", "https": "https://api.ipify.org"}


def probe(settings: ProxySettings) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    for field, target in _IP_URLS.items():
        proxy_url = settings.values.get(field)
        if not proxy_url:
            results[field] = {"status": "unconfigured"}
            continue
        try:
            # An explicit proxy makes this check independent of NO_PROXY and
            # of any ambient proxy variables. The target only sees the proxy's
            # public egress IP, which the user explicitly requests to inspect.
            with httpx2.Client(proxy=proxy_url, trust_env=False, timeout=8) as client:
                response = client.get(target)
                response.raise_for_status()
            address = str(ipaddress.ip_address(response.text.strip()))
            results[field] = {"status": "ok", "egress_ip": address}
        except Exception as exc:
            # Exception messages can contain credentials or proxy URLs.
            results[field] = {"status": "failed", "reason": type(exc).__name__}
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--defaults", type=Path, required=True)
    parser.add_argument("--local", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = ProxySettings.load(args.defaults, args.local)
        result = probe(settings)
    except Exception as exc:
        result = {"error": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
