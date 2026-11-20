"""AP01 账户工作台模式：账户余额 + 充电功率显示服务。

启动后默认监听 0.0.0.0:18766：
  /screen.gif   单张 GIF，兼容每分钟取图的旧加载器
  /live.bin     stream-v2 长连接，300 块 x 24576 字节，每秒一块
  /health       运行状态和设备诊断

数据源：Codex app-server、WorkBuddy 本机快照、DeepSeek 余额接口，
以及可选的 Grok 周额度（Cursor Dashboard 的 GetSandUsageStatus）。
Grok 这一路是显示服务自己的只读登录，不读 Grok Bot 桌面端的
sand-secrets.json，也不消耗 bot 额度。
读取失败显示“不可用”或“未授权”，绝不伪造 0。
"""
from pathlib import Path
import argparse
import base64
import datetime
import io
import json
import math
import os
import queue
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import ssl
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from PIL import Image, ImageDraw, ImageFont

import ap01_config as cfg
from credentials import get_key, protect, unprotect
from power_reader import ChargingReader

ROOT = cfg.STATE_DIR
ROOT.mkdir(parents=True, exist_ok=True)
BIND = cfg.BIND
PORT = cfg.DISPLAY_PORT
DEVICE_IP = cfg.DEVICE_IP
GIF = ROOT / 'screen.gif'
STATE = ROOT / 'current.json'
PREVIEW = ROOT / 'preview.png'
LOG = ROOT / 'runtime.log'
# Codex 升级会新建哈希目录，不能写死某一个版本；优先复用上次成功的。
CODEX_BIN_ROOT = Path(os.path.expandvars(cfg.get(
    'sources', 'codex_bin_root', default='%LOCALAPPDATA%/OpenAI/Codex/bin')))
_CODEX_LAST_GOOD = None
WB_SNAPSHOT = cfg.get('sources', 'workbuddy_snapshot',
                      default='http://127.0.0.1:8791/snapshot')
DS_ENDPOINT = cfg.get('sources', 'deepseek_endpoint',
                      default='https://api.deepseek.com/user/balance')


