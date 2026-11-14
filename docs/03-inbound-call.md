# 呼入实现：别人打 SIM 号，SIP 分机振铃

> 阶段目标：外部电话拨打设备里的 SIM 号码 → SIP 分机响铃 → 接听后双向通话。
> 实现日期：2026-11-14。

---

## 一、链路

```
外部手机拨打 195xxxxxxxx
        │  VoLTE（运营商）
        ▼
   基带 / 模组  ──URC──>  /run/mu300-at/urc/stty_nr0.log
                              │  "+CRING: VOICE" / '+CLIP: "号码"'
                              ▼
                     mu300-volte-in.py（常驻守护）
                       │  ①写 /tmp/volte-incoming（主叫号码）
                       │  ②AMI Originate → PJSIP/6001，接听后执行 AudioSocket
                       ▼
                     Asterisk ──INVITE──> SIP 分机（6001）响铃
                       │                         │ 用户接听
                       │<──────── 200 OK ───────┘
                       │
                       │  AudioSocket (127.0.0.1:9092)
                       ▼
                  mu300-bridge.py
                       │  ③AT+CLCC 看到 state=4（振铃中）
                       │  ④下发 ATA 接听 VoLTE
                       │  ⑤等 state=0，启动 DSP 场景与双向音频
                       ▼
                  对方听到接通，开始通话
```

**顺序上有一处关键**：必须等分机接听之后才下发 `ATA`。反过来做的话，对方那边早就接通并开始计费，而这边的分机还在响。

---

## 二、为什么这么实现（三个绕不开的约束）

### 1. 来电事件只能从 URC 日志里拿

`mu300-atd` 是**单读者**模型：一条 AT 通道，任何客户端都不能独占。
所以不能开一个进程去阻塞读串口，只能**只读地跟随**它已经写好的 URC 日志：

```
/run/mu300-at/urc/stty_nr0.log
```

这个文件由 `mu300-atd` 维护，超过 64 KB 会轮转，所以跟随逻辑必须处理 inode 变化和截断。

另外必须在模组上开 `AT+CLIP=1`，否则振铃时不会带主叫号码。

### 2. 发起呼叫只能用 AMI

设备上的 Asterisk 是**精简构建**，`/usr/lib/asterisk/modules` 只有 19 个 `.so`：

- 没有 `res_clioriginate.so` → `channel originate` 命令不存在
- 旧的 `originate` 命令也不存在
- AMI 是内建的，只是 `manager.conf` 里默认 `enabled = no`

所以用 AMI 的 `Action: Originate`，直接指定：

```
Channel:     PJSIP/6001
Application: AudioSocket
Data:        <uuid>,127.0.0.1:9092
Timeout:     25000
```

分机一接听，通道就直接进入 `AudioSocket`，连到桥接程序。

### 3. 挂断通知是 `^CEND`，不是 `NO CARRIER`

实测这台模组挂断时发的是：

```
^CEND: 1,,104,999
^CEND: 17,,104,111,2
```

不是标准的 `NO CARRIER`。最初只匹配 `NO CARRIER`，导致通话结束后内部状态一直不复位，
**后续所有来电都被当成"同一通还在响"丢掉**——表现出来就是"打几通只有第一通能接到"。

---

## 三、踩过的坑（按现象索引）

| 现象 | 原因 | 解法 |
|---|---|---|
| 手机根本不响 | `channel originate` 命令不存在，一个包都没发出去 | 改用 AMI Originate |
| 日志里主叫号码是 `unknown` | 收到 `+CRING` 后在读循环里 `sleep` 等号码，把 `+CLIP` 那行堵住了 | 改成 `threading.Timer` 异步触发，读循环不阻塞 |
| 打几通只有第一通能接 | 挂断通知 `^CEND` 没被识别，`busy` 永不复位 | 识别 `^CEND` + 40 秒兜底强制复位 |
| 第一通响几秒自动挂断 | `mu300-voice` 场景启动耗时波动（0.3~1.3 s），aplay 提前启动遇 underrun，`--fatal-errors` 让它直接退出 | 呼入时启动等待从 0.8 s 提到 1.8 s |
| 我挂了，对方半天才断 | 结束流程写成 `if clcc_state() is not None` 才发 CHUP，查询超时就根本不挂断 | 改为**无条件** `AT+CHUP` |
| 对方挂了，分机还在响 | 只清了本地状态，没终止 Asterisk 上仍停在振铃的 `PJSIP/6001-*` 通道 | 收到挂断通知后 `channel request hangup` 逐个挂掉 |
| 某一通完全没声音 | 同一时刻既在呼出又在呼入，两路抢同一条 DSP 音频通路 | 呼出前查 `AT+CLCC`，发现 state=4 就放弃呼出 |

