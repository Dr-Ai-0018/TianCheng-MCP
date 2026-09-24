"""Resolve optional outbound proxy settings without changing the default path."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping
from urllib.parse import urlsplit


_FIELDS = {
    "http": ("HTTP_PROXY", "http_proxy"),
    "https": ("HTTPS_PROXY", "https_proxy"),
    "noProxy": ("NO_PROXY", "no_proxy"),
}
_LOOPBACK_BYPASS = ("localhost", "127.0.0.1", "::1")


def _read_proxy_section(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise ValueError("launcher config must be an object")
    section = document.get("proxy", {})
    if not isinstance(section, dict):
        raise ValueError("proxy config must be an object")
    unknown = set(section) - {*_FIELDS, "agent"}
    if unknown:
        raise ValueError("proxy config has unknown fields")
    return section


@dataclass(frozen=True)
class ProxySettings:
    values: Mapping[str, str]
    agent: str = "off"
    configured: bool = False

    @classmethod
    def load(
        cls,
        defaults_path: Path,
        local_path: Path,
        environment: Mapping[str, str] | None = None,
    ) -> "ProxySettings":
        section = _read_proxy_section(defaults_path)
        section.update(_read_proxy_section(local_path))
        source = os.environ if environment is None else environment
        agent = section.get("agent", "off")
        if agent == "inherit":  # Existing local configs keep their old meaning.
            agent = "always"
        if agent not in ("off", "selective", "always"):
            raise ValueError("proxy.agent must be 'off', 'selective', or 'always'")
        values: dict[str, str] = {}
        configured = False
        for field, variants in _FIELDS.items():
            raw = section.get(field, "")
            if not isinstance(raw, str):
                raise ValueError(f"proxy.{field} must be a string")
            for name in variants:
                if name in source:
                    raw = source[name]
                    configured = True
                    break
            else:
                configured |= bool(raw)
            if (
                not isinstance(raw, str)
                or any(ord(character) < 32 for character in raw)
                or len(raw) > 4096
            ):
                raise ValueError(f"proxy.{field} must be bounded text")
            if field != "noProxy" and raw:
                try:
                    parts = urlsplit(raw)
                    valid = (
                        parts.scheme in ("http", "https", "socks5", "socks5h")
                        and bool(parts.hostname)
                        and parts.path in ("", "/")
                        and not parts.query
                        and not parts.fragment
                        and (parts.port is None or 1 <= parts.port <= 65535)
                    )
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError(f"proxy.{field} must be an HTTP(S) or SOCKS5 proxy URL")
            if raw:
                values[field] = raw
        if configured:
            bypass = [part.strip() for part in values.get("noProxy", "").split(",") if part.strip()]
            known = {item.casefold() for item in bypass}
            for host in _LOOPBACK_BYPASS:
                if host.casefold() not in known:
                    bypass.append(host)
            values["noProxy"] = ",".join(bypass)
        return cls(values=values, agent=agent, configured=configured)

    def apply_to_process(self, environment: MutableMapping[str, str] | None = None) -> None:
        if not self.configured:
            return
        target = os.environ if environment is None else environment
        for field, value in self.values.items():
            for name in _FIELDS[field]:
                target[name] = value
        for field, names in _FIELDS.items():
            if field not in self.values:
                for name in names:
                    target.pop(name, None)

    def agent_environment(self) -> dict[str, str]:
        if self.agent == "off" or not any(
            self.values.get(field) for field in ("http", "https")
        ):
            return {}
        return {name: value for field, value in self.values.items() for name in _FIELDS[field]}


def add_agent_proxy(
    child_environment: MutableMapping[str, str], proxy_environment: Mapping[str, str]
) -> None:
    """Never overwrite a child value, including a differently cased Windows key."""

    existing = {name.casefold() for name in child_environment}
    for field, variants in _FIELDS.items():
        if not any(name.casefold() in existing for name in variants):
            for name in variants:
                if name in proxy_environment:
                    child_environment[name] = proxy_environment[name]
