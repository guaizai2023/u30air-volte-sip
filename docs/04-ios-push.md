# 让 iOS 在锁屏/被杀之后仍能收到来电

> 这是整个项目最后一个、也是最难的一个坑。
> 结论：**可行**，靠 Yak Phone 的推送中继接口，由设备侧的守护进程主动触发。

---

## 一、问题

前面的呼入链路在设备侧是完整的，但 iOS 终端有一个绕不过去的限制：

| 档位（iPhone 16e / iOS 27.2.1） | 结果 |
|---|---|
| 锁屏 15 分钟 | ✅ 响铃 1.5 s |
| 锁屏 52 分钟 | ✅ 响铃 1 s |
| **锁屏 2 小时** | ❌ **不响**，连通话记录都没有 |

日志证明这不是网络或 SIP 的问题：

```
AMI originate -> ok queued          ← INVITE 确实发出去了
6001/sip:6001@192.168.77.245:65311  ← TCP 源端口几小时没变，连接从未断过
```

**iOS 把 App 进程挂起了，但 socket 还挂在系统手里**。连接"看起来在线"，App 却处理不了来电。
这也是为什么"分机显示在线"根本不能用来判断它能不能接电话。

### 为什么普通做法不行

- **Asterisk 没有原生推送**：截至 Asterisk 22（当前 LTS），`res_pjsip_registrar` 不解析 Contact 里的
  `pn-provider` / `pn-prid`（RFC 8599），注册过期后 INVITE 照旧发给旧地址，失败。
- **PortSIP 的推送绑死 PortSIP PBX**：它的 REGISTER 要带 `x-p-push` 头，由 PortSIP PBX 发 APNs。用 Asterisk 永远不通。

---

## 二、Yak Phone 的做法

Yak Phone（App Store `id6763033863`，基于 baresip，Pro 订阅）把推送中继**开放成 HTTP 接口**：

```
POST https://push.yakteam.com/v1/notify
Content-Type: application/json

{
  "token":       "<设备的 VoIP Push Token>",
  "caller_uri":  "sip:13800138000@192.168.77.1",
  "caller_name": "13800138000",
  "type":        "voip",              // voip=来电 / missed=漏接 / message=SIP 消息
  "call_id":     "<唯一 ID>"
}
```

返回 `{"ok":true}` 即表示已交给 APNs。

关键点：**Yak 云端只知道设备 token，不知道什么时候有人打进来**——它不是你的 SIP 服务器，
访问不到内网的 Asterisk。所以**必须由设备在振铃那一刻主动调用这个接口**。

这就解释了之前的现象：我们从来没调过它，所以手机被挂起后什么都收不到。

---

## 三、集成方式

改 `tools/mu300-volte-in.py`，在发起呼叫前插入一步推送：

```
检测到 +CRING → 收集 +CLIP → POST notify(type=voip) → 等 1.6 s → AMI originate 呼叫分机
                                ↑                      ↑
                          唤醒手机           给 App 重新 REGISTER 的时间
```

**顺序不能反**。先叫后推的话，INVITE 会发给那个已经僵死的旧地址，照样失败。

无人接听（25 秒超时）时再补一条 `type=missed`，锁屏上至少能看到漏接记录。

### 配置

```sh
# 设备上，token 从 Yak「设置 → 消息推送 → VoIP Push Token」复制
mkdir -p /etc/mu300
echo '<64 位 token>' > /etc/mu300/yak-push.token
chmod 600 /etc/mu300/yak-push.token
```

可用环境变量覆盖：`MU300_YAK_PUSH_URL`、`MU300_YAK_TOKEN_FILE`、`MU300_YAK_WAKE_DELAY`、`MU300_YAK_DOMAIN`。

实现上用 `curl` 而不是 `urllib`——设备上的 Python 缺 `idna` 编码器，走域名会直接 `LookupError`。

### 容错

- 读不到 token → 跳过推送，正常呼叫（不退化成"完全收不到"）
- 推送失败 → 只记日志，不阻断呼叫
- 所以你可以在没有 Pro 订阅的情况下继续用这版，只是回到"1~2 小时"的老问题

---

## 四、实测结果

| 测试 | 结果 |
|---|---|
| 接口连通 | ✅ `{"ok":true}` HTTP 200（设备经 4G 出网访问 Cloudflare） |
| App 在前台/后台 | ✅ 响铃 |
| **从多任务里杀掉 App，等 30 秒再拨** | ✅ **响铃** ← 决定性证据 |

杀掉进程后仍能响，说明是被 APNs 唤醒并由 CallKit 接管，不是"App 恰好还活着"。

待验证：隔夜（8 小时以上）是否依然有效。

---

## 五、局限

1. **绑定 Yak Phone**。换别的软电话，这套推送就不适用了。
2. **需要 Pro 订阅**（免费版没有中继推送）。
3. **token 会变**。重装 App、重置系统后要重新复制。
   Yak 支持「Token 回调」（POST 到自定义 URL），但我们设备在内网接不了公网回调，
   所以目前是手工写文件。
4. 只对**一台设备**配置了一个 token；多设备需要扩展成 token 列表。

---

## 六、这条路的普适意义

如果你的 PBX 是 Asterisk / FreeSWITCH 这类没有 RFC 8599 的实现，想让 iOS 稳定收来电，
本质上只有三条路：

1. **客户端自带中继接口**（如 Yak）—— 成本最低，绑 App
2. **在 PBX 前加 Kamailio / OpenSIPS** 做 RFC 8599 代理 —— 技术最正统，配置量大
3. **换 Flexisip**（Linphone 的开源代理，原生支持 APNs）—— 等于换掉整个 SIP 层

自己写 APNs 网关也能做，但要 Apple 开发者账号（$99/年）并长期维护证书，为单台座机不值。
