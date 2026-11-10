"""真实 Windows 传感器 + 真实 PresentMon 帧事件。

拿不到就写 None，界面显示「—」，绝不编数。数据来源：
  CPU 功耗/温度/风扇  LibreHardwareMonitor（由 cpu_sensor.ps1 常驻读出）
  CPU 占用            Windows「Processor Information / % Processor Utility」
  GPU 功耗/温度/显存  NVIDIA NVML（只有 N 卡有）
  GPU 占用            Windows GPU Engine 3D 最忙引擎
  帧率                PresentMon 真实帧事件
"""
from __future__ import annotations

import csv
import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import warnings
from collections import deque
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ap01_config import get, resolve

try:
    import psutil  # noqa: E402
except ImportError:
    raise SystemExit(
        "缺少依赖 psutil。先在仓库根目录装一遍依赖：\n"
        "    python -m pip install -r requirements.txt\n"
    ) from None

with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    try:
        import pynvml as nvml
    except ImportError:  # 没有 N 卡驱动也能跑，GPU 那几格显示「—」
        nvml = None

HERE = Path(__file__).resolve().parent
LOGS = resolve(get("hardware", "log_dir", default="runtime/logs"))
LOGS.mkdir(parents=True, exist_ok=True)

# PresentMon：MSI Afterburner / RTSS 会自带一份，也可以自己下官方 exe 后在这里填路径
PRESENTMON_CANDIDATES = [
    get("hardware", "presentmon", default=""),
    r"C:\Program Files (x86)\RivaTuner Statistics Server\Plugins\Client\PresentMonDataProvider\PresentMon-2.3.1-x64.exe",
    r"C:\Program Files\RivaTuner Statistics Server\Plugins\Client\PresentMonDataProvider\PresentMon-2.3.1-x64.exe",
    str(HERE / "PresentMon-2.3.1-x64.exe"),
]
# LibreHardwareMonitor 的 dll，同样由 Afterburner/RTSS 自带
LHM_CANDIDATES = [
    get("hardware", "lhm_dll", default=""),
    r"C:\Program Files (x86)\RivaTuner Statistics Server\Plugins\Client\LHMDataProvider\LibreHardwareMonitorLib.dll",
    r"C:\Program Files\RivaTuner Statistics Server\Plugins\Client\LHMDataProvider\LibreHardwareMonitorLib.dll",
    str(HERE / "LibreHardwareMonitorLib.dll"),
]
# 只统计「真正在打游戏/跑渲染」的进程，这些系统外壳类进程的帧事件是噪声
IGNORED = {
    "dwm.exe", "explorer.exe", "chatgpt.exe", "codex.exe", "gamepp.exe",
    "msedgewebview2.exe", "steamwebhelper.exe", "steam.exe", "searchhost.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "nvidia overlay.exe",
    "nvidia share.exe", "chrome.exe", "msedge.exe",
}
IGNORED |= {name.lower() for name in get("hardware", "ignore_processes", default=[]) or []}


