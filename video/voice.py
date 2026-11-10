"""生成中文旁白，并把每句的时长测出来，用于排时间轴。

用系统自带的 Microsoft Huihui（简体中文）合成，不联网、不调外部服务。
"""
import json
import os
import wave

import win32com.client

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'audio')
os.makedirs(OUT, exist_ok=True)

# 每一段旁白：id、场景、文本
LINES = [
    ('s1', '开场', '酷态科十号、十五号电能基站那块屏，能显示自己的内容了。'),
    ('s2', '版本', '先看版本号。零零三一直接能用。零零四一能降到零零三一，真机做过两次。零零五五是升级，降不回来，别升。'),
    ('s3', '硬件', '第一种，显示电脑状态：CPU 和显卡的功率、温度、占用、风扇，内存显存，游戏帧率，四个充电口的功率。'),
    ('s4', '账户', '第二种，显示账号：Codex 剩余额度和重置时间，两个 WorkBuddy 账号的积分，DeepSeek 余额，都在一块屏上。'),
    ('s5', '不可用', '数据读不到就显示不可用，不写假的零。'),
    ('s6', '结尾', '方法固件脚本全部开源，GitHub 搜 cuktech-ap01-workbench。刷机有风险，先确认版本。'),
]

RATE = 3   # SAPI 语速档：-10..10


def speak(text, path, rate=RATE):
    voice = win32com.client.Dispatch('SAPI.SpVoice')
    voice.Voice = voice.GetVoices().Item(0)   # Microsoft Huihui Desktop
    voice.Rate = rate
    voice.Volume = 100
    stream = win32com.client.Dispatch('SAPI.SpFileStream')
    stream.Open(path, 3, False)
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()
    with wave.open(path) as w:
        frames = w.getnframes()
        rate_hz = w.getframerate()
    return frames / rate_hz


def main():
    manifest = []
    for key, scene, text in LINES:
        path = os.path.join(OUT, f'{key}.wav')
        dur = speak(text, path)
        manifest.append({'id': key, 'scene': scene, 'text': text,
                         'audio': path, 'duration': round(dur, 3),
                         'chars': len(text)})
        print(f'{key:3} {scene:5} {dur:5.2f}s  {len(text):3}字  {text[:22]}…')
    total = sum(m['duration'] for m in manifest)
    print(f'\n旁白总时长 {total:.2f}s（还不算停顿）')
    with open(os.path.join(HERE, 'voice.json'), 'w', encoding='utf-8') as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
