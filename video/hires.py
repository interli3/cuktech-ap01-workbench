"""按仓库里的面板绘制逻辑重绘一份 4 倍分辨率的高清面板。

为什么不用现成的 320x240 截图放大：
那块屏只有 320x240，放大到 1080p 一定是虚的。这里照着
tools/display_service.py 和 tools/hardware_monitor/dashboard.py 的
布局和配色，重新按 1280x960 画一遍，文字全部是矢量级清晰的。

所有坐标都按原始 320x240 的逻辑写，函数内部统一乘以 S。
"""
from PIL import Image, ImageDraw, ImageFont

S = 4                      # 4 倍
PW, PH = 320 * S, 240 * S  # 1280 x 960

CN = 'C:/Windows/Fonts/msyh.ttc'
CNB = 'C:/Windows/Fonts/msyhbd.ttc'
NUM = 'C:/Windows/Fonts/bahnschrift.ttf'

_cache = {}


def f(size, bold=False):
    key = (size, bold)
    if key not in _cache:
        _cache[key] = ImageFont.truetype(CNB if bold else CN, size)
    return _cache[key]


def nf(size):
    key = ('n', size)
    if key not in _cache:
        _cache[key] = ImageFont.truetype(NUM, size)
    return _cache[key]


def gradient(w, h, top, bottom):
    layer = Image.new('RGB', (w, h))
    d = ImageDraw.Draw(layer)
    for row in range(h):
        t = row / max(1, h - 1)
        d.line((0, row, w - 1, row), fill=tuple(
            round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return layer


class Canvas:
    """按 320x240 的逻辑坐标作图，内部统一放大 S 倍。"""

    def __init__(self, size=(320, 240), bg=(9, 17, 27)):
        self.im = Image.new('RGB', (size[0] * S, size[1] * S), bg)
        self.d = ImageDraw.Draw(self.im)

    def text(self, x, y, s, fnt, fill):
        self.d.text((x * S, y * S), s, font=fnt, fill=fill)

    def line(self, x0, y0, x1, y1, fill, width=1):
        self.d.line((x0 * S, y0 * S, x1 * S, y1 * S), fill=fill, width=width * S)

    def rect(self, x0, y0, x1, y1, fill):
        self.d.rectangle((x0 * S, y0 * S, x1 * S, y1 * S), fill=fill)

    def ellipse(self, x0, y0, x1, y1, fill):
        self.d.ellipse((x0 * S, y0 * S, x1 * S, y1 * S), fill=fill)

    def width(self, s, fnt):
        b = self.d.textbbox((0, 0), s, font=fnt)
        return (b[2] - b[0]) / S

    def ralign(self, x1, y, s, fnt, fill):
        """右对齐到 x1（逻辑坐标）。"""
        self.d.text((x1 * S - self.width(s, fnt) * S, y * S), s, font=fnt, fill=fill)

    def shade(self, x0, y0, x1, y1, grad, border, radius=4):
        """grad = (top_color, bottom_color)，border = 描边色。"""
        w = (x1 - x0 + 1) * S
        h = (y1 - y0 + 1) * S
        mask = Image.new('L', (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=radius * S, fill=255)
        self.im.paste(gradient(w, h, grad[0], grad[1]), (x0 * S, y0 * S), mask)
        self.d.rounded_rectangle((x0 * S, y0 * S, x1 * S, y1 * S),
                                 radius=radius * S, outline=border, width=S)


# ---------------------------------------------------------------- 硬件面板
HW_BG = '#09111b'
HW_INK = '#17222d'
HW_SOFT = '#43505d'
HW_LINE = '#283d46'


def hw_panel(cpu_model='i5-14600KF', gpu_model='RTX 5070',
             app='Game.exe', clock='01:55:17', pulse=True,
             cpu=(46, 66, 80.7, 890), gpu=(61, 78, 175.7, 1659),
             ram=(26.4, 31.8), vram=(9.6, 11.9), fps=35):
    """返回 1280x960 的硬件面板。cpu/gpu = (温度, 占用%, 功率W, 风扇RPM)。"""
    c = Canvas(bg=HW_BG)

    c.text(9, 40, '实时硬件监测', f(11 * S), '#e2edf8')
    src = app
    while c.width(src, f(8 * S)) > 160:
        src = src[:-1]
    c.ralign(311, 42, src, f(8 * S), '#b8c8d6')

    # 充电状态（左侧大框，浅底深字，跟真机一致）
    c.shade(8, 55, 208, 101, ((247, 250, 252), (174, 185, 196)), '#ffffff')
    c.text(17, 58, '充电状态', f(9 * S), HW_SOFT)
    c.text(17, 73, '未充电', f(19 * S), HW_INK)

    # 帧率（右侧小框）
    c.shade(214, 55, 311, 101, ((20, 34, 51), (27, 59, 103)), '#8299af')
    c.text(222, 58, '帧率 FPS', f(8 * S), '#dce8f2')
    c.ralign(303, 69, str(fps), nf(27 * S), '#e1ff94')

    # CPU / GPU
    rows = (
        (8, (255, 192, 120), (255, 101, 0), '#fff0d6', 'CPU', cpu_model,
         cpu[0], cpu[2], cpu[1], cpu[3]),
        (163, (120, 227, 255), (0, 139, 255), '#e4fcff', 'GPU', gpu_model,
         gpu[0], gpu[2], gpu[1], gpu[3]),
    )
    for x, top, bottom, border, title, model, temp, watts, usage, rpm in rows:
        c.shade(x, 108, x + 148, 174, (top, bottom), border)
        c.text(x + 8, 111, title, nf(12 * S), HW_INK)
        c.text(x + 46, 114, model, nf(8 * S), HW_INK)
        c.text(x + 7, 122, f'{temp}°C', nf(27 * S), HW_INK)
        c.text(x + 117, 112, '占用', f(8 * S), HW_INK)
        c.ralign(x + 140, 122, f'{usage}%', nf(27 * S), HW_INK)
        c.line(x + 8, 151, x + 140, 151, HW_LINE)
        c.text(x + 8, 154, f'{watts:.1f} W', nf(14 * S), HW_INK)
        c.ralign(x + 140, 154, f'{rpm} RPM', nf(14 * S), HW_INK)

    # 内存 / 显存
    bars = (
        (8, (216, 250, 136), (164, 237, 0), '#f4ffda', '内存', ram[0], ram[1],
         '#b4ce77', '#466a0a'),
        (163, (203, 220, 255), (115, 123, 255), '#f1f5ff', '显存', vram[0], vram[1],
         '#a2b9de', '#355c99'),
    )
    for x, top, bottom, border, label, used, total, track, bar in bars:
        c.shade(x, 181, x + 148, 214, (top, bottom), border)
        c.text(x + 8, 184, label, f(9 * S), HW_INK)
        c.ralign(x + 140, 183, f'{used:.1f}/{total:.1f}G', nf(14 * S), HW_INK)
        c.rect(x + 9, 205, x + 139, 208, track)
        length = min(130, max(1, round(130 * used / total)))
        c.rect(x + 9, 205, x + 9 + length, 208, bar)

    # 底栏
    c.shade(8, 220, 311, 237, ((16, 29, 42), (16, 29, 42)), '#3e566b')
    c.text(17, 222, clock, nf(10 * S), '#dce8f2')
    c.text(245, 222, '实时更新', f(8 * S), '#dce8f2')
    c.ellipse(296, 226, 300, 230, '#9afa9b' if pulse else '#547560')
    return c.im


# ---------------------------------------------------------------- 账户面板
AC_INK = (23, 34, 45)
AC_SOFT = (67, 80, 93)
AC_DIM = (184, 200, 214)
AC_BAD = (255, 120, 110)
AC_CREAM = (255, 242, 230)
AC_HAIR = (135, 148, 160)
GRAD_SILVER = ((247, 250, 252), (174, 185, 196))
GRAD_WB = ((255, 193, 35), (255, 98, 22))
GRAD_CODEX = ((181, 128, 91), (91, 55, 38))
GRAD_DS = ((53, 224, 255), (10, 107, 240))


def ac_panel(port='C1', watts=44.6, volts=9.1, amps=4.9,
             acc_a='账号A', val_a=1280, acc_b='账号B', val_b=3560,
             codex_short=42, codex_long=78, reset='剩 2小时32分',
             deepseek=88.88, stamp='12:00', unavailable=False):
    c = Canvas(bg=(9, 17, 27))
    # 缺数据时「—」用深墨色：亮色渐变底上淡红看不清
    missing = AC_INK
    if unavailable:
        # 数据源全断：所有卡片都写「—」，不能留着最后一帧的数字骗人
        val_a = val_b = codex_short = codex_long = deepseek = None
        reset = '重置未知'

    # 顶部充电区
    c.shade(8, 42, 311, 85, GRAD_SILVER, (255, 255, 255), radius=6)
    if unavailable:
        c.text(17, 46, '充电输出', f(9 * S), AC_SOFT)
        c.text(17, 59, '数据暂不可用', f(17 * S), AC_SOFT)
        # 右侧电压电流位同样置空
        c.text(201, 47, '电压', f(9 * S), AC_SOFT)
        c.text(201, 65, '电流', f(9 * S), AC_SOFT)
        c.ralign(302, 46, '—', nf(14 * S), missing)
        c.ralign(302, 64, '—', nf(14 * S), missing)
    else:
        c.text(17, 46, port + ' 输出', f(9 * S), AC_SOFT)
        c.text(16, 61, '≈', f(14 * S), AC_INK)
        c.text(32, 56, f'{watts:.1f} W', nf(25 * S), AC_INK)
        c.line(190, 51, 190, 77, AC_HAIR)
        for y, label, value in ((47, '电压', f'{volts:.1f} V'),
                                (65, '电流', f'{amps:.1f} A')):
            c.text(201, y, label, f(9 * S), AC_SOFT)
            c.ralign(302, y - 1, value, nf(14 * S), AC_INK)

    # WORKBUDDY
    c.shade(8, 91, 162, 219, GRAD_WB, (255, 233, 194), radius=6)
    c.line(17, 155, 153, 155, (198, 94, 16))
    c.text(17, 95, 'WORKBUDDY', nf(9 * S), AC_INK)
    for name, value, ny, vy in ((acc_a, val_a, 97, 112), (acc_b, val_b, 161, 176)):
        c.ralign(153, ny, name, f(10 * S), AC_INK)
        txt = f'{value:,}' if value is not None else '—'
        c.text(16, vy, txt, nf(30 * S), AC_INK if value is not None else missing)
        c.ralign(151, vy + 24, '积分', f(8 * S), AC_INK)

    # CODEX
    c.shade(168, 91, 311, 170, GRAD_CODEX, (232, 197, 170), radius=6)
    c.text(177, 95, 'CODEX', nf(12 * S), AC_CREAM)
    c.ralign(302, 97, '剩余', f(8 * S), AC_CREAM)
    for label, value, y in (('5小时', codex_short, 111), ('本周', codex_long, 133)):
        c.text(177, y + 4, label, f(10 * S), AC_CREAM)
        txt = f'{value}%' if value is not None else '—'
        c.ralign(303, y - 1, txt, nf(23 * S),
                 AC_CREAM if value is not None else missing)
    c.text(177, 157, '5小时窗口 · ' + reset, f(8 * S), AC_CREAM)

    # DEEPSEEK
    c.shade(168, 176, 311, 219, GRAD_DS, (207, 246, 255), radius=6)
    c.text(177, 180, 'DEEPSEEK', nf(10 * S), AC_INK)
    txt = f'¥{deepseek:.2f}' if deepseek is not None else '—'
    c.text(176, 191, txt, nf(26 * S), AC_INK if deepseek is not None else missing)

    c.text(9, 225, '余额更新 ' + stamp, f(9 * S), AC_DIM)
    c.ralign(311, 225, '额度与账户余额', f(9 * S), AC_DIM)
    return c.im


def device_frame(panel, bezel=26, radius=18):
    """给面板加一圈设备外壳，模拟真机上的那块屏。"""
    w = panel.width + bezel * 2
    h = panel.height + bezel * 2
    out = Image.new('RGBA', (w, h), (26, 29, 34, 255))
    d = ImageDraw.Draw(out)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius + bezel,
                        fill=(26, 29, 34, 255))
    d.rounded_rectangle((bezel - 2, bezel - 2, w - bezel + 1, h - bezel + 1),
                        radius=radius, fill=(8, 10, 14, 255))
    out.paste(panel, (bezel, bezel))
    d.rounded_rectangle((bezel - 3, bezel - 3, w - bezel + 2, h - bezel + 2),
                        radius=radius + 2, outline=(58, 64, 74, 255), width=2)
    # 圆角外抠成透明，方便贴到画面上
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1),
                                           radius=radius + bezel, fill=255)
    out.putalpha(mask)
    return out


if __name__ == '__main__':
    import os
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else 'panels'
    os.makedirs(out, exist_ok=True)
    hw_panel().save(f'{out}/hw.png')
    ac_panel().save(f'{out}/account.png')
    ac_panel(unavailable=True).save(f'{out}/account_unavailable.png')
    print('已输出高清面板到', out)
