"""画一块 320x240 的硬件面板，外加一帧「数据已过期」的兜底画面。

三帧循环：正常 0.5 秒 → 呼吸灯亮 4.5 秒 → 过期提示 60 秒。
过期那帧是故意的：电脑关了、网断了，屏上就是要写「已过期」，
而不是把最后一帧冻在那里骗人。
"""
import io
import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ap01_config import get  # noqa: E402

CPU = '#78b8ff'
GPU = '#ffb476'
RAM = '#c3a6ff'
FPS = '#78e0b8'
WHITE = '#e9f0f7'
MUTED = '#8d9eaf'
BACKGROUND = '#040809'

# 卡片上那行小字型号，按自己的机器改 config.json 的 hardware.cpu_model / gpu_model
CPU_MODEL = str(get('hardware', 'cpu_model', default='CPU'))
GPU_MODEL = str(get('hardware', 'gpu_model', default='GPU'))

CN_FONT = get('hardware', 'font_cn', default='C:/Windows/Fonts/msyh.ttc')
NUM_FONT = get('hardware', 'font_num', default='C:/Windows/Fonts/bahnschrift.ttf')


def font(size):
    try:
        return ImageFont.truetype(CN_FONT, size)
    except OSError:  # 字体缺失就退回 Pillow 自带位图字体，宁可难看也别崩
        return ImageFont.load_default()


def number(size):
    try:
        return ImageFont.truetype(NUM_FONT, size)
    except OSError:
        return ImageFont.load_default()


def value(number, suffix='', digits=0):
    return '—' if number is None else f'{number:.{digits}f}{suffix}'


def charging_text(charging):
    if not charging:
        return '充电数据 —', ''
    active = [(name, p) for name, p in charging['ports'].items() if p['watts'] > 0]
    if not active:
        return '未充电', ''
    if len(active) > 1:
        return f"总功率 ≈{charging['watts']:.1f} W", ''
    name, port = active[0]
    return f"{name} ≈{port['watts']:.1f} W", f"{port['volts']:.1f} V · {port['amps']:.1f} A"


