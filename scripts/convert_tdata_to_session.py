#!/usr/bin/env python3
"""Convert a Telegram Desktop `tdata` directory into a Telethon `.session` SQLite file.

Supports both modern (64-bit user_id marker) and legacy `dbiMtpAuthorization` (block 75)
TDF files without requiring GUI libraries (uses stdlib + pyaes + telethon).
Never prints `auth_key`, `local_key`, or passcode values.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile

import pyaes
from telethon import TelegramClient
from telethon.sessions import SQLiteSession

logger = logging.getLogger(__name__)

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

TDF_MAGIC = b"TDF$"
TDATA_DBI_MTP_AUTHORIZATION = 75  # 0x4B
TDATA_AUTH_KEY_BYTES = 256
# Telegram production MTProto DC endpoints (datacenter_id -> (ipv4, port))
DC_ENDPOINTS: dict[int, tuple[str, int]] = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}
MAIN_ACCOUNT_DATA_CANDIDATES = ("D877F783D5D3EF8Cs", "D877F783D5D3EF8C1", "D877F783D5D3EF8C0")


@dataclass(frozen=True)
class ParsedTdataSession:
    """Decrypted main DC credentials extracted from a TDesktop `tdata` folder."""

    dc_id: int
    user_id: int
    auth_key: bytes


def _read_qbytearray(buf: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(buf):
        raise ValueError("Unexpected EOF reading Qt QByteArray length")
    ln = struct.unpack(">i", buf[pos : pos + 4])[0]
    pos += 4
    if ln < 0:
        return b"", pos
    if pos + ln > len(buf):
        raise ValueError(f"Unexpected EOF reading QByteArray ({ln} bytes)")
    return buf[pos : pos + ln], pos + ln


def _read_tdf(path: Path) -> tuple[int, bytes]:
    raw = path.read_bytes()
    if len(raw) < 24 or raw[:4] != TDF_MAGIC:
        raise ValueError(f"Invalid TDF header in {path}")
    version = struct.unpack("<I", raw[4:8])[0]
    body = raw[8:-16]
    digest_expected = raw[-16:]
    to_hash = body + struct.pack("<II", len(body), version) + TDF_MAGIC
    if hashlib.md5(to_hash).digest() != digest_expected:
        raise ValueError(f"TDF MD5 checksum mismatch in {path}")
    return version, body


def _derive_local_key(salt: bytes, passcode: str = "") -> bytes:
    pass_bytes = passcode.encode("utf-8")
    iterations = 100_000 if pass_bytes else 1
    pass_hash = hashlib.sha512(salt + pass_bytes + salt).digest()
    return hashlib.pbkdf2_hmac("sha512", pass_hash, salt, iterations, 256)


def _aes256_ige_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(data) % 16 != 0:
        raise ValueError("AES-IGE ciphertext must be 16-byte aligned")
    cipher = pyaes.AES(key)
    out = bytearray()
    prev_c = bytearray(iv[:16])
    prev_p = bytearray(iv[16:32])
    for off in range(0, len(data), 16):
        blk = data[off : off + 16]
        xored = bytes(a ^ b for a, b in zip(blk, prev_p))
        dec = cipher.decrypt(xored)
        plain = bytes(a ^ b for a, b in zip(dec, prev_c))
        out.extend(plain)
        prev_c = bytearray(blk)
        prev_p = bytearray(plain)
    return bytes(out)


def _prepare_aes_oldmtp_decrypt(key: bytes, msg_key: bytes) -> tuple[bytes, bytes]:
    a = hashlib.sha1(msg_key[:16] + key[8:40]).digest()
    b = hashlib.sha1(key[40:56] + msg_key[:16] + key[56:72]).digest()
    c = hashlib.sha1(key[72:104] + msg_key[:16]).digest()
    d = hashlib.sha1(msg_key[:16] + key[104:136]).digest()
    aes_key = a[:8] + b[8:20] + c[4:16]
    aes_iv = a[8:20] + b[:8] + c[16:20] + d[:8]
    return aes_key, aes_iv


def _decrypt_local_payload(encrypted: bytes, local_key: bytes) -> bytes:
    if len(encrypted) < 32:
        raise ValueError("Encrypted TDF payload is too short")
    msg_key = encrypted[:16]
    aes_key, aes_iv = _prepare_aes_oldmtp_decrypt(local_key, msg_key)
    plain = _aes256_ige_decrypt(encrypted[16:], aes_key, aes_iv)
    if hashlib.sha1(plain).digest()[:16] != msg_key:
        raise ValueError("TDF local decryption failed (wrong passcode or corrupted key)")
    declared_len = struct.unpack("<I", plain[:4])[0]
    if declared_len < 4 or declared_len > len(plain):
        raise ValueError(f"Invalid decrypted block length {declared_len}")
    return plain[4:declared_len]


def _find_main_auth_tdf(tdata_dir: Path) -> Path:
    for name in MAIN_ACCOUNT_DATA_CANDIDATES:
        cand = tdata_dir / name
        if cand.is_file():
            return cand
    for cand in sorted(tdata_dir.iterdir()):
        if cand.is_file() and re.fullmatch(r"[0-9A-Fa-f]{16}[s01]", cand.name):
            return cand
    raise FileNotFoundError(f"No encrypted account data file found in {tdata_dir}")


def _parse_mtp_authorization_blob(blob: bytes) -> ParsedTdataSession:
    if len(blob) < 12:
        raise ValueError("MTP authorization blob too short")
    legacy_uid, legacy_dc = struct.unpack(">ii", blob[:8])
    pos = 8
    if legacy_uid == -1 and legacy_dc == -1:
        if pos + 16 > len(blob):
            raise ValueError("Truncated modern 64-bit MTP authorization header")
        user_id, main_dc_id, keys_count = struct.unpack(">qii", blob[pos : pos + 16])
        pos += 16
    else:
        user_id = legacy_uid
        main_dc_id = legacy_dc
        keys_count = struct.unpack(">i", blob[pos : pos + 4])[0]
        pos += 4

    if keys_count <= 0 or keys_count > 16:
        raise ValueError(f"Unexpected DC key count in tdata: {keys_count}")

    dc_keys: dict[int, bytes] = {}
    for _ in range(keys_count):
        if pos + 4 + TDATA_AUTH_KEY_BYTES > len(blob):
            raise ValueError("Truncated DC auth_key inside tdata")
        dc_id = struct.unpack(">i", blob[pos : pos + 4])[0]
        pos += 4
        auth_key = blob[pos : pos + TDATA_AUTH_KEY_BYTES]
        pos += TDATA_AUTH_KEY_BYTES
        dc_keys[dc_id] = auth_key

    chosen_dc = main_dc_id if main_dc_id in dc_keys else next(iter(dc_keys))
    return ParsedTdataSession(dc_id=chosen_dc, user_id=int(user_id), auth_key=dc_keys[chosen_dc])


def extract_tdata_session(tdata_dir: Path, *, passcode: str = "") -> ParsedTdataSession:
    """Decrypt `key_datas` and main session TDF from `tdata_dir`."""
    resolved = Path(tdata_dir)
    if (resolved / "tdata").is_dir() and not (resolved / "key_datas").is_file():
        resolved = resolved / "tdata"
    key_datas_path = resolved / "key_datas"
    if not key_datas_path.is_file():
        raise FileNotFoundError(f"Missing key_datas in {resolved}")

    _, kd_body = _read_tdf(key_datas_path)
    salt, pos = _read_qbytearray(kd_body, 0)
    key_encrypted, _ = _read_qbytearray(kd_body, pos)

    passcode_key = _derive_local_key(salt, passcode=passcode)
    secret_key = _decrypt_local_payload(key_encrypted, passcode_key)

    sess_tdf = _find_main_auth_tdf(resolved)
    _, sess_body = _read_tdf(sess_tdf)
    sess_encrypted, _ = _read_qbytearray(sess_body, 0)
    stream = _decrypt_local_payload(sess_encrypted, secret_key)

    sp = 0
    while sp + 4 <= len(stream):
        blk_id = struct.unpack(">I", stream[sp : sp + 4])[0]
        sp += 4
        if blk_id == TDATA_DBI_MTP_AUTHORIZATION:
            auth_blob, sp = _read_qbytearray(stream, sp)
            return _parse_mtp_authorization_blob(auth_blob)
        raise ValueError(f"Unsupported TDF stream block id: {blk_id}")
    raise ValueError("MTP authorization block not found in tdata")


def write_telethon_session(parsed: ParsedTdataSession, output_path: Path) -> Path:
    """Atomically write a Telethon SQLite `.session` with restrictive 0600 permissions."""
    if parsed.dc_id not in DC_ENDPOINTS:
        raise ValueError(f"Unknown Telegram DC id: {parsed.dc_id}")
    ip_addr, port = DC_ENDPOINTS[parsed.dc_id]
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tdata_conv_") as tmp_dir:
        tmp_stem = Path(tmp_dir) / "out"
        session = SQLiteSession(str(tmp_stem))
        session.set_dc(parsed.dc_id, ip_addr, port)
        from telethon.crypto import AuthKey

        session.auth_key = AuthKey(data=parsed.auth_key)
        session.save()
        session.close()
        tmp_file = tmp_stem.with_suffix(".session")
        shutil.copyfile(tmp_file, dest)
    os.chmod(dest, 0o600)
    return dest


def _mask_phone(raw_phone: str | None) -> str:
    digits = re.sub(r"\D", "", str(raw_phone or ""))
    if len(digits) <= 4:
        return "***"
    return f"+{digits[:4]}***{digits[-2:]}"


def _load_dotenv_if_needed() -> None:
    env_path = WORKTREE_ROOT / ".env"
    if not env_path.is_file():
        return
    for ln in env_path.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def _verify_session_live(session_path: Path) -> dict[str, object]:
    _load_dotenv_if_needed()
    api_id = int(os.getenv("TG_API_ID") or "0")
    api_hash = str(os.getenv("TG_API_HASH") or "").strip()
    if api_id <= 0 or not api_hash:
        raise RuntimeError("Missing TG_API_ID / TG_API_HASH in environment or .env")
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()
    try:
        authed = await client.is_user_authorized()
        me = await client.get_me() if authed else None
        return {
            "authorized": bool(authed),
            "user_id": int(getattr(me, "id", 0) or 0) if me is not None else 0,
            "username": str(getattr(me, "username", "") or "") if me is not None else "",
            "phone_masked": _mask_phone(getattr(me, "phone", None)) if me is not None else "",
            "restricted": bool(getattr(me, "restricted", False)) if me is not None else None,
        }
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert TDesktop tdata to Telethon .session")
    parser.add_argument("--tdata", required=True, help="Path to tdata directory (or its parent)")
    parser.add_argument("--out", required=True, help="Destination .session file path")
    parser.add_argument("--passcode", default="", help="Optional local TDesktop passcode")
    parser.add_argument("--verify", action="store_true", help="Run live Telegram get_me() check")
    args = parser.parse_args(argv)

    parsed = extract_tdata_session(Path(args.tdata), passcode=args.passcode)
    out_path = write_telethon_session(parsed, Path(args.out))
    print(f"converted: out={out_path} dc_id={parsed.dc_id} user_id={parsed.user_id}", flush=True)
    if args.verify:
        info = asyncio.run(_verify_session_live(out_path))
        print(f"live_verify: {info}", flush=True)
        if not info.get("authorized"):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
