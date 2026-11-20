"""给账户余额屏单独登录一次 Cursor，用来只读查询 Grok 周额度。

这是一次独立登录。脚本只读写 config.json 里配置的那几个文件，
绝不会打开 Grok Bot 桌面端的 sand-secrets.json。

流程和 Cursor CLI 的 loginDeepControl 一样：
  - verifier = 96 个随机字节的 base64url（无填充）
  - challenge = SHA-256(verifier 的 ASCII) 的 base64url（无填充）
  - 浏览器打开 https://cursor.com/loginDeepControl?challenge=..&uuid=..&mode=login&redirectTarget=cli
  - 轮询 GET https://api2.cursor.sh/auth/poll?uuid=..&verifier=..
      404 = 还没点授权；200 = JSON，里面有 accessToken 和 refreshToken
  - 用量 POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus
      body 是 {}。屏幕只用这个接口。
      --check 还会再打一次 GetCurrentPeriodUsage，只做交叉核对，屏幕不采用它的数字。
      不要改去 cursor.com/api/usage-summary，周额度套餐上那个接口的数是错的。

token 用 Windows DPAPI（当前用户）加密后写到 grok_cursor_auth.bin。
必须和显示服务是同一个 Windows 登录用户，否则服务解不开。

用法（在仓库根目录）：
  python tools/grok_cursor_auth.py --start    # 打印链接，后台轮询最多 20 分钟
  python tools/grok_cursor_auth.py --check    # 解密后只读调用用量接口，输出打码
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ap01_config as cfg

POLL_WINDOW_S = 20 * 60
BASE_DELAY, MAX_DELAY, BACKOFF = 1.0, 10.0, 1.2
CST = timezone(timedelta(hours=8))
SENSITIVE = ('token', 'email', 'secret', 'key', 'auth', 'userid', 'user_id', 'name')


def _url(key: str, default: str) -> str:
    return str(cfg.get('sources', key, default=default))


def login_url() -> str:
    return _url('grok_login_url', 'https://cursor.com/loginDeepControl')


def poll_url() -> str:
    return _url('grok_poll_url', 'https://api2.cursor.sh/auth/poll')


def dashboard_url() -> str:
    base = _url('grok_dashboard_url', 'https://api2.cursor.sh/aiserver.v1.DashboardService/')
    return base if base.endswith('/') else base + '/'


def paths() -> dict[str, Path]:
    link = cfg.resolve(cfg.get('sources', 'grok_link_file', default='run/grok_auth_link.txt'))
    store = cfg.resolve(cfg.get('sources', 'grok_auth_store', default='run/grok_cursor_auth.bin'))
    status = cfg.resolve(cfg.get('sources', 'grok_auth_status', default='run/grok_auth_status.json'))
    for path in (link, store, status):
        if 'sand-secrets' in path.name.lower():
            raise SystemExit(
                '这个脚本是独立登录，不会读写 Grok Bot 桌面端的 sand-secrets.json。\n'
                '请把 config.json 里的 grok_auth_store / grok_auth_status / grok_link_file 改成别的文件名。'
            )
    return {'link': link, 'store': store, 'status': status}


def _crypto():
    if sys.platform != 'win32':
        raise SystemExit('授权脚本依赖 Windows DPAPI，请在运行显示服务的那台 Windows 上执行。')
    try:
        from credentials import protect, unprotect
    except OSError as error:
        raise SystemExit('加载 DPAPI 失败，请在 Windows 上执行。') from error
    return protect, unprotect


def load_creds() -> dict | None:
    store = paths()['store']
    if not store.exists():
        return None
    _, unprotect = _crypto()
    return json.loads(unprotect(store.read_bytes()))


def save_creds(creds: dict) -> None:
    store = paths()['store']
    protect, _ = _crypto()
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_suffix(store.suffix + '.tmp')
    tmp.write_bytes(protect(json.dumps(creds)))
    os.replace(tmp, store)


def now() -> datetime:
    return datetime.now(CST)


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def jwt_exp(token: str) -> int | None:
    try:
        part = token.split('.')[1]
        part += '=' * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part))['exp'])
    except Exception:
        return None


def write_status(status: str, **extra) -> None:
    target = paths()['status']
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {'status': status, 'updated_at': now().isoformat(timespec='seconds')}
    data.update(extra)
    tmp = target.with_suffix(target.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, target)


def poll_worker() -> int:
    import requests

    files = paths()
    files['link'].parent.mkdir(parents=True, exist_ok=True)
    verifier = b64url(secrets.token_bytes(96))
    challenge = b64url(hashlib.sha256(verifier.encode('ascii')).digest())
    uid = str(uuidlib.uuid4())
    link = requests.Request('GET', login_url(), params={
        'challenge': challenge, 'uuid': uid, 'mode': 'login', 'redirectTarget': 'cli'}).prepare().url
    started = now()
    deadline = started + timedelta(seconds=POLL_WINDOW_S)
    files['link'].write_text(link + '\n', encoding='utf-8')
    write_status('waiting_for_user', started_at=started.isoformat(timespec='seconds'),
                 deadline=deadline.isoformat(timespec='seconds'), pid=os.getpid())

    delay, errors = BASE_DELAY, 0
    session = requests.Session()
    while now() < deadline:
        time.sleep(delay)
        try:
            response = session.get(poll_url(), params={'uuid': uid, 'verifier': verifier}, timeout=15)
        except requests.RequestException as error:
            errors += 1
            write_status('waiting_for_user', started_at=started.isoformat(timespec='seconds'),
                         deadline=deadline.isoformat(timespec='seconds'), pid=os.getpid(),
                         last_error=type(error).__name__, consecutive_errors=errors)
            delay = min(delay * BACKOFF, MAX_DELAY)
            continue
        if response.status_code == 404:
            errors = 0
            delay = min(delay * BACKOFF, MAX_DELAY)
            continue
        if response.ok:
            try:
                body = response.json()
                access, refresh = body.get('accessToken'), body.get('refreshToken')
            except ValueError:
                access = refresh = None
            if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
                write_status('failed', reason='poll 200 but response had no accessToken/refreshToken',
                             at=now().isoformat(timespec='seconds'))
                return 2
            exp = jwt_exp(access)
            save_creds({'accessToken': access, 'refreshToken': refresh,
                        'obtained_at': now().isoformat(timespec='seconds'), 'access_exp': exp})
            write_status('authorized', authorized_at=now().isoformat(timespec='seconds'),
                         access_token_expires_at=(datetime.fromtimestamp(exp, CST).isoformat(timespec='seconds')
                                                  if exp else None))
            try:
                files['link'].unlink()
            except OSError:
                pass
            return 0
        errors += 1
        write_status('waiting_for_user', started_at=started.isoformat(timespec='seconds'),
                     deadline=deadline.isoformat(timespec='seconds'), pid=os.getpid(),
                     last_error=f'HTTP {response.status_code}', consecutive_errors=errors)
        delay = min(delay * BACKOFF, MAX_DELAY)
    write_status('expired', deadline=deadline.isoformat(timespec='seconds'))
    return 1


def start() -> int:
    if sys.platform != 'win32':
        raise SystemExit('请在 Windows 上运行：token 用当前用户的 DPAPI 加密，必须和显示服务同一个登录用户。')
    files = paths()
    files['link'].parent.mkdir(parents=True, exist_ok=True)
    if files['link'].exists():
        files['link'].unlink()
    pyw = Path(sys.executable).with_name('pythonw.exe')
    exe = str(pyw if pyw.exists() else Path(sys.executable))
    flags = 0x00000008 | 0x00000200 | 0x01000000
    script = str(Path(__file__).resolve())
    try:
        proc = subprocess.Popen([exe, script, '--poll-worker'],
                                creationflags=flags, close_fds=True, cwd=str(cfg.ROOT))
    except OSError:
        proc = subprocess.Popen([exe, script, '--poll-worker'],
                                creationflags=0x00000008 | 0x00000200, close_fds=True,
                                cwd=str(cfg.ROOT))
    for _ in range(100):
        if files['link'].exists():
            break
        time.sleep(0.1)
    if not files['link'].exists():
        print('没有生成登录链接。看 run/grok_auth_status.json 里的 status。')
        return 1
    status = json.loads(files['status'].read_text(encoding='utf-8'))
    print('worker_pid:', proc.pid)
    print('deadline:', status.get('deadline'))
    print('link_file:', files['link'])
    print('link:', files['link'].read_text(encoding='utf-8').strip())
    return 0


def flatten(obj, prefix=''):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from flatten(value, f'{prefix}.{key}' if prefix else key)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from flatten(value, f'{prefix}[{index}]')
    else:
        yield prefix, obj


def show(name: str, data) -> None:
    print(f'== {name}')
    for key, value in flatten(data):
        if any(word in key.lower() for word in SENSITIVE) or (isinstance(value, str) and '@' in value):
            value = '<redacted>'
        elif isinstance(value, str) and value.isdigit() and len(value) == 13:
            value = f'{value} ({datetime.fromtimestamp(int(value) / 1000, CST):%Y-%m-%d %H:%M:%S} UTC+8)'
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and 1e12 < value < 1e13:
            value = f'{value} ({datetime.fromtimestamp(value / 1000, CST):%Y-%m-%d %H:%M:%S} UTC+8)'
        print(f'  {key} = {value}')


def check() -> int:
    import requests

    creds = load_creds()
    if not creds:
        print('还没有凭据。先运行 --start，在浏览器里点一次登录。')
        return 1
    exp = creds.get('access_exp')
    if exp:
        print('access_token_expires_at:', datetime.fromtimestamp(exp, CST).isoformat(timespec='seconds'))
    headers = {'Authorization': f"Bearer {creds['accessToken']}",
               'Content-Type': 'application/json', 'connect-protocol-version': '1'}
    code = 0
    # 屏幕只认 GetSandUsageStatus。另一个接口仅供对照，数字可能和周额度对不上。
    for endpoint, note in (
        ('GetSandUsageStatus', '屏幕使用这个'),
        ('GetCurrentPeriodUsage', '只做交叉核对，屏幕不使用'),
    ):
        print(f'-- {note}')
        try:
            response = requests.post(dashboard_url() + endpoint, headers=headers, data='{}', timeout=15)
        except requests.RequestException as error:
            print(f'== {endpoint}: 网络错误 {type(error).__name__}')
            code = 1
            continue
        if not response.ok:
            print(f'== {endpoint}: HTTP {response.status_code}')
            code = 1
            continue
        try:
            show(endpoint, response.json())
        except ValueError:
            print(f'== {endpoint}: 返回的不是 JSON')
            code = 1
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--start', action='store_true', help='生成登录链接并在后台轮询')
    parser.add_argument('--poll-worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--check', action='store_true', help='读用量并打码输出，不打印 token')
    args = parser.parse_args()
    if args.start:
        return start()
    if args.poll_worker:
        try:
            return poll_worker()
        except Exception as error:
            try:
                write_status('failed', reason=type(error).__name__, at=now().isoformat(timespec='seconds'))
            except Exception:
                pass
            return 3
    if args.check:
        return check()
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