def _as_bool(value, default=True):
    """配置里的开关。缺省用 default；字符串 "false" 不能被 bool() 当成真。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _refuse_bot_secrets(path):
    """独立登录的凭据文件，不允许指到 Grok Bot 桌面端的 sand-secrets.json。"""
    if 'sand-secrets' in Path(path).name.lower():
        raise RuntimeError('refusing sand-secrets.json')


# Grok 周额度。路径和开关都走 config.json；接口地址有默认值，方便上游改路径时只改配置。
# 这一路绝不能去读 Grok Bot 桌面端的 sand-secrets.json，那是另一次登录。
GROK_ENABLED = _as_bool(cfg.get('sources', 'grok_enabled', default=True), default=True)
GROK_FILE = cfg.resolve(cfg.get('sources', 'grok_cache', default='run/grok_usage.json'))
GROK_AUTH_STORE = cfg.resolve(cfg.get('sources', 'grok_auth_store',
                                      default='run/grok_cursor_auth.bin'))
GROK_AUTH_STATUS = cfg.resolve(cfg.get('sources', 'grok_auth_status',
                                       default='run/grok_auth_status.json'))
GROK_API = str(cfg.get(
    'sources', 'grok_usage_url',
    default='https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus'))
GROK_REFRESH_URL = str(cfg.get(
    'sources', 'grok_refresh_url',
    default='https://api2.cursor.sh/auth/exchange_user_api_key'))
GROK_POLL_S = 300             # 真正打到接口上，最多 5 分钟一次；启动后的第一次不等
GROK_RENEW_BEFORE_S = 7 * 86400
GROK_HINT_AGE_S = 30 * 60     # 缓存比这更旧：数字照常显示，说明改成「x时前更新」
_GROK = {'next_due': 0.0, 'reauth_logged': False}
# /live.bin 每个槽 24576 字节，含 4 字节长度，GIF 本体不能超过 24572。
STREAM_GIF_MAX = 24572


def validate_gif(path):
    """设备端只接受 320x240、至少两帧、单帧不超过 24572 字节的 GIF89a。"""
    body = Path(path).read_bytes()
    if len(body) > STREAM_GIF_MAX or not body.startswith(b'GIF89a') or not body.endswith(b';'):
        raise RuntimeError('GIF must be GIF89a, <=24572 bytes, with a valid trailer')
    with Image.open(path) as image:
        if image.size != (320, 240) or image.n_frames < 2:
            raise RuntimeError('GIF must be 320x240 with at least two frames')
    return body


# Palette taken from the stock 1.0.2_0031 firmware UI assets:
# pure black background, oversized white numerals, orange/amber/red-orange
# glow accents and four coloured ring gauges. No green, no purple.
W, H = 320, 240
# Final palette: borrowed from the Codex build the user already approved on the
# real LCD - dark blue-black base, 1px light borders, saturated gradients with
# DARK ink on the bright cards, one neutral silver charging card.
BG = (9, 17, 27)
WHITE = (233, 240, 247)
DIM = (184, 200, 214)
ORANGE = (255, 174, 10)
AMBER = (255, 194, 74)
REDORANGE = (255, 122, 60)
GLOW = (255, 101, 0)
CYAN = (22, 245, 255)
BAD = (255, 120, 110)
INK = (23, 34, 45)
INK_SOFT = (67, 80, 93)
PANEL_WARM = (16, 29, 42)

# Gradient endpoints (top -> bottom)
GRAD_SILVER = ((247, 250, 252), (174, 185, 196))   # charging, only neutral area
GRAD_WB = ((255, 193, 35), (255, 98, 22))          # WorkBuddy, one card / two rows
GRAD_CODEX = ((146, 32, 40), (58, 12, 18))         # Codex, dark crimson -> deep wine
GRAD_DS = ((53, 224, 255), (10, 107, 240))         # DeepSeek, cyan -> blue
GRAD_GROK = ((74, 84, 98), (18, 22, 30))           # Grok，石墨到近黑，不用绿也不用紫
GRAD_FOOT = ((41, 68, 87), (24, 46, 64))
BORDER_SILVER = (255, 255, 255)
BORDER_WB = (255, 233, 194)
BORDER_CODEX = (255, 168, 162)
BORDER_DS = (207, 246, 255)
BORDER_GROK = (236, 241, 246)
BORDER_FOOT = (62, 86, 107)
HAIRLINE = (135, 148, 160)
STREAM_FRAMES = 300
# 五张卡加跑马灯时 56 色可能超过 24572。从多到少试，记住上次放得下的那一档。
PALETTE_STEPS = (56, 44, 36, 28)


def make_balance_handler(gif_path):
    """Serve both the existing minute pull and the verified one-second stream."""
    class Handler(BaseHTTPRequestHandler):
        started = time.time()
        requests = []
        diagnostic = None

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/live.bin':
                self.stream_frames(parsed.query)
                return
            if parsed.path == '/health':
                intervals = [b - a for a, b in zip(self.requests, self.requests[1:])]
                payload = {'ok': gif_path.is_file(), 'uptime': int(time.time() - self.started),
                           'requests': len(self.requests),
                           'last_request': self.requests[-1] if self.requests else None,
                           'median_request_interval': statistics.median(intervals) if intervals else None,
                           'device_diagnostic': self.diagnostic, 'error': None}
                body = json.dumps(payload).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body); return
            if parsed.path != '/screen.gif':
                self.send_error(404); return
            if not gif_path.is_file():
                self.send_error(503); return
            body = gif_path.read_bytes()
            self.send_response(200); self.send_header('Content-Type', 'image/gif')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close'); self.send_header('Cache-Control', 'no-store')
            self.end_headers(); self.wfile.write(body)
            if self.client_address[0] == DEVICE_IP:
                self.requests.append(time.time()); self.requests[:] = self.requests[-600:]

        def stream_frames(self, query):
            if self.client_address[0] != DEVICE_IP:
                self.send_error(403); return
            try:
                fields = parse_qs(query)
                diagnostic = {key: int(fields.get(key, ['0'])[0]) for key in ('v', 'g', 'a', 'e')}
                if diagnostic['v'] != 2:
                    raise ValueError()
            except ValueError:
                self.send_error(400); return
            type(self).diagnostic = diagnostic | {'at': time.time()}
            if not gif_path.is_file():
                self.send_error(503); return
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(200); self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(STREAM_FRAMES * 24576))
            self.send_header('Connection', 'close'); self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            began = time.time()
            try:
                for index in range(STREAM_FRAMES):
                    time.sleep(max(0, began + index - time.time()))
                    body = gif_path.read_bytes()
                    if len(body) > 24572:
                        return
                    self.wfile.write(struct.pack('<I', len(body)) + body + bytes(24572 - len(body)))
                    self.wfile.flush()
                    self.requests.append(time.time()); self.requests[:] = self.requests[-600:]
            except OSError:
                pass
            finally:
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        def log_message(self, *_):
            pass
    return Handler


def log(**fields):
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'at': time.time(), **fields}, ensure_ascii=False) + '\n')


def font(size):
    return ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', size)


def numfont(size):
    """Bahnschrift, matching the stock instrument numerals."""
    return ImageFont.truetype('C:/Windows/Fonts/bahnschrift.ttf', size)


_POWER_READER = None


def power_reader():
    global _POWER_READER
    if _POWER_READER is None:
        try:
            _POWER_READER = ChargingReader()  # Codex-verified 5s independent poll
        except Exception:
            _POWER_READER = False
    return _POWER_READER or None


def codex_candidates():
    """Newest usable codex.exe first; prefer the one that worked last time."""
    global _CODEX_LAST_GOOD
    if not CODEX_BIN_ROOT.is_dir():
        return []
    exes = [p / 'codex.exe' for p in CODEX_BIN_ROOT.iterdir() if p.is_dir()]
    exes = [p for p in exes if p.is_file()]
    exes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if _CODEX_LAST_GOOD:
        last = Path(_CODEX_LAST_GOOD)
        if last in exes:
            exes.remove(last)
            exes.insert(0, last)
    return [str(p) for p in exes]


def _read_limits(exe):
    proc = subprocess.Popen([exe, 'app-server', '--listen', 'stdio://'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                            creationflags=subprocess.CREATE_NO_WINDOW)
    messages = queue.Queue()

    def read_lines():
        try:
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            messages.put(None)

    threading.Thread(target=read_lines, daemon=True).start()
    try:
        for message in [
            {'id': 1, 'method': 'initialize',
             'params': {'clientInfo': {'name': 'ap01-mode-b-account', 'version': '1'},
                        'capabilities': {'experimentalApi': True}}},
            {'id': 2, 'method': 'account/rateLimits/read', 'params': None},
        ]:
            proc.stdin.write(json.dumps(message) + '\n')
            proc.stdin.flush()
            end = time.monotonic() + 30
            while True:
                response = messages.get(timeout=max(0.01, end - time.monotonic()))
                if response is None:
                    raise RuntimeError('Quota process closed')
                if response.get('id') == message['id']:
                    break
                if time.monotonic() >= end:
                    raise TimeoutError('Quota read timed out')
            if 'error' in response:
                raise RuntimeError('Quota response unavailable')
            if message['id'] == 1:
                proc.stdin.write('{"method":"initialized","params":{}}\n')
                proc.stdin.flush()
        result = response['result']
        limits = (result.get('rateLimitsByLimitId') or {}).get('codex') or result.get('rateLimits') or {}
        windows = []
        for key in ('primary', 'secondary'):
            raw = limits.get(key)
            if not isinstance(raw, dict) or raw.get('usedPercent') is None:
                continue
            used = float(raw['usedPercent'])
            if not 0 <= used <= 100:
                raise RuntimeError('Invalid usage percentage')
            windows.append({'remaining': 100 - used, 'minutes': raw.get('windowDurationMins'),
                            'resets_at': raw.get('resetsAt')})
        if not windows:
            raise RuntimeError('No quota windows supplied')
        return {'status': 'live', 'windows': windows}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def fetch_codex():
    """Try every installed Codex build; survive client upgrades without edits."""
    global _CODEX_LAST_GOOD
    errors = []
    for exe in codex_candidates():
        try:
            data = _read_limits(exe)
            _CODEX_LAST_GOOD = exe
            return data
        except Exception as error:
            errors.append(Path(exe).parent.name + ':' + type(error).__name__)
    raise RuntimeError('No usable Codex client (' + ', '.join(errors) + ')')


def fetch_workbuddy():
    with urllib.request.urlopen(WB_SNAPSHOT, timeout=8) as response:
        data = json.load(response)
    accounts = [{'name': str(a.get('name') or '?'),
                 'balance': float(a.get('balance') or 0)}
                for a in (data.get('accounts') or [])]
    if not accounts:
        raise RuntimeError('No accounts in snapshot')
    return {'status': 'live', 'accounts': accounts, 'online': bool(data.get('online', True))}


def fetch_deepseek():
    key = get_key()
    if not key:
        raise RuntimeError('Key not configured')
    request = urllib.request.Request(DS_ENDPOINT, headers={
        'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=12, context=ssl.create_default_context()) as response:
        data = json.load(response)
    infos = data.get('balance_infos') or []
    cny = next((x for x in infos if x.get('currency') == 'CNY'), None)
    if not cny:
        raise RuntimeError('No CNY balance')
    return {'status': 'live', 'cny': float(cny['total_balance'])}


def _pct(value):
    value = float(value)
    if not (0 <= value <= 100) or math.isnan(value):
        raise ValueError('percent out of range')
    return value


def _grok_post(url, bearer, timeout=12):
    """只读 POST，body 固定是 {}。Bearer 只放进请求头，不进日志。"""
    request = urllib.request.Request(url, data=b'{}', method='POST', headers={
        'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json',
        'connect-protocol-version': '1', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout,
                                context=ssl.create_default_context()) as response:
        return json.load(response)


def _grok_load_auth():
    _refuse_bot_secrets(GROK_AUTH_STORE)
    if not GROK_AUTH_STORE.is_file():
        raise FileNotFoundError('grok auth missing')
    creds = json.loads(unprotect(GROK_AUTH_STORE.read_bytes()))
    if not isinstance(creds, dict) or not creds.get('accessToken') or not creds.get('refreshToken'):
        raise ValueError('grok auth incomplete')
    return creds


def _grok_jwt_exp(token):
    try:
        part = token.split('.')[1]
        part += '=' * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part))['exp'])
    except Exception:
        return None


def _atomic_write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_bytes(data)
    tmp.replace(path)


def _grok_auth_status(status, **extra):
    """状态文件只写状态和时间，不写 token。"""
    payload = {'status': status,
               'updated_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
    payload.update(extra)
    _refuse_bot_secrets(GROK_AUTH_STATUS)
    _atomic_write(GROK_AUTH_STATUS, json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))


def _grok_note_reauth(reason):
    if _GROK['reauth_logged']:
        return
    _GROK['reauth_logged'] = True
    try:
        log(event='grok_auth_needed', reason=str(reason)[:80])
    except Exception:
        pass
    try:
        _grok_auth_status('needs_reauth', reason=str(reason)[:80])
    except Exception:
        pass


def _grok_refresh(creds):
    """用 refresh token 换新的 access token。响应里没有新 refresh token 就留着旧的。"""
    try:
        reply = _grok_post(GROK_REFRESH_URL, creds['refreshToken'], timeout=15)
        access = reply.get('accessToken')
        if not isinstance(access, str) or not access.strip():
            raise ValueError('no accessToken')
        new_refresh = reply.get('refreshToken')
        fresh = dict(creds, accessToken=access,
                     refreshToken=new_refresh if isinstance(new_refresh, str) and new_refresh
                     else creds['refreshToken'],
                     access_exp=_grok_jwt_exp(access),
                     refreshed_at=datetime.datetime.now().astimezone().isoformat(timespec='seconds'))
        _refuse_bot_secrets(GROK_AUTH_STORE)
        _atomic_write(GROK_AUTH_STORE, protect(json.dumps(fresh)))
        exp = fresh.get('access_exp')
        _grok_auth_status('authorized', refreshed_at=fresh['refreshed_at'],
                          access_token_expires_at=datetime.datetime.fromtimestamp(exp).astimezone()
                          .isoformat(timespec='seconds') if exp else None)
        _GROK['reauth_logged'] = False
        log(event='grok_token_refreshed')
        return fresh
    except Exception as error:
        reason = (f'HTTP {error.code}' if isinstance(error, urllib.error.HTTPError)
                  else type(error).__name__)
        _grok_note_reauth(reason)
        return None


def _grok_query_api():
    """一次 GetSandUsageStatus。任何字段不对就抛错，调用方继续用缓存，不填 0。"""
    creds = _grok_load_auth()
    exp = creds.get('access_exp') or _grok_jwt_exp(creds['accessToken'])
    if exp and exp - time.time() < GROK_RENEW_BEFORE_S:
        creds = _grok_refresh(creds) or creds
    try:
        raw = _grok_post(GROK_API, creds['accessToken'])
    except urllib.error.HTTPError as error:
        if error.code not in (401, 403):
            raise
        creds = _grok_refresh(creds)
        if not creds:
            raise PermissionError('needs_reauth')
        try:
            raw = _grok_post(GROK_API, creds['accessToken'])
        except urllib.error.HTTPError as again:
            if again.code in (401, 403):
                _grok_note_reauth(f'HTTP {again.code}')
                raise PermissionError('needs_reauth')
            raise
    if not isinstance(raw, dict) or raw.get('usagePercent') is None:
        raise ValueError('usagePercent missing')
    used = _pct(raw['usagePercent'])
    stamp = raw.get('nextResetTimestampUtc')
    if not stamp:
        raise ValueError('nextResetTimestampUtc missing')
    resets = datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    if resets.tzinfo is None:
        raise ValueError('reset needs an offset')
    out = {'used': used, 'resets': resets.astimezone()}
    cents = raw.get('prepaidAvailableCents')
    if cents is not None:
        out['credits_usd'] = round(float(cents) / 100, 2)
    return out


def _grok_write_cache(api):
    previous = {}
    try:
        previous = json.loads(GROK_FILE.read_text(encoding='utf-8-sig'))
    except Exception:
        pass
    used = round(api['used'], 3)
    cache = {'status': 'live',
             'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
             'source': 'api',
             'plan': previous.get('plan') if isinstance(previous, dict) else None,
             'used_percent': used,
             'remaining_percent': round(100 - used, 3),
             'resets_at': api['resets'].isoformat(timespec='seconds'),
             'resets_at_unix': int(api['resets'].timestamp()),
             'credits_usd': api.get('credits_usd', previous.get('credits_usd')
                                    if isinstance(previous, dict) else None),
             'note': 'GetSandUsageStatus, read by the display service'}
    _refuse_bot_secrets(GROK_FILE)
    _atomic_write(GROK_FILE, json.dumps(cache, ensure_ascii=False, indent=2).encode('utf-8'))


def _grok_from_cache():
    """上次成功的数字。放多久都还能用；只有没有文件，或重置时间已经过了，才不能用。"""
    _refuse_bot_secrets(GROK_FILE)
    if not GROK_FILE.is_file():
        raise FileNotFoundError('grok_usage.json missing')
    raw = json.loads(GROK_FILE.read_text(encoding='utf-8-sig'))
    if not isinstance(raw, dict) or raw.get('status') != 'live':
        raise ValueError('source status not live')
    checked = datetime.datetime.fromisoformat(str(raw['checked_at']))
    if checked.tzinfo is None:
        raise ValueError('checked_at needs an offset')
    now = datetime.datetime.now().astimezone()
    age = (now - checked).total_seconds()
    if age < -300:
        raise ValueError('checked_at in the future')
    used = _pct(raw['used_percent']) if raw.get('used_percent') is not None else None
    remaining = _pct(raw['remaining_percent']) if raw.get('remaining_percent') is not None else None
    if remaining is None and used is None:
        raise ValueError('no percentage')
    if remaining is None:
        remaining = 100 - used
    if used is not None and abs(used + remaining - 100) > 0.5:
        raise ValueError('used + remaining != 100')
    if raw.get('resets_at'):
        resets = datetime.datetime.fromisoformat(str(raw['resets_at']))
        if resets.tzinfo is None:
            raise ValueError('resets_at needs an offset')
        resets_unix = resets.timestamp()
    elif raw.get('resets_at_unix') is not None:
        resets_unix = float(raw['resets_at_unix'])
    else:
        resets_unix = None
    if resets_unix is not None and resets_unix < now.timestamp():
        raise TimeoutError('reset_passed')
    out = {'status': 'live', 'remaining': remaining, 'used': used,
           'resets_at': resets_unix, 'plan': raw.get('plan'),
           'checked_at': checked.isoformat(timespec='seconds'), 'age_s': max(0, int(age))}
    if raw.get('credits_usd') is not None:
        out['credits_usd'] = float(raw['credits_usd'])
    return out


def fetch_grok():
    """Grok 周额度。接口最多 5 分钟打一次；失败就退回缓存。

    没有凭据、也没有能用的缓存时，返回 unauthorized，不抛给主循环。
    有缓存就一直显示那个数，哪怕这次接口失败，也不改成 0。
    """
    if not GROK_ENABLED:
        return {'status': 'disabled'}
    source, api_error = 'cache', None
    try:
        authed = GROK_AUTH_STORE.is_file() and 'sand-secrets' not in GROK_AUTH_STORE.name.lower()
    except OSError:
        authed = False
    if 'sand-secrets' in GROK_AUTH_STORE.name.lower():
        raise RuntimeError('refusing sand-secrets.json')
    if time.monotonic() >= _GROK['next_due']:
        if not authed:
            api_error = 'unauthorized'
            if not _GROK['reauth_logged']:
                _GROK['reauth_logged'] = True
                try:
                    log(event='grok_auth_needed', reason='missing')
                except Exception:
                    pass
                try:
                    _grok_auth_status('unauthorized', reason='missing')
                except Exception:
                    pass
        else:
            _GROK['next_due'] = time.monotonic() + GROK_POLL_S
            try:
                _grok_write_cache(_grok_query_api())
                source = 'api'
                try:
                    log(event='grok_api_ok', source='api')
                except Exception:
                    pass
            except Exception as error:
                api_error = (f'HTTP {error.code}' if isinstance(error, urllib.error.HTTPError)
                             else str(error) if isinstance(error, PermissionError)
                             else type(error).__name__)
                try:
                    log(event='grok_api_failed', reason=api_error)
                except Exception:
                    pass
    try:
        out = _grok_from_cache()
    except TimeoutError:
        raise
    except Exception:
        if not authed:
            return {'status': 'unauthorized', 'reason': 'unauthorized'}
        raise
    out['source'] = source
    if api_error:
        out['api_error'] = api_error
    return out


def collect():
    out = {'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
    for name, fn in (('codex', fetch_codex), ('workbuddy', fetch_workbuddy),
                     ('deepseek', fetch_deepseek), ('grok', fetch_grok)):
        try:
            out[name] = fn()
        except Exception as error:
            reason = type(error).__name__
            if name == 'grok' and str(error) == 'reset_passed':
                reason = str(error)
            out[name] = {'status': 'unavailable', 'reason': reason}
    # Charging output: service-4 port values are PACKED voltage/current integers.
    # Reuses Codex's decode_ports + independent 5 s poll; a failed read never
    # becomes a fake 0 W. (Earlier "no wattage available" conclusion was wrong.)
    reader = power_reader()
    sample = None
    if reader:
        for _ in range(14):  # allow the first poll to land, at most 7 s
            sample = reader.sample()
            if sample:
                break
            time.sleep(0.5)
    if sample:
        out['power'] = {'status': 'live', 'watts': sample['watts'],
                        'label': sample['label'], 'mode': sample.get('mode'),
                        'ports': sample['ports']}
    else:
        out['power'] = {'status': 'unavailable', 'reason': 'no-fresh-sample'}
    return out


def reset_text(window):
    stamp = window.get('resets_at')
    if not stamp:
        return '重置时间未提供'
    try:
        when = datetime.datetime.fromtimestamp(float(stamp)).astimezone()
        left = (when - datetime.datetime.now().astimezone()).total_seconds()
        if left < 0:
            return '即将重置'
        hours, minutes = divmod(int(left) // 60, 60)
        left_text = f"剩 {hours}小时{minutes}分" if hours else f"剩 {minutes}分"
        return f"{left_text} · {when.strftime('%m-%d %H:%M')}"
    except Exception:
        return '重置时间未知'


def reset_short(window):
    """一行放得下的重置提示（长卡只有 155px 宽）。"""
    stamp = window.get('resets_at')
    if not stamp:
        return '重置时间未提供'
    try:
        when = datetime.datetime.fromtimestamp(float(stamp)).astimezone()
        left = (when - datetime.datetime.now().astimezone()).total_seconds()
        if left < 0:
            return '即将重置'
        hours, minutes = divmod(int(left) // 60, 60)
        left_text = f"{hours}小时{minutes}分" if hours else f"{minutes}分"
        return f"剩 {left_text} · {when.strftime('%H:%M')} 重置"
    except Exception:
        return '重置时间未知'


WEEKDAY = '一二三四五六日'


def reset_grok(stamp):
    """「6天20时后 · 周五05:44」。后面由卡片再接「 重置」。"""
    if not stamp:
        return '重置时间未提供'
    try:
        when = datetime.datetime.fromtimestamp(float(stamp)).astimezone()
        left = (when - datetime.datetime.now().astimezone()).total_seconds()
        if left < 0:
            return '即将重置'
        days, rest = divmod(int(left) // 60, 1440)
        hours, minutes = divmod(rest, 60)
        if days:
            left_text = f"{days}天{hours}时"
        elif hours:
            left_text = f"{hours}时{minutes}分"
        else:
            left_text = f"{minutes}分"
        return f"{left_text}后 · 周{WEEKDAY[when.weekday()]}{when.strftime('%H:%M')}"
    except Exception:
        return '重置时间未知'


def _age_hint(age):
    age = max(0, int(age))
    hours = age // 3600
    if hours >= 24:
        return f"{hours // 24}天前更新"
    if hours:
        return f"{hours}时前更新"
    return f"{max(1, age // 60)}分前更新"


def grok_lines(grok):
    """Grok 卡上的三行字。没有可信读数时数字是「—」，不会变成 0%。"""
    grok = grok or {}
    remaining = grok.get('remaining')
    ok = (grok.get('status') == 'live' and isinstance(remaining, (int, float))
          and not isinstance(remaining, bool) and 0 <= float(remaining) <= 100)
    age = int(grok.get('age_s') or 0)
    if ok and age > GROK_HINT_AGE_S:
        caption = _age_hint(age)
        caption_small = True
    elif grok.get('status') == 'unauthorized':
        caption = '未授权'
        caption_small = False
    else:
        caption = '本周剩余'
        caption_small = False
    if ok:
        number = f"{float(remaining):.0f}%"
        footer = reset_grok(grok.get('resets_at')) + ' 重置'
    else:
        number = '—'
        if grok.get('status') == 'unauthorized':
            footer = '请先完成授权'
        elif grok.get('reason') == 'reset_passed':
            footer = '已过重置时间 · 待刷新'
        else:
            footer = '额度数据暂不可用'
    return {'caption': caption, 'number': number, 'footer': footer,
            'ok': ok, 'caption_small': caption_small}


def text_width(draw, text, fnt):
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0]


def _gradient(w, h, top, bottom):
    layer = Image.new('RGB', (w, h))
    d = ImageDraw.Draw(layer)
    for row in range(h):
        t = row / max(1, h - 1)
        d.line((0, row, w - 1, row), fill=tuple(
            round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return layer


def shaded_box(im, draw, box, grad, border, radius=4):
    """One rounded gradient field with a 1px light border (Codex final look)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0 + 1, y1 - y0 + 1
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    im.paste(_gradient(w, h, *grad), (x0, y0), mask)
    draw.rounded_rectangle(box, radius=radius, outline=border, width=1)


