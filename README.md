# U30 Air VoLTE ↔ SIP 语音网关

把一台**中兴 U30 Air 随身 Wi-Fi**（刷成 OpenWrt 的紫光展锐 UMS9620 平台）改造成一台真正能打电话的 **SIP 语音网关**：

> 手机上的 SIP 软电话 / IP 话机 → 拨号 → 这台随身 Wi-Fi 用内置 SIM 卡**拨出 VoLTE 电话** → 双向实时通话。

不用额外硬件、不用运营商 SIP 账号，设备本身就是一部"能接网线的手机"。

---

## 当前进度（2026-11-14）

| 能力 | 状态 | 说明 |
|---|---|---|
| SIP 服务器（Asterisk） | ✅ 已跑通 | 23.1.0，`192.168.77.1:5060`（UDP + TCP），开机自启 |
| 分机注册 / 互拨 / 回音测试 | ✅ | 6001、6002，`600` 与 `*43` 为 Echo |
| **呼出：SIP → 手机号 → 双向语音** | ✅ **可用** | |
| **呼入：外部拨打 SIM 号 → SIP 分机振铃 → 双向语音** | ✅ **可用** | 见 [`docs/03-inbound-call.md`](docs/03-inbound-call.md) |
| 双向语音延迟 | ✅ 已可接受 | 单向约 200~300 ms（详见 [`docs/02`](docs/02-latency-investigation.md)） |
| 挂断同步（双向） | ✅ | 任一方挂断，另一侧 1~2 秒内结束 |
| **长时间保活（iOS 后台）** | ⬜ **待测** | 需锁屏放置数小时观察 |
| **Android 端** | ⬜ **待测** | 尚未验证 |

---

## 硬件与系统

| 项 | 值 |
|---|---|
| 设备 | 中兴 U30 Air 随身 Wi-Fi |
| SoC | 紫光展锐 `UMS9620`（sprd，SC2730 音频编解码） |
| 系统 | OpenWrt 25.12.6 / Linux 7.2.8（mainline 移植版） |
| 包管理 | `apk`（OpenWrt 25.x），不是 opkg |
| 蜂窝 | 中国移动 46000 / 5G，`sipa_eth0` 上网 |
| 声卡 | `sprdphone-sc2730`（card 1） |
| 注入节点 | `hw:1,53` = `FE_ST_VOICE_PCM_P`（16 kHz 注入，上行） |
| 采集节点 | `hw:1,16` = `FE_ST_DUMP`（48 kHz 采集，下行） |

> 该机型原生 Android 不自带可用音频栈，需先落地社区的 `agdsp.bin` + Audio HAL 才能出声，本仓库默认这一步已完成。

---

## 架构

```
  SIP 软电话 / IP 话机
          │  SIP + RTP (G.711 8 kHz)
          ▼
  ┌───────────────────────────────┐
  │  Asterisk 23.1                │   192.168.77.1:5060/udp
  │  extensions.conf              │   拨号计划：手机号 → AudioSocket
  └───────────────┬───────────────┘
                  │  AudioSocket (TCP 127.0.0.1:9092)
                  │  帧：1 字节类型 + 2 字节长度 + PCM
                  ▼
  ┌───────────────────────────────┐
  │  mu300-bridge.py              │   ← 本仓库核心
  │                               │
  │  下行（对方→我）               │
  │   arecord hw:1,16 @48k        │
  │     → 6:1 抽取 → 8k           │
  │     → 20 ms 一帧送 Asterisk   │
  │                               │
  │  上行（我→对方）               │
  │   Asterisk 8k                 │
  │     → 有界队列(200ms)          │
  │     → 2× 线性插值 → 16k       │
  │     → aplay hw:1,53 @16k      │
  └───────────────┬───────────────┘
                  │  sprd VBC / DSP
                  ▼
            基带 VoLTE（SIM 卡）
```

**为什么采样率是 48k 采、16k 注？**
`FE_ST_VOICE_PCM_P` 这个注入节点按 **16 kHz 消费**，写 8 kHz 数据进去会变成 2 倍速（声音变尖、像花栗鼠）；而 `FE_ST_DUMP` 采集口输出的是 **48 kHz**。桥接层负责两边的换算。

---

## 关键实现点（都是踩过的坑）

1. **采样率必须严格匹配**。注入 16 kHz、采集 48 kHz，写错就是倍速或慢放。
2. **DSP 要先"上电"**。`mu300-voice audio start dsp` 成功之前，PCM 节点是哑的；场景启动失败（`Resource busy` / `did not start`）时重启 DSP 再重试一次。
3. **接通前就得持续送 RTP**。SIM 侧建链要 5~6 秒，之后 DSP 还要几秒才出音频。这期间对端收不到 RTP 会以"无媒体"挂断（实测约 15 秒）。所以桥接一连上就按 20 ms 节拍送静音帧，媒体流不断，对端就等得住。
4. **ALSA 小缓冲**。aplay 默认会把缓冲区填满才开始播，缓冲多大延迟就多大：
   ```
   aplay --period-size=320 --buffer-size=960   # 20 ms / 60 ms @16 kHz
   ```