def find_presentmon():
    for candidate in PRESENTMON_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def find_lhm():
    for candidate in LHM_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def foreground_pid():
    """当前最前面那个窗口属于哪个进程——用来在多个游戏里优先选它。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value


class Collector:
    def __init__(self, session_name):
        self.lock = threading.Lock()
        self.cpu = None
        self.frames = {}
        self.selected_pid = None
        self.session = session_name
        self.presentmon = find_presentmon()
        self.stderr = open(LOGS / "metrics-sensors-error.log", "a", encoding="utf-8")

        lhm = find_lhm()
        if not lhm:
            print(
                "[提示] 没找到 LibreHardwareMonitorLib.dll，CPU 功耗/温度/风扇会在屏上显示「—」。\n"
                "       装 MSI Afterburner（带 RTSS）一般就有，或在 config.json 里写 hardware.lhm_dll。",
                file=sys.stderr, flush=True,
            )
        command = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(HERE / "cpu_sensor.ps1"), "-ParentPid", str(os.getpid()),
        ]
        if lhm:
            command += ["-LhmDll", lhm]
        self.cpu_process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read_cpu, daemon=True).start()

        self.fps_process = None
        if self.presentmon:
            # 上次被强杀可能留下同名的 ETW 会话，先精确清掉自己那一个
            subprocess.run(
                [self.presentmon, "--session_name", self.session, "--terminate_existing_session"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.fps_process = subprocess.Popen(
                [self.presentmon, "--output_stdout", "--no_console_stats", "--v1_metrics",
                 "--no_track_gpu", "--no_track_input", "--session_name", self.session],
                stdout=subprocess.PIPE, stderr=self.stderr, text=True, encoding="utf-8",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._read_frames, daemon=True).start()
        else:
            print(
                "[提示] 没找到 PresentMon，帧率那一格会显示「—」。\n"
                "       装 MSI Afterburner / RTSS 会自带，或在 config.json 里写 hardware.presentmon。",
                file=sys.stderr, flush=True,
            )

        self.gpu = None
        if nvml is not None:
            try:
                nvml.nvmlInit()
                self.gpu = nvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                self.gpu = None
        psutil.cpu_percent(None)

    def _read_cpu(self):
        for line in self.cpu_process.stdout:
            try:
                row = json.loads(line)
                with self.lock:
                    self.cpu = row
            except ValueError:
                continue

    def _read_frames(self):
        for row in csv.DictReader(self.fps_process.stdout):
            try:
                app = row["Application"]
                if app.lower() in IGNORED:
                    continue
                pid = int(row["ProcessID"])
                moment = float(row["TimeInSeconds"])
                duration = float(row["msBetweenPresents"])
                if not math.isfinite(duration) or duration <= 0:
                    continue
                key = (pid, row["SwapChainAddress"])
                with self.lock:
                    entry = self.frames.setdefault(key, {"app": app, "samples": deque(maxlen=5000), "seen": 0})
                    entry["samples"].append((moment, duration))
                    entry["seen"] = time.monotonic()
                    while entry["samples"] and moment - entry["samples"][0][0] > 1:
                        entry["samples"].popleft()
            except (ValueError, KeyError):
                continue

    def _fps(self):
        now = time.monotonic()
        foreground = foreground_pid()
        with self.lock:
            self.frames = {k: v for k, v in self.frames.items() if now - v["seen"] < 2}
            choices = []
            for (pid, _swap), entry in self.frames.items():
                samples = entry["samples"]
                if len(samples) < 2:
                    continue
                ms = sum(s[1] for s in samples) / len(samples)
                choices.append(
                    {"pid": pid, "application": entry["app"], "fps": 1000 / ms,
                     "frame_ms": ms, "samples": len(samples), "age": now - entry["seen"]}
                )
        if not choices:
            self.selected_pid = None
            return {"fps": None, "application": None, "frame_ms": None, "source": "PresentMon", "samples": 0}
        # 排序优先级：前台窗口 > 上次选中的进程 > 样本多的
        choices.sort(key=lambda c: (c["pid"] == foreground, c["pid"] == self.selected_pid, c["samples"]), reverse=True)
        selected = choices[0]
        self.selected_pid = selected["pid"]
        return selected | {"source": "PresentMon"}

    def sample(self):
        vm = psutil.virtual_memory()
        result = {
            "at": time.time(),
            "cpu_percent": None,
            "cpu_busy_time_percent": psutil.cpu_percent(None),
            "cpu_watts": None,
            "cpu_temperature": None,
            "cpu_fan_rpm": None,
            "gpu_percent": None,
            "gpu_driver_busy_percent": None,
            "gpu_watts": None,
            "gpu_temperature": None,
            "gpu_fan_rpm": None,
            "vram_used_gb": None,
            "vram_total_gb": None,
            "ram_used_gb": vm.used / 2**30,
            "ram_total_gb": vm.total / 2**30,
            "ram_percent": vm.percent,
            "cpu_power_source": "LibreHardwareMonitor CPU Package",
            "cpu_usage_source": "Windows Processor Information % Processor Utility",
            "gpu_source": "NVIDIA NVML power/temperature/memory",
            "gpu_usage_source": "Windows GPU Engine 3D busiest engine, summed across processes",
        }
        with self.lock:
            cpu = self.cpu
        if cpu and time.time() - cpu.get("at", 0) < 3:
            result["cpu_watts"] = cpu.get("watts")
            result["cpu_temperature"] = cpu.get("temperature")
            result["cpu_percent"] = cpu.get("usage")
            result["cpu_fan_rpm"] = cpu.get("cpu_fan_rpm")
            result["gpu_percent"] = cpu.get("gpu_3d")
        if self.gpu is not None:
            try:
                result["gpu_watts"] = nvml.nvmlDeviceGetPowerUsage(self.gpu) / 1000
            except Exception:
                pass
            try:
                result["gpu_driver_busy_percent"] = nvml.nvmlDeviceGetUtilizationRates(self.gpu).gpu
            except Exception:
                pass
            try:
                result["gpu_temperature"] = nvml.nvmlDeviceGetTemperature(self.gpu, nvml.NVML_TEMPERATURE_GPU)
            except Exception:
                pass
            try:
                result["gpu_fan_rpm"] = nvml.nvmlDeviceGetFanSpeedRPM(self.gpu)
            except Exception:
                pass
            try:
                mem = nvml.nvmlDeviceGetMemoryInfo(self.gpu)
                result["vram_used_gb"], result["vram_total_gb"] = mem.used / 2**30, mem.total / 2**30
            except Exception:
                pass
        result["frames"] = self._fps()
        result["sensor_process_alive"] = self.cpu_process.poll() is None
        result["presentmon_alive"] = self.fps_process.poll() is None if self.fps_process else False
        return result

    def close(self):
        if self.presentmon:
            subprocess.run(
                [self.presentmon, "--session_name", self.session, "--terminate_existing_session"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        if self.fps_process:
            try:
                self.fps_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.fps_process.terminate()
        # CPU 那个 PowerShell 会盯着父进程活着，父进程一退它自己关库退出
        if self.gpu is not None:
            try:
                nvml.nvmlShutdown()
            except Exception:
                pass


if __name__ == "__main__":
    collector = Collector(f"AP01_Probe_{os.getpid()}")
    try:
        for _ in range(5):
            time.sleep(1)
            print(json.dumps(collector.sample(), ensure_ascii=False), flush=True)
    finally:
        collector.close()
