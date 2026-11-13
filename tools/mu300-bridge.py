#!/usr/bin/env python3
"""mu300 SIP 桥接 v13

v3 关键修复：
  * 采样率：上行写 16 kHz（8k 帧 ×2 线性插值），下行读 48 kHz（6:1 抽取成 8k）
    —— v2 直接写 8k 会让 DSP 按 16k 播放，声音变尖（倍速）
  * 会话开始先确保声卡在场（mu300-audio-dsp load / start）
  * 每 2 秒打印上下行字节数
  * 监控连续 3 次无 CLCC 才结束

v4 关键修复（延迟 3-4s）：
  * **显式指定 ALSA period/buffer**：aplay 默认会把整块缓冲区填满才开始播，
    缓冲区多大延迟就多大。播放侧改为 20ms period / 60ms buffer，
    采集侧 20ms period / 80ms buffer。
  * 打印每次写入的阻塞时长（阻塞 ≈ 播放侧积压），用于判断是否还在堆积。
"""
import array, collections, fcntl, os, re, socket, struct, subprocess, threading, time

AUDIO, UUID, HANGUP, ERROR = 0x10, 0x01, 0x00, 0xFF
PORT = 9092
TARGET = "/tmp/bridge-target"
LOG = "/tmp/bridge.log"
CAP = ["arecord", "-D", "hw:1,16", "-f", "S16_LE", "-r", "48000", "-c", "1", "-t", "raw", "-q",
       "--fatal-errors"]      # 默认缓冲；出错即退出（xrun 后重启流会 panic，见 FINDINGS 24e）
PLAY_DEV = os.environ.get("MU300_PLAY_DEV", "53")
PLAY = ["aplay", "-D", "hw:1,%s" % PLAY_DEV, "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw",
        "--fatal-errors", "-v",
        "--period-size=320", "--buffer-size=960"]   # 20ms period / 60ms buffer @16k
# 缩小缓冲的依据：日志里「写阻塞 均 19ms」= 每次写都被卡住，说明 aplay 的环形缓冲始终是满的，
# 于是「现在写进去的声音」要等整个缓冲播完才出声 → 缓冲多大，延迟就多大。
# 安全性：--fatal-errors 保证 xrun 时进程直接退出（会话结束），不会重启流触发 5.4 的 panic。
CAP_FB = CAP
PLAY_FB = PLAY