5. **★ 延迟的真凶不是缓冲，是速率失配**。详见下一节。
6. **挂断顺序不能错**：先 `AT+CHUP` 挂断 → 再 `mu300-voice audio stop` → 最后关 PCM。反过来会把 DSP 语音流掐在半路，DSP 进入深睡且唤不醒，下一次通话必然失败。

---

## 延迟问题：一次完整的逐层排查

最初的表现是**单向**延迟：对方说话我这边"秒回"，但我说话到对方那边有延迟，而且**通话一分钟后越来越长**（从 2~3 秒涨到 10 秒）。

逐层排除：

| 层 | 观测手段 | 结果 | 结论 |
|---|---|---|---|
| RTP / 网络 | 抓包 60 s | 20 ms 稳定节奏，零抖动 | 排除 |
| 对端手机 / 运营商 | 换号、换设备 | 延迟一致 | 排除 |
| 桥接进程 | 收帧→写盘耗时 | ~0.1 ms | 排除 |
| Linux 管道 | `F_SETPIPE_SZ=4096` | 延迟不变 | 排除 |
| ALSA 用户层 | aplay 回显 `buffer_size` | 960 帧 = 60 ms | 排除 |
| ALSA 驱动层 | `/proc/asound/.../status` 的 `delay` | 稳定 40~60 ms，`hw_ptr` 紧跟 `appl_ptr` | 排除 |
| **DSP 注入段** | 有界队列的丢帧计数 | **持续丢帧** | **真凶** |

结论：**DSP 的注入通路实际吞吐跟不上我们"刚好跑满 32 kB/s"的送入速率**。Asterisk 的 8 kHz 时钟与 DSP 的 16 kHz 时钟不同源，微小频差被无余量地一路累积，于是越用越慢。

**解法**：把"读 socket"与"写声卡"解耦，中间放一个**有界队列**（10 帧 = 200 ms），超限就丢最旧的帧：

```python
MAXQ = 10
while len(inq) > MAXQ:
    inq.popleft()     # 丢最旧，延迟封顶
```

代价是极端情况下会丢一点音频，换来的是**延迟永远封顶在 200 ms 左右，不再随时间恶化**。实测整通电话丢了约 12 秒的陈旧帧（正常语速下几乎听不出来），体验从"没法对话"变成"可以接受"。

完整数据见 [`docs/02-latency-investigation.md`](docs/02-latency-investigation.md)。

---

## 前置：固件与音频栈

这几块是整个项目最难找的材料，已打包在 [`firmware/`](firmware/)：

| 材料 | 作用 | 缺了会怎样 |
|---|---|---|
| `agdsp.bin`（6,291,456 B） | AUDCP 固件，DSP 的"操作系统" | 全程无声 |
| `luci-app-airsim-0.1.0-r23.apk` | 社区音频栈（音频模块 + 工具 + LuCI） | 没有 `/dev/snd`、没有 `mu300-voice` |
| `airsim-install-r23.sh` | 官方安装脚本（验签 + 安装） | 手动装易漏依赖 |

```sh
curl -LO https://github.com/guaizai2023/u30air-volte-sip/raw/main/firmware/mu300-audio-kit-r23.tar.gz
tar xzf mu300-audio-kit-r23.tar.gz && cd mu300-audio-kit-r23
sh airsim-install-r23.sh --check     # 先干跑
sh airsim-install-r23.sh --install   # 再安装
```

> **硬约束：语音只在 `5.4.254-gb50db5b6224c` 内核下可用。** 装前 `uname -r` 确认。
> 细节、sha256 与来源见 [`firmware/README.md`](firmware/README.md)。

---

## 部署

### 1. 文件

```sh
# 设备上（OpenWrt）
scp tools/mu300-bridge.py       root@192.168.77.1:/usr/bin/mu300-bridge.py
scp etc/init.d/mu300-bridge     root@192.168.77.1:/etc/init.d/mu300-bridge
chmod +x /usr/bin/mu300-bridge.py /etc/init.d/mu300-bridge
/etc/init.d/mu300-bridge enable
/etc/init.d/mu300-bridge start
```

### 2. Asterisk

```sh
scp etc/asterisk/extensions.conf.example root@192.168.77.1:/etc/asterisk/extensions.conf
scp etc/asterisk/manager.conf.example    root@192.168.77.1:/etc/asterisk/manager.conf
asterisk -rx "dialplan reload"
asterisk -rx "manager reload"
```

> **呼入必须开 AMI**：这台 Asterisk 是精简构建，没有 `res_clioriginate`，
> CLI 的 `channel originate` 不存在，只能通过 AMI 发起呼叫。

### 3. 呼入服务

```sh
scp tools/mu300-volte-in.py   root@192.168.77.1:/usr/bin/mu300-volte-in.py
scp etc/init.d/mu300-volte-in root@192.168.77.1:/etc/init.d/mu300-volte-in
chmod +x /usr/bin/mu300-volte-in.py /etc/init.d/mu300-volte-in
/etc/init.d/mu300-volte-in enable
/etc/init.d/mu300-volte-in start
```

