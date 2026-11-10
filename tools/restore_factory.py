"""把一台 AP01 恢复成原厂 1.0.2_0031（正常未签名 OTA）。

默认只做离线检查；真正写设备必须同时加 --go 和 --did。
一次只允许一台，写命令只发一次，失败不自动重试。
"""
from pathlib import Path
import argparse
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import socket
import ssl
import sys
import threading
import time

import ap01_config as cfg

ROOT = Path(__file__).resolve().parents[1]
REPO = cfg.UPSTREAM
IMAGE = cfg.resolve(cfg.get('firmware', 'stock_0031'))
IMAGE_SHA = '8a721fc8ef25458d415b2460e4a251e0503a82f7743fdff85b12612190e5c1cb'
IMAGE_MD5 = '469a7329d496f81ae8625c4d76ccf56d'
MODEL = cfg.MODEL
HW_VER = cfg.HW_VER
ALLOWED = cfg.get('allowed_dids', default={}) or {}
SERVE = cfg.JOURNAL_DIR / 'ap01-ota-image.bin'


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def record(stream, event, **fields):
    import os
    stream.write(json.dumps({'at': time.time(), 'event': event, **fields}, ensure_ascii=False) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def state(client, did):
    try:
        info = client.rpc(did, 'miIO.info', [])
        ota = client.rpc(did, 'miIO.get_ota_state', [])
    except Exception:
        return None
    if info.get('code') != 0 or not isinstance(info.get('result'), dict):
        return None
    data = info['result']
    require(data.get('model') == MODEL and data.get('hw_ver') == HW_VER, 'Identity changed; stop')
    return {'version': data.get('fw_ver'), 'life': data.get('life'), 'state': ota.get('result')}


def execute(did, body):
    sys.path[:0] = [str(REPO)] + ([str(cfg.DEPS)] if cfg.DEPS else [])
    import requests
    from features.official_firmware_source import XiaomiCloudClient
    session = requests.Session()
    session.trust_env = False
    session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
    client = XiaomiCloudClient(env_file=cfg.ENV_FILE, session=session, timeout=12)
    devices = [d for d in client.devices() if str(d.get('did')) == did]
    require(len(devices) == 1 and devices[0].get('model') == MODEL
            and devices[0].get('isOnline') is True, 'Target unavailable')
    peer = str(ipaddress.IPv4Address(devices[0].get('localip', '')))
    require(ipaddress.ip_address(peer).is_private, 'Invalid target LAN address')
    before = state(client, did)
    require(before is not None and before['version'] == '1.0.2_0031'
            and before['state'] == ['idle'], 'Target must be idle stock 0031')
    require(isinstance(before['life'], int), 'No uptime baseline')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((peer, 54321))
        bind = route.getsockname()[0]
    require(bind != peer and ipaddress.ip_address(bind).is_private, 'Invalid host route')

    spec = importlib.util.spec_from_file_location('existing_sender', REPO / 'tools/ota_https_serve.py')
    sender = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sender)
    sender.FIRMWARE = SERVE
    transferred = threading.Event()
    base_handler = sender.make_handler()

    class Handler(base_handler):
        def do_GET(self):
            if self.client_address[0] not in {bind, peer}:
                self.send_error(403)
                return
            super().do_GET()

    def transfer_log(message):
        if message.startswith(f'SERVED /miio_fw/{SERVE.name} {len(body)} bytes to {peer} '):
            transferred.set()
            print(json.dumps({'at': time.time(), 'event': 'target_complete_send', 'bytes': len(body)}), flush=True)
        elif message.startswith('SEND_ABORTED'):
            print(json.dumps({'at': time.time(), 'event': 'send_aborted'}), flush=True)

    sender.log = transfer_log
    server = sender.ThreadingHTTPServer((bind, 443), Handler)
    server.daemon_threads = True
    thread = None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers('ECDHE-RSA-AES128-GCM-SHA256')
        context.load_cert_chain(sender.CERT_FILE, sender.KEY_FILE)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        test_context = ssl._create_unverified_context()
        test_context.minimum_version = test_context.maximum_version = ssl.TLSVersion.TLSv1_2
        test_context.set_ciphers('ECDHE-RSA-AES128-GCM-SHA256')
        connection = http.client.HTTPSConnection(bind, 443, context=test_context, timeout=20)
        try:
            connection.request('GET', '/miio_fw/' + SERVE.name)
            response = connection.getresponse()
            require(response.status == 200 and response.read() == body, 'Local readback differs')
        finally:
            connection.close()
        require(not transferred.is_set(), 'Self-test counted as target download')
        require(state(client, did)['state'] == ['idle'], 'State changed before dispatch')
        cfg.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        journal_path = cfg.JOURNAL_DIR / f'restore-factory-{did}-{time.strftime("%Y%m%d_%H%M%S")}.jsonl'
        with journal_path.open('x', encoding='utf-8') as journal:
            record(journal, 'begin', did=did, target=ALLOWED.get(did, '未登记设备'), sha256=IMAGE_SHA,
                   initial_life=before['life'], peer=peer)
            record(journal, 'dispatch_intent', method='miIO.ota')
            response = client.rpc(did, 'miIO.ota', {
                'app_url': f'https://{bind}/miio_fw/{SERVE.name}',
                'file_md5': IMAGE_MD5, 'proc': 'dnld install', 'mode': 'normal',
                'signed_file': False, 'original_length': len(body),
                'install': '1', 'cert_verify': 'optional',
            })
            accepted = response.get('code') == 0 and response.get('result') == ['ok']
            record(journal, 'dispatch_result', accepted=accepted, code=response.get('code'))
            require(accepted, 'Dispatch not accepted; no retry')
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                observed = state(client, did)
                if observed is None:
                    record(journal, 'read_unavailable')
                else:
                    record(journal, 'observed', **observed, complete_send=transferred.is_set())
                    rebooted = isinstance(observed['life'], int) and observed['life'] < before['life']
                    if transferred.is_set() and rebooted and observed['version'] == '1.0.2_0031':
                        record(journal, 'stock_0031_restored', physical_acceptance=False)
                        print('STOCK_0031_RESTORED', flush=True)
                        return
                time.sleep(5)
            record(journal, 'unconfirmed_timeout', complete_send=transferred.is_set())
    finally:
        if thread is not None:
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--did', required=True)
    parser.add_argument('--go', action='store_true', help='Actually write to the device')
    args = parser.parse_args()
    if args.did in cfg.EXAMPLE_DIDS:
        raise SystemExit(f'--did {args.did} 还是示例值，先改成你自己的设备 DID 再执行。')
    if ALLOWED and args.did not in ALLOWED:
        raise SystemExit('该 DID 不在 allowed_dids 白名单里，拒绝执行。')
    body = IMAGE.read_bytes()
    require(hashlib.sha256(body).hexdigest() == IMAGE_SHA
            and hashlib.md5(body).hexdigest() == IMAGE_MD5
            and body[:4] == b'BFNP', 'Stock image identity mismatch')
    SERVE.parent.mkdir(parents=True, exist_ok=True)
    SERVE.write_bytes(body)
    print(json.dumps({'mode': 'EXECUTE' if args.go else 'DRY_RUN',
                      'did': args.did, 'target': ALLOWED.get(args.did, '未登记设备'),
                      'other_device_untouched': True,
                      'image': IMAGE.name, 'bytes': len(body),
                      'max_mutating_dispatches': 1,
                      'note': 'Only valid while the device is online on 1.0.2_0031'},
                     ensure_ascii=False, indent=2))
    if args.go:
        execute(args.did, body)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('STOPPED: ' + type(error).__name__ + '; no automatic retry', file=sys.stderr)
        if type(error) is RuntimeError:
            print(str(error), file=sys.stderr)
        sys.exit(1)