---

## 四、部署

```sh
# 1. 守护进程
scp tools/mu300-volte-in.py  root@192.168.77.1:/usr/bin/
scp etc/init.d/mu300-volte-in root@192.168.77.1:/etc/init.d/
ssh root@192.168.77.1 'chmod +x /usr/bin/mu300-volte-in.py /etc/init.d/mu300-volte-in
                       /etc/init.d/mu300-volte-in enable
                       /etc/init.d/mu300-volte-in start'

# 2. AMI（呼入发起呼叫需要）
scp etc/asterisk/manager.conf.example root@192.168.77.1:/etc/asterisk/manager.conf
ssh root@192.168.77.1 'asterisk -rx "manager reload"'

# 3. 拨号计划（备用入口，手动测试用）
ssh root@192.168.77.1 'asterisk -rx "dialplan reload"'

# 4. 确认
ssh root@192.168.77.1 'ubus call mu300dash list >/dev/null 2>&1; true'
ssh root@192.168.77.1 'netstat -lnt | grep 5038'          # AMI 在监听
ssh root@192.168.77.1 'tail -f /tmp/volte-in.log'         # 来电日志
```

依赖：`mu300-bridge.py` v14 必须在跑（它负责 `ATA` 与音频）。

---

## 五、日志怎么读

`/tmp/volte-in.log` 一次正常来电：

```
11:29:26 检测到振铃
11:29:26   主叫: 18776737112
11:29:27 主叫号码: 18776737112
11:29:27 呼叫 SIP 分机 6001
11:29:27   AMI originate -> ok queued
```

`/tmp/bridge.log` 对应的一次呼入：

```
11:29:31 asterisk connected from ('127.0.0.1', 38994)
11:29:31 呼入来电（振铃中）: 18776737112 | CLCC state=4
11:29:31 音频就绪（0.0s）
11:29:31 呼入：下发 ATA 接听
11:29:31 呼入已接通（0.1s）
11:29:32 mu300-voice: VOICE-RUNNING hw:1,5 both directions, 8000 Hz, output dsp
11:29:47 downlink 异常: ConnectionError('asterisk closed')
11:29:48 结束流程：CLCC=0，下发 AT+CHUP
11:29:48   AT+CHUP -> OK
```

判断要点：

- `呼入已接通（0.1s）` 说明 `ATA` 生效
- `VOICE-RUNNING ... both directions` 说明 DSP 双向通路起来了
- 之后每 2 秒的统计行里，**上行/下行字节都应在增长**；只有一边增长就是单通了
- `结束流程` 后必须出现 `AT+CHUP -> OK`，否则对面会一直听回铃

---

## 六、已知限制

1. **分机必须保持在线**。iOS 会把后台的 SIP App 挂起（除非开了 Force Background 且没被系统回收），
   挂起后 INVITE 发过去没人应答，表现就是"不响"。
2. 一通来电最多等 25 秒（`Dial` 与 AMI 的 `Timeout` 对齐），超时自动挂断。
3. 同一时刻不支持呼入与呼出并行（两者抢同一条 DSP 通路），呼出会被拒绝。
4. 只支持一路并发通话。

---

## 七、测试状态

| 项目 | 状态 | 说明 |
|---|---|---|
| 呼入振铃 | ✅ | 6001 正常响铃 |
| 接听 + 双向语音 | ✅ | 实测多通正常 |
| 挂断同步（我挂→对方断） | ✅ | `AT+CHUP -> OK`，对面 1~2 秒内断 |
| 挂断同步（对方挂→我停） | ✅ | 已加 `channel request hangup` |
| 连续多通 | ✅ | `^CEND` 修复后不再丢 |
| 未接超时 | ✅ | 25 秒后自动挂断 |
| 呼出回归 | ✅ | 呼出未受影响 |
| **长时间保活（iOS 后台）** | ⬜ **待测** | 需锁屏放置数小时观察 |
| **Android 端** | ⬜ **待测** | 尚未验证 |

测试用机：**iPhone 16e（美版无锁，iOS 27.2.1）**，软电话 PortSIP UC 12.4.6。
