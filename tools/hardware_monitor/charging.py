"""只读取电数据（纯只读，不写设备）。

独立线程每 5 秒问一次云端的四个口及握手协议，云端卡住也不会拖死界面。
三条铁律：
  1. 任何一次失败都保留上一份有效数据，绝不把「读不到」写成「0 瓦」；
  2. 超过 15 秒没有新数据就判定过期，返回 None，界面显示「—」；
  3. 只发 get_properties，永远不发 set_properties。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ap01_config import CFG, DEPS, DID as CONFIG_DID, ENV_FILE, UPSTREAM

PORTS = {4: "C1", 7: "C2", 9: "C3", 11: "USB-A"}
PPS_GEARS = {0xE8: 100, 0x26: 55, 0xC2: 45}  # 与原厂屏实测核对
PD_GEARS = {0x96: 15}  # 与原厂屏和米家实测核对
MODES = {1: "AI智配", 2: "极速单充", 3: "均衡输出", 4: "自定义"}
# hardware.did 优先，没填就用 device.did
DID = str(CFG.get("hardware", {}).get("did") or CONFIG_DID)
SAMPLE_SECONDS = 5
EXPIRE_SECONDS = 15


def decode_ports(rows):
    """把 miIO 的 get_properties 回包翻译成电压/电流/功率。

    编码规则：整数高 24 位是电压（0.1V），次 8 位是电流（0.1A）。
    bool 会被 type(v) is int 挡掉，这是 Python 的坑——True 是 int 的子类。
    """
    ports = {}
    mode = next((MODES.get(r.get("value")) for r in rows if r.get("siid") == 4
                 and r.get("piid") == 1 and r.get("code") == 0), None)
    handshake = next((r.get("value") for r in rows if r.get("siid") == 4
                      and r.get("piid") == 12 and r.get("code") == 0
                      and type(r.get("value")) is int), None)
    for row in rows:
        value = row.get("value")
        if row.get("siid") != 4 or row.get("piid") not in PORTS or row.get("code") != 0:
            continue
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            continue
        volts = ((value >> 24) & 255) / 10
        amps = ((value >> 16) & 255) / 10
        ports[PORTS[row["piid"]]] = {"volts": volts, "amps": amps, "watts": volts * amps}
    if len(ports) != 4:
        return None
    if handshake is not None:
        for name, shift in (("C1", 16), ("C2", 0)):
            code = (handshake >> shift) & 0xFFFF
            kind, tier = code >> 8, code & 255
            if ports[name]["watts"] > 0:
                ports[name]["protocol"] = {7: "PD", 8: "PPS"}.get(kind)
                ports[name]["gear"] = "5V" if kind == 2 else None
                ports[name]["gear_watts"] = (PPS_GEARS.get(tier) if kind == 8 else
                                               PD_GEARS.get(tier) if kind == 7 else None)
    active = [name for name, p in ports.items() if p["watts"] > 0]
    return {
        "at": time.time(),
        "ports": ports,
        "mode": mode,
        "watts": sum(p["watts"] for p in ports.values()),
        "label": active[0] if len(active) == 1 else "USB",
        "source": "base rounded voltage x current",
    }


class ChargingReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        # 依赖上游仓库里的小米云端客户端，以及它可能需要的额外依赖目录
        for path in (UPSTREAM, DEPS):
            if path and str(path) not in sys.path:
                sys.path.insert(0, str(path))
        try:
            import requests
            from features.official_firmware_source import XiaomiCloudClient
        except ImportError as error:
            # 上游仓库没配好时只影响充电那一格，别的地方照常显示，不要吐 traceback
            print(
                "[提示] 充电数据没启动：找不到上游仓库里的云客户端。\n"
                f"       缺失的模块：{error.name}\n"
                "       把 config.json 的 upstream.repo 指向 cuktech-ap01-firmware-tools 的实际路径。\n"
                "       其余传感器照常工作，充电位置会显示「等待充电数据」。",
                file=sys.stderr, flush=True,
            )
            return

        session = requests.Session()
        session.trust_env = False  # 别让系统代理把局域网/云端请求带偏
        try:
            try:
                client = XiaomiCloudClient(env_file=ENV_FILE, session=session, timeout=6)
            except Exception as error:
                print(
                    f"[提示] 充电数据没启动：小米云客户端初始化失败（{type(error).__name__}）。\n"
                    f"       检查 {ENV_FILE} 是否存在、是否已完成小米账号登录。",
                    file=sys.stderr, flush=True,
                )
                return
            while not self.stop.is_set():
                try:
                    response = client.rpc(
                        DID,
                        "get_properties",
                        [{"did": DID, "siid": 4, "piid": iid} for iid in (1, *PORTS, 12)],
                    )
                    data = (
                        decode_ports(response.get("result", []))
                        if response.get("code") == 0
                        else None
                    )
                    if data:
                        with self.lock:
                            self.data = data
                except Exception:
                    pass  # 旧样本会自然过期；失败绝不写成 0 瓦
                self.stop.wait(SAMPLE_SECONDS)
        finally:
            session.close()

    def sample(self):
        with self.lock:
            data = self.data
        return data if data and time.time() - data["at"] < EXPIRE_SECONDS else None

    def close(self):
        self.stop.set()
