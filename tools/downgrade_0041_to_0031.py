"""从原厂 0041 降到原厂 0031（首字节预反转路线）。

默认只做离线检查，真正写设备必须加 --execute-once。
一次运行最多发 3 个写命令：ota.downgrade、miIO.ota、miIO.recovery 各一次。
事务文件存在就拒绝重跑；不要删事务文件来绕过防重。
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
ORIGINAL = cfg.resolve(cfg.get('firmware', 'stock_0031'))
CANDIDATE = cfg.resolve(cfg.get('firmware', 'downgrade_0031'))
TRANSACTION = cfg.JOURNAL_DIR / 'downgrade-0041-to-0031.jsonl'
DID = cfg.DID
MODEL = cfg.MODEL
SERVE = cfg.JOURNAL_DIR / 'ap01-ota-image.bin'
STOCK_SHA = '8a721fc8ef25458d415b2460e4a251e0503a82f7743fdff85b12612190e5c1cb'
WIRE_SHA = '828adbb3ba6087765e9888f40c42177104b4a327088d8e9a2847a35ee61b48de'
WIRE_MD5 = '5b6e53f94fd19b4d458785885620e412'


class ReadUnavailable(RuntimeError):
    """A read-only observation failed; it must not authorize any mutation."""


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def validate_files():
    original, wire = ORIGINAL.read_bytes(), CANDIDATE.read_bytes()
    require(hashlib.sha256(original).hexdigest() == STOCK_SHA, 'Original identity mismatch')
    require(hashlib.sha256(wire).hexdigest() == WIRE_SHA, 'Transport identity mismatch')
    require(hashlib.md5(wire).hexdigest() == WIRE_MD5, 'Transport MD5 mismatch')
    require(len(wire) == len(original) == 6804520, 'Length mismatch')
    require(wire[0] == (original[0] ^ 255) and wire[1:] == original[1:], 'Unexpected difference')
    return wire


def record(stream, event, **fields):
    # Whitelisted facts only; never serialize cloud responses, headers or credentials.
    import os
    stream.write(json.dumps({'at': time.time(), 'event': event, **fields}) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def state(client):
    try:
        info = client.rpc(DID, 'miIO.info', [])
        ota = client.rpc(DID, 'miIO.get_ota_state', [])
    except Exception:
        raise ReadUnavailable('Read-only transport unavailable') from None
    if info.get('code') != 0 or ota.get('code') != 0:
        raise ReadUnavailable('Read-only query unavailable')
    info = info.get('result')
    require(isinstance(info, dict), 'Unexpected info response')
    require(info.get('model') == MODEL and info.get('hw_ver') == 'BL618-BL4', 'Identity changed')
    return info.get('fw_ver'), ota.get('result') == ['idle']


def observe(client, journal):
    try:
        version, idle = state(client)
    except ReadUnavailable:
        record(journal, 'read_unavailable', recovery_allowed=False)
        return None
    record(journal, 'observed', version=version, idle=idle)
    return version, idle


def mutate_once(client, journal, sent, method, params):
    require(method in {'ota.downgrade', 'miIO.ota', 'miIO.recovery'}, 'Method forbidden')
    require(method not in sent, 'Repeat blocked')
    sent.add(method)
    # Durable intent BEFORE dispatch. Unknown outcomes are never retried.
    record(journal, 'dispatch_intent', method=method)
    response = client.rpc(DID, method, params)
    accepted = response.get('code') == 0 and response.get('result') == ['ok']
    record(journal, 'dispatch_result', method=method, accepted=accepted)
    require(accepted, 'Unrecognized response; stop without retry')


def execute(wire):
    sys.path[:0] = [str(REPO)] + ([str(cfg.DEPS)] if cfg.DEPS else [])
    import requests
    from features.official_firmware_source import XiaomiCloudClient
    session = requests.Session()
    session.trust_env = False
    # No HTTP retries; all mutating RPC calls are issued exactly once at most.
    session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
    client = XiaomiCloudClient(env_file=cfg.ENV_FILE, session=session, timeout=12)
    devices = [d for d in client.devices() if str(d.get('did')) == DID]
    require(len(devices) == 1, 'Fixed target absent or ambiguous')
    device = devices[0]
    require(device.get('model') == MODEL and device.get('isOnline') is True, 'Target not online')
    peer = str(ipaddress.IPv4Address(device.get('localip', '')))
    require(ipaddress.ip_address(peer).is_private and not ipaddress.ip_address(peer).is_loopback,
            'Target LAN address unavailable')
    require(state(client) == ('1.0.2_0041', True), 'Target must be idle stock 0041')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((peer, 54321))  # Route selection only: sends no UDP payload.
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
        # Drop request headers entirely. Self-test downloads cannot set this event.
        expected = f'SERVED /miio_fw/{SERVE.name} {len(wire)} bytes to {peer} '
        if message.startswith(expected):
            transferred.set()
            print(json.dumps({'at': time.time(), 'event': 'target_complete_send', 'bytes': len(wire)}), flush=True)
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
            require(response.status == 200 and response.read() == wire, 'HTTPS self-test failed')
        finally:
            connection.close()
        require(not transferred.is_set(), 'Self-test incorrectly counted as target download')
        require(state(client) == ('1.0.2_0041', True), 'State changed before dispatch')
        # Exclusive creation is also the persistent inter-process/rerun guard.
        with TRANSACTION.open('x', encoding='utf-8') as journal:
            record(journal, 'begin', target=DID, wire_sha256=WIRE_SHA)
            sent = set()
            mutate_once(client, journal, sent, 'ota.downgrade', {'downgrade': 1})
            mutate_once(client, journal, sent, 'miIO.ota', {
                'app_url': f'https://{bind}/miio_fw/{SERVE.name}',
                'file_md5': WIRE_MD5, 'proc': 'dnld install', 'mode': 'normal',
                'signed_file': True, 'original_length': len(wire),
                'install': '1', 'app_force': 1, 'cert_verify': 'optional',
            })
            deadline, idle_since = time.monotonic() + 600, None
            while time.monotonic() < deadline:
                observed = observe(client, journal)
                if observed is None:
                    # Keep the same transfer available; reset the stable-state window.
                    # This retries READS only. Never dispatch another OTA request.
                    idle_since = None
                    time.sleep(5)
                    continue
                version, idle = observed
                record(journal, 'transfer_observed', complete_send=transferred.is_set())
                if version == '1.0.2_0031':
                    record(journal, 'version_0031_observed', physical_acceptance=False)
                    return
                require(version == '1.0.2_0041', 'Unexpected firmware; stop')
                if transferred.is_set() and idle:
                    if idle_since is None:
                        idle_since = time.monotonic()
                    if time.monotonic() - idle_since >= 30:
                        break
                else:
                    idle_since = None
                time.sleep(5)
            else:
                record(journal, 'stopped_no_stable_download', complete_send=transferred.is_set())
                return
            record(journal, 'complete_send_and_idle', stored_flash_verified=False)
            # Server-side send completion is NOT a target-flash readback.
            mutate_once(client, journal, sent, 'miIO.recovery', {'module': 'app'})
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                time.sleep(5)
                try:
                    version, _ = state(client)
                except ReadUnavailable:
                    continue  # Read-only observation during possible reboot, never re-dispatch.
                record(journal, 'post_recovery_observed', version=version)
                if version == '1.0.2_0031':
                    record(journal, 'version_0031_observed', physical_acceptance=False)
                    return
                require(version == '1.0.2_0041', 'Unexpected firmware after recovery')
            record(journal, 'unconfirmed_after_recovery', no_retry=True)
    finally:
        if thread is not None:
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-once', action='store_true', help='Mutates ONE device. Separate informed approval required.')
    args = parser.parse_args()
    wire = validate_files()
    SERVE.parent.mkdir(parents=True, exist_ok=True)
    SERVE.write_bytes(wire)
    require(not TRANSACTION.exists(), 'Prior transaction exists: stop; do not delete guard to retry')
    print(json.dumps({'mode': 'EXECUTE' if args.execute_once else 'OFFLINE_PLAN_ONLY',
                      'target': DID, 'other_target_forbidden': True,
                      'bytes': len(wire), 'wire_md5': WIRE_MD5,
                      'expected_stored_sha256': STOCK_SHA,
                      'max_mutating_dispatches': 3,
                      'physical_success_verified': False,
                      'risk': 'May require pairing reset; no guaranteed hardware or remote rescue'}, indent=2))
    if args.execute_once:
        cfg.require_identity()
        execute(wire)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Avoid exception strings from network libraries which may contain private payloads.
        print('STOPPED: ' + type(error).__name__ + '; no automatic retry', file=sys.stderr)
        if type(error) is RuntimeError:
            print(str(error), file=sys.stderr)
        sys.exit(1)
