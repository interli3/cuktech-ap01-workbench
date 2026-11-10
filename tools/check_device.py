"""只读体检：列出米家账号里的 AP01，告诉你现在是什么版本、能不能降级。

这个脚本**绝对不写设备**，只发 miIO.info / miIO.get_ota_state 两个查询。
降级前后各跑一次：
    降级前  → 看是不是 1.0.2_0041 且 ota 状态是 idle
    降级后  → 看是不是 1.0.2_0031，life 有没有重新从小数字开始涨

用法：
    python tools/check_device.py
    python tools/check_device.py --did 你的设备DID     # 只看指定那一台
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ap01_config as cfg  # noqa: E402


def hint(message: str):
    print(message, file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--did", default="", help="只看指定 DID；留空就列账号下所有 AP01")
    args = parser.parse_args()

    if not cfg.UPSTREAM or not (cfg.UPSTREAM / "features").is_dir():
        hint(
            "[停止] 没找到上游工具仓库。\n"
            f"       当前 upstream.repo = {cfg.UPSTREAM}\n"
            "       先按 README 第 0 步把 cuktech-ap01-firmware-tools 克隆到本地，\n"
            "       再把 config.json 的 upstream.repo 改成它的绝对路径。"
        )
        return 2
    if not cfg.ENV_FILE.is_file():
        hint(
            f"[停止] 没找到小米登录态：{cfg.ENV_FILE}\n"
            "       在上游仓库目录里执行下面这条，用米家 App 扫码登录一次即可：\n"
            "         python app/ap01_official_firmware.py login\n"
            "       登录态会写进上游仓库的 env/mi-cloud.env，本仓库不会保存你的账号密码。"
        )
        return 2

    sys.path[:0] = [str(cfg.UPSTREAM)] + ([str(cfg.DEPS)] if cfg.DEPS else [])
    try:
        import requests
        from features.official_firmware_source import XiaomiCloudClient
    except ImportError as error:
        hint(f"[停止] 上游工具缺依赖：{error.name}。先在仓库根目录执行 python -m pip install -r requirements.txt")
        return 2

    session = requests.Session()
    session.trust_env = False
    try:
        client = XiaomiCloudClient(env_file=cfg.ENV_FILE, session=session, timeout=12)
        devices = client.devices()
    except Exception as error:
        hint(f"[停止] 连不上小米云：{type(error).__name__}。检查网络、代理和 env/mi-cloud.env 是否过期（过期就重新 login）。")
        return 2
    finally:
        session.close()

    ap01 = [d for d in devices if str(d.get("model", "")).startswith("njcuk.enstor.ap01")]
    if args.did:
        ap01 = [d for d in ap01 if str(d.get("did")) == args.did]
    if not ap01:
        hint("[停止] 这个米家账号下没找到 AP01。确认扫码用的是绑定了这块屏的那个账号。")
        return 1

    print(f"找到 {len(ap01)} 台 AP01：\n")
    for device in ap01:
        did = str(device.get("did"))
        ip = device.get("localip", "?")
        online = device.get("isOnline") is True
        print(f"   DID {did}   局域网 IP {ip}   {'在线' if online else '离线'}")
        try:
            info = client.rpc(did, "miIO.info", [])
            ota = client.rpc(did, "miIO.get_ota_state", [])
            result = info.get("result") if isinstance(info.get("result"), dict) else {}
            version = result.get("fw_ver", "?")
            life = result.get("life", "?")
            model = result.get("model", "?")
            state = ota.get("result") if ota.get("code") == 0 else "?"
        except Exception as error:
            print(f"      查询失败：{type(error).__name__}（设备可能在重启，等 1 分钟再试）")
            continue
        print(f"      型号 {model}   固件 {version}   运行秒数 life={life}   OTA={state}")
        if version == "1.0.2_0041" and state == ["idle"]:
            print("      → 可以降级：当前是 0041 且空闲，按 docs/02 走。")
        elif version == "1.0.2_0031":
            print("      → 已经是 0031，不用降级，直接去装加载器（README 第 3、4 步）。")
        elif version == "1.0.2_0055":
            print("      → 是 0055。本仓库没有可用的降级路线，不要刷，见 docs/03。")
        else:
            print("      → 状态不明确，先别动设备，把上面这行发给懂的人看。")
        print()

    print("提示：把这里的 DID 和局域网 IP 填进 config.json 的 device.did / device.device_ip。")
    print("      如果账号里有多台，一次只操作一台，别把 DID 填混了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
