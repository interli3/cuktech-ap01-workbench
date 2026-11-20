# 14 · Grok 周额度卡片（2026-11-20）

账户工作台（端口 18766，`tools/display_service.py`）在左下角加了第五张卡，
显示 Cursor 订阅里 Grok / Cursor 共用的**本周剩余百分比**。
硬件监测模式（18765）和 `firmware/` 都没有改。

![Grok 卡，示例数据](../images/mode-b-grok.png)

图里的账号名和数字是示例。真机上的中文是微软雅黑，数字是 Bahnschrift。

## 1. 屏幕上长什么样

打开 `sources.grok_enabled`（默认就是开的）之后：

| 位置 | 内容 |
|---|---|
| 左上 `(8,91)-(162,153)` | Codex。5 小时剩余和本周剩余并排，下面一行重置 |
| 左下 `(8,158)-(162,219)` | GROK。石墨到近黑的底，不用绿也不用紫 |
| 右上 | WorkBuddy 两个账号，卡片略矮 |
| 右下 | DeepSeek，一行大号金额 |

GROK 卡上三行：

- 大号白字，本周剩余百分比，例如 `73%`；
- 「本周剩余」。缓存超过大约 30 分钟时，这行改成「6时前更新」「1天前更新」这种说明，**数字仍是上一次查到的**；
- 琥珀色一整行重置倒计时，例如 `6天20时后 · 周五05:44 重置`。

跑马灯仍是每张卡一条，相位按卡片数量等分（五张卡就是 1/5 圈）。

把 `sources.grok_enabled` 设为 `false` 再重启服务：不画这张卡，也不访问 Cursor，
Codex 回到左侧整块，就是 11-10 定稿的四卡布局。

没授权、也没有缓存时，卡上是「未授权」和「—」，下面写「请先完成授权」。
进程不退出，其它卡照常更新。

## 2. 数字从哪来

显示服务自己查，不唤醒 Grok Bot，不消耗 bot 额度。

```http
POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus
Authorization: Bearer <access token>
connect-protocol-version: 1
Content-Type: application/json

{}
```

用到的字段：

| 字段 | 用法 |
|---|---|
| `usagePercent` | 本周已用百分比。剩余 = `100 - usagePercent`。超出 0～100 就当这次失败 |
| `nextResetTimestampUtc` | 下次重置的 UTC 时间，必须带时区 |
| `prepaidAvailableCents` | 预付余额，只写进缓存，卡上不画金额 |

启动后立刻查一次，之后**最多 5 分钟打一次接口**。
60 秒一轮的账户采集在这之间只读本地缓存，不会每分钟都打 Cursor。

这是一次**独立登录**。凭据只放在配置项 `sources.grok_auth_store`
（默认 `run/grok_cursor_auth.bin`）。
脚本拒绝文件名里带 `sand-secrets` 的路径，不会去读 Grok Bot 桌面端的 `sand-secrets.json`。

不要改用 `https://cursor.com/api/usage-summary`。
那个接口在周额度套餐上给出的数字是错的，和这张卡要对的不是同一个数。
`tools/grok_cursor_auth.py --check` 会额外打一次 `GetCurrentPeriodUsage`，
只打印出来做交叉核对，屏幕不采用它。

## 3. 一次性授权

在**运行显示服务的那个 Windows 用户**下，仓库根目录执行：

```bash
python tools/grok_cursor_auth.py --start
```

终端会打印一条 `https://cursor.com/loginDeepControl?...` 链接。
用浏览器打开，登录你想显示在屏幕上的那个 Cursor 账号，点一次授权。
后台最多等 20 分钟。成功之后：

- access token 和 refresh token 用 Windows DPAPI（当前用户）加密写到
  `run/grok_cursor_auth.bin`；
- `run/grok_auth_status.json` 里只有 `authorized` 和时间，没有 token；
- 登录链接文件会删掉。

然后再确认用量（输出已打码，不会打印 token）：

