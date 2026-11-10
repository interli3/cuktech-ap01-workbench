"""生成 1080p 宣传视频的画面帧。

做法：全部用代码画，不做 AI 生成——AI 出的中文和数值不可控，
而且这块屏只有 320x240，只能靠高清重绘 + 排版才看得清。

画面节奏（每个场景一个函数，t 是场景内时间，0..1 为进度）：
  s1 开场      0.0 - 6.5s
  s2 版本提醒  6.5 - 17.5s
  s3 硬件模式  17.5 - 28.0s
  s4 账户模式  28.0 - 38.5s
  s5 不可用    38.5 - 43.0s
  s6 结尾      43.0 - 52.0s
"""
import math

from PIL import Image, ImageDraw, ImageFont

from hires import ac_panel, device_frame, hw_panel

W, H = 1920, 1080
FPS = 30

BG_TOP = (7, 12, 20)
BG_BOTTOM = (14, 22, 34)
ORANGE = (255, 174, 10)
AMBER = (255, 200, 90)
CYAN = (22, 245, 255)
RED = (255, 110, 96)
GREEN = (126, 240, 160)
WHITE = (236, 243, 250)
DIM = (150, 170, 190)
INK = (9, 14, 22)

CN = 'C:/Windows/Fonts/msyh.ttc'
CNB = 'C:/Windows/Fonts/msyhbd.ttc'
NUM = 'C:/Windows/Fonts/bahnschrift.ttf'

_fc = {}


def F(path, size):
    key = (path, size)
    if key not in _fc:
        _fc[key] = ImageFont.truetype(path, size)
    return _fc[key]


def cn(size, bold=False):
    return F(CNB if bold else CN, size)


def num(size):
    return F(NUM, size)


