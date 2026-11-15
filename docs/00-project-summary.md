# 项目总结：把 U30 Air 做成 SIP ↔ VoLTE 语音网关

> 一张 SIM 卡、一台两百来块的随身 Wi-Fi、一部手机，做出一个能呼出、能接听、
> 锁屏甚至 App 被杀掉也照样响的家用座机。
>
> 本文是完整复盘：架构、实施步骤、**每一处踩过的坑**、参数速查和排查手册。
> 重装或换设备时照着走即可，不必再从头试错。

---

## 0. 最终结果

| 能力 | 状态 | 说明 |
|---|---|---|
| SIP 服务器 | ✅ | Asterisk 23.1.0，`5060` 上 UDP + TCP 双传输 |
| 分机注册 / 互拨 / Echo | ✅ | 6001、6002、`600`、`*43` |
| 呼出（分机 → 手机号） | ✅ | 双向实时语音，单向延迟 200~300 ms |
| 呼入（外部拨打 SIM 号 → 分机响） | ✅ | 接听、双向语音、双向挂断同步 |
| 未接超时 | ✅ | 25 秒自动挂断，不占着对方的线路 |
| iOS 锁屏 / App 被杀后仍能收来电 | ✅ | 靠推送中继唤醒，实测放置 7.5 小时仍有效 |
| Android 端 | ⬜ | 唯一未验证项 |

---

## 1. 项目背景

设备原本是一台**中兴 U30 Air 随身 Wi-Fi**：插一张中国移动 SIM 卡，把 5G 转成 WiFi。
它的硬件其实是一部完整的 Android 手机（Spreadtrum UMS9620 + 基带 + 音频编解码），
只是被厂商锁在"热点"这一个用途上。

目标：**把它变成一个能打电话的 SIP 网关**。SIM 卡留在设备里，
手机上的 SIP 软电话拨号就走这张卡呼出；别人打这个号，也能转到 SIP 分机上接听。

难点不在 SIP，而在三处：

1. **音频通路**——VoLTE 的通话音频走的是基带内部的 DSP 通道，普通 ALSA 设备访问不到；
2. **来电事件**——AT 通道是单读者模型，不能有第二个进程去阻塞读串口；
3. **iOS 后台**——系统会在 1~2 小时内冻结后台 VoIP App，导致收不到来电。

这三个问题分别对应本文的第 5 节里三组坑。

---

## 2. 硬件与系统底座

### 2.1 设备

| 项 | 值 |
|---|---|
| 机型 | 中兴 U30 Air（随身 Wi-Fi） |
| SoC | Spreadtrum UMS9620 |
| 系统 | **OpenWrt 25.12.6**，内核 7.2.8 mainline |
| 包管理 | **apk**（OpenWrt 25.x），不是 opkg |
| LuCI | `http://192.168.77.1` |
| WiFi | SSID `ZTE_107525`（设备自己做 AP） |
| 蜂窝 | 中国移动 46000 / 5G，`sipa_eth0` 拿到的地址**每次拨号都会变**，不能写死 |

> 这台设备默认没有 `scp`、`nohup`、`timeout`、`sftp-server`。
> 上传文件用 `ssh 'cat > 目标文件' < 本地文件`，后台进程用 `start-stop-daemon -S -b`。

### 2.2 音频栈（最关键的一块）

原厂固件里，VoLTE 的语音通路由一套叫 **AUDCP** 的 DSP 固件驱动，OpenWrt 侧没有对应驱动。
必须补齐：

- `agdsp.bin`（AUDCP 固件，锁定 5.4.254 内核版本）
- airsim 音频栈 APK
- 厂商的音频参数 `audio_params/`

这些都打包在 [`firmware/`](../firmware/) 里，附安装脚本。

装好之后会多出声卡：

| 用途 | 设备节点 | 格式 |
|---|---|---|
| **上行注入**（把我们的话送到对方） | `hw:1,53`（`FE_ST_VOICE_PCM_P`） | 16 kHz / 16 bit / 单声道 |
| **下行采集**（把对方的话取出来） | `hw:1,16`（`FE_ST_DUMP`） | 48 kHz / 16 bit / 单声道 |

声卡名：`sprdphone-sc2730`，卡片号 **1**。

真正让 DSP 起来的是这条命令（不是简单的 aplay）：

```sh
mu300-voice audio start dsp      # 启动通话场景
mu300-voice audio stop           # 收场景
```

