"""AP01 固件小工具：首字节预反转、等长 URL 替换并重算 CRC、校验清单。

为什么需要首字节预反转：
    0041 的写入器在接收固件时会先把第一个字节取反，正常收尾时再翻回来。
    发送原版 0031 时，签名拒绝分支可能提前退出，首字节没被翻回，落盘镜像
    就坏了。先把首字节 0x42 改成 0xBD，设备取反后反而得到正确的 0x42。

用法：
    python patch_firmware.py preinvert --in 原厂_0031.bin --out build/0031_transport.bin
    python patch_firmware.py patch-url --in 成功加载器.bin --out build/loader.bin \
        --old http://192.168.1.160:18765/live.bin \
        --new http://192.168.1.160:18766/live.bin
    python patch_firmware.py verify --dir firmware
"""
from __future__ import annotations

import argparse
import hashlib
import zlib
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"写入 {path}")
    print(f"  长度   {len(data)}")
    print(f"  SHA256 {sha256(data)}")
    print(f"  MD5    {md5(data)}")


def cmd_preinvert(args) -> None:
    original = Path(args.input).read_bytes()
    if not original.startswith(b"BFNP"):
        raise SystemExit("输入文件不是 AP01 固件（缺少 BFNP 头）。")
    changed = bytes([original[0] ^ 0xFF]) + original[1:]
    if changed[1:] != original[1:]:
        raise SystemExit("内部错误：除首字节外还有字节被改动。")
    write(Path(args.output), changed)
    print(f"  首字节 {original[0]:#04x} -> {changed[0]:#04x}（设备写入器会再取反一次）")


def cmd_patch_url(args) -> None:
    old = args.old.encode("utf-8")
    new = args.new.encode("utf-8")
    if len(old) != len(new):
        raise SystemExit(
            f"新旧 URL 长度必须完全一致：old={len(old)} new={len(new)}。"
            "短了用端口号等长替换，长了只能重新编译加载器。"
        )
    data = Path(args.input).read_bytes()
    if not data.startswith(b"BFNP"):
        raise SystemExit("输入文件不是 AP01 固件（缺少 BFNP 头）。")
    count = data.count(old)
    if count == 0:
        raise SystemExit("固件里找不到旧 URL，未做任何修改。")
    patched = data.replace(old, new)
    if len(patched) != len(data):
        raise SystemExit("内部错误：替换后长度变化。")
    # 原厂恢复算法：对除末尾 4 字节外的全部内容算 CRC32，再小端写入。
    crc = zlib.crc32(patched[:-4], 0xFFFFFFFF) ^ 0xFFFFFFFF
    patched = patched[:-4] + crc.to_bytes(4, "little")
    print(f"替换 {count} 处 URL：{args.old} -> {args.new}")
    print(f"尾部 CRC 重算为 {crc:#010x}")
    write(Path(args.output), patched)


def cmd_verify(args) -> None:
    directory = Path(args.dir)
    manifest = directory / "SHA256SUMS.txt"
    if not manifest.is_file():
        raise SystemExit(f"找不到 {manifest}")
    bad = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, name = line.split(maxsplit=1)
        path = directory / name.strip()
        if not path.is_file():
            print(f"缺失 {name}")
            bad += 1
            continue
        actual = sha256(path.read_bytes())
        ok = actual == expected
        print(f"{'OK  ' if ok else 'FAIL'} {name}")
        bad += 0 if ok else 1
    raise SystemExit(1 if bad else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preinvert", help="把固件首字节取反，用于 0041 -> 0031 降级传输")
    p.add_argument("--in", dest="input", required=True)
    p.add_argument("--out", dest="output", required=True)
    p.set_defaults(func=cmd_preinvert)

    p = sub.add_parser("patch-url", help="等长替换固件内的 URL 并重算尾部 CRC")
    p.add_argument("--in", dest="input", required=True)
    p.add_argument("--out", dest="output", required=True)
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    p.set_defaults(func=cmd_patch_url)

    p = sub.add_parser("verify", help="按 SHA256SUMS.txt 校验固件")
    p.add_argument("--dir", default="firmware")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