def render(data, sequence):
    frames = []
    for pulse in (False, True):
        image = Image.new('RGB', (320, 240), '#09111b')
        draw = ImageDraw.Draw(image)
        draw.text((9, 40), '实时硬件监测', font=font(11), fill='#e2edf8')
        source = data['frames'].get('application') or '等待游戏'
        while draw.textlength(source, font=font(8)) > 160:
            source = source[:-1]
        draw.text((311 - draw.textlength(source, font=font(8)), 42), source, font=font(8), fill='#b8c8d6')

        def box(bounds, fill, border):
            shades = {
                '#142233': ('#315e92', '#1b3b67'),
                '#112c35': ('#f7fafc', '#aeb9c4'),
                '#ffc078': ('#ffae0a', '#ff6500'),
                '#78e3ff': ('#16f5ff', '#008bff'),
                '#d8fa88': ('#e4ff0a', '#a4ed00'),
                '#cbdcff': ('#99baff', '#737bff'),
                '#101d2a': ('#294457', '#182e40'),
            }
            x, y, right, lower = bounds
            width, height = right - x + 1, lower - y + 1
            top, bottom = shades[fill]
            start, end = ImageColor.getrgb(top), ImageColor.getrgb(bottom)
            gradient = Image.new('RGB', (width, height))
            gradient_draw = ImageDraw.Draw(gradient)
            for row in range(height):
                color = tuple(round(a + (b - a) * row / (height - 1)) for a, b in zip(start, end))
                gradient_draw.line((0, row, width - 1, row), fill=color)
            mask = Image.new('L', (width, height))
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=4, fill=255)
            image.paste(gradient, (x, y), mask)
            draw.rounded_rectangle(bounds, radius=4, outline=border, width=1)

        box((214, 55, 311, 101), '#142233', '#8299af')
        draw.text((222, 58), '帧率 FPS', font=font(8), fill='#dce8f2')
        fps_text = value(data['frames']['fps'])
        draw.text((303 - draw.textlength(fps_text, font=number(27)), 69), fps_text,
                  font=number(27), fill='#e1ff94')
        charging = data.get('charging')
        active = [(name, p) for name, p in charging['ports'].items() if p['watts'] > 0] if charging else []
        if len(active) == 1:
            name, port = active[0]
            box((8, 55, 208, 101), '#112c35', '#ffffff')
            draw.text((17, 58), name + ' 输出', font=font(9), fill='#43505d')
            draw.text((16, 79), '≈', font=font(14), fill='#17222d')
            power = value(port['watts'], ' W', 1)
            size = 23
            while draw.textlength(power, font=number(size)) > 91:
                size -= 1
            draw.text((30, 73), power, font=number(size), fill='#17222d')
            draw.line((125, 62, 125, 94), fill='#8794a0')
            for y, label, reading in [(58, '电压', value(port['volts'], 'V', 1)),
                                       (80, '电流', value(port['amps'], 'A', 1))]:
                draw.text((134, y + 2), label, font=font(8), fill='#53616e')
                draw.text((201 - draw.textlength(reading, font=number(13)), y), reading,
                          font=number(13), fill='#17222d')
            draw.line((133, 78, 201, 78), fill='#8794a0')
        else:
            box((8, 55, 208, 101), '#112c35', '#ffffff')
            draw.text((17, 58), '总功率' if active else '充电状态', font=font(9), fill='#43505d')
            if active:
                draw.text((17, 79), '≈', font=font(16), fill='#17222d')
                draw.text((34, 73), value(charging['watts'], ' W', 1), font=number(25), fill='#17222d')
            else:
                draw.text((17, 73), '未充电' if charging else '等待充电数据', font=font(19), fill='#17222d')

        ink = '#102130'
        for x, fill, border, title, model, temperature, watts, usage, fan_rpm in [
            (8, '#ffc078', '#fff0d6', 'CPU', CPU_MODEL, data.get('cpu_temperature'), data['cpu_watts'], data['cpu_percent'], data.get('cpu_fan_rpm')),
            (163, '#78e3ff', '#e4fcff', 'GPU', GPU_MODEL, data.get('gpu_temperature'), data['gpu_watts'], data['gpu_percent'], data.get('gpu_fan_rpm')),
        ]:
            box((x, 108, x + 148, 174), fill, border)
            draw.text((x + 8, 111), title, font=number(12), fill=ink)
            draw.text((x + 46, 114), model, font=number(8), fill=ink)
            draw.text((x + 7, 122), value(temperature, '°C'), font=number(27), fill=ink)
            draw.text((x + 117, 112), '占用', font=font(8), fill=ink)
            usage_text = value(usage, '%')
            draw.text((x + 140 - draw.textlength(usage_text, font=number(27)), 122), usage_text,
                      font=number(27), fill=ink)
            draw.line((x + 8, 151, x + 140, 151), fill='#283d46')
            power_text = value(watts, ' W', 1)
            draw.text((x + 8, 154), power_text, font=number(14), fill=ink)
            rpm_text = value(fan_rpm, ' RPM')
            draw.text((x + 140 - draw.textlength(rpm_text, font=number(14)), 154), rpm_text,
                      font=number(14), fill=ink)

        for x, label, used, total, fill, border, track, bar in [
            (8, '内存', data['ram_used_gb'], data['ram_total_gb'], '#d8fa88', '#f4ffda', '#b4ce77', '#466a0a'),
            (163, '显存', data.get('vram_used_gb'), data.get('vram_total_gb'), '#cbdcff', '#f1f5ff', '#a2b9de', '#355c99'),
        ]:
            box((x, 181, x + 148, 237), fill, border)
            percent = f'{used / total * 100:.0f}%' if used is not None and total else '—'
            draw.text((x + 8, 185), label, font=font(11), fill=ink)
            text = f'{used:.1f}/{total:.1f}G' if used is not None and total else '—'
            draw.text((x + 140 - draw.textlength(text, font=number(17)), 183), text, font=number(17), fill=ink)
            draw.text((x + 8, 199), percent, font=number(17), fill=ink)
            draw.rectangle((x + 9, 219, x + 139, 228), fill=track)
            if used is not None and total:
                length = min(130, max(1, round(130 * used / total)))
                draw.rectangle((x + 9, 219, x + 9 + length, 228), fill=bar)
        frames.append(image)
    stale = Image.new('RGB', (320, 240), BACKGROUND)
    draw = ImageDraw.Draw(stale)
    draw.text((28, 87), '监测数据已过期', font=font(26), fill=GPU)
    draw.text((27, 135), '请保持电脑和网络在线', font=font(19), fill=MUTED)
    palette = frames[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    encoded = [palette, frames[1].quantize(palette=palette, dither=Image.Dither.NONE),
               stale.quantize(palette=palette, dither=Image.Dither.NONE)]
    stream = io.BytesIO()
    encoded[0].save(stream, format='GIF', save_all=True, append_images=encoded[1:],
                    duration=[500, 4500, 60000], optimize=True)
    body = stream.getvalue()
    if len(body) > 24572 or not body.startswith(b'GIF89a'):
        raise RuntimeError('Dashboard outside streaming image bounds')
    return body, frames[0]
