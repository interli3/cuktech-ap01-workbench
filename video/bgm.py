"""背景音乐：纯代码合成，不碰任何第三方版权素材。

主题贴合「一块小屏显示实时数据」——科技感但不吵：
  - 低频 pad 铺底（C - Am - F - G 循环）
  - 一层很轻的琶音，音高跟着 pad 走，做出「数据在流动」的感觉
  - 极轻的低频鼓点，给节奏骨架
  - 一层几乎听不见的高频空气声

电平策略：整体峰值压到 -20dBFS 左右，混音时人声段再闪避 8dB。
"""
import numpy as np
import wave

SR = 44100

# C - Am - F - G，每段 4 秒
CHORDS = [
    (130.81, 261.63, 329.63),   # C3 C4 E4
    (110.00, 220.00, 261.63),   # A2 A3 C4
    (87.31, 174.61, 220.00),    # F2 F3 A3
    (98.00, 196.00, 246.94),    # G2 G3 B3
]
SEG = 4.0


def _adsr(n, a=0.4, r=0.5):
    env = np.ones(n)
    ai, ri = int(n * a), int(n * r)
    if ai:
        env[:ai] = np.linspace(0, 1, ai)
    if ri:
        env[-ri:] = np.linspace(1, 0, ri)
    return env


def _pad(freq, dur, detune=0.0):
    n = int(SR * dur)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for mult, gain in ((1, 1.0), (2, 0.34), (3, 0.16), (4, 0.08), (6, 0.04)):
        out += gain * np.sin(2 * np.pi * freq * mult * (1 + detune) * t)
    lfo = 1 + 0.004 * np.sin(2 * np.pi * 0.23 * t)
    return out * lfo * _adsr(n, 0.35, 0.4)


def _pluck(freq, dur, gain=1.0):
    """短促的合成器拨弦，尾巴有衰减。"""
    n = int(SR * dur)
    t = np.arange(n) / SR
    out = (np.sin(2 * np.pi * freq * t)
           + 0.32 * np.sin(2 * np.pi * freq * 2 * t)
           + 0.14 * np.sin(2 * np.pi * freq * 3 * t))
    env = np.exp(-t * 4.2) * _adsr(n, 0.02, 0.25)
    return out * env * gain


def build(duration=52.0, out='audio/bgm.wav', peak=0.10):
    total = int(SR * duration)
    buf = np.zeros(total)

    # --- pad 铺底 ---
    pos, idx = 0.0, 0
    while pos < duration:
        lo, hi, third = CHORDS[idx % len(CHORDS)]
        dur = min(SEG, duration - pos)
        if dur <= 0.05:
            break
        voice = (_pad(lo, dur) * 0.50 + _pad(hi, dur, 0.0015) * 0.34
                 + _pad(third, dur, 0.0025) * 0.20)
        s = int(pos * SR)
        seg = voice[:int(dur * SR)]
        buf[s:s + len(seg)] += seg[:max(0, len(buf) - s)]
        pos += SEG
        idx += 1

    # --- 琶音：八分音符，走和弦音 + 顺手滑音，像数据在跳 ---
    step = 0.5
    pos, idx, arp_i = 0.0, 0, 0
    pattern = [0, 1, 2, 1, 2, 0, 1, 2]
    while pos < duration:
        lo, hi, third = CHORDS[idx % len(CHORDS)]
        notes = [hi, third, hi * 2]
        f = notes[pattern[arp_i % len(pattern)]]
        f *= 2 ** ((arp_i % 8 == 7) * 0.083)   # 每 8 步升半音，制造推进感
        pluck = _pluck(f, 0.9, 0.115)
        s = int(pos * SR)
        e = min(total, s + len(pluck))
        buf[s:e] += pluck[:e - s]
        pos += step
        arp_i += 1
        if arp_i % 8 == 0:
            idx += 1

    # --- 低频鼓点 ---
    for start_t in np.arange(0, duration, 1.0):
        s = int(start_t * SR)
        n = int(0.22 * SR)
        if s + n > total:
            break
        env = np.exp(-np.linspace(0, 7, n))
        tt = np.arange(n) / SR
        buf[s:s + n] += np.sin(2 * np.pi * 58 * tt) * env * 0.13

    # --- 空气声 ---
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 1, total)
    alpha = 0.0022
    acc = 0.0
    for i in range(total):
        acc += alpha * (noise[i] - acc)
        noise[i] = acc
    buf += noise * 1100.0

    # 整体淡入淡出
    fade = int(2.0 * SR)
    buf[:fade] *= np.linspace(0, 1, fade)
    buf[-fade:] *= np.linspace(1, 0, fade)

    m = np.max(np.abs(buf)) or 1.0
    buf = buf / m * peak

    data = (buf * 32767).astype(np.int16)
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    print(f'BGM：{out}  {duration:.1f}s  峰值 {peak:.3f}（≈{20 * np.log10(peak):.1f} dBFS）')


if __name__ == '__main__':
    import os
    os.makedirs('audio', exist_ok=True)
    build()
