"""只读充电功率采样：独立线程，每 5 秒读一次四个端口。

端口值是一个 32 位整数：
    电压 = 最高字节 / 10
    电流 = 次高字节 / 10
    功率 = 电压 × 电流

同一个 siid=4 服务下还读两个附加属性：
    piid=1   功率分配模式（1 AI智配 / 2 极速单充 / 3 均衡输出 / 4 自定义）
    piid=12  C1/C2 握手协议，高 16 位是 C1，低 16 位是 C2

握手值的解码见下面 PPS_GEARS / PD_GEARS 的说明，只识别与原厂屏对得上的编码。

读失败不会写入 0，旧样本超过 15 秒就返回 None。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import ap01_config as cfg

PORTS = {4: "C1", 7: "C2", 9: "C3", 11: "USB-A"}

# 握手半字的低字节 -> 协商档位瓦数。
# 只收与原厂屏逐条核对过的组合，不做「瓦数 x10 取低字节」的通用反推：
# 原厂固件能显示带小数的 W，单字节可能有歧义，猜错会把 28W 实际输出标成「28W 档位」。
PPS_GEARS = {0xE8: 100, 0x26: 55, 0xC2: 45}
PD_GEARS = {0x96: 15}

MODES = {1: "AI智配", 2: "极速单充", 3: "均衡输出", 4: "自定义"}


def decode_ports(rows):
    ports = {}
    mode = next((MODES.get(row.get("value")) for row in rows
                 if row.get("siid") == 4 and row.get("piid") == 1
                 and row.get("code") == 0), None)
    handshake = next((row.get("value") for row in rows
                      if row.get("siid") == 4 and row.get("piid") == 12
                      and row.get("code") == 0 and type(row.get("value")) is int), None)
    for row in rows:
        value = row.get("value")
        if row.get("siid") != 4 or row.get("piid") not in PORTS or row.get("code") != 0:
            continue
        # bool 是 int 的子类，false 会被误当成 0，必须显式排除。
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            continue
        volts = ((value >> 24) & 255) / 10
        amps = ((value >> 16) & 255) / 10
        ports[PORTS[row["piid"]]] = {
            "volts": volts,
            "amps": amps,
            "watts": volts * amps,
        }
    if len(ports) != 4:
        return None
    if handshake is not None:
        # 只给「此刻确实在输出」的口绑定握手状态：空闲口也会带着上一次的编码，
        # 直接显示会让人以为没插东西却在跑 100W。
        for name, shift in (("C1", 16), ("C2", 0)):
            code = (handshake >> shift) & 0xFFFF
            kind, tier = code >> 8, code & 255
            if ports[name]["watts"] <= 0:
                continue
            ports[name]["protocol"] = {7: "PD", 8: "PPS"}.get(kind)
            ports[name]["gear"] = "5V" if kind == 2 else None
            ports[name]["gear_watts"] = (PPS_GEARS.get(tier) if kind == 8 else
                                         PD_GEARS.get(tier) if kind == 7 else None)
    active = [name for name, port in ports.items() if port["watts"] > 0]
    return {
        "at": time.time(),
        "ports": ports,
        "mode": mode,
        "watts": sum(port["watts"] for port in ports.values()),
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
        sys.path.insert(0, str(cfg.UPSTREAM))
        if cfg.DEPS:
            sys.path.insert(0, str(cfg.DEPS))
        try:
            import requests
            from features.official_firmware_source import XiaomiCloudClient
        except ImportError as error:
            # 上游仓库没配好时，只影响充电这一块，别的照常工作，不要吐 traceback 吓人
            print(
                "[提示] 充电数据没启动：找不到上游仓库里的云客户端。\n"
                f"       缺失的模块：{error.name}\n"
                "       把 config.json 的 upstream.repo 指向 cuktech-ap01-firmware-tools 的实际路径，\n"
                "       并确认已经 python -m pip install -r requirements.txt。\n"
                "       其余部分照常运行，充电位置会显示「等待充电数据」。",
                file=sys.stderr, flush=True,
            )
            return

        session = requests.Session()
        session.trust_env = False
        try:
            try:
                client = XiaomiCloudClient(env_file=cfg.ENV_FILE, session=session, timeout=6)
            except Exception as error:
                print(
                    f"[提示] 充电数据没启动：小米云客户端初始化失败（{type(error).__name__}）。\n"
                    f"       检查 {cfg.ENV_FILE} 是否存在、是否已经完成小米账号登录。",
                    file=sys.stderr, flush=True,
                )
                return
            while not self.stop.is_set():
                try:
                    response = client.rpc(
                        cfg.DID,
                        "get_properties",
                        [{"did": cfg.DID, "siid": 4, "piid": iid} for iid in (1, *PORTS, 12)],
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
                    # 旧样本自然过期；绝不用假的 0W 覆盖。
                    pass
                self.stop.wait(5)
        finally:
            session.close()

    def sample(self):
        with self.lock:
            data = self.data
        return data if data and time.time() - data["at"] < 15 else None

    def close(self):
        self.stop.set()