**这两条命令的调用顺序，是整个项目里最容易踩坑的地方之一。**

---

## 3. 整体架构

### 3.1 呼出

```
SIP 分机拨 138xxxxxxxx
      │  INVITE
      ▼
  Asterisk ── 拨号计划 _1XXXXXXXXXX ──> 写 /tmp/bridge-target
      │                                      │
      │  AudioSocket (127.0.0.1:9092)        │
      ▼                                      ▼
 mu300-bridge.py ──ATD──> 模组 ──VoLTE──> 对方
      │
      │ 上行：SIP 音频 → hw:1,53 (16k)
      │ 下行：hw:1,16 (48k) → 抽取 6:1 → SIP 音频
```

### 3.2 呼入

```
外部手机拨 SIM 号
      │  VoLTE
      ▼
    模组 ──URC──> /run/mu300-at/urc/stty_nr0.log
                        │  "+CRING: VOICE" / '+CLIP: "号码"'
                        ▼
                mu300-volte-in.py
                  │  ① POST push.yakteam.com/v1/notify   ← 唤醒手机
                  │  ② 轮询注册状态（≤8 s，注册即走）
                  │  ③ AMI Originate → PJSIP/6001，接听后执行 AudioSocket
                  ▼
              Asterisk ──INVITE──> SIP 分机响铃
                  │
                  │  AudioSocket (9092)
                  ▼
            mu300-bridge.py
                  │  ④ AT+CLCC 看到 state=4（振铃中）
                  │  ⑤ 下发 ATA 接听 VoLTE
                  │  ⑥ 等 state=0，启动 DSP 场景，开始双向音频
                  ▼
              对方听到接通
```

**顺序上有一处反直觉但必须遵守**：呼入时必须**等分机接听之后**才下发 `ATA`。
反过来做的话，对方那边立即接通并开始计费，而这边的分机还在响。

---

## 4. 实施路线

按这个顺序做，每一步都有明确的验收标准。

### 4.1 固件与音频栈

1. 刷 OpenWrt 25.12.6（保留原厂分区，不动 `nr_fixnv*` / `persist`）
2. 解包 `firmware/mu300-audio-kit-r23.tar.gz`，按 `airsim-install-r23.sh` 安装
3. **验收**：`arecord -l` 能看到 `sprdphone-sc2730`；`mu300-voice audio start dsp` 返回 `VOICE-RUNNING`

> 装完音频栈后，原有的 `mu300-sms` 等组件可能被覆盖。若要用短信功能，
> 需要补 `sms-pool.sh`、`sim-scope.sh` 并重建 `mu300-sms`（本项目未纳入仓库）。

### 4.2 SIP 服务器

```sh
apk add asterisk asterisk-pjsip asterisk-res-rtp-asterisk \
        asterisk-app-audiosocket asterisk-app-dial asterisk-chan-pjsip
```

`/etc/asterisk/pjsip.conf` 两个关键点：

```ini
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
local_net=192.168.77.0/24

[transport-tcp]          ; iOS 软电话建议走 TCP
type=transport
protocol=tcp
bind=0.0.0.0:5060
local_net=192.168.77.0/24
```

分机必须是 `ulaw`/`alaw`，**不要开 Opus**（客户端优先 Opus 会导致协商失败或单通）：

```ini
disallow=all
allow=ulaw
allow=alaw
```

**验收**：软电话注册成功，`pjsip show contacts` 能看到 Contact。

### 4.3 呼出

1. 部署 `tools/mu300-bridge.py` 与 `etc/init.d/mu300-bridge`
2. 拨号计划里手机号落到 AudioSocket：

```asterisk
exten => _1XXXXXXXXXX,1,Answer()
 same => n,System(echo ${EXTEN} > /tmp/bridge-target)
 same => n,AudioSocket(<uuid>,127.0.0.1:9092)
 same => n,Hangup()
```

3. **验收**：能听到双向声音，且延迟稳定。**延迟异常先看 `docs/02`**，那是逐层排查记录。

### 4.4 呼入

1. 开 `AT+CLIP=1`（来电显示，否则振铃不带号码）
2. **打开 AMI**（见下方坑 5.3）：
   ```ini
   [general]
   enabled = yes
   port = 5038
   bindaddr = 127.0.0.1
   ```
