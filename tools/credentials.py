"""用 Windows DPAPI 保存 DeepSeek 密钥（只对当前 Windows 用户可解）。

    python credentials.py set <key>   # 一次性写入，密钥只经过内存
    python credentials.py check        # 只打印是否已配置和长度
    python credentials.py delete       # 删除密文

磁盘上不会出现明文；换用户或重装系统后需要重新 set。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

import ap01_config as cfg

STORE = cfg.resolve(cfg.get("sources", "deepseek_key_store", default="run/deepseek_key.bin"))


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPCWSTR), ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
kernel32.LocalFree.restype = wintypes.HLOCAL


def _make_blob(data: bytes):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _convert(source, output):
    size = int(output.cbData)
    try:
        return bytes(bytearray(output.pbData[:size]))
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def protect(text: str) -> bytes:
    raw = text.encode("utf-8")
    source, keep = _make_blob(raw)
    output = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return _convert(source, output)


def unprotect(blob: bytes) -> str:
    source, keep = _make_blob(blob)
    output = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return _convert(source, output).decode("utf-8")


def get_key():
    if not STORE.exists():
        return None
    try:
        return unprotect(STORE.read_bytes())
    except Exception:
        return None


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "set":
        if len(sys.argv) < 3:
            raise SystemExit("usage: python credentials.py set <key>")
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_bytes(protect(sys.argv[2]))
        print("stored_encrypted", STORE.exists(), "bytes", STORE.stat().st_size)
    elif action == "delete":
        if STORE.exists():
            STORE.unlink()
        print("deleted", not STORE.exists())
    elif action == "check":
        key = get_key()
        print("configured", key is not None, "length", len(key) if key else 0)
    else:
        raise SystemExit("usage: set <key> | check | delete")


if __name__ == "__main__":
    main()
