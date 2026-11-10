"""所有脚本共用的配置读取。

把仓库根目录的 config.example.json 复制成 config.json，填好自己的设备信息。
也可以用环境变量 AP01_CONFIG 指定另一份配置文件。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("AP01_CONFIG") or ROOT / "config.json")


def _wants_help() -> bool:
    """看 --help 时不该因为还没建 config.json 就报错。"""
    return any(arg in ("-h", "--help") for arg in sys.argv[1:])


def _load() -> dict:
    if not CONFIG_PATH.is_file():
        if _wants_help():
            return {}
        raise SystemExit(
            f"缺少配置文件：{CONFIG_PATH}\n"
            "请先复制 config.example.json 为 config.json，并填写设备 DID、IP 和上游仓库路径。"
        )
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(f"配置文件不是合法 JSON：{CONFIG_PATH}\n{error}") from None


CFG = _load()


def get(*keys: str, default=None):
    """按层级读取配置，例如 get('network', 'bind', default='0.0.0.0')。"""
    node = CFG
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def resolve(value: str | os.PathLike | None) -> Path:
    """相对路径按仓库根目录解析，绝对路径原样返回。

    value 为空时返回仓库根目录兜底：这样 `--help` 能在还没建 config.json
    的时候正常打印，不会先抛一个路径异常。
    """
    if not value:
        return ROOT
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT / path)


DID = str(get("device", "did", default=""))
MODEL = str(get("device", "model", default="njcuk.enstor.ap01"))
HW_VER = str(get("device", "hw_ver", default="BL618-BL4"))
DEVICE_IP = str(get("device", "device_ip", default=""))
BIND = str(get("network", "bind", default="0.0.0.0"))
DISPLAY_PORT = int(get("network", "display_port", default=18766))
UPSTREAM = resolve(get("upstream", "repo", default=""))
DEPS = resolve(get("upstream", "deps", default="")) if get("upstream", "deps") else None
ENV_FILE = UPSTREAM / str(get("upstream", "env_file", default="env/mi-cloud.env"))
STATE_DIR = resolve(get("paths", "state_dir", default="run"))
JOURNAL_DIR = resolve(get("paths", "journal_dir", default="run"))


EXAMPLE_DIDS = {"1234567890", "0987654321"}


def require_identity() -> None:
    """在真正写设备前调用，避免拿示例配置误刷。"""
    if not DID or not DID.isdigit():
        raise SystemExit("config.json 里的 device.did 必须是纯数字的设备 DID。")
    if DID in EXAMPLE_DIDS:
        raise SystemExit(
            f"device.did 还是示例值 {DID}，先改成你自己那块屏的 DID 再执行。\n"
            "写设备前必须能确认目标是真的那台机器。"
        )
    if not DEVICE_IP:
        raise SystemExit("config.json 里的 device.device_ip 不能为空。")
