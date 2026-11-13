# U30 Air VoLTE ↔ SIP 语音网关

把一台**中兴 U30 Air 随身 Wi-Fi**（刷成 OpenWrt 的紫光展锐 UMS9620 平台）改造成一台真正能打电话的 **SIP 语音网关**：

> 手机上的 SIP 软电话 / IP 话机 → 拨号 → 这台随身 Wi-Fi 用内置 SIM 卡**拨出 VoLTE 电话** → 双向实时通话。

不用额外硬件、不用运营商 SIP 账号，设备本身就是一部"能接网线的手机"。

---

## 当前进度（2026-11-13）

| 能力 | 状态 | 说明 |
|---|---|---|
| SIP 服务器（Asterisk） | ✅ 已跑通 | 23.1.0，`192.168.77.1:5060/udp`，开机自启 |
| 分机注册 / 互拨 / 回音测试 | ✅ | 6001、6002，`600` 与 `*43` 为 Echo |
| **呼出：SIP → 手机号 → 双向语音** | ✅ **可用** | 本文档主角 |
| 双向语音延迟 | ✅ 已可接受 | 单向约 200~300 ms（详见 `docs/02-latency-investigation.md`） |
| **呼入：别人打进来 → SIP 分机振铃** | ⬜ **下一阶段** | 方案已定，见文末 Roadmap |

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
asterisk -rx "dialplan reload"
```

关键拨号规则（手机号 → 写目标号 → 交给 AudioSocket）：

```
exten => _1XXXXXXXXXX,1,Answer()
 same => n,System(echo ${EXTEN} > /tmp/bridge-target)
 same => n,AudioSocket(${UNIQUEID},127.0.0.1:9092)
 same => n,Hangup()
```

### 3. 验证

```sh
# 本机，与设备同网段
python3 tools/sip_verify.py                    # 默认 6001 → 600（Echo）
SIP_TARGET=600 python3 tools/sip_verify.py
```

或用任意 SIP 客户端注册 `6001` / `u30air-6001`，拨 `600` 听回音，拨手机号打电话。

### 4. 日志

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

- 只支持**呼出**；呼入未实现。
- 依赖已落地的 `agdsp.bin` 与 Audio HAL（未包含在本仓库，见上游社区）。
- 有界队列在长时间通话中会丢掉少量音频，属于用"可听性"换"延迟上限"的权衡。
- 未做 NAT / 公网暴露，仅限内网使用。

---

## Roadmap：呼入（下一阶段）

目标：别人拨打 SIM 卡号码 → SIP 分机振铃 → 接听后双向通话。

计划路径：

1. 用 `mu300-at` 订阅 URC，监听 `RING` / `+CLIP`，拿到主叫号码；
2. 触发 Asterisk `originate`，呼叫指定分机（如 6001）；
3. 分机接听后，把这条 VoLTE 通话接入同一个 AudioSocket 桥接（复用现有 `handle()`）；
4. 分机拒接/超时则 `AT+CHUP` 挂断。

需要一个常驻的 URC 监听进程（`mu300-atd` 已是单读者模型，需在其上做分发），预计改动集中在 `bridge.py` 的会话入口，DSP 与音频部分可完全复用。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

仅供个人学习与自用。请遵守当地电信法规与运营商条款，不要用于任何违规用途。