def ease_out(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t):
    t = max(0.0, min(1.0, t))
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def backdrop(draw, accent=ORANGE):
    """深色底 + 一层淡淡的斜向光带。"""
    for y in range(H):
        t = y / H
        draw.line((0, y, W, y),
                  fill=tuple(round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t)
                             for i in range(3)))
    # 右上角一抹强调色光晕
    glow = Image.new('RGB', (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(760, 0, -14):
        a = (1 - r / 760) ** 2.6
        col = tuple(int(accent[i] * a * 0.30) for i in range(3))
        gd.ellipse((W * 0.62 - r, -260 + r, W * 0.62 + r, -260 + r), fill=col)
    return glow


GLOW = {}


def glow_for(accent):
    if accent not in GLOW:
        tmp = Image.new('RGB', (W, H))
        gd = ImageDraw.Draw(tmp)
        for r in range(760, 0, -14):
            a = (1 - r / 760) ** 2.6
            col = tuple(int(accent[i] * a * 0.30) for i in range(3))
            gd.ellipse((W * 0.62 - r, -260 + r, W * 0.62 + r, -260 + r), fill=col)
        GLOW[accent] = tmp
    return GLOW[accent]


def slide_in(draw, x, y, text, font, fill, t, dx=-70, dy=0, anchor='la'):
    """文字从侧边滑入 + 淡入。返回是否画了。"""
    if t <= 0:
        return
    p = ease_out(min(1.0, t))
    alpha = p
    col = tuple(int(fill[i] * alpha) for i in range(3))
    if anchor == 'la':
        draw.text((x + dx * (1 - p), y + dy * (1 - p)), text, font=font, fill=col)
    elif anchor == 'ra':
        w = draw.textlength(text, font=font)
        draw.text((x - w + dx * (1 - p), y + dy * (1 - p)), text, font=font, fill=col)
    elif anchor == 'ma':
        w = draw.textlength(text, font=font)
        draw.text((x - w / 2 + dx * (1 - p), y + dy * (1 - p)), text, font=font, fill=col)


def bullet(draw, x, y, text, font, fill, t, accent=ORANGE, r=9):
    """一个带圆点的条目，依次点亮。"""
    if t <= 0:
        return
    p = ease_out(min(1.0, t))
    a = p
    dot = tuple(int(accent[i] * a) for i in range(3))
    txt = tuple(int(fill[i] * a) for i in range(3))
    draw.ellipse((x, y + r - 4, x + r * 2, y + r + 4), fill=dot)
    draw.text((x + r * 2 + 20, y), text, font=font, fill=txt)


def fade(image, t):
    """整体透明度（返回带 alpha 的副本）。"""
    if t >= 1:
        return image
    p = max(0.0, min(1.0, t))
    out = image.convert('RGBA')
    alpha = out.split()[3].point(lambda v: int(v * p))
    out.putalpha(alpha)
    return out


def panel_device(which, scale=1.0):
    if which == 'hw':
        panel = hw_panel()
    elif which == 'account':
        panel = ac_panel()
    else:
        panel = ac_panel(unavailable=True)
    dev = device_frame(panel)
    if scale != 1.0:
        dev = dev.resize((int(dev.width * scale), int(dev.height * scale)),
                         Image.LANCZOS)
    return dev


# ------------------------------------------------------------------ 场景
def scene_title(base, t):
    """s1 开场：标题 + 一句定位 + 两块屏剪影。"""
    d = ImageDraw.Draw(base)
    slide_in(d, W / 2, 210, '酷态科 10 / 15 号电能基站屏幕', cn(88, True),
             WHITE, t / 0.35, dy=40, anchor='ma')
    slide_in(d, W / 2, 330, '自定义显示', cn(88, True), ORANGE,
             (t - 0.18) / 0.35, dy=40, anchor='ma')
    slide_in(d, W / 2, 460, '一块屏，两种内容：电脑状态 · 账号额度',
             cn(38), DIM, (t - 0.42) / 0.35, dy=26, anchor='ma')
    bar_w = 720 * ease_out(min(1.0, max(0.0, (t - 0.5) / 0.4)))
    d.rectangle((W / 2 - bar_w / 2, 540, W / 2 + bar_w / 2, 544), fill=ORANGE)
    slide_in(d, W / 2, 578, '开源 · GitHub：interli3/cuktech-ap01-workbench',
             cn(30), (110, 132, 155), (t - 0.62) / 0.35, anchor='ma')

    # 底部并排两块屏的剪影，把下半部分填上
    pt = (t - 0.5) / 0.45
    if pt > 0:
        p = ease_out(min(1.0, pt))
        for i, which in enumerate(('hw', 'account')):
            dev = panel_device(which, 1.0)
            dev = dev.resize((int(dev.width * 0.30), int(dev.height * 0.30)),
                             Image.LANCZOS)
            cx = W / 2 + (i * 2 - 1) * 400
            cy = 820
            base.paste(dev, (int(cx - dev.width / 2), int(cy - dev.height / 2)), dev)
        label = '硬件监控台' if True else ''
        for i, (txt, col) in enumerate((('硬件监控台', CYAN), ('账户工作台', ORANGE))):
            cx = W / 2 + (i * 2 - 1) * 400
            slide_in(d, cx, 960, txt, cn(30, True), col,
                     (t - 0.75 - i * 0.08) / 0.3, anchor='ma')
    return base


def scene_version(base, t):
    """s2 版本提醒：三张卡片。"""
    d = ImageDraw.Draw(base)
    slide_in(d, 120, 150, '先确认版本号', cn(64, True), WHITE, t / 0.25, dy=-30)
    slide_in(d, 120, 235, '米家里显示成 1.0.2_00XX 这种格式', cn(30),
             DIM, (t - 0.12) / 0.25, dy=-16)

    cards = [
        ('0031', '直接用', GREEN, '已验证'),
        ('0041', '可降到 0031', ORANGE, '真机做过两次'),
        ('0055', '别去升级', RED, '升上去目前降不回来'),
    ]
    cw, ch, gap = 500, 400, 60
    x0 = (W - (cw * 3 + gap * 2)) // 2
    for i, (ver, act, col, note) in enumerate(cards):
        ct = (t - 0.25 - i * 0.13) / 0.28
        if ct <= 0:
            continue
        p = ease_out(min(1.0, ct))
        x = x0 + i * (cw + gap)
        y = 340 + int(60 * (1 - p))
        card = Image.new('RGBA', (cw, ch), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=26,
                             fill=(255, 255, 255, 16),
                             outline=col + (255,), width=3)
        cd.text((40, 46), ver, font=num(104), fill=col)
        cd.text((40, 186), act, font=cn(44, True), fill=WHITE)
        cd.line((40, 268, cw - 40, 268), fill=(90, 110, 130, 200), width=2)
        cd.text((40, 292), note, font=cn(28), fill=DIM)
        card = card.rotate(0)
        base.paste(card, (x, y), card)
    return base


def _feature_scene(base, t, title, sub, items, panel, accent, panel_x=1250,
                   panel_y=210, panel_h=660):
    """s3 / s4 共用：左边文字要点，右边真机屏幕。"""
    d = ImageDraw.Draw(base)
    slide_in(d, 120, 150, title, cn(64, True), WHITE, t / 0.25, dy=-30)
    slide_in(d, 120, 235, sub, cn(30), accent, (t - 0.12) / 0.25, dy=-16)
    for i, item in enumerate(items):
        bullet(d, 126, 340 + i * 92, item, cn(36), WHITE,
               (t - 0.3 - i * 0.11) / 0.25, accent=accent)

    pt = (t - 0.2) / 0.3
    if pt > 0:
        p = ease_out(min(1.0, pt))
        # 屏幕从 0.96 缓慢推到 1.06，让画面有呼吸感，也让人看清数字
        zoom = 0.96 + 0.10 * min(1.0, max(0.0, (t - 0.5) / 6.0))
        dev = panel.resize((int(panel.width * zoom), int(panel.height * zoom)),
                           Image.LANCZOS)
        px = int(panel_x - dev.width / 2)
        py = int(panel_y - dev.height / 2 + 30 * (1 - p))
        base.paste(dev, (px, py), dev)
    return base


def scene_hw(base, t):
    dev = panel_device('hw', 1.0)
    dev = dev.resize((int(dev.width * 0.66), int(dev.height * 0.66)), Image.LANCZOS)
    return _feature_scene(
        base, t, '模式一 · 硬件监控台', '屏幕显示你电脑此刻的真实状态',
        ['CPU / 显卡：功率、温度、占用、风扇转速',
         '内存、显存用量',
         '游戏实时帧率',
         'C1 / C2 / C3 / USB-A 四个口的充电功率',
         '读不到就显示「不可用」，不写假的 0'],
        dev, CYAN, panel_x=1420, panel_y=530)


def scene_account(base, t):
    dev = panel_device('account', 1.0)
    dev = dev.resize((int(dev.width * 0.66), int(dev.height * 0.66)), Image.LANCZOS)
    return _feature_scene(
        base, t, '模式二 · 账户工作台', '屏幕显示你几个 AI 账号还剩多少',
        ['Codex：剩余额度 + 重置时间',
         'WorkBuddy：两个账号的积分',
         'DeepSeek：人民币余额',
         '同时显示充电功率、电压、电流',
         '数据源断了就显示「不可用」'],
        dev, ORANGE, panel_x=1420, panel_y=530)


def scene_unavailable(base, t):
    """s5：读不到数据时的样子。"""
    d = ImageDraw.Draw(base)
    slide_in(d, W / 2, 150, '数据断了，屏上就写「不可用」', cn(58, True),
             WHITE, t / 0.25, dy=-26, anchor='ma')
    pt = (t - 0.15) / 0.3
    if pt > 0:
        p = ease_out(min(1.0, pt))
        dev = panel_device('unavailable', 1.0)
        dev = dev.resize((int(dev.width * 0.50), int(dev.height * 0.50)),
                         Image.LANCZOS)
        base.paste(dev, (int(W / 2 - dev.width / 2), int(280 + 20 * (1 - p))), dev)
    slide_in(d, W / 2, 880, '电脑关了、网断了、账号掉线了 —— 屏上不会停在最后一帧骗你',
             cn(32), DIM, (t - 0.45) / 0.3, anchor='ma')
    return base


QR = None


def qr_image(text='https://github.com/interli3/cuktech-ap01-workbench', size=260):
    global QR
    if QR is None:
        import qrcode
        qr = qrcode.QRCode(version=None, box_size=10, border=2)
        qr.add_data(text)
        qr.make(fit=True)
        QR = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    return QR.resize((size, size), Image.NEAREST)


def scene_end(base, t):
    """s6：结尾，给 B 站观众一个可点的入口。"""
    d = ImageDraw.Draw(base)
    slide_in(d, W / 2, 150, '全部开源', cn(76, True), WHITE, t / 0.25,
             dy=-30, anchor='ma')
    slide_in(d, W / 2, 268, 'GitHub：interli3/cuktech-ap01-workbench',
             cn(44, True), ORANGE, (t - 0.15) / 0.25, anchor='ma')

    # 二维码：扫一下直接到仓库
    pt = (t - 0.28) / 0.3
    if pt > 0:
        p = ease_out(min(1.0, pt))
        size = int(280 * p)
        qr = qr_image(size=size)
        qx = int(W / 2 - size / 2)
        qy = 352
        # 白色底衬，保证在深色背景上对比够
        pad = 18
        plate = Image.new('RGB', (size + pad * 2, size + pad * 2), (255, 255, 255))
        plate.paste(qr, (pad, pad))
        base.paste(plate, (qx - pad, qy - pad))
        slide_in(d, W / 2, qy + size + 24, '扫码进仓库', cn(26),
                 (140, 160, 180), (t - 0.4) / 0.25, anchor='ma')

    items = [
        '0041 降 0031 这条路，是 slepher 先走通并公开日志的',
        '刷机工具链来自 andy-JustSayWhen/cuktech-ap01-firmware-tools',
        '小米登录流程来自 PiotrMachowski/Xiaomi-cloud-tokens-extractor',
    ]
    for i, it in enumerate(items):
        slide_in(d, W / 2, 762 + i * 54, it, cn(26), (168, 188, 208),
                 (t - 0.45 - i * 0.08) / 0.25, anchor='ma')

    # 风险提示条
    pt = (t - 0.66) / 0.3
    if pt > 0:
        p = ease_out(min(1.0, pt))
        bar = Image.new('RGBA', (1180, 92), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar)
        bd.rounded_rectangle((0, 0, 1179, 91), radius=18,
                             fill=(255, 110, 96, 30), outline=(255, 110, 96, 255),
                             width=2)
        bd.text((40, 28), '刷机可能变砖、失去保修。先确认版本，再动手。',
                font=cn(32, True), fill=(255, 190, 180))
        bar = fade(bar, p)
        base.paste(bar, (int(W / 2 - 590), 946), bar)
    return base


# 时间轴
SCENES = [
    ('s1', 0.0, 6.5, scene_title),
    ('s2', 6.5, 17.5, scene_version),
    ('s3', 17.5, 28.0, scene_hw),
    ('s4', 28.0, 38.5, scene_account),
    ('s5', 38.5, 43.0, scene_unavailable),
    ('s6', 43.0, 52.0, scene_end),
]
ACCENT = {'s1': ORANGE, 's2': ORANGE, 's3': CYAN, 's4': ORANGE,
          's5': RED, 's6': ORANGE}
