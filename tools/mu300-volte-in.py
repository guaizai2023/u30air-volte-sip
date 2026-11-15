#!/usr/bin/env python3
"""mu300-volte-in -- 监听 VoLTE 来电，把 SIP 分机叫响

链路：
  1. tail /run/mu300-at/urc/stty_nr0.log（mu300-atd 实时写入 URC，只读，不占 AT 通道）
  2. 看到 +CRING / RING，起一个 1 秒的定时器（不阻塞读循环，好让 +CLIP 能落进来）
  3. 定时器到点：取主叫 -> 写 /tmp/volte-incoming -> 用 AMI 让 Asterisk 呼叫分机，
     分机接听后就执行 AudioSocket 连到 mu300-bridge
  4. 桥接发现没有呼出目标号、AT+CLCC 是 4(振铃)，就下发 ATA 接听并接通音频
  5. 25 秒没人接，主动 AT+CHUP 挂断

为什么不用 CLI originate：这个 Asterisk 是精简构建，没有 res_clioriginate 模块，
`channel originate` 不存在；AMI 是内建的，只是默认没开。
"""

import json
import os
import re
import socket
import subprocess
import threading
import time

URC = os.environ.get("MU300_URC_LOG", "/run/mu300-at/urc/stty_nr0.log")
LOG = os.environ.get("MU300_IN_LOG", "/tmp/volte-in.log")
FLAG = "/tmp/volte-incoming"
AT = os.environ.get("MU300_AT", "/usr/bin/mu300-at")
EXT = os.environ.get("MU300_IN_EXT", "6001")
AUDIOSOCKET = os.environ.get("MU300_IN_UUID",
                             "6d7f8a3e-4bcd-4e2f-9a01-0123456789ab") + ",127.0.0.1:9092"
