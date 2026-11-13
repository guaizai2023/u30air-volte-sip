#!/usr/bin/env python3
"""U30 Air SIP 最小闭环验证：REGISTER -> INVITE 600(Echo) -> RTP 回环 -> BYE"""
import hashlib, os, random, re, socket, struct, sys, time

SERVER = "192.168.77.1"
SPORT = 5060
USER = os.environ.get("SIP_USER", "6001")
PASS = os.environ.get("SIP_PASS", "u30air-6001")
TARGET = os.environ.get("SIP_TARGET", "600")

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", 0))
s.settimeout(2.0)
LOCAL_IP, LPORT = s.getsockname()
if LOCAL_IP.startswith("0.0.0.0"):
    t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); t.connect((SERVER, SPORT)); LOCAL_IP = t.getsockname()[0]; t.close()

BRANCH = "z9hG4bK%08x" % random.getrandbits(32)
CALLID = "%08x%08x@u30lab" % (random.getrandbits(32), random.getrandbits(32))
FTAG = "%08x" % random.getrandbits(32)
URI = "sip:%s" % SERVER
log = []
def P(*a):
    line = " ".join(str(x) for x in a); print(line); log.append(line)

def send(msg):
    s.sendto(msg.encode(), (SERVER, SPORT))

def recv_all(dur=2.0):
    """收集响应，返回 [(代码, 原始文本)]"""
    out, end = [], time.time() + dur
    while time.time() < end:
        s.settimeout(max(0.05, end - time.time()))
        try:
            data, addr = s.recvfrom(8192)
        except socket.timeout:
            break
        if data[:1] == b"\x80":      # RTP，非 SIP
            continue
        txt = data.decode("utf-8", "replace")
        m = re.match(r"SIP/2\.0 (\d{3})", txt)
        out.append((int(m.group(1)) if m else 0, txt))
        if out and out[-1][0] >= 200:
            # 200 之后可能还有重传，稍等即可返回
            break
    return out

def hdr(txt, name):
    m = re.search(r"^%s:\s*(.+)$" % re.escape(name), txt, re.M | re.I)
    return m.group(1).strip() if m else ""

