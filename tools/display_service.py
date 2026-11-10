"""AP01 账户工作台模式：账户余额 + 充电功率显示服务。

启动后默认监听 0.0.0.0:18766：
  /screen.gif   单张 GIF，兼容每分钟取图的旧加载器
  /live.bin     stream-v2 长连接，300 块 x 24576 字节，每秒一块
  /health       运行状态和设备诊断

数据源：Codex app-server、WorkBuddy 本机快照、DeepSeek 余额接口。
读取失败显示“不可用”，绝不伪造 0。
"""
from pathlib import Path
import argparse
import datetime
import io
import json
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from PIL import Image, ImageDraw, ImageFont

import ap01_config as cfg
from credentials import get_key
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


def validate_gif(path):
    """设备端只接受 320x240、至少两帧、单帧不超过 24572 字节的 GIF89a。"""
    body = Path(path).read_bytes()
    if len(body) > 24572 or not body.startswith(b'GIF89a') or not body.endswith(b';'):
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
GRAD_CODEX = ((181, 128, 91), (91, 55, 38))        # Codex, caramel -> mocha
GRAD_DS = ((53, 224, 255), (10, 107, 240))         # DeepSeek, cyan -> blue
GRAD_FOOT = ((41, 68, 87), (24, 46, 64))
BORDER_SILVER = (255, 255, 255)
BORDER_WB = (255, 233, 194)
BORDER_CODEX = (232, 197, 170)
BORDER_DS = (207, 246, 255)
BORDER_FOOT = (62, 86, 107)
HAIRLINE = (135, 148, 160)
STREAM_FRAMES = 300


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


def collect():
    out = {'checked_at': datetime.datetime.now().astimezone().isoformat(timespec='seconds')}
    for name, fn in (('codex', fetch_codex), ('workbuddy', fetch_workbuddy),
                     ('deepseek', fetch_deepseek)):
        try:
            out[name] = fn()
        except Exception as error:
            out[name] = {'status': 'unavailable', 'reason': type(error).__name__}
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
                        'label': sample['label'], 'ports': sample['ports']}
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


def right_align(draw, x1, y, text, fnt, color):
    draw.text((x1 - text_width(draw, text, fnt), y), text, font=fnt, fill=color)


def render(data, pulse=False):
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
        draw.text((17, 46), name + ' 输出', font=font(9), fill=INK_SOFT)
        draw.text((16, 61), '≈', font=font(14), fill=INK)
        reading = f"{port['watts']:.1f} W"
        size = 25
        while text_width(draw, reading, numfont(size)) > 147:
            size -= 1
        draw.text((32, 56), reading, font=numfont(size), fill=INK)
        draw.line((190, 51, 190, 77), fill=HAIRLINE, width=1)
        for y, label, value in ((47, '电压', f"{port['volts']:.1f} V"),
                                 (65, '电流', f"{port['amps']:.1f} A")):
            draw.text((201, y), label, font=font(9), fill=INK_SOFT)
            right_align(draw, 302, y - 1, value, numfont(14), INK)
    elif len(active) > 1:
        draw.text((17, 47), '总输出', font=font(10), fill=INK_SOFT)
        draw.text((90, 61), '≈', font=font(14), fill=INK)
        draw.text((108, 52), f"{power['watts']:.1f} W", font=numfont(28), fill=INK)
    else:
        draw.text((17, 46), '充电输出', font=font(9), fill=INK_SOFT)
        draw.text((17, 59), '未充电' if live else '数据暂不可用',
                  font=font(17), fill=INK if live else INK_SOFT)

    # LEFT: one continuous WorkBuddy card with two internal account rows.
    shaded_box(im, draw, (8, 91, 162, 219), GRAD_WB, BORDER_WB, radius=6)
    draw.line((17, 155, 153, 155), fill=(198, 94, 16), width=1)
    wb = data.get('workbuddy', {})
    accounts = wb.get('accounts', []) if wb.get('status') == 'live' else []
    for index, (fallback, name_y, value_y) in enumerate(
            (('账号A', 97, 112), ('账号B', 161, 176))):
        account = accounts[index] if index < len(accounts) else None
        name = str((account or {}).get('name', fallback))
        value = f"{account['balance']:,.0f}" if account else '—'
        if index == 0:
            draw.text((17, 95), 'WORKBUDDY', font=numfont(9), fill=INK)
        right_align(draw, 153, name_y, name, font(10), INK)
        draw.text((16, value_y), value, font=numfont(30),
                  fill=INK if account else BAD)
        right_align(draw, 151, value_y + 24, '积分', font(8), INK)

    # RIGHT: Codex receives more height; DeepSeek only needs one large value.
    shaded_box(im, draw, (168, 91, 311, 170), GRAD_CODEX, BORDER_CODEX, radius=6)
    cream = (255, 242, 230)
    draw.text((177, 95), 'CODEX', font=numfont(12), fill=cream)
    codex = data.get('codex', {})
    windows = codex.get('windows', []) if codex.get('status') == 'live' else []
    short = next((w for w in windows if w.get('minutes') and w['minutes'] <= 1440), None)
    long = next((w for w in windows if w.get('minutes') and w['minutes'] > 1440), None)
    right_align(draw, 302, 97, '剩余', font(8), cream)
    for label, window, y in (('5小时', short, 111), ('本周', long, 133)):
        draw.text((177, y + 4), label, font=font(10), fill=cream)
        right_align(draw, 303, y - 1,
                    f"{window['remaining']:.0f}%" if window else '—',
                    numfont(23), cream if window else BAD)
    reset = reset_text(short).split(' · ')[0].replace(' ', '') if short else '重置未知'
    draw.text((177, 157), '5小时窗口 · ' + reset, font=font(8), fill=cream)

    shaded_box(im, draw, (168, 176, 311, 219), GRAD_DS, BORDER_DS, radius=6)
    draw.text((177, 180), 'DEEPSEEK', font=numfont(10), fill=INK)
    ds = data.get('deepseek', {})
    ds_ok = ds.get('status') == 'live'
    draw.text((176, 191), f"¥{ds['cny']:.2f}" if ds_ok else '—',
              font=numfont(26), fill=INK if ds_ok else BAD)

    draw.text((9, 225), '余额更新 ' + stamp, font=font(9), fill=DIM)
    right_align(draw, 311, 225, '额度与账户余额', font(9), DIM)
    return im