AMI_HOST = os.environ.get("MU300_AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("MU300_AMI_PORT", "5038"))
AMI_USER = os.environ.get("MU300_AMI_USER", "mu300")
AMI_SECRET = os.environ.get("MU300_AMI_SECRET", "mu300ami2026")
RING_TIMEOUT = 25                        # 与 [volte-in] 里 Dial 的 25 秒对齐

# ---- Yak Phone 推送中继 ------------------------------------------------------
# Yak 云端只知道本机 device token，不知道什么时候有人打进来；必须在振铃时由我们
# 主动 POST /v1/notify，它才会发 APNs VoIP push，把被 iOS 挂起的 App 叫醒。
# 这正是 PortSIP 做不到的地方（它的推送绑死在 PortSIP PBX 上）。
YAK_PUSH_URL = os.environ.get("MU300_YAK_PUSH_URL", "https://push.yakteam.com/v1/notify")
YAK_TOKEN_FILE = os.environ.get("MU300_YAK_TOKEN_FILE", "/etc/mu300/yak-push.token")
YAK_WAKE_MIN = float(os.environ.get("MU300_YAK_WAKE_MIN", "0.8"))   # 推完先等这么久再查
YAK_WAIT_MAX = float(os.environ.get("MU300_YAK_WAIT_MAX", "15.0"))  # 等分机注册的上限
YAK_SIP_DOMAIN = os.environ.get("MU300_YAK_DOMAIN", "192.168.77.1")

state = {"busy": False, "clip": "", "ring_at": 0.0, "triggered": False,
         "triggered_at": 0.0, "timer": None}
lock = threading.Lock()


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def at(cmd, t=8):
    try:
        r = subprocess.run([AT, "-t", str(t), cmd], capture_output=True,
                           text=True, timeout=t + 6)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:                                # noqa: BLE001
        return "ERR %s" % e


def clcc_state():
    out = at("AT+CLCC", 6)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("+CLCC:"):
            f = line.split(",")
            if len(f) > 2 and f[2].strip().isdigit():
                return int(f[2].strip())
    return None


class AMI:
    """最小 AMI 客户端，只做 Login + Originate。"""

    def __init__(self):
        self.host, self.port = AMI_HOST, AMI_PORT

    @staticmethod
    def _msg(s):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head.decode("utf-8", "replace"), rest

    def _read_response(self, s, limit=12):
        for _ in range(limit):
            head, rest = self._msg(s)
            if head.startswith("Response:"):
                return head, rest
        return "", b""

    def originate(self, channel, app, data, timeout=25000):
        # 设备上的 Python 缺 idna，create_connection 会走 getaddrinfo 报错；目标是纯 IP，直接 connect
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(6)
        s.connect((self.host, self.port))
        try:
            try:
                s.settimeout(1.0)
                s.recv(1024)                 # 可选 banner
            except OSError:
                pass
            s.settimeout(8)
            s.sendall(("Action: Login\r\nUsername: %s\r\nSecret: %s\r\n\r\n"
                       % (AMI_USER, AMI_SECRET)).encode())
            head, _ = self._read_response(s)
            if "Success" not in head:
                return False, "login: " + head.replace("\r\n", " ")

            act = ["Action: Originate",
                   "Channel: %s" % channel,
                   "Application: %s" % app,
                   "Data: %s" % data,
                   "Timeout: %d" % timeout,
                   "Async: true",
                   "CallerID: VoLTE <%s>" % (state.get("clip") or "unknown")]
            s.sendall(("\r\n".join(act) + "\r\n\r\n").encode())
            head, _ = self._read_response(s)
            if "Success" not in head:
                return False, head.replace("\r\n", " ")
            return True, "queued"
        finally:
            try:
                s.close()
            except OSError:
                pass


def hangup_later(my_ts):
    """25 秒还没接通就挂断，避免对方一直听回铃。

    带上是哪一次的标记：如果这期间已经换成新的一通来电，就别去挂断它。

    判据用「不是已接通」而不是「等于振铃」：实测 CLCC 查询偶尔返回空，
    按 `st == 4` 判断会整段跳过，结果我方分机一直响到 Asterisk 超时，
    更糟的是 VoLTE 侧被留成假振铃——那通假来电占着线路，下一通直接打不进来。
    """
    time.sleep(RING_TIMEOUT + 2)
    with lock:
        if state.get("triggered_at") != my_ts or not state["busy"]:
            return                    # 已经换了一通，或本通早已结束
    st = clcc_state()
    if st != 0:                       # None=已挂断、4=还在响，都不该继续占着
        log("呼入未接通（CLCC=%s），清理并挂断", st)
        at("AT+CHUP", 6)
        hangup_extension()            # 同时把还在响的分机通道掐掉
        with lock:
            num = state.get("clip") or "unknown"
        yak_notify(num, "missed")
        reset()


def ring_watchdog(my_ts):
    """振铃期间盯住 CLCC：主叫一旦挂断，立刻把还在响的分机挂掉。

    原来只靠 URC 的 `^CEND` 感知挂断，但模组有时不发、或发得比预期晚，
    结果主叫都挂了这边还在响十几秒。更严重的是 VoLTE 侧会留下"假振铃"
    （CLCC 一直停在 4），后续来电全部被拒绝，表现为"打不进去"。
    """
    time.sleep(1.2)
    while True:
        with lock:
            if state.get("triggered_at") != my_ts or not state["busy"]:
                return                        # 已经换了一通，或本通已收场
        st = clcc_state()
        if st == 0:
            return                            # 已接通，交给正常通话流程
        if st is None:
            time.sleep(1.0)                   # 再确认一次，避免 AT 偶发超时误判
            with lock:
                if state.get("triggered_at") != my_ts or not state["busy"]:
                    return
            if clcc_state() is None:
                log("来电已消失（主叫挂断），挂断分机")
                hangup_extension()
                reset()
                return
        time.sleep(1.5)


def trigger():
    """定时器到点：真正发起呼叫。"""
    with lock:
        if state["triggered"]:
            return
        state["triggered"] = True
        state["triggered_at"] = time.time()
        num = state["clip"] or "unknown"
        ts = state["triggered_at"]

    log("主叫号码:", num)
    try:
        with open(FLAG, "w") as f:
            f.write(num)
    except OSError:
        pass

    # ① 先推：iOS 可能早就把这 App 挂起了，不推的话 INVITE 发过去没人处理，
    #    用户连未接记录都看不到（实测过）。
    if yak_notify(num, "voip"):
        if not wait_registered():
            # 分机始终没注册上来，这时候发 INVITE 只会让 Asterisk 报
            # "Could not create dialog to invalid URI"，对方听到的是一通空响。
            # 不如放弃，改推一条漏接通知。
            log("分机未注册，放弃本次呼叫")
            at("AT+CHUP", 6)
            with lock:
                num2 = state.get("clip") or num
            yak_notify(num2, "missed")
            reset()
            return

    # ② 再叫分机
    log("呼叫 SIP 分机", EXT)
    ok, detail = AMI().originate("PJSIP/" + EXT, "AudioSocket", AUDIOSOCKET)
    log("  AMI originate ->", "ok" if ok else "失败", detail[:100])
    if not ok:
        # 分机叫不响，别让对方一直等
        at("AT+CHUP", 6)
        reset()
        return
    threading.Thread(target=hangup_later, args=(ts,), daemon=True).start()
    threading.Thread(target=ring_watchdog, args=(ts,), daemon=True).start()


def reset():
    with lock:
        if state["timer"]:
            state["timer"].cancel()
            state["timer"] = None
        state["busy"] = False
        state["clip"] = ""
        state["triggered"] = False
    try:
        if os.path.exists(FLAG):
            os.unlink(FLAG)
    except OSError:
        pass


def on_ring():
    """振铃：启动 1 秒定时器去发起呼叫（不阻塞读循环，好让 +CLIP 落进来）。"""
    if state["busy"]:
        age = time.time() - state.get("triggered_at", 0)
        if age < 3:
            return                       # 同一通振铃的后续几声
        if age > 40:
            # 一通来电从振铃到被运营商放弃最多 30 秒左右；超过 40 秒还挂着 busy，
            # 只能是状态残留（比如漏了一个挂断通知），必须放行，否则以后再也收不到
            log("状态残留 %.0fs，强制复位" % age)
            reset()
        else:
            st = clcc_state()
            if st in (0, 4):
                return                   # 同一通：还在振铃或在通话
            log("检测到残留状态（CLCC=%s），强制复位" % st)
            reset()
    with lock:
        if state["busy"]:
            return
        state["busy"] = True
        state["triggered"] = False
        state["ring_at"] = time.time()
        state["timer"] = threading.Timer(1.0, trigger)
        state["timer"].daemon = True
        state["timer"].start()
    log("检测到振铃")


def yak_notify(caller, kind="voip", call_id=None):
    """调 Yak 的推送中继把手机叫醒。失败只记日志，不阻断呼叫。

    设备上的 Python 缺 idna 编码器，urllib 走域名会直接 LookupError，
    所以用 curl 发（这台机器 curl/wget 都有）。
    """
    try:
        with open(YAK_TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        log("  未配置 Yak token（%s），跳过推送" % YAK_TOKEN_FILE)
        return False
    if not token:
        return False

    payload = {
        "token": token,
        "caller_uri": "sip:%s@%s" % (caller, YAK_SIP_DOMAIN),
        "caller_name": caller,
        "type": kind,
        "call_id": call_id or ("mu300-%d" % int(time.time() * 1000)),
    }
    try:
        r = subprocess.run(
            ["curl", "-sS", "-m", "8", "-X", "POST", YAK_PUSH_URL,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload, ensure_ascii=False)],
            capture_output=True, text=True, timeout=12)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        ok = '"ok":true' in out.replace(" ", "")
        log("  Yak 推送(%s) -> %s %s" % (kind, "ok" if ok else "失败", out[:60]))
        return ok
    except Exception as e:                                # noqa: BLE001
        log("  Yak 推送异常:", repr(e))
        return False


def extension_registered():
    """分机现在在 Asterisk 上有没有 contact。"""
    try:
        r = subprocess.run(["asterisk", "-rx", "pjsip show contacts"],
                           capture_output=True, text=True, timeout=6)
        return ("%s/" % EXT) in (r.stdout or "")
    except Exception:                                     # noqa: BLE001
        return False


def wait_registered():
    """推送之后等分机重新注册上来，注册好就立刻返回。

    为什么不能固定 sleep：App 被系统杀掉时，从收到推送到重新 REGISTER 要好几秒，
    固定等 1.6 s 会在它还没注册完就把 INVITE 发出去，照样落空（实测见过）。
    而 App 本来就活着时，第一次检查就已注册，几乎不增加延迟。
    """
    t0 = time.time()
    time.sleep(YAK_WAKE_MIN)
    while time.time() - t0 < YAK_WAIT_MAX:
        if extension_registered():
            log("  分机已注册（等待 %.1fs）" % (time.time() - t0))
            return True
        time.sleep(0.4)
    log("  分机在 %.0fs 内未注册上，仍尝试呼叫" % YAK_WAIT_MAX)
    return False


def hangup_extension():
    """把 Asterisk 上还在振铃的分机通道挂掉。

    VoLTE 侧挂断（^CEND）时，Asterisk 那边仍停在 Dial/Originate 的振铃状态，
    要等 25 秒超时才结束——这期间分机一直在响，主叫却早就挂了。
    必须主动挂断，分机才会立刻停止振铃。
    """
    try:
        r = subprocess.run(["asterisk", "-rx", "core show channels concise"],
                           capture_output=True, text=True, timeout=8)
        names = []
        for line in (r.stdout or "").splitlines():
            if line.startswith("PJSIP/%s-" % EXT):
                names.append(line.split("!")[0])
        for ch in names:
            subprocess.run(["asterisk", "-rx", "channel request hangup %s" % ch],
                           capture_output=True, text=True, timeout=8)
            log("  已挂断分机通道:", ch)
        return bool(names)
    except Exception as e:                                # noqa: BLE001
        log("挂断分机失败:", repr(e))
        return False


def on_line(line):
    s = line.strip()
    if not s:
        return
    if s in ("RING", "RINGING") or s.startswith("+CRING"):
        on_ring()
    elif s.startswith("+CLIP:") or s.startswith("+CNAP:"):
        m = re.search(r'"([^"]*)"', s)
        if m and m.group(1):
            with lock:
                changed = state["clip"] != m.group(1)
                state["clip"] = m.group(1)
            if changed:                  # 每声振铃都会重复一次，只记变化
                log("  主叫:", m.group(1))
    elif s.startswith("NO CARRIER") or s.startswith("^CEND") or s.startswith("+CEND"):
        # 实测这台模组挂断时发的是 "^CEND: 1,,104,999"，不是 NO CARRIER；
        # 只认 NO CARRIER 会导致 busy 永远不复位，下一通来电被当成重复振铃丢掉。
        with lock:
            was_ringing = state["busy"]
        if was_ringing:
            # 主叫已经挂了，别让分机一直响到 25 秒超时
            hangup_extension()
        reset()


def watchdog():
    """通话结束后复位状态。

    注意判据要宽松：振铃/通话期间 mu300-at 偶发一次查询超时（返回 None）很常见，
    原来连续 2 次（4 秒）就复位，会让同一通来电被重复触发第二次 originate，
    表现为"有时能接通、有时接不起来"。改成连续 6 次（约 12 秒）才认。
    """
    idle = 0
    while True:
        time.sleep(2)
        st = clcc_state()
        if st is None:
            idle += 1
            if idle >= 6:
                with lock:
                    was_busy = state["busy"]
                if was_busy:
                    log("通话结束，复位")
                reset()
        else:
            idle = 0


def tail():
    f = None
    ino = None
    pos = 0
    while True:
        try:
            st = os.stat(URC)
            if f is None or ino != st.st_ino:
                if f:
                    f.close()
                f = open(URC, "r", errors="replace")
                ino = os.fstat(f.fileno()).st_ino
                f.seek(0, os.SEEK_END)
                pos = f.tell()
                log("跟随 URC:", URC, "offset", pos)
            if os.path.getsize(URC) < pos:
                f.seek(0)
                pos = 0
            line = f.readline()
            if line:
                pos = f.tell()
                yield line
            else:
                time.sleep(0.12)
        except FileNotFoundError:
            time.sleep(0.5)
        except OSError as e:
            log("tail 异常:", repr(e))
            time.sleep(0.5)


def main():
    log("volte-in 启动（分机 %s）" % EXT)
    out = at("AT+CLIP=1", 6)
    log("AT+CLIP=1 ->", out.strip().splitlines()[-1] if out.strip() else "?")
    threading.Thread(target=watchdog, daemon=True).start()
    for line in tail():
        try:
            on_line(line)
        except Exception as e:                            # noqa: BLE001
            log("处理失败:", repr(e))


if __name__ == "__main__":
    main()
