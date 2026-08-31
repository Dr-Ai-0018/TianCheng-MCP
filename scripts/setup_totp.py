"""One-time local TOTP enrollment for chat-approved external grants."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import struct
import sys
import time
from pathlib import Path
from urllib.parse import quote


def code_for(secret: str, counter: int) -> str:
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def valid_code(secret: str, value: str) -> bool:
    if len(value) != 6 or not value.isdecimal():
        return False
    now = int(time.time() // 30)
    return any(hmac.compare_digest(value, code_for(secret, now + delta)) for delta in (-1, 0, 1))


def update_env(path: Path, secret: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated = False
    output: list[str] = []
    for line in lines:
        if line.strip().startswith("TIANCHENG_TOTP_SECRET="):
            output.append(f"TIANCHENG_TOTP_SECRET={secret}")
            updated = True
        else:
            output.append(line)
    if not updated:
        if output and output[-1] != "":
            output.append("")
        output.append(f"TIANCHENG_TOTP_SECRET={secret}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Enroll a TOTP authenticator for TianCheng MCP")
    parser.add_argument("--env-file", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--qr-file", default=str(Path(__file__).resolve().parents[1] / "config" / "tiancheng-totp.svg"))
    parser.add_argument("--account", default="tiancheng-local")
    parser.add_argument("--issuer", default="TianCheng Local MCP")
    parser.add_argument("--keep-qr", action="store_true", help="Keep the QR file after successful enrollment")
    args = parser.parse_args()

    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        print("qrcode dependency missing; run: uv sync --frozen", file=sys.stderr)
        return 2

    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    label = quote(f"{args.issuer}:{args.account}")
    issuer = quote(args.issuer)
    uri = f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
    qr_path = Path(args.qr_file).resolve()
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    image = qrcode.make(uri, image_factory=SvgPathImage)
    image.save(str(qr_path))

    print(f"二维码已生成：{qr_path}")
    if os.name == "nt":
        try:
            os.startfile(str(qr_path))  # type: ignore[attr-defined]
        except OSError:
            pass
    print("请用你的 2FA 软件扫描二维码，然后输入当前 6 位验证码。")
    entered = input("验证码：").strip()
    if not valid_code(secret, entered):
        qr_path.unlink(missing_ok=True)
        print("验证码不正确，未写入 .env。", file=sys.stderr)
        return 1
    update_env(Path(args.env_file).resolve(), secret)
    if not args.keep_qr:
        qr_path.unlink(missing_ok=True)
    print(f"TOTP 已写入：{Path(args.env_file).resolve()}")
    print("之后请使用 run-mcp-grants.ps1，并重启 Tunnel/MCP。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