3. 部署 `tools/mu300-volte-in.py` 与 `etc/init.d/mu300-volte-in`
4. 拨号计划加 `[volte-in]`
5. **验收**：外部拨入 → 分机响 → 接听 → 双向语音 → 任一方挂断，另一侧 1~2 秒内结束

### 4.5 iOS 推送

1. 装 **Yak Phone**（Pro），注册分机，从「设置 → 消息推送」复制 **VoIP Push Token**
2. 写到设备：

```sh
mkdir -p /etc/mu300
echo '<64 位 token>' > /etc/mu300/yak-push.token
chmod 600 /etc/mu300/yak-push.token
```

3. **验收**：把 App 从多任务里划掉，等 30 秒，再拨——能响就说明推送链路通了

细节见 [`04-ios-push.md`](04-ios-push.md)。

---

## 5. 踩坑总表

按「现象」索引。遇到问题时先在这里找，比翻日志快。

### 5.1 音频

| 现象 | 原因 | 解法 |
|---|---|---|
| 延迟从 3 s 一路涨到 10 s | DSP 注入（`hw:1,53`）的有效吞吐略低于 32 kB/s，8k→16k 无余量送入会累积 | 有界播放队列，**超限丢最旧帧**，把延迟封顶在 200 ms |
| 通话中途整通断掉，日志 `BrokenPipeError` | `mu300-voice` 起场景耗时波动（0.3~1.3 s），aplay 抢跑遇 underrun，`--fatal-errors` 让它直接退出 | 起场景后多等一会儿（呼入 1.8 s）；会话开始先清理残留 aplay/arecord |
| 接通但**完全没声音**，上下行 0 字节 | 上一次异常中断留下的 `aplay`/`arecord` 一直占着 `pcmC1D5p`，报 `Resource busy` | 会话开始前 `pidof aplay arecord` 有残留就 kill，并 `mu300-voice audio stop` |
| 下一次通话必定失败、DSP 唤不醒 | 挂断顺序错了 | **先 `AT+CHUP` → 再 `audio stop` → 最后关 PCM**，绝不能反过来 |
| 接通前对方就挂了 | 建链要 5~6 s，期间没有 RTP，对端约 15 s 判"无媒体" | 一连上就按 20 ms 节拍持续送**静音帧**，让对端等得住 |

### 5.2 Asterisk / SIP

| 现象 | 原因 | 解法 |
|---|---|---|
| INVITE 返回 **488** | 缺 `asterisk-res-rtp-asterisk` | 装上该包 |
| 后续 BYE 返回 **481** | Asterisk 的 `To` tag 是带 `-` 的 UUID，截断即 ACK 失配 | 不要截断/改写 To tag |
| 改 transport 不生效 | 该构建**没有 `pjsip reload`** 命令 | 必须重启 Asterisk |
| `channel originate` 报 "No such command" | 精简构建，模块目录只有 19 个 `.so`，没有 `res_clioriginate` | 改用 **AMI**（核心内建，默认关闭） |

### 5.3 呼入

| 现象 | 原因 | 解法 |
|---|---|---|
| 手机完全不响，日志说 originate 不存在 | 用了 CLI 的 `channel originate` | 开 AMI，用 `Action: Originate` |
| 主叫号码显示 `unknown` | 收到 `+CRING` 后在读循环里 `sleep` 等号码，把后面的 `+CLIP` 堵住了 | 改成 `threading.Timer` 异步触发，读循环不阻塞 |
| 打几通只有第一通能接 | 挂断通知是 **`^CEND`**（不是 `NO CARRIER`），没识别导致 `busy` 永不复位 | 识别 `^CEND`，并加 40 秒兜底强制复位 |
| 同一通来电被叫了两次 | `watchdog` 判据太激进，CLCC 偶发超时就复位 | 改为连续 12 秒无通话才复位；振铃时先现场查 CLCC |
| 我挂了，对面半天才断 | 结束流程写成 `if clcc_state() is not None` 才发 CHUP，查询超时就根本不挂断 | **无条件** `AT+CHUP` |
| 对方挂了，分机还在响 | 只清了本地状态，没终止 Asterisk 上仍停在振铃的 `PJSIP/6001-*` 通道 | 收到挂断通知后 `channel request hangup` 逐个挂掉 |
| 对方挂了分机继续响，**接着下一通直接打不进来** | 模组有时不发 `^CEND`，VoLTE 侧留成**假振铃**（`AT+CLCC` 一直返回 state=4），这通"幽灵来电"占着线路，新来电被拒 | ①振铃期间每 1.5 s 查 CLCC，来电消失就挂断分机；②25 s 兜底改成「**非已接通**就 CHUP + 挂通道」，不能再按 `state==4` 判断（查询返回空会整段跳过） |
| 某一通完全没声音 | 同一时刻既在呼出又在呼入，两路抢同一条 DSP 通路 | 呼出前查 `AT+CLCC`，发现 state=4 就拒绝呼出 |
| AMI 连不上，报 `LookupError: unknown encoding: idna` | 设备上的 Python 缺 `idna` 编码器，`socket.create_connection` 走域名解析直接崩 | 用 `socket(AF_INET)+connect((ip, port))` 直连，或用 `curl` |