def split_shaded_box(im, draw, box, grad_top, grad_bottom, split_y, borders):
    """ONE continuous rounded block, two gradient fields, one internal hairline."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0 + 1, y1 - y0 + 1
    upper = split_y - y0
    layer = Image.new('RGB', (w, h))
    layer.paste(_gradient(w, upper, *grad_top), (0, 0))
    layer.paste(_gradient(w, h - upper, *grad_bottom), (0, upper))
    ld = ImageDraw.Draw(layer)
    ld.line((8, upper, w - 9, upper), fill=(96, 110, 122), width=1)
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=4, fill=255)
    im.paste(layer, (x0, y0), mask)
    draw.rounded_rectangle(box, radius=4, outline=borders[0], width=1)
    # tint the two halves' rims without creating a second card
    draw.line((x0 + 4, y0 + 1, x1 - 4, y0 + 1), fill=borders[0], width=1)
    draw.line((x0 + 4, y1 - 1, x1 - 4, y1 - 1), fill=borders[1], width=1)


_PATH_CACHE = {}


def border_points(box, radius=6):
    """把圆角矩形边框离散成像素点，顺时针排列，按坐标缓存。"""
    key = (box, radius)
    cached = _PATH_CACHE.get(key)
    if cached is not None:
        return cached
    x0, y0, x1, y1 = box
    r = radius
    pts = []

    def seg(ax, ay, bx, by):
        steps = max(1, int(max(abs(bx - ax), abs(by - ay))))
        for i in range(steps):
            t = i / steps
            pts.append((round(ax + (bx - ax) * t), round(ay + (by - ay) * t)))

    def arc(cx, cy, a0, a1):
        steps = max(2, int(abs(a1 - a0) * r))
        for i in range(steps):
            a = a0 + (a1 - a0) * i / steps
            pts.append((round(cx + r * math.cos(a)), round(cy + r * math.sin(a))))

    seg(x0 + r, y0, x1 - r, y0)
    arc(x1 - r, y0 + r, -math.pi / 2, 0)
    seg(x1, y0 + r, x1, y1 - r)
    arc(x1 - r, y1 - r, 0, math.pi / 2)
    seg(x1 - r, y1, x0 + r, y1)
    arc(x0 + r, y1 - r, math.pi / 2, math.pi)
    seg(x0, y1 - r, x0, y0 + r)
    arc(x0 + r, y0 + r, math.pi, math.pi * 1.5)
    _PATH_CACHE[key] = pts
    return pts


# 关掉 Grok 时维持 11-10 定稿的四卡。打开后 Codex 缩到左上，Grok 放左下。
RELAY_BOXES_ACCOUNT = (
    (8, 42, 311, 85),       # 充电：顶部
    (8, 91, 162, 219),      # Codex：左侧大块
    (168, 91, 311, 170),    # WorkBuddy：右上
    (168, 176, 311, 219),   # DeepSeek：右下
)
RELAY_BOXES_WITH_GROK = (
    (8, 42, 311, 85),       # 充电：顶部
    (8, 91, 162, 153),      # Codex：左上，缩小
    (168, 91, 311, 153),    # WorkBuddy：右上，略矮
    (168, 158, 311, 219),   # DeepSeek：右下
    (8, 158, 162, 219),     # Grok：左下
)


def relay_boxes():
    return RELAY_BOXES_WITH_GROK if GROK_ENABLED else RELAY_BOXES_ACCOUNT


def synchronized_marquees(draw, boxes, phase, radius=6,
                          head=(255, 255, 255), tail=(178, 190, 204)):
    """每张卡一条光带，相位按卡片数量等分，逐像素 smoothstep 渐变。

    光带画在卡片外圈 1px：外面是黑底，纯白才亮得起来；画在边框上会像掉漆。
    每块相差 1/N 圈，所以每一拍正好轮到下一块经过起点，首尾自然接上。
    """
    for beat, (x0, y0, x1, y1) in enumerate(boxes):
        path = border_points((x0 - 1, y0 - 1, x1 + 1, y1 + 1), radius + 1)
        total = len(path)
        runner_head = int(((phase + beat / len(boxes)) % 1.0) * total)
        span = max(32, int(total * 0.20))
        for offset in range(span):
            point = (runner_head - span + 1 + offset) % total
            progress = offset / max(1, span - 1)
            fade = progress * progress * (3.0 - 2.0 * progress)
            color = tuple(round(lo + (hi - lo) * fade) for lo, hi in zip(tail, head))
            draw.line([path[point], path[(point + 1) % total]], fill=color, width=2)


def right_align(draw, x1, y, text, fnt, color):
    draw.text((x1 - text_width(draw, text, fnt), y), text, font=fnt, fill=color)


def render(data, pulse=False, phase=0.0):
    im = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(im)
    stamp = datetime.datetime.fromisoformat(data['checked_at']).strftime('%H:%M')

    # 0..39 is the stock clock area. Charging begins directly below it.
    shaded_box(im, draw, (8, 42, 311, 85), GRAD_SILVER, BORDER_SILVER, radius=6)
    power = data.get('power', {})
    live = power.get('status') == 'live'
    active = [(name, p) for name, p in (power.get('ports') or {}).items()
              if p['watts'] > 0] if live else []
    if len(active) == 1:
        name, port = active[0]
        draw.text((17, 46), name + ' 输出 · ' + (power.get('mode') or '模式未知'),
                  font=font(9), fill=INK_SOFT)
        reading = f"{port['watts']:.1f} W"
        size = 25
        while text_width(draw, reading, numfont(size)) > 147:
            size -= 1
        draw.text((17, 56), reading, font=numfont(size), fill=INK)
        draw.line((190, 51, 190, 77), fill=HAIRLINE, width=1)
        gear = f"{port['gear_watts']}W" if port.get('gear_watts') else port.get('gear') or '未知'
        draw.text((199, 44), '档位 ' + gear, font=font(10), fill=INK)
        draw.text((199, 58), '协议 ' + (port.get('protocol') or '未知'), font=font(10), fill=INK)
        draw.text((199, 72), f"{port['volts']:.1f}V {port['amps']:.1f}A",
                  font=numfont(10), fill=INK)
    elif len(active) > 1:
        draw.text((17, 47), '总输出 · ' + (power.get('mode') or '模式未知'),
                  font=font(10), fill=INK_SOFT)
        draw.text((90, 61), '≈', font=font(14), fill=INK)
        draw.text((108, 52), f"{power['watts']:.1f} W", font=numfont(28), fill=INK)
        for index, (name, port) in enumerate(active[:2]):
            gear = f"{port['gear_watts']}W" if port.get('gear_watts') else port.get('gear') or '未知'
            y = 43 + index * 21
            draw.text((220, y), f"{name} {port.get('protocol') or ''} {gear}",
                      font=font(8), fill=INK)
            draw.text((220, y + 10), f"{port['volts']:.1f}V {port['amps']:.1f}A",
                      font=numfont(9), fill=INK)
    else:
        draw.text((17, 46), '充电输出', font=font(9), fill=INK_SOFT)
        draw.text((17, 59), '未充电' if live else '数据暂不可用',
                  font=font(17), fill=INK if live else INK_SOFT)

    cream = (255, 240, 236)
    codex = data.get('codex', {})
    windows = codex.get('windows', []) if codex.get('status') == 'live' else []
    short = next((w for w in windows if w.get('minutes') and w['minutes'] <= 1440), None)
    long = next((w for w in windows if w.get('minutes') and w['minutes'] > 1440), None)
    if GROK_ENABLED:
        # 左上 Codex 缩小：5 小时和本周并排，下面一行重置。
        shaded_box(im, draw, (8, 91, 162, 153), GRAD_CODEX, BORDER_CODEX, radius=6)
        draw.text((16, 103), 'CODEX', font=numfont(13), fill=cream, anchor='ls')
        right_align(draw, 154, 93, '剩余', font(9), cream)
        for label, window, x0, x1 in (('5小时', short, 16, 82), ('本周', long, 90, 154)):
            draw.text((x0, 122), label, font=font(10), fill=cream, anchor='lm')
            draw.text((x1, 122), f"{window['remaining']:.0f}%" if window else '—',
                      font=numfont(21), fill=cream if window else BAD, anchor='rm')
        reset = reset_short(short) if short else '重置时间未知'
        draw.text((16, 143), reset, font=font(10), fill=cream, anchor='lm')
        _draw_grok_card(im, draw, data)
        _draw_workbuddy(im, draw, data, box=(168, 91, 311, 153), rows=(117, 139), num_size=18)
        _draw_deepseek(im, draw, data, box=(168, 158, 311, 219), large=True)
    else:
        # 左侧整块给 Codex：它有三行内容（5 小时、本周、重置时间），需要高度。
        # 标题和两个窗口标签都放大过，太小的字在实物 LCD 上看不清。
        shaded_box(im, draw, (8, 91, 162, 219), GRAD_CODEX, BORDER_CODEX, radius=6)
        draw.text((17, 107), 'CODEX', font=numfont(16), fill=cream)
        right_align(draw, 152, 99, '剩余', font(10), cream)
        for label, window, cy in (('5小时', short, 136), ('本周', long, 175)):
            draw.text((17, cy), label, font=font(13), fill=cream, anchor='lm')
            draw.text((152, cy),
                      f"{window['remaining']:.0f}%" if window else '—',
                      font=numfont(28), fill=cream if window else BAD, anchor='rm')
        reset = reset_short(short) if short else '重置时间未知'
        draw.text((17, 197), reset, font=font(11), fill=cream)
        _draw_workbuddy(im, draw, data, box=(168, 91, 311, 170), rows=(121, 150), num_size=19)
        _draw_deepseek(im, draw, data, box=(168, 176, 311, 219), large=False)

    # 底边 240，9 号中文字形实际高度约 13px，再往下会吃掉下半截。
    # 颜色必须够亮：调色板会把淡灰压成杂色，看起来像被切了一半。
    draw.text((9, 221), '余额更新 ' + stamp, font=font(11), fill=WHITE)
    right_align(draw, 311, 221, '额度与账户余额', font(11), WHITE)

    # 每张卡一条光带，按阅读顺序等分相位。
    synchronized_marquees(draw, relay_boxes(), phase, radius=6)
    return im


def _draw_workbuddy(im, draw, data, box, rows, num_size):
    """两个账号左右对齐。名称只在接口没给时才用「账号A / 账号B」占位。"""
    shaded_box(im, draw, box, GRAD_WB, BORDER_WB, radius=6)
    title = 'WORKBUDDY'
    title_y = box[1] + 13
    draw.text((177, title_y), title, font=numfont(10), fill=INK, anchor='ls')
    # 和上面那串字母同一条基线，中文才不会看起来往下掉。
    draw.text((177 + text_width(draw, title, numfont(10)) + 6, title_y),
              '积分', font=font(9), fill=INK, anchor='ls')
    wb = data.get('workbuddy', {})
    accounts = wb.get('accounts', []) if wb.get('status') == 'live' else []
    for index, (fallback, cy) in enumerate(zip(('账号A', '账号B'), rows)):
        account = accounts[index] if index < len(accounts) else None
        name = str((account or {}).get('name', fallback))
        value = f"{account['balance']:,.0f}" if account else '—'
        draw.text((177, cy), name, font=font(11), fill=INK, anchor='lm')
        size = num_size
        while size > 11 and text_width(draw, value, numfont(size)) > 96:
            size -= 1
        draw.text((303, cy), value, font=numfont(size),
                  fill=INK if account else BAD, anchor='rm')


def _draw_deepseek(im, draw, data, box, large):
    shaded_box(im, draw, box, GRAD_DS, BORDER_DS, radius=6)
    ds = data.get('deepseek', {})
    ds_ok = ds.get('status') == 'live'
    value = f"¥{ds['cny']:.2f}" if ds_ok else '—'
    if large:
        draw.text((177, box[1] + 12), 'DEEPSEEK', font=numfont(10), fill=INK, anchor='ls')
        size = 28
        while size > 14 and text_width(draw, value, numfont(size)) > 126:
            size -= 1
        draw.text((177, box[1] + 38), value, font=numfont(size),
                  fill=INK if ds_ok else BAD, anchor='lm')
    else:
        draw.text((177, 180), 'DEEPSEEK', font=numfont(10), fill=INK)
        draw.text((176, 191), value, font=numfont(26), fill=INK if ds_ok else BAD)


def _draw_grok_card(im, draw, data):
    """左下石墨卡：大号白字剩余百分比，琥珀色重置倒计时。没有读数就画「—」。"""
    shaded_box(im, draw, (8, 158, 162, 219), GRAD_GROK, BORDER_GROK, radius=6)
    lines = grok_lines(data.get('grok'))
    draw.text((16, 172), 'GROK', font=numfont(15), fill=WHITE, anchor='ls')
    draw.text((16, 186), lines['caption'],
              font=font(10 if lines['caption_small'] else 11),
              fill=DIM if lines['ok'] else BAD, anchor='lm')
    draw.text((154, 181), lines['number'], font=numfont(34),
              fill=WHITE if lines['ok'] else BAD, anchor='rm')
    footer = lines['footer']
    size = 12
    while size > 8 and text_width(draw, footer, font(size)) > 140:
        size -= 1
    draw.text((16, 209), footer, font=font(size),
              fill=AMBER if lines['ok'] else BAD, anchor='lm')


def render_expired():
    im = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(im)
    shaded_box(im, draw, (8, 80, 311, 164), GRAD_FOOT, BORDER_FOOT)
    draw.text((24, 96), '数据已过期', font=font(26), fill=WHITE)
    draw.text((24, 134), '请检查电脑上的显示服务', font=font(14), fill=DIM)
    shaded_box(im, draw, (8, 220, 311, 237), GRAD_FOOT, BORDER_FOOT)
    draw.text((17, 222), 'AP01 目标设备 · 开机登录后自动恢复', font=font(9), fill=WHITE)
    return im


_PHASE = 0.0
PHASE_STEP = 0.05   # 一圈 20 秒；相位按卡片数等分
MOTION_FRAMES = 6   # 一秒切成 6 个子帧，运动才不会一跳一格
_PALETTE_AT = 0     # 上次放进 24572 的那一档；下次先试比它多一档的


def _save_gif(motion, expired, colors, tmp):
    palette = motion[0].quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    frames = [palette]
    frames.extend(frame.quantize(palette=palette, dither=Image.Dither.NONE)
                  for frame in motion[1:])
    frames.append(expired.quantize(palette=palette, dither=Image.Dither.NONE))
    frames[0].save(tmp, format='GIF', save_all=True,
                   append_images=frames[1:],
                   duration=[170, 170, 170, 170, 160, 160, 419000, 60000],
                   optimize=True)
    return tmp.stat().st_size


def publish(data):
    global _PHASE, _PALETTE_AT
    phases = [_PHASE + PHASE_STEP * index / MOTION_FRAMES
              for index in range(MOTION_FRAMES + 1)]
    motion = [render(data, phase=phase) for phase in phases]
    _PHASE = (_PHASE + PHASE_STEP) % 1.0
    expired = render_expired()
    # 五张卡时 56 色可能到 26 KB 以上，/live.bin 会把超限帧直接丢掉。
    # 按 56、44、36、28 往下降，直到放进 24572。下次从上次档位往上试一档，
    # 画面变简单时还能回到 56 色，不必每秒把四档都压一遍。
    tmp = GIF.with_suffix('.gif.tmp')
    first = max(0, _PALETTE_AT - 1)
    colors = PALETTE_STEPS[-1]
    for index, colors in enumerate(PALETTE_STEPS[first:], first):
        size = _save_gif(motion, expired, colors, tmp)
        if size <= STREAM_GIF_MAX:
            _PALETTE_AT = index
            break
    else:
        _PALETTE_AT = len(PALETTE_STEPS) - 1
        log(event='gif_over_budget', bytes=tmp.stat().st_size, colors=colors)
    data['gif_colors'] = colors
    validate_gif(tmp)
    tmp.replace(GIF)
    # Preview from the DECODED gif, so it shows the real transmitted colours.
    with Image.open(GIF) as gif:
        gif.seek(0)
        gif.convert('RGB').save(PREVIEW)
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return len(GIF.read_bytes())


def refresh():
    data = collect()
    size = publish(data)
    log(statuses={k: data.get(k, {}).get('status')
                 for k in ('codex', 'workbuddy', 'deepseek', 'grok')},
        gif_bytes=size, gif_colors=data.get('gif_colors'))
    return data


def refresh_power(data):
    """把独立五秒线程的充电样本复制进下一帧。

    mode 必须跟着一起复制，否则卡片标题会一直停在「模式未知」。
    """
    reader = power_reader()
    sample = reader.sample() if reader else None
    if sample:
        data['power'] = {'status': 'live', 'watts': sample['watts'],
                         'label': sample['label'], 'mode': sample.get('mode'),
                         'ports': sample['ports']}
    else:
        data['power'] = {'status': 'unavailable', 'reason': 'no-fresh-sample'}
    return publish(data)


def bootstrap():
    """先放一张占位图，再去做第一次数据采集。

    第一次采集要串行等 Codex、WorkBuddy、DeepSeek 三个源，慢的时候十几秒。
    不先放图的话，这十几秒里 /health 是 false、/screen.gif 是 404，
    看起来像程序坏了。
    """
    frames = []
    for lit in (False, True):
        image = Image.new('RGB', (320, 240), '#0d141c')
        draw = ImageDraw.Draw(image)
        draw.text((26, 92), '正在读取数据', font=font(26), fill='#ffb476')
        draw.text((26, 140), '首次启动约需 10 秒', font=font(15), fill='#8d9eaf')
        draw.ellipse((152, 186, 168, 202), fill='#9afa9b' if lit else '#2c4a38')
        frames.append(image)
    tmp = GIF.with_suffix('.gif.tmp')
    frames[0].save(tmp, format='GIF', save_all=True, append_images=frames[1:],
                   duration=[600, 600], optimize=True)
    validate_gif(tmp)
    tmp.replace(GIF)
    log(event='bootstrap_image', gif_bytes=GIF.stat().st_size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Generate one image, no server')
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.once:
        data = refresh()
        print(json.dumps({k: data.get(k, {}).get('status')
                          for k in ('codex', 'workbuddy', 'deepseek', 'grok')},
                         ensure_ascii=False))
        print('gif_bytes', GIF.stat().st_size)
        return
    bootstrap()
    server = ThreadingHTTPServer((BIND, PORT), make_balance_handler(GIF))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(event='server_started', bind=BIND, port=PORT)
    # 先把充电采集线程支起来，别让它排在账户请求后面，否则第一帧充电位是空的。
    power_reader()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        data = refresh()
        balance_due = time.monotonic() + 60
        power_due = time.monotonic() + 5
        balance_future = None
        # 每秒重画一次是给边框动画用的；余额仍 60 秒、功率仍 5 秒刷新。
        #
        # 关键：余额刷新放到后台线程做。collect() 要依次请求 Codex、WorkBuddy、
        # DeepSeek，打开 Grok 卡时再加上那一路，网络慢的时候会卡十几秒；以前它跑在主循环里，
        # 这段时间既不重绘也不复制充电样本，表现就是「功率数字半天不动」。
        # 放到后台后主循环始终每秒走一圈，充电读数不再被账户请求拖住。
        while True:
            time.sleep(1)
            now = time.monotonic()
            if balance_future and balance_future.done():
                try:
                    updated = balance_future.result()
                    # 只回写账户字段。整份 data 覆盖会把主循环刚更新的充电样本
                    # 换成后台线程启动时的旧值，等于白回退 60 秒。
                    for name in ('codex', 'workbuddy', 'deepseek', 'grok'):
                        data[name] = updated[name]
                    data['checked_at'] = updated['checked_at']
                    log(statuses={name: data[name].get('status') for name in
                                  ('codex', 'workbuddy', 'deepseek', 'grok')})
                except Exception as error:
                    log(event='balance_refresh_failed', reason=type(error).__name__)
                balance_future = None
            if now >= balance_due and balance_future is None:
                balance_future = executor.submit(collect)
                balance_due = now + 60
            if now >= power_due:
                refresh_power(data)
                power_due = now + 5
            else:
                publish(data)
    finally:
        executor.shutdown(wait=False)
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
