"""主渲染：画帧 -> 混音 -> 合成 MP4。

音频处理顺序：
  1. 各句旁白按场景起点对齐，插入静音拼成一条人声轨
  2. BGM 按人声包络自动闪避（有人声时降 8dB），避免盖住说话
  3. 人声轨 + 闪避后的 BGM 合成最终音轨
"""
import json
import os
import subprocess
import wave

import numpy as np
from PIL import Image, ImageDraw

import scenes
from scenes import ACCENT, FPS, H, SCENES, W

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, 'out')
os.makedirs(OUT_DIR, exist_ok=True)

SR = 44100
VOICE_GAIN = 1.0
DUCK_DB = -8.0          # 有人声时 BGM 降多少
TOTAL = 52.0


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# ------------------------------------------------------------------ 音频
def read_wav(path):
    with wave.open(path) as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    if rate != SR:
        # 线性重采样
        n = int(len(data) * SR / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n),
                         np.arange(len(data)), data).astype(np.float32)
    return data.astype(np.float32) / 32768.0


def write_wav(path, data):
    peak = float(np.max(np.abs(data))) or 1.0
    if peak > 0.99:
        data = data / peak * 0.99
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((data * 32767).astype(np.int16).tobytes())


def build_voice_track(manifest):
    """把各句旁白拼成一条与场景对齐的人声轨，同时给出有人声的时间区间。"""
    total = int(SR * TOTAL)
    track = np.zeros(total, dtype=np.float32)
    spans = []
    scene_start = {sid: st for sid, st, _e, _fn in SCENES}
    for item in manifest:
        start = scene_start.get(item['id'])
        if start is None:
            continue
        audio = read_wav(item['audio'])
        # 每句前面留 0.35 秒呼吸，后面留 0.25 秒
        offset = int((start + 0.35) * SR)
        end = min(total, offset + len(audio))
        track[offset:end] += audio[:end - offset] * VOICE_GAIN
        spans.append((offset / SR, end / SR))
    return track, spans


def duck_bgm(bgm, spans, duck_db=DUCK_DB):
    """有人声时把 BGM 压下去，用平滑包络避免突然的抽动。"""
    gain = np.ones(len(bgm), dtype=np.float32)
    factor = 10 ** (duck_db / 20.0)
    for start, end in spans:
        i0 = max(0, int((start - 0.25) * SR))
        i1 = min(len(gain), int((end + 0.35) * SR))
        if i1 <= i0:
            continue
        gain[i0:i1] *= factor
    # 平滑：两次滑动平均，消除硬边
    k = int(0.12 * SR)
    kernel = np.ones(k, dtype=np.float32) / k
    gain = np.convolve(gain, kernel, mode='same')
    # 卷积后首尾会有衰减，补回来
    gain = np.clip(gain / max(1e-6, 1 - (1 - factor) * 0.5), None, 1.0)
    return bgm * gain


def mix_audio(manifest):
    voice, spans = build_voice_track(manifest)
    bgm = read_wav(os.path.join(HERE, 'audio', 'bgm.wav'))
    if len(bgm) < len(voice):
        bgm = np.pad(bgm, (0, len(voice) - len(bgm)))
    bgm = bgm[:len(voice)]
    ducked = duck_bgm(bgm, spans)

    # 人声做一点轻压缩，音量统一
    peak = float(np.max(np.abs(voice))) or 1.0
    if peak > 0:
        voice = voice / peak * 0.86

    final = voice + ducked * 0.85
    path = os.path.join(OUT_DIR, 'audio_final.wav')
    write_wav(path, final)
    rms = float(np.sqrt((final ** 2).mean()))
    print(f'混音（未归一）：{path}  峰值 {np.max(np.abs(final)):.3f}  RMS {rms:.4f} '
          f'({20 * np.log10(rms):.1f} dBFS)')
    return path


def normalize_audio(path):
    """把整体响度拉到 B 站舒适区（-16 LUFS），真峰值留在 -1.5 dB。"""
    ff = ffmpeg()
    out = os.path.join(OUT_DIR, 'audio_norm.wav')
    cmd = [ff, '-y', '-i', path,
           '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100',
           '-ac', '2', '-ar', '44100', out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode('utf-8', 'replace')[-1500:])
    print('响度归一完成（-16 LUFS，双声道）')
    return out


# ------------------------------------------------------------------ 画面
def render_frames():
    """逐帧画，输出成 ffmpeg 能直接吃的裸视频流。"""
    ff = ffmpeg()
    cmd = [ff, '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
           '-s', f'{W}x{H}', '-r', str(FPS), '-i', '-',
           '-an', '-c:v', 'libx264', '-preset', 'medium', '-crf', '18',
           '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
           os.path.join(OUT_DIR, 'video_silent.mp4')]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    base_cache = {}
    total_frames = int(TOTAL * FPS)
    for i in range(total_frames):
        t = i / FPS
        sid = None
        local = 0.0
        for s, st, en, fn in SCENES:
            if st <= t < en:
                sid, local = s, t - st
                break
        if sid is None:
            sid, local = SCENES[-1][0], t - SCENES[-1][1]

        if sid not in base_cache:
            base = Image.new('RGB', (W, H), scenes.BG_TOP)
            d = ImageDraw.Draw(base)
            for y in range(H):
                tt = y / H
                d.line((0, y, W, y),
                       fill=tuple(round(scenes.BG_TOP[i] +
                                        (scenes.BG_BOTTOM[i] - scenes.BG_TOP[i]) * tt)
                                  for i in range(3)))
            base = Image.blend(base, scenes.glow_for(ACCENT[sid]), 0.85)
            base_cache[sid] = base
        frame = base_cache[sid].copy()

        fn = dict((s, f) for s, _a, _b, f in SCENES)[sid]
        frame = fn(frame, local)
        proc.stdin.write(frame.tobytes())

        if i % 150 == 0:
            print(f'  渲染 {i}/{total_frames} 帧', flush=True)

    proc.stdin.close()
    err = proc.stderr.read().decode('utf-8', 'replace')
    if proc.wait() != 0:
        raise RuntimeError('ffmpeg 出错：\n' + err[-2000:])
    print('无声视频已生成')
    return os.path.join(OUT_DIR, 'video_silent.mp4')


def mux(video_path, audio_path, out_name='AP01_宣传视频_1080p.mp4'):
    ff = ffmpeg()
    out = os.path.join(OUT_DIR, out_name)
    cmd = [ff, '-y', '-i', video_path, '-i', audio_path,
           '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
           '-movflags', '+faststart', '-shortest', out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode('utf-8', 'replace')[-2000:])
    print('成片已生成：', out)
    return out


if __name__ == '__main__':
    with open(os.path.join(HERE, 'voice.json'), encoding='utf-8') as fp:
        manifest = json.load(fp)
    audio = normalize_audio(mix_audio(manifest))
    video = render_frames()
    final = mux(video, audio)
    size = os.path.getsize(final) / 1024 / 1024
    print(f'\n完成：{final}  {size:.1f} MB  {TOTAL:.1f}s')