def spawn(args, fb, name, mode):
    """按小缓冲启动；参数不被支持时自动退回默认缓冲。"""
    errfile = "/tmp/%s.err" % name
    try:
        f = open(errfile, "wb")
    except OSError:
        f = subprocess.DEVNULL
    kw = dict(stderr=f, bufsize=0)
    if mode == "out":
        pr = subprocess.Popen(args, stdout=subprocess.PIPE, **kw)
    else:
        pr = subprocess.Popen(args, stdin=subprocess.PIPE, **kw)
    time.sleep(0.4)
    if pr.poll() is not None and fb != args:
        try:
            msg = open(errfile, "rb").read().decode("utf-8", "replace").strip().splitlines()[-1:]
        except OSError:
            msg = []
        log("%s 小缓冲启动失败，回退默认缓冲: %s" % (name, (msg[0] if msg else "")[:120]))
        if mode == "out":
            pr = subprocess.Popen(fb, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        else:
            pr = subprocess.Popen(fb, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        time.sleep(0.4)
    return pr

def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

def sh(cmd, t=15):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=t)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return "ERR %s" % e

def at(cmd, t=8):
    return sh(["mu300-at", "-t", str(t), cmd], t + 6)

def card_ok():
    try:
        return any("sprdphone" in l for l in open("/proc/asound/cards"))
    except OSError:
        return False

_STATUS_DUMPED = [False]


def sock_backlog(sock):
    """内核 socket 接收队列里还没被我们读走的字节数。
    持续增长 = Asterisk 发得比我们读得快 → 延迟在 socket 里累积。"""
    try:
        import array as _a, termios as _t
        buf = _a.array("i", [0])
        fcntl.ioctl(sock.fileno(), _t.FIONREAD, buf)
        return buf[0]
    except Exception:
        return -1


def alsa_status_raw(dev="53"):
    try:
        return open("/proc/asound/card1/pcm%sp/sub0/status" % dev, "r").read()
    except OSError as e:
        return "读不到: %r" % e


def alsa_delay(dev="53"):
    """驱动报告的播放延迟（未播出的帧数）。这是 aplay 之下、DSP 之上的真实积压。"""
    try:
        txt = open("/proc/asound/card1/pcm%s%s/sub0/status" % (dev, "p"), "r").read()
    except OSError:
        return None
    d = {}
    for ln in txt.splitlines():
        k, _, v = ln.partition(":")
        d[k.strip()] = v.strip()
    try:
        return int(d.get("delay", "-1")), d.get("state", "?")
    except ValueError:
        return None


def ensure_card():
    """声卡在场 + DSP 真的醒着。DSP 会在通话结束后睡死且唤不醒，
    所以每次通话前都查一次，必要时重新 start。"""
    if not card_ok():
        log("声卡不在，尝试加载模块")
        sh(["mu300-audio-dsp", "load"], 90)
        if not card_ok():
            log("加载后仍无声卡")
            return False
        log("声卡已注册")
    # 不再调 `mu300-audio-dsp status`：它内部会开一次 PCM（timeout 8 aplay），
    # 实测拖了 10.7 秒，把 SIP 侧的无媒体计时完全耗尽。
    # DSP 是否醒来交给场景启动失败后的重试路径去发现，那里才会真正 start。
    return True

def clcc_state():
    out = at("AT+CLCC", 6)
    ok = "OK" in out
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("+CLCC"):
            f = line.split(",")
            if len(f) > 2:
                return f[2].strip(), ok
    return None, ok

def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            raise ConnectionError("asterisk closed")
        buf += d
    return buf

def read_exact(fp, n):
    buf = b""
    while len(buf) < n:
        chunk = fp.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

def send_frame(sock, kind, payload=b""):
    sock.sendall(struct.pack("!BH", kind, len(payload)) + payload)

def upsample2(pcm8k, prev):
    """8k -> 16k：线性插值，返回 (16k bytes, 新 prev)"""
    a = array.array("h"); a.frombytes(pcm8k)
    out = array.array("h", bytes(2 * len(a) * 2))
    o = 0
    p = prev
    for s in a:
        out[o] = p
        out[o + 1] = (p + s) >> 1
        o += 2
        p = s
    return out.tobytes(), p

def downsample6(pcm48k):
    """48k -> 8k：6 点平均抽取"""
    a = array.array("h"); a.frombytes(pcm48k)
    n = len(a) // 6
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        b = i * 6
        out[i] = (a[b] + a[b+1] + a[b+2] + a[b+3] + a[b+4] + a[b+5]) // 6
    return out.tobytes()

def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(4)
    log("bridge v13 listening on 127.0.0.1:%d (上行16k/下行48k)" % PORT)
    while True:
        conn, addr = srv.accept()
        log("asterisk connected from", addr)
        try:
            handle(conn)
        except Exception as e:
            log("session error:", repr(e))
        finally:
            try:
                conn.close()
            except OSError:
                pass

def handle(conn):
    conn.settimeout(10)
    kind, ln = struct.unpack("!BH", recv_exact(conn, 3))
    recv_exact(conn, ln)
    log("首帧 kind=0x%02x len=%d" % (kind, ln))

    num = ""
    if os.path.exists(TARGET):
        num = open(TARGET).read().strip()
        os.unlink(TARGET)
    if not re.fullmatch(r"[0-9*#]{3,20}", num or ""):
        log("没有有效目标号码:", repr(num))
        send_frame(conn, ERROR, b"no target")
        return
    log("目标号码:", num)

    # --- 一连上就按 20ms 节拍持续送音频，队列空就送静音 -------------------------------
    # 原因：SIM 侧建立通话要 5-6 s，之后 mu300-voice 还要若干秒才出音频；
    # 这段时间对端收不到 RTP，PortSIP 会以「无媒体」挂断（实测约 15 s 后 BYE）。
    # 只要媒体流不断，对端就等得住。
    stop = threading.Event()
    outq = collections.deque()
    qlock = threading.Lock()
    stat = {"up": 0, "down": 0, "wmax": 0.0, "wsum": 0.0, "wn": 0,
            "cmax": 0.0, "csum": 0.0, "cn": 0}
    SILENCE = b"\x00" * 320

    def sender():
        tick = time.time()
        sent_sil = 0
        while not stop.is_set():
            with qlock:
                p = outq.popleft() if outq else None
            try:
                send_frame(conn, AUDIO, p if p else SILENCE)
            except OSError as e:
                log("sender 异常:", repr(e))
                stop.set()
                return
            if p is None:
                sent_sil += 1
            tick += 0.02
            d = tick - time.time()
            if d > 0:
                time.sleep(d)
            else:
                tick = time.time()          # 落后就重新对齐，不做追赶式突发
        log("sender 结束（静音帧 %d）" % sent_sil)

    threading.Thread(target=sender, daemon=True).start()

    t_setup = time.time()
    if not ensure_card():
        send_frame(conn, ERROR, b"no sound card")
        stop.set()
        return
    log("音频就绪（%.1fs），开始拨号" % (time.time() - t_setup))

    at("ATD%s;" % num, 15)
    t0, active = time.time(), False
    while time.time() - t0 < 35:
        st, ok = clcc_state()
        if st == "0":
            active = True
            break
        if st is None and ok and time.time() - t0 > 5:
            break
        time.sleep(1)
    if not active:
        log("拨号未接通")
        send_frame(conn, ERROR, b"no answer")
        at("AT+CHUP", 6)
        return
    log("通话已接通（%.1fs）" % (time.time() - t0))
    def start_scene():
        _t = time.time()
        out = sh(["mu300-voice", "audio", "start", "dsp"], 45)
        log("  mu300-voice 耗时 %.1fs" % (time.time() - _t))
        for line in out.strip().splitlines()[-3:]:
            log("  mu300-voice:", line)
        return out

    r = start_scene()
    if "did not start" in r or "Resource busy" in r:
        log("场景启动失败，重启 DSP 后重试一次")
        for l in [x for x in sh(["mu300-audio-dsp", "start"], 120).strip().splitlines() if x.strip()][-2:]:
            log("  dsp start:", l)
        time.sleep(1)
        r = start_scene()
        if "did not start" in r or "Resource busy" in r:
            log("场景仍起不来，放弃本次通话")
            send_frame(conn, ERROR, b"scene failed")
            at("AT+CHUP", 6)
            return
    time.sleep(0.8)

    cap = spawn(CAP, CAP_FB, "arecord", "out")
    play = spawn(PLAY, PLAY_FB, "aplay", "in")
    # 关键：Linux 管道默认 64KB，按 16 kHZ/16 bit 算够积 2 秒音频！
    # 把它压到 4 KB（约 128 ms），确保我们和 aplay/arecord 之间不可能藏延迟。
    F_SETPIPE_SZ = 1031
    for _f, _n in ((play.stdin, "aplay-stdin"), (cap.stdout, "arecord-stdout")):
        try:
            _sz = fcntl.fcntl(_f.fileno(), F_SETPIPE_SZ, 4096)
            log("  %s 管道缓冲: %d 字节" % (_n, _sz))
        except OSError as e:
            log("  %s 管道缓冲设置失败: %r" % (_n, e))

    try:
        for _l in open("/tmp/aplay.err", "rb").read().decode("utf-8", "replace").splitlines():
            _l = _l.strip()
            if any(k in _l.upper() for k in ("BUFFER", "PERIOD", "RATE", "HW PARAMS")):
                log("  aplay 参数:", _l[:110])
    except OSError:
        pass
    if cap.poll() is not None or play.poll() is not None:
        log("音频节点打不开：cap=%s play=%s" % (cap.poll(), play.poll()))
        send_frame(conn, ERROR, b"pcm open failed")
        at("AT+CHUP", 6)
        return
    # stop / stat 已在拨号前定义，这里不能重建

    def uplink():                          # 基带 48k 下行 -> 8k -> Asterisk
        try:
            last = time.time()
            while not stop.is_set():
                chunk = read_exact(cap.stdout, 1920)     # 20 ms @48k

                now = time.time(); dtc = now - last; last = now
                stat["csum"] += dtc; stat["cn"] += 1
                if dtc > stat["cmax"]:
                    stat["cmax"] = dtc

                if chunk is None:
                    log("arecord 结束")
                    break
                pcm = downsample6(chunk)
                with qlock:
                    if len(outq) < 25:       # 最多积压 500ms，超出直接丢，避免延迟累积
                        outq.append(pcm)
                stat["up"] += 320
        except Exception as e:
            log("uplink 异常:", repr(e))
        finally:
            stop.set()

    _bl = {"sum": 0, "n": 0, "max": -1}
    # 有界队列：读 socket 与写 aplay 解耦。队列超过 MAXQ 帧就丢最旧的，
    # 让延迟封顶在 MAXQ*20ms，而不是随通话时长无限累积。
    MAXQ = 10
    inq = collections.deque()
    inqlock = threading.Lock()
    stat["drop"] = 0

    def downlink():                        # Asterisk 8k -> 16k -> 基带上行
        try:
            conn.settimeout(None)
            prev = 0
            while not stop.is_set():
                sb = sock_backlog(conn)
                _bl["n"] += 1
                _bl["sum"] += sb
                if sb > _bl["max"]:
                    _bl["max"] = sb
                k, l = struct.unpack("!BH", recv_exact(conn, 3))
                p = recv_exact(conn, l) if l else b""
                if k == AUDIO and p:
                    with inqlock:
                        inq.append(p)
                        _d = 0
                        while len(inq) > MAXQ:
                            inq.popleft()
                            _d += 1
                        stat["drop"] += _d
                    stat["down"] += len(p)
                elif k in (HANGUP, ERROR):
                    log("收到挂断/错误帧 0x%02x" % k)
                    break
        except Exception as e:
            log("downlink 异常:", repr(e))
        finally:
            stop.set()

    def writer():                          # 有界队列 -> aplay（16k）
        prev = 0
        while not stop.is_set():
            with inqlock:
                p = inq.popleft() if inq else None
            if p is None:
                time.sleep(0.004)
                continue
            up, prev = upsample2(p, prev)
            w0 = time.time()
            try:
                play.stdin.write(up)
                play.stdin.flush()
            except OSError as e:
                log("writer 异常:", repr(e))
                stop.set()
                return
            dt = time.time() - w0
            stat["wsum"] += dt
            stat["wn"] += 1
            if dt > stat["wmax"]:
                stat["wmax"] = dt

    for t in (threading.Thread(target=uplink, daemon=True),
              threading.Thread(target=downlink, daemon=True),
              threading.Thread(target=writer, daemon=True)):
        t.start()

    strikes = 0
    while not stop.is_set():
        time.sleep(2)
        st, ok = clcc_state()
        if st in ("0", "1"):
            strikes = 0
        elif st is None and ok:
            strikes += 1
            if strikes >= 3:
                log("通话已结束（连续 %d 次无 CLCC）" % strikes)
                break
        avg = (stat["wsum"] / stat["wn"] * 1000) if stat["wn"] else 0.0
        cavg = (stat["csum"] / stat["cn"] * 1000) if stat["cn"] else 0.0
        if not _STATUS_DUMPED[0]:
            _STATUS_DUMPED[0] = True
            for _ln in alsa_status_raw(PLAY_DEV).splitlines():
                if _ln.strip():
                    log("  驱动状态:", _ln.strip()[:100])
        ad = alsa_delay(PLAY_DEV)
        ad_s = ("驱动延迟 %d帧(%.0fms) %s" % (ad[0], ad[0] / 16.0, ad[1])) if ad else "驱动延迟 ?"
        bl_avg = (_bl["sum"] // _bl["n"]) if _bl["n"] else -1
        log("  上行 %d B / 下行 %d B | 写阻塞 均%.1f 峰%.0fms | 读间隔 均%.1f 峰%.0fms | %s | socket队列 均%d 峰%d B | 播放队列 %d 丢%d"
            % (stat["up"], stat["down"], avg, stat["wmax"] * 1000, cavg, stat["cmax"] * 1000, ad_s,
               bl_avg, _bl["max"], len(inq), stat["drop"]))
        _bl["sum"] = 0; _bl["n"] = 0; _bl["max"] = -1
        stat["wmax"] = 0.0; stat["wsum"] = 0.0; stat["wn"] = 0
        stat["cmax"] = 0.0; stat["csum"] = 0.0; stat["cn"] = 0

    stop.set()
    # 顺序很重要：先挂断电话，再收场景，最后关 PCM。
    # 反过来（先杀 arecord/aplay 再挂断）会把 DSP 的语音流掐在半路，
    # 之后 DSP 进入深层睡眠且唤不醒，下一次通话必然失败。
    st, _ = clcc_state()
    if st is not None:
        at("AT+CHUP", 6)
    try:
        send_frame(conn, HANGUP, b"")
    except OSError:
        pass
    time.sleep(0.3)
    for pr in (play, cap):
        try:
            pr.terminate()
        except OSError:
            pass
    time.sleep(0.8)
    for pr in (play, cap):
        if pr.poll() is None:
            try:
                pr.kill()
            except OSError:
                pass
    sh(["mu300-voice", "audio", "stop"], 20)
    log("已挂断并收场景")
    log("会话结束：上行 %d B / 下行 %d B" % (stat["up"], stat["down"]))

if __name__ == "__main__":
    main()