### 5.4 iOS 终端

| 现象 | 原因 | 解法 |
|---|---|---|
| 锁屏 1~2 小时后收不到，**连通话记录都没有** | iOS 冻结了后台 App；socket 还在，App 不处理 INVITE | 必须有推送，见下一行 |
| 怎么才有推送 | Asterisk 无 RFC 8599；PortSIP 的推送绑死 PortSIP PBX | 用 Yak Phone（Pro）的中继接口，由设备在振铃时主动 POST |
| 「显示在线」但打不通 | TCP 连接由系统维持，端口几小时不变，但 App 已被挂起 | **不能用注册状态判断能否接电话**，只能靠实际拨测 |
| AMI/推送都发了手机还是不响 | ①顺序反了（先叫后推）；②App 被杀，1.6 s 内还没注册完 | **先推 → 轮询 `pjsip show contacts` 直到注册上（≤8 s）→ 再 originate** |

---

## 6. 参数速查

### 6.1 网络与账号

| 项 | 值 |
|---|---|
| 设备地址 | `192.168.77.1` |
| LuCI | `root` / `u30air2026` |
| SSH | `root@192.168.77.1`（公钥免密） |
| WiFi | `ZTE_107525` / `1234567890` |
| 分机 6001 | 密码 `u30air-6001` |
| 分机 6002 | 密码 `u30air-6002` |
| SIP 端口 | `5060`（UDP + TCP） |
| AMI | `127.0.0.1:5038`，`mu300` / `mu300ami2026` |

### 6.2 关键文件

| 路径 | 作用 |
|---|---|
| `/usr/bin/mu300-bridge.py` | 音频桥（呼出/呼入共用） |
| `/usr/bin/mu300-volte-in.py` | 来电守护：URC → 推送 → 呼叫分机 |
| `/etc/init.d/mu300-bridge` | 桥接开机自启 |
| `/etc/init.d/mu300-volte-in` | 来电守护开机自启 |
| `/etc/asterisk/pjsip.conf` | 分机与传输层 |
| `/etc/asterisk/extensions.conf` | 拨号计划（含 `[volte-in]`） |
| `/etc/asterisk/manager.conf` | AMI |
| `/etc/mu300/yak-push.token` | iOS 推送 token |
| `/run/mu300-at/urc/stty_nr0.log` | 模组 URC 日志（来电事件源） |
| `/tmp/bridge.log` | 桥接日志 |
| `/tmp/volte-in.log` | 来电守护日志 |

### 6.3 音频设备

| 用途 | 节点 | 速率 |
|---|---|---|
| 上行注入 | `hw:1,53` | 16 kHz |
| 下行采集 | `hw:1,16` | 48 kHz（6:1 抽取到 8k） |

---

## 7. 故障排查手册

先看两条日志，绝大多数问题能直接定位：

```sh
tail -30 /tmp/volte-in.log     # 来电检测、推送、originate
tail -40 /tmp/bridge.log       # 呼叫建立、DSP 场景、音频字节数
```

### 7.1 打进来不响

按顺序查：

1. `tail /tmp/volte-in.log` 有没有 `检测到振铃`
   - **没有** → 来电没到设备，查 SIM 信号、`AT+CLRE`、URC 日志
   - **有** → 下一步
2. 有没有 `Yak 推送(voip) -> ok`
   - **没有或失败** → token 失效 / 设备出不了网，`curl -v https://push.yakteam.com` 试
3. 有没有 `AMI originate -> ok queued`
   - **失败** → AMI 没开，或分机没注册
4. `tail /tmp/bridge.log` 有没有 `呼入来电（振铃中）`
   - **没有** → 分机没接（或 App 没醒）

### 7.2 接通了没声音