守护启动时会自动下发 `AT+CLIP=1`（来电显示），否则振铃时不带主叫号码。
完整说明见 [`docs/03-inbound-call.md`](docs/03-inbound-call.md)。

关键拨号规则（手机号 → 写目标号 → 交给 AudioSocket）：

```
exten => _1XXXXXXXXXX,1,Answer()
 same => n,System(echo ${EXTEN} > /tmp/bridge-target)
 same => n,AudioSocket(${UNIQUEID},127.0.0.1:9092)
 same => n,Hangup()
```

### 4. 验证

```sh
# 本机，与设备同网段
python3 tools/sip_verify.py                    # 默认 6001 → 600（Echo）
SIP_TARGET=600 python3 tools/sip_verify.py
```

或用任意 SIP 客户端注册 `6001` / `u30air-6001`，拨 `600` 听回音，拨手机号打电话。

### 5. 日志

```sh
tail -f /tmp/bridge.log
```

每 2 秒一行，含写阻塞、读间隔、驱动延迟、socket 队列、播放队列与丢帧数：

```
上行 6400 B / 下行 6400 B | 写阻塞 均19.3 峰31ms | 读间隔 均20.0 峰22ms | 驱动延迟 960帧(60ms) RUNNING | socket队列 均0 峰0 B | 播放队列 0 丢617
```

---

## 进入设备后台

| 入口 | 地址 | 账号 / 密码 | 说明 |
|---|---|---|---|
| **OpenWrt 后台（LuCI）** | `http://192.168.77.1` | `root` / `u30air2026` | 当前系统，已实测可登录 |
| OpenWrt SSH | `ssh root@192.168.77.1` | 公钥免密 | 端口 22 |
| Wi-Fi 热点 | SSID `ZTE_107525` | `1234567890`（WPA2） | 手机连这个才能访问上面地址 |
| 原厂 Android 后台 | `http://192.168.0.1` | — | 刷 OpenWrt 后**已不存在** |
| 原厂高级后台 | `http://192.168.0.1:2333` | `admin` / `924492` | 同上，当前无监听 |

> 刷了 OpenWrt 之后，原厂 Android 的 `192.168.0.1` 与 `:2333` 都不再监听，日常运维只走 `192.168.77.1`。
> 后台里另有两个专用页面：`luci-app-mu300`（蜂窝/设备控制）与 `luci-app-airsim`（音频场景管理）。

---

## 已知限制

- **分机必须在线**：iOS 会把后台的 VoIP App 挂起，挂起后 INVITE 没人应答，表现就是"不响"。
  需在软电话里打开 Force Background / Allow VoIP Calls，并把传输改成 **TCP**。
- 同一时刻不支持呼入与呼出并行（两者抢同一条 DSP 音频通路，呼出会被拒绝）。
- 只支持一路并发通话。
- 一通来电最多等 25 秒，超时自动挂断。
- 需先落地音频栈与 `agdsp.bin`（已打包在 [`firmware/`](firmware/)，锁定 5.4.254 内核）。
- 有界队列在长时间通话中会丢掉少量音频，属于用"可听性"换"延迟上限"的权衡。
- 未做 NAT / 公网暴露，仅限内网使用。

---

## 呼入（已实现）

目标已达成：别人拨打 SIM 卡号码 → SIP 分机振铃 → 接听后双向通话。

实现要点（细节与踩坑记录见 [`docs/03-inbound-call.md`](docs/03-inbound-call.md)）：

1. `mu300-volte-in.py` **只读跟随** `/run/mu300-at/urc/stty_nr0.log`，捕获 `+CRING` / `+CLIP`；
   （`mu300-atd` 是单读者模型，不能独占 AT 通道，所以只能读它写好的 URC 日志）
2. 用 **AMI** `Action: Originate` 呼叫分机——该 Asterisk 无 `res_clioriginate`，CLI 无法发起呼叫；
3. 分机接听后通道直接进入 `AudioSocket`，桥接检测到 `AT+CLCC=4`（振铃中）后下发 `ATA` 接听 VoLTE；
   **必须先等分机接听再 ATA**，否则对方提前计费；
4. 任一方挂断都立即同步：收到 `^CEND` → 挂掉分机通道；分机挂断 → 无条件 `AT+CHUP`。

## 下一步：还需要验证什么

| 项目 | 说明 |
|---|---|
| **长时间保活（iOS）** | 锁屏放置 30 分钟 / 2 小时 / 隔夜，分别拨入，看是否仍能响。iOS 会回收后台 VoIP App，这是目前最大的不确定性 |
| **Android 端** | 尚未在 Android 软电话上验证。Android 可用前台服务常驻，预期比 iOS 稳，但需实测确认 |

测试机：**iPhone 16e（美版无锁，iOS 27.2.1）**，软电话 PortSIP UC 12.4.6。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

仅供个人学习与自用。请遵守当地电信法规与运营商条款，不要用于任何违规用途。