def render_expired():
    im = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(im)
    shaded_box(im, draw, (8, 80, 311, 164), GRAD_FOOT, BORDER_FOOT)
    draw.text((24, 96), '数据已过期', font=font(26), fill=WHITE)
    draw.text((24, 134), '请检查电脑上的显示服务', font=font(14), fill=DIM)
    shaded_box(im, draw, (8, 220, 311, 237), GRAD_FOOT, BORDER_FOOT)
    draw.text((17, 222), 'AP01 目标设备 · 开机登录后自动恢复', font=font(9), fill=WHITE)
    return im


def publish(data):
    first, second = render(data), render(data, True)
    expired = render_expired()
    # Same 128-colour Median Cut palette as the Codex build: keeps gradients
    # intact on the LCD's limited palette and stays well inside the size cap.
    palette = first.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    frames = [palette,
              second.quantize(palette=palette, dither=Image.Dither.NONE),
              expired.quantize(palette=palette, dither=Image.Dither.NONE)]
    tmp = GIF.with_suffix('.gif.tmp')
    frames[0].save(tmp, format='GIF', save_all=True,
                   append_images=frames[1:], duration=[1000, 419000, 60000],
                   optimize=True)
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
    log(statuses={k: data.get(k, {}).get('status') for k in ('codex', 'workbuddy', 'deepseek')},
        gif_bytes=size)
    return data


def refresh_power(data):
    """Copy the independent five-second charging sample into the next GIF."""
    reader = power_reader()
    sample = reader.sample() if reader else None
    if sample:
        data['power'] = {'status': 'live', 'watts': sample['watts'],
                         'label': sample['label'], 'ports': sample['ports']}
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
                          for k in ('codex', 'workbuddy', 'deepseek')}, ensure_ascii=False))
        print('gif_bytes', GIF.stat().st_size)
        return
    bootstrap()
    server = ThreadingHTTPServer((BIND, PORT), make_balance_handler(GIF))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(event='server_started', bind=BIND, port=PORT)
    try:
        data = refresh()
        balance_due = time.monotonic() + 60
        while True:
            time.sleep(5)
            if time.monotonic() >= balance_due:
                data = refresh()
                balance_due = time.monotonic() + 60
            else:
                refresh_power(data)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
