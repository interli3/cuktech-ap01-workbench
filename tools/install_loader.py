"""把自定义显示加载器装到一台 AP01 上（默认端口 18766）。

默认只做离线检查，真正写设备必须加 --execute-once。
安装前会核对 DID、型号、版本、局域网 IP、电脑路由、显示服务健康状态和 GIF。
事务文件存在就拒绝重跑。
"""
from pathlib import Path
import argparse
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import ssl
import sys
import threading
import time

import ap01_config as cfg

REPO = cfg.UPSTREAM
ROOT = cfg.STATE_DIR
IMAGE = cfg.resolve(cfg.get('firmware', 'loader'))
JOURNAL = cfg.JOURNAL_DIR / 'install-loader.jsonl'
SHA = str(cfg.get('firmware', 'loader_sha256', default=''))
SERVE = cfg.JOURNAL_DIR / 'ap01-ota-image.bin'
BIND = cfg.BIND
BRIDGE_PORT = cfg.DISPLAY_PORT
DID = cfg.DID
PEER_EXPECTED = cfg.DEVICE_IP
MODEL = cfg.MODEL


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def record(stream, event, **fields):
    stream.write(json.dumps({'at': time.time(), 'event': event, **fields}, ensure_ascii=False) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def request_local(path):
    host = BIND if BIND not in ('0.0.0.0', '') else '127.0.0.1'
    connection = http.client.HTTPConnection(host, BRIDGE_PORT, timeout=5)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        require(response.status == 200, 'Bridge HTTP failure')
        return response.read()
    finally:
        connection.close()


def read_state(client):
    info = client.rpc(DID, 'miIO.info', [])
    ota = client.rpc(DID, 'miIO.get_ota_state', [])
    progress = client.rpc(DID, 'miIO.get_ota_progress', [])
    if info.get('code') != 0 or not isinstance(info.get('result'), dict):
        return None
    data = info['result']
    require(data.get('model') == MODEL and data.get('hw_ver') == 'BL618-BL4', 'Identity changed')
    require(data.get('fw_ver') == '1.0.2_0031', 'Firmware changed')
    return {'version': data['fw_ver'], 'life': data.get('life'),
            'state': ota.get('result'), 'progress': progress.get('result')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-once', action='store_true')
    args = parser.parse_args()
    body = IMAGE.read_bytes()
    require(hashlib.sha256(body).hexdigest() == SHA and body[:4] == b'BFNP', 'Image changed')
    SERVE.parent.mkdir(parents=True, exist_ok=True)
    SERVE.write_bytes(body)
    require(not JOURNAL.exists(), 'Existing install transaction; never resend')
    print(json.dumps({'mode': 'execute' if args.execute_once else 'offline',
                      'image_sha256': SHA, 'target': DID,
                      'other_target_forbidden': True, 'bytes': len(body),
                      'bridge_port': BRIDGE_PORT}, ensure_ascii=False), flush=True)
    if not args.execute_once:
        return
    cfg.require_identity()
    sys.path[:0] = [str(REPO)] + ([str(cfg.DEPS)] if cfg.DEPS else [])
    import requests
    from features.official_firmware_source import XiaomiCloudClient
    session = requests.Session()
    session.trust_env = False
    session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
    client = XiaomiCloudClient(env_file=cfg.ENV_FILE, session=session, timeout=12)
    devices = [d for d in client.devices() if str(d.get('did')) == DID]
    require(len(devices) == 1 and devices[0].get('model') == MODEL
            and devices[0].get('isOnline') is True, 'Fixed target unavailable')
    peer = devices[0]['localip']
    require(peer == PEER_EXPECTED, 'LAN address changed; firewall and route require review')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((peer, 54321))
        require(route.getsockname()[0] == BIND, 'Host route changed')
    health = json.loads(request_local('/health'))
    require(health.get('ok') is True, 'Bridge unhealthy')
    gif = request_local('/screen.gif')
    require(gif[:6] == b'GIF89a' and gif[-1:] == b';', 'Artwork invalid')
    from PIL import Image as PillowImage
    with PillowImage.open(io.BytesIO(gif)) as artwork:
        require(artwork.size == (320, 240) and artwork.n_frames >= 2,
                'Bridge artwork dimensions/frames invalid')
    initial = read_state(client)
    require(initial is not None and initial['state'] == ['idle'], 'Not idle stock 0031')
    require(isinstance(initial['life'], int), 'No uptime baseline')
    spec = importlib.util.spec_from_file_location('existing_sender', REPO / 'tools/ota_https_serve.py')
    sender = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sender)
    sender.FIRMWARE = SERVE
    transferred = threading.Event()
    base_handler = sender.make_handler()

    class Handler(base_handler):
        def do_GET(self):
            if self.client_address[0] not in {BIND, peer}:
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
    server = sender.ThreadingHTTPServer((BIND, 443), Handler)
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
        connection = http.client.HTTPSConnection(BIND, 443, context=test_context, timeout=20)
        try:
            connection.request('GET', '/miio_fw/' + SERVE.name)
            response = connection.getresponse()
            require(response.status == 200 and response.read() == body, 'OTA local readback differs')
        finally:
            connection.close()
        require(not transferred.is_set(), 'Self-download counted as target')
        current = read_state(client)
        require(current is not None and current['state'] == ['idle'], 'Preinstall state changed')
        require(hashlib.sha256(IMAGE.read_bytes()).hexdigest() == SHA, 'Preinstall file changed')
        with JOURNAL.open('x', encoding='utf-8') as journal:
            record(journal, 'begin', target=DID, sha256=SHA,
                   initial_life=initial['life'], bridge_port=BRIDGE_PORT)
            record(journal, 'dispatch_intent', method='miIO.ota')
            try:
                response = client.rpc(DID, 'miIO.ota', {
                    'app_url': f'https://{BIND}/miio_fw/{SERVE.name}',
                    'file_md5': hashlib.md5(body).hexdigest(), 'proc': 'dnld install',
                    'mode': 'normal', 'signed_file': False, 'original_length': len(body),
                    'install': '1', 'cert_verify': 'optional',
                })
                accepted = response.get('code') == 0 and response.get('result') == ['ok']
                record(journal, 'dispatch_result', accepted=accepted, code=response.get('code'))
                if not accepted:
                    return
            except Exception as error:
                record(journal, 'dispatch_unknown', exception_type=type(error).__name__)
                return
            deadline = time.monotonic() + 600
            reboot_seen_at = None
            while time.monotonic() < deadline:
                try:
                    observed = read_state(client)
                except RuntimeError:
                    raise
                except Exception:
                    observed = None
                if observed is None:
                    record(journal, 'read_unavailable')
                else:
                    record(journal, 'observed', **observed, complete_send=transferred.is_set())
                    rebooted = isinstance(observed['life'], int) and observed['life'] < initial['life']
                    if rebooted and reboot_seen_at is None:
                        reboot_seen_at = time.time()
                    try:
                        bridge = json.loads(request_local('/health'))
                        target_get = reboot_seen_at is not None and (bridge.get('last_request') or 0) > reboot_seen_at
                    except Exception:
                        target_get = False
                    if transferred.is_set() and rebooted and target_get:
                        record(journal, 'loader_request_and_reboot_observed', physical_acceptance=False)
                        print('TARGET_LOADER_REQUEST_AND_REBOOT_OBSERVED', flush=True)
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


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('STOPPED: ' + type(error).__name__, file=sys.stderr)
        if type(error) is RuntimeError:
            print(str(error), file=sys.stderr)
        sys.exit(1)