def digest(method, uri, chal):
    realm = re.search(r'realm="?([^",]+)"?', chal).group(1)
    nonce = re.search(r'nonce="?([^",]+)"?', chal).group(1)
    qop = (re.search(r'qop="?([^",]+)"?', chal) or [None, ""])[1]
    opaque = (re.search(r'opaque="?([^",]+)"?', chal) or [None, None])[1]
    ha1 = hashlib.md5(f"{USER}:{realm}:{PASS}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if "auth" in qop:
        nc, cnonce = "00000001", "%08x" % random.getrandbits(32)
        resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        a = (f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
             f'response="{resp}", algorithm=MD5, qop=auth, nc={nc}, cnonce="{cnonce}"')
    else:
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        a = (f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
             f'response="{resp}", algorithm=MD5')
    if opaque:
        a += f', opaque="{opaque}"'
    return a

def do(method, requri, extra="", auth=None, body="", cseq=1, totag=None, to_uri=None):
    to_uri = to_uri or requri
    branch = "z9hG4bK%08x%04x" % (random.getrandbits(32), cseq)
    lines = [f"{method} {requri} SIP/2.0",
             f"Via: SIP/2.0/UDP {LOCAL_IP}:{LPORT};branch={branch};rport",
             "Max-Forwards: 70",
             f"From: <sip:{USER}@{SERVER}>;tag={FTAG}",
             f"To: <{to_uri}>" + (f";tag={totag}" if totag else ""),
             f"Call-ID: {CALLID}",
             f"CSeq: {cseq} {method}",
             f"Contact: <sip:{USER}@{LOCAL_IP}:{LPORT}>",
             "User-Agent: u30lab-sipprobe/1.0",
             "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER"]
    if auth: lines.append(f"Authorization: {auth}")
    if extra: lines.append(extra)
    if body:
        lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
    else:
        lines.append("Content-Length: 0")
    send("\r\n".join(lines) + "\r\n\r\n" + body)

def trace(method, requri, extra="", body="", cseq=1, totag=None, to_uri=None):
    do(method, requri, extra, None, body, cseq, totag, to_uri)
    r = recv_all(2.5)
    if r and r[0][0] == 401:
        chal = hdr(r[0][1], "WWW-Authenticate")
        P(f"  <- 401 需要认证 ({method})")
        do(method, requri, extra, digest(method, requri, chal), body, cseq, totag, to_uri)
        r = recv_all(3.0)
    return r

P("=" * 62)
P(f"目标 Asterisk  : {SERVER}:{SPORT}")
P(f"本机            : {LOCAL_IP}:{LPORT}  (SIP+RTP 同端口)")
P(f"测试分机        : {USER}  目标: {TARGET}")
P("=" * 62)

# ---------- 1. OPTIONS ----------
P("\n[1] OPTIONS 探测")
r = trace("OPTIONS", URI, cseq=1)
P("  结果:", ", ".join(str(c) for c, _ in r) or "无响应")
opt_ok = any(c in (200, 405, 401) for c, _ in r)

# ---------- 2. REGISTER ----------
P("\n[2] REGISTER 注册")
r = trace("REGISTER", URI, extra="Expires: 300", cseq=2, to_uri=f"sip:{USER}@{SERVER}")
codes = [c for c, _ in r]
P("  结果:", codes)
reg_ok = 200 in codes
if reg_ok:
    P("  ✓ 注册成功 200 OK")

# ---------- 3. INVITE ----------
sdp = ("v=0\r\no=- 1 1 IN IP4 %s\r\ns=u30lab\r\nc=IN IP4 %s\r\nt=0 0\r\n"
       "m=audio %d RTP/AVP 0 8\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n") % (LOCAL_IP, LOCAL_IP, LPORT)
P(f"\n[3] INVITE sip:{TARGET}  (Echo 测试)")
r = trace("INVITE", f"sip:{TARGET}@{SERVER}", body=sdp, cseq=3)
codes = [c for c, _ in r]
P("  结果:", codes)
to_tag = None
rtp_port = None
for c, txt in r:
    if c in (180, 183, 200):
        tg = re.search(r"To:[^\r\n]*?;tag=([0-9a-zA-Z\-]+)", txt, re.I)
        if tg: to_tag = tg.group(1)
    if c == 200:
        m = re.search(r"m=audio (\d+)", txt)
        if m: rtp_port = int(m.group(1))
invite_ok = 200 in codes
if invite_ok:
    P(f"  ✓ 呼叫建立 200 OK，对方 RTP 端口 {rtp_port}, To-tag {to_tag}")

# ---------- 4. ACK ----------
if invite_ok:
    P("\n[4] ACK")
    do("ACK", f"sip:{TARGET}@{SERVER}", cseq=3, totag=to_tag)
    P("  ✓ ACK 已发送")

# ---------- 5. RTP 回环 ----------
rtp_rx = 0; rtp_tx = 0; payloads = []
if invite_ok and rtp_port:
    P("\n[5] RTP 回环测试（PCMU 1kHz 音，3 秒）")
    dst = (SERVER, rtp_port)
    seq, ts, ssrc = random.getrandbits(16), random.getrandbits(32), random.getrandbits(32)
    t0 = time.time()
    nextt = t0
    while time.time() - t0 < 3.0:
        n = int((time.time() - t0) * 8000)
        samp = int(12000 * __import__("math").sin(2 * 3.14159265358979 * 1000 * n / 8000))
        pl = b"\xff" * 160 if samp > 0 else b"\x7f" * 160   # PCMU 正/负半周
        h = struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
        s.sendto(h + pl, dst); rtp_tx += 1
        seq += 1; ts += 160
        nextt += 0.02
        # 非阻塞收
        s.settimeout(0.001)
        try:
            while True:
                d, a = s.recvfrom(8192)
                if d[:1] == b"\x80" and len(d) > 12:
                    rtp_rx += 1
                    if len(payloads) < 5: payloads.append(d[12:16].hex())
        except socket.timeout:
            pass
        slp = nextt - time.time()
        if slp > 0: time.sleep(slp)
    s.settimeout(0.3)
    for _ in range(40):
        try:
            d, a = s.recvfrom(8192)
            if d[:1] == b"\x80" and len(d) > 12:
                rtp_rx += 1
        except socket.timeout:
            break
    P(f"  发送 RTP {rtp_tx} 包 / 收到 RTP {rtp_rx} 包")
    P(f"  回环样本(前5包载荷前4字节): {payloads}")
    rtp_ok = rtp_rx > 10
    P("  " + ("✓ 双向 RTP 通（Echo 回送确认）" if rtp_ok else "✗ 未收到回送 RTP"))
else:
    rtp_ok = False

# ---------- 6. BYE ----------
if invite_ok:
    P("\n[6] BYE 挂断")
    r = trace("BYE", f"sip:{TARGET}@{SERVER}", cseq=4, totag=to_tag)
    P("  结果:", [c for c, _ in r])
    bye_ok = any(c == 200 for c, _ in r)
else:
    bye_ok = False

P("\n" + "=" * 62)
P("结论")
P(f"  信令可达(OPTIONS) : {'PASS' if opt_ok else 'FAIL'}")
P(f"  注册认证(REGISTER): {'PASS' if reg_ok else 'FAIL'}")
P(f"  呼叫建立(INVITE)  : {'PASS' if invite_ok else 'FAIL'}")
P(f"  媒体通路(RTP)     : {'PASS' if rtp_ok else 'FAIL'}")
P(f"  正常挂断(BYE)     : {'PASS' if bye_ok else 'FAIL'}")
P("=" * 62)
open("/tmp/sip_verify_result.txt", "w").write("\n".join(log))
sys.exit(0 if all([opt_ok, reg_ok, invite_ok, rtp_ok, bye_ok]) else 1)
