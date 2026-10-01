"""Bounded incremental UTF-8 framing shared by Agent protocols."""
from __future__ import annotations

import codecs


class ProtocolStreamError(ValueError):
    pass


class JsonlStream:
    def __init__(self, max_line_bytes: int = 256 * 1024) -> None:
        self.maximum = max_line_bytes
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.pending = ""
        self.discard_to_newline = False
        self.finished = False

    def reset_after_gap(self) -> None:
        self.decoder.reset()
        self.pending = ""
        self.discard_to_newline = True

    def feed(self, data: bytes, *, final: bool = False) -> list[str]:
        if self.finished:
            return []
        if self.discard_to_newline:
            index = data.find(b"\n")
            if index < 0:
                if final:
                    self.finished = True
                return []
            data = data[index + 1:]
            self.discard_to_newline = False
        try:
            text = self.decoder.decode(data, final=final)
        except UnicodeError as exc:
            raise ProtocolStreamError("Invalid UTF-8 in Agent protocol") from exc
        parts = (self.pending + text).split("\n")
        self.pending = parts.pop()
        if final:
            if self.pending:
                parts.append(self.pending)
            self.pending = ""
            self.finished = True
        if any(len(line.encode("utf-8")) > self.maximum for line in [*parts, self.pending]):
            self.pending = ""
            raise ProtocolStreamError("Agent protocol line exceeded its byte limit")
        return [line.removesuffix("\r") for line in parts]
