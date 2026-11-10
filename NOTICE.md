# 上游项目、引用与固件归属

本仓库的代码是我们自己写的，但路线、协议和工具链建立在上游公开成果之上。
下面把**最该感谢的起点**放在第一位，其余按出场时间列出，不遗漏。

## 一、一切的起点：slepher

作者：**slepher**（<https://github.com/slepher>）

- 主仓库：<https://github.com/slepher/cuktech-ap01-firmware>
- 🔴 最关键的工作日志：
  <https://github.com/slepher/cuktech-ap01-firmware/blob/master/project-workflow/ap01-app-update/status.md>
- 应用仓库：<https://github.com/slepher/codex_status>

这份 `status.md` 是倒序流水账，最新状态写在文件最开头：10 月 18 日首次实机安装、
10 月 19 日真机刷入 0041 自定义 ROM 并于深夜用
`miIO.recovery({"module":"app"})` 降回 0031、11 月 5 日双槽部署、
11 月 6 日仍在修 UI 与启动延迟，全过程连失败也照实记录。

**本仓库最关键的 0041 → 0031 降级路线，就是逐条分析这份日志后研判出来的**：
原厂恢复通道不查小米 PKI 签名、写命令只发一次、设备空闲后补一次 recovery、
验收只看版本与 `life` 而不看 RPC 回执。结论后来由我们在自己的真机上验证，
但路是他先走出来的。所有结论不代表上游作者背书。

## 二、最早开路的两位

### wqytommy666 / cuktech-screen-controller（2026-07-16）

作者：**wqytommy666**

链接：<https://github.com/wqytommy666/cuktech-screen-controller>

自定义图片与 GIF、Claude/Codex 实时额度、Wi-Fi 局域网刷新、RAM 更新。
「把 AP01 当账户工作台」这个用法，以及小米授权、加密请求、固件上传、
下载核对和安装命令的调用顺序，都参考并改编自这个项目。
它的兼容文档也是最早写清「只支持 0031、0041 直接拦」的第一手依据。

许可：MIT License，Copyright (c) 2026 Wqytommy，完整文本见
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。

### XiaoTianWork / cuktech-ap01-screen-kit（2026-07-17）

作者：**XiaoTianWork**

链接：<https://github.com/XiaoTianWork/cuktech-ap01-screen-kit>

RAM 加载器方案（固件槽 221,445 字节 / 加载器 256 KiB），
最早把「不重刷整机、只换显示内容」这条路讲清楚。
本项目前一版致谢漏了这一位，特此补上。

## 三、刷机工具链

### andy-JustSayWhen / cuktech-ap01-firmware-tools（2026-08-20）

作者：**andy**（GitHub 账号 **andy-JustSayWhen**）

链接：<https://github.com/andy-JustSayWhen/cuktech-ap01-firmware-tools>

本仓库的刷机流程、固件制作框架、`mi-cloud.env` 凭据读取方式，以及
`features/official_firmware_source`、`tools/ota_https_serve.py` 等基础模块，
都来自这个项目。没有它，降级和显示加载器都无从下手。

许可：MIT License，完整文本见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。

## 四、小米登录流程

### Piotr Machowski / Xiaomi-cloud-tokens-extractor

作者：**Piotr Machowski**

链接：<https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor>

小米账号二维码登录流程改编自该项目。

许可：MIT License，Copyright (c) 2020 Piotr Machowski，完整文本见
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。

## 五、还要感谢

在这些仓库的 issue 里持续反馈、把失败案例也写下来的所有人。
本项目能少走弯路，靠的正是那些「没成功」的记录。

## 六、固件归属

`firmware/` 目录里的 `原厂_0031.bin`、`原厂_0041.bin`、`原厂_0055.bin`
版权归**酷态科（CUKTECH）**所有，本仓库仅作为个人恢复和研究的存档，不主张任何权利。
`降级用_0031_首字节预反转.bin`、`备用_0031_60秒取图.bin`、
`备用_18766_60秒取图.bin`、`加载器_0031_18765_硬件监测.bin`、
`加载器_0031_18766_账户余额.bin` 是在原厂固件基础上做单字节或 URL 级修改的衍生文件。

> 原厂固件里带有厂商构建时留下的字符串（例如一个 `@xiaomi.com` 邮箱），
> 那是固件自身内容，不是本仓库作者或使用者的个人信息。

如果你是该设备的权利人，认为这些文件不应公开，请联系仓库作者删除。

## 七、免责声明

本项目与酷态科（CUKTECH）、小米、OpenAI、DeepSeek、WorkBuddy 均无隶属关系。
刷机可能变砖、失去保修、触发账号风控。所有操作由使用者自行承担风险。
