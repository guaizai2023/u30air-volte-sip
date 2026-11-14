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
    """
    time.sleep(RING_TIMEOUT + 2)
    with lock:
        if state.get("triggered_at") != my_ts:
            return
    st = clcc_state()
    if st == 4:
        log("呼入无人接听，挂断 VoLTE")
        at("AT+CHUP", 6)
        reset()


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

    log("呼叫 SIP 分机", EXT)
    ok, detail = AMI().originate("PJSIP/" + EXT, "AudioSocket", AUDIOSOCKET)
    log("  AMI originate ->", "ok" if ok else "失败", detail[:100])
    if not ok:
        # 分机叫不响，别让对方一直等
        at("AT+CHUP", 6)
        reset()
        return
    threading.Thread(target=hangup_later, args=(ts,), daemon=True).start()


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
