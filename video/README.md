# video/ · 宣传视频生成工程

这一段代码用来生成 B 站宣传片 `AP01_宣传视频_1080p.mp4`（52 秒，1080p，中文旁白 + 配乐）。

## 为什么不走 AI 生成视频

两个原因：

1. **中文和数值不可控。** AI 出的中文字幕常带错字，屏幕上的数字更是随机的。
   这块屏显示的就是功率、余额、额度，数字错了等于毁掉可信度。
2. **那块屏只有 320×240。** 任何"把小截图放大"的思路都会糊。
   所以这里的做法是：照着 `tools/display_service.py` 和
   `tools/hardware_monitor/dashboard.py` 的绘制逻辑，**重新按 4 倍分辨率画一遍**
   （`hires.py`，1280×960），文字是矢量级清晰的。

## 文件

| 文件 | 作用 |
|---|---|
| `hires.py` | 高清面板重绘：硬件面板、账户面板、不可用状态，外加设备外壳 |
| `scenes.py` | 六个场景的排版与动画（标题、版本卡、两种模式、不可用、结尾） |
| `voice.py` | 用系统自带的中文语音（Microsoft Huihui）合成旁白，并测出每句时长 |
| `bgm.py` | 纯代码合成的配乐：低频 pad + 琶音 + 鼓点，不涉及第三方版权素材 |
| `render.py` | 主程序：混音（人声自动闪避 BGM）→ 逐帧渲染 → 合成 MP4 |

## 跑起来

```bash
# 依赖
python -m pip install pillow numpy imageio-ffmpeg qrcode pywin32

# 1. 生成高清面板素材（可选，脚本会自动调用）
python hires.py preview

# 2. 合成旁白，产出 voice.json（里面记着每句的时长）
python voice.py

# 3. 生成配乐
python bgm.py

# 4. 混音 + 渲染 + 合成（1560 帧，约 1 分钟）
python render.py
```

成片在 `out/AP01_宣传视频_1080p.mp4`。

## 音频是怎么处理的

- 旁白按场景起点对齐，每句前面留 0.35 秒呼吸。
- BGM 在**有人声的那几秒自动压低 8 dB**，说完立刻回来，
  包络做了平滑，不会出现"抽动"。
- 人声段比音乐段高约 23 dB，音乐始终垫在底下。
- 最后用 `loudnorm` 把整体响度拉到 **-16 LUFS**、真峰值 -2 dBFS，
  这是网络视频的舒适区，手机外放也不刺耳。

## 音频工具说明

- 旁白用的是 Windows 自带的 SAPI 语音，需要系统装有中文语音包
  （`Microsoft Huihui` / `Kangkang` / `Yaoyao` 任意一个）。
  macOS / Linux 上把 `voice.py` 换成任意 TTS 即可，其余代码不用动。
- 编码用 `imageio-ffmpeg` 自带的 ffmpeg，不需要另外装。

## 画面参数

- 1920×1080，30 fps，52 秒，H.264 High profile，`yuv420p`，`+faststart`。
- 音频 AAC 192 kbps，44.1 kHz，立体声。
- 结尾带二维码，直接扫到 GitHub 仓库。