看 `bridge.log` 里每 2 秒的统计行：

```
上行 306240 B / 下行 433600 B | 驱动延迟 640帧(40ms) RUNNING | 播放队列 0 丢51
```

- **上下行都是 0** → DSP 没起来，看有没有 `Resource busy`；有残留 aplay 就 kill
- **只有一边增长** → 单通，检查 `hw:1,53` / `hw:1,16` 是否被别的进程占用
- **`驱动延迟` 持续上涨、`播放队列` 接近 10** → 注入吞吐不够，队列在丢帧，检查是不是有别的进程在抢 DSP

### 7.3 挂断不同步

- **我挂了对面不断** → 看有没有 `AT+CHUP -> OK`；没有说明结束流程没走到
- **对面挂了我还响** → 看有没有 `已挂断分机通道: PJSIP/6001-xxx`；没有说明 `^CEND` 没识别到

### 7.4 分机注册失败

```sh
grep -i "authenticate" /var/log/asterisk/messages.log | tail -5
```

- `Failed to authenticate` → 密码错（注意 `u30air-6001` 中间是**短横线**）
- 注册不上但不报错 → 传输协议不匹配（服务端有没有开对应的 TCP/UDP）
- 客户端优先 Opus → 关掉，只留 PCMU/PCMA

---

## 8. 已知限制

1. **单路并发**：同一时刻只能一通电话。呼入呼出也不能并行（抢同一条 DSP 通路）。
2. **iOS 端依赖 Yak Phone（Pro）**：换别的软电话需自行解决推送。
   token 手工维护，重装 App 后要更新。
3. **纯内网**：没做 NAT 穿透/端口映射，手机必须连设备的 WiFi。
4. **25 秒振铃上限**：超时自动挂断。
5. **DSP 场景对顺序敏感**：挂断必须先 `AT+CHUP` 再收场景，反了会掐死 DSP。
6. **未验证 Android**。

---

## 9. 仓库文件说明

```
u30air-volte-sip/
├── README.md                    总览 + 进度表
├── docs/
│   ├── 00-project-summary.md    本文（从零到可用的完整手册）
│   ├── 02-latency-investigation.md  延迟逐层排查记录
│   ├── 03-inbound-call.md       呼入实现与踩坑
│   └── 04-ios-push.md           iOS 推送方案
├── tools/
│   ├── mu300-bridge.py          音频桥
│   ├── mu300-volte-in.py        来电守护（含推送）
│   └── sip_verify.py            SIP 测试工具
├── etc/
│   ├── init.d/                  两个 procd 服务
│   └── asterisk/                pjsip / extensions / manager 示例
└── firmware/                    音频栈固件与安装脚本
```

另有实验分支 [`u30air-volte-multiring`](https://github.com/guaizai2023/u30air-volte-multiring)：
一个来电同时叫多台分机，适合以后加第二台终端。

---

## 10. 时间线

| 时间 | 进展 |
|---|---|
| 11-13 | 打通呼出：双向语音、延迟压到 200~300 ms |
| 11-14 上午 | 呼入链路（URC → AMI → AudioSocket → ATA），修掉五个 bug |
| 11-14 下午 | 摸清 iOS 后台边界（15 min / 52 min 正常，2 h 失效） |
| 11-14 深夜 | 接上 Yak 推送中继，**杀掉 App 也能响** |
| 11-15 上午 | 隔夜 7.5 小时保活验证通过，项目收尾 |

---

## 11. 一些体会

**最难的不是写代码，是知道该怀疑什么。**

- 延迟越涨越高，直觉会去查 ALSA、管道、驱动——查了一圈全是干净的。
  真正的原因是 DSP 注入吞吐比理论值差一点，**必须有界丢弃**而不是硬塞。
- 接通没声音，直觉会去查编码协商——实际是上一次崩溃留下的进程占着设备。
- 打进来不响，直觉会去查 SIP 注册——实际是 iOS 把 App 冻了，连接还亮着。

每条日志都指向了正确答案，前提是**愿意逐层排除而不是猜**。

**另一个体会是：很多"无解"其实是"缺一个通道"。**
iOS 后台收不到电话，技术上是有解的（推送），难的是**谁来发这个推送**。
Asterisk 不会发，PortSIP 只给自己发，最后是 Yak 把接口开放出来才通了。
所以遇到死路时，值得再问一句：是不是少了某个环节的提供方？