```bash
python tools/grok_cursor_auth.py --check
```

重启 `tools/display_service.py`，或等下一轮采集。卡片上应出现百分比。

换 Windows 用户、重装系统之后 DPAPI 解不开，要重新 `--start`。
不要把 `grok_cursor_auth.bin` 拷给别人，那就是这个账号的登录会话。

## 4. 刷新，以及「一直显示最后一次」

每次接口成功，都会原子写入 `run/grok_usage.json`。之后按下面的规则画，**不会编一个 0**：

| 情况 | 卡片 |
|---|---|
| 刚查到，或缓存还新 | 百分比 +「本周剩余」+ 琥珀色倒计时 |
| 接口失败，但缓存还在，而且重置时间没过 | 继续显示缓存里的百分比。超过约 30 分钟，说明改成「x时前更新」 |
| 401 / 403 | 状态文件标成需要重新授权；有缓存就继续显示缓存 |
| access token 剩余不足 7 天 | 用 refresh token POST `https://api2.cursor.sh/auth/exchange_user_api_key`，body 仍是 `{}`。响应里没有新的 refresh token 就保留旧的，再拿新的 access token 去查 |
| 完全没有缓存，而且没有凭据 | 「未授权 / —」 |
| 有凭据，但一次都没查成功，缓存也没有 | 「—」和「额度数据暂不可用」 |
| 缓存里的重置时间已经过了 | 「—」和「已过重置时间 · 待刷新」。那个百分比属于上一周，不能再当本周剩余 |

接口返回的 `usagePercent` 真是 0 时，卡片会显示 `0%`。
这是查到的数，不是失败时填上去的。

## 5. GIF 体积

`/live.bin` 单帧上限 24572 字节，超了的帧会被直接丢掉。
五张卡再加多口充电时，56 色 Median Cut 可能超过这个上限。

`publish()` 按 `56 → 44 → 36 → 28` 降档，直到放进 24572。
它记住上次用的档位，下一次只往上试一档：画面变简单时会回到 56 色，
不必每秒把四档都压一遍。四档都放不下会记一条 `gif_over_budget`，
随后校验抛错，超限的临时文件不会替换正在发送的那张 GIF。
正常画面在 28 色这一档放得下，这是兜底。

离线复测时这台环境没有微软雅黑和 Bahnschrift，中文用的是文泉驿，
体积和真机会有出入，但降档行为是同一段代码：

| 画面 | 结果 |
|---|---|
| 未充电 + 五卡示例（仓库里的预览图） | 56 色，24049 字节 |
| 多口充电压力，直接压 56 / 44 / 36 / 28 色 | 26177 / 25686 / 24564 / 21563 字节。前两档超限，后两档放得下 |
| 同一压力画面交给 `publish()` | 自动降到 36 色，写出 23987 字节 |
| 关掉 Grok 的四卡 | 56 色，22324 字节 |

跑马灯相位不同，字节数会有几百的出入。上表是同一次离线运行里记下来的。

## 6. 风险

- `GetSandUsageStatus` 不是 Cursor 文档里的稳定公开接口。路径、字段、登录流程都可能改，改了卡片就会停在缓存上，直到缓存过期或你改配置里的 URL。
- 配置项 `sources.grok_usage_url` 有默认值。可以改主机或路径，但不要换成 `cursor.com/api/usage-summary`。
- token 等同这个 Cursor 账号的会话。`.gitignore` 挡住了 `grok_cursor_auth.bin`、`grok_usage.json`、`grok_auth_status.json` 和 `grok_auth_link.txt`。默认目录 `run/` 本来也不会提交。
- 登录链接里带有一次性的 challenge。授权完成前不要把链接或 `grok_auth_link.txt` 发给别人。
- 这一路和 Codex 的 `codex.exe app-server`、和 Grok Bot 桌面端的凭据都是分开的。关掉卡片不会影响另外两路。
