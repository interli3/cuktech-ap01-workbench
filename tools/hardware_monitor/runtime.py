"""硬件监测模式（硬件监测屏）一秒钟刷新的实时服务。

它做两件事：
  1. 每秒读一次 Windows 的真实传感器 + 真实帧事件，画成 320x240 的 GIF；
  2. 用两条路把图给设备：
       /screen.gif  普通 HTTP，设备每次来取一张（给老加载器用）
       /live.bin    stream-v2 长连接，一条连接连续推 300 张（给新加载器用）

运行：python tools/hardware_monitor/runtime.py
自检：python tools/hardware_monitor/runtime.py --probe   （不联网，只在本机生成 probe.gif/png）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import socket
import statistics
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))

from ap01_config import BIND, DEVICE_IP, get, resolve  # noqa: E402

# 画面存放目录：老加载器取 screen.gif，新加载器走 /live.bin 不需要落盘
ART = resolve(get("hardware", "art_dir", default="runtime/loader-install"))
LOGS = resolve(get("hardware", "log_dir", default="runtime/logs"))
PORT = int(get("hardware", "port", default=18765))
DASH_PORT = int(get("hardware", "dash_port", default=0))  # 0 = 不开第二个端口
ART.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

LOG = logging.getLogger("ap01-metrics")
LOG.setLevel(logging.INFO)
_handler = RotatingFileHandler(
    LOGS / "realtime-monitor.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(message)s"))
LOG.addHandler(_handler)

from charging import ChargingReader  # noqa: E402
from dashboard import render  # noqa: E402


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.body = None
        self.data = None
        self.sequence = 0
        self.requests = []
        self.started = time.time()
        self.diagnostic = None


STATE = State()
STREAM_FRAMES = 300  # 一条长连接连推 300 秒，之后设备自己重连


def _is_device(address: str) -> bool:
    """判断这个请求是不是屏自己发来的（只对屏记详细日志，避免日志被刷爆）。"""
    return bool(DEVICE_IP) and address == DEVICE_IP


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/live.bin":
            self.stream_frames(parsed.query)
            return
        with STATE.lock:
            body, data, sequence = STATE.body, STATE.data, STATE.sequence
            request_times = list(STATE.requests)
        if parsed.path == "/health":
            intervals = [b - a for a, b in zip(request_times, request_times[1:])]
            payload = {
                "ok": data is not None and time.time() - data["at"] < 3,
                "mode": "system-monitor",
                "uptime": time.time() - STATE.started,
                "sequence": sequence,
                "metrics": data,
                "target_request_count_recent": len(request_times),
                "last_request": request_times[-1] if request_times else None,
                "device_diagnostic": STATE.diagnostic,
                "median_request_interval": statistics.median(intervals) if intervals else None,
            }
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if parsed.path != "/screen.gif":
            self.send_error(404)
            return
        if body is None:
            self.send_error(503)
            return
        started = time.time()
        from_device = _is_device(self.client_address[0])
        if from_device:
            LOG.info(json.dumps({"at": started, "event": "target_request_started", "sequence": sequence}))
        self.send_response(200)
        self.send_header("Content-Type", "image/gif")
        # HTTP/1.0 靠关连接表示结束，让主机先关，设备不用背 TIME_WAIT。
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError as error:
            LOG.info(json.dumps({"at": time.time(), "event": "send_failed", "type": type(error).__name__}))
            return
        if from_device:
            now = time.time()
            with STATE.lock:
                STATE.requests.append(now)
                STATE.requests = STATE.requests[-120:]
            LOG.info(
                json.dumps(
                    {
                        "at": now,
                        "event": "target_image_served",
                        "sequence": sequence,
                        "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "sample_age": now - data["at"],
                        "send_seconds": now - started,
                        "framing": "http10-close",
                    }
                )
            )

    def log_message(self, *_):
        pass  # 屏每秒来一次，默认日志会把磁盘写满

    def stream_frames(self, query):
        try:
            fields = parse_qs(query)
            diagnostic = {key: int(fields.get(key, ["0"])[0]) for key in ["v", "g", "a", "e"]}
            if diagnostic["v"] != 2:
                raise ValueError()
        except ValueError:
            self.send_error(400)
            return
        with STATE.lock:
            data = STATE.data
            if _is_device(self.client_address[0]):
                STATE.diagnostic = diagnostic | {"at": time.time()}
        if (HERE / "network-paused.flag").exists():
            if _is_device(self.client_address[0]):
                with STATE.lock:
                    STATE.requests.append(time.time())
                    STATE.requests = STATE.requests[-120:]
            self.send_error(503)
            return
        if data is None or time.time() - data["at"] > 3:
            self.send_error(503)
            return
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(STREAM_FRAMES * 24576))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        started = time.time()
        from_device = _is_device(self.client_address[0])
        if from_device:
            LOG.info(json.dumps({"at": started, "event": "stream_started", **diagnostic}))
        try:
            for index in range(STREAM_FRAMES):
                # 严格按 started + index 发，避免一秒一秒地累积漂移
                time.sleep(max(0, started + index - time.time()))
                with STATE.lock:
                    body, data, sequence = STATE.body, STATE.data, STATE.sequence
                if body is None or time.time() - data["at"] > 3:
                    LOG.info(json.dumps({"at": time.time(), "event": "stream_source_stale"}))
                    return
                packet = struct.pack("<I", len(body)) + body + bytes(24572 - len(body))
                self.wfile.write(packet)
                self.wfile.flush()
                if from_device:
                    now = time.time()
                    with STATE.lock:
                        STATE.requests.append(now)
                        STATE.requests = STATE.requests[-120:]
                    LOG.info(
                        json.dumps(
                            {
                                "at": now,
                                "event": "target_image_served",
                                "sequence": sequence,
                                "bytes": len(body),
                                "sha256": hashlib.sha256(body).hexdigest(),
                                "sample_age": now - data["at"],
                                "protocol": "stream-v2",
                                "block": index,
                                "connection_at": started,
                            }
                        )
                    )
        except OSError as error:
            LOG.info(json.dumps({"at": time.time(), "event": "stream_send_failed", "type": type(error).__name__}))
        finally:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true", help="只在本机生成 probe.gif/probe.png，不开端口")
    parser.add_argument("--bind", default=BIND, help=f"监听地址，默认 {BIND}")
    parser.add_argument("--port", type=int, default=PORT, help=f"监听端口，默认 {PORT}")
    args = parser.parse_args()

    server = None
    if not args.probe:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        LOG.info(json.dumps({"event": "listening", "bind": args.bind, "port": args.port, "device_ip": DEVICE_IP}))
        print(f"模式 A 服务已启动：http://{args.bind}:{args.port}/screen.gif", flush=True)
        if DASH_PORT:
            dash = ThreadingHTTPServer((args.bind, DASH_PORT), Handler)
            threading.Thread(target=dash.serve_forever, daemon=True).start()
            print(f"健康检查：http://{args.bind}:{DASH_PORT}/health", flush=True)

    from collector import Collector

    name = f"AP01_Probe_{os.getpid()}" if args.probe else "AP01_SystemMonitor"
    collector = Collector(name)
    charging = ChargingReader()
    deadline = time.monotonic()
    try:
        while True:
            data = collector.sample()
            data["charging"] = charging.sample()
            with STATE.lock:
                sequence = STATE.sequence + 1
            body, preview = render(data, sequence)
            with STATE.lock:
                STATE.body, STATE.data, STATE.sequence = body, data, sequence
            if args.probe:
                (HERE / "probe.gif").write_bytes(body)
                preview.save(HERE / "probe.png")
            else:
                tmp = ART / "screen.gif.tmp"
                tmp.write_bytes(body)
                tmp.replace(ART / "screen.gif")
                tmp_json = HERE / "current.json.tmp"
                tmp_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp_json.replace(HERE / "current.json")
                if sequence % 10 == 0:
                    preview.save(HERE / "preview.png")
            if sequence % 10 == 0 or args.probe:
                LOG.info(json.dumps({"event": "sample", "sequence": sequence, **data}, ensure_ascii=False))
            if args.probe and sequence >= 6:
                print(json.dumps({"sequence": sequence, "bytes": len(body), "data": data}, ensure_ascii=False), flush=True)
                return
            deadline += 1
            time.sleep(max(0, deadline - time.monotonic()))
    finally:
        charging.close()
        collector.close()
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    sys.stderr = open(LOGS / "realtime-monitor-error.log", "a", encoding="utf-8", buffering=1)
    main()
