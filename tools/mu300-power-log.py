#!/usr/bin/env python3
"""mu300-power-log -- 记录电池的充电/放电过程。

为什么要单独记：
    OpenWrt 的 logread 和 dmesg 全在内存里，掉电即清零。2026-11-16 设备因电池耗尽
    关机，事后想反推「什么时候拔的电、撑了几个小时、最后一通电话是不是压垮它的」，
    什么证据都没留下，只能靠猜。这个服务就是把这个信息补上。

记录策略（省闪存）：
    - 状态变化（charging / discharging / full）立刻写一条
    - 整数电量变化 1% 时写一条
    - 其余情况每 30 分钟写一条心跳
    正常使用一天约 200~400 行，几 KB。

字段坑（实测）：
    /sys/class/power_supply/ac/online        插着电也报 0，不可用
    /sys/class/power_supply/battery/capacity 与 sc27xx-fgu 差很多（2% vs 26%），
                                             sc27xx-fgu 才是真正的电量计
    所以插电看 usb/online，电量看 sc27xx-fgu。
"""

import json
import os
import subprocess
import time

LOG = os.environ.get("MU300_POWER_LOG", "/etc/mu300/power.log")
MAX_BYTES = 256 * 1024
INTERVAL = int(os.environ.get("MU300_POWER_INTERVAL", "60"))
HEARTBEAT = 1800                       # 无变化时也至少半小时留一行
LOW_WARN = int(os.environ.get("MU300_POWER_LOW", "20"))
TOKEN_FILE = os.environ.get("MU300_YAK_TOKEN_FILE", "/etc/mu300/yak-push.token")
PUSH_URL = os.environ.get("MU300_YAK_PUSH_URL", "https://push.yakteam.com/v1/notify")

FGU = "/sys/class/power_supply/sc27xx-fgu"
BAT = "/sys/class/power_supply/battery"
USB = "/sys/class/power_supply/usb"


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def number(path, scale=1.0, default=0.0):
    try:
        return float(read(path, "0")) / scale
    except (TypeError, ValueError):
        return default


def sample():
    """取一次电量快照。电量优先用 fgu，缺失时退回 battery。"""
def read_capacity():
    """电量百分比（0~100）。

    踩过的坑：`sc27xx-fgu/capacity` 靠不住——它和 cycle_count 几乎同值
    （实测 978 vs 985），充满之后会溢出成三位数，日志因此写进了一堆 cap=985%。
    `battery/capacity` 才是驱动聚合出来的正确百分比，所以优先用它，
    并且只接受 0~100 的读数，异常值直接跳过。
    """
    for path in (BAT + "/capacity", FGU + "/capacity"):
        try:
            n = int(read(path))
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 100:
            return str(n)
    return "?"


def sample():
    """取一次电量快照。"""
    cap = read_capacity()
    volt = number(FGU + "/voltage_now", 1e6) or number(BAT + "/voltage_now", 1e6)
    cur = number(BAT + "/current_now", 1000.0) or number(FGU + "/current_now", 1000.0)
    plugged = read(USB + "/online", "0") == "1"
    return {"cap": cap, "volt": volt, "cur": cur, "plugged": plugged}


def state_of(s):
    """插电时的三种状态。

    判满用电压而不是电量百分比：这台机器的电量计虚高，3.84 V（实际约 45%）就报 100%，
    按百分比判满会在刚开始充的时候就记成 full（实测 09:58 就误报了）。
    锂电池 4.15 V 以上且电流降下来，才算真的充满。
    """
    if not s["plugged"]:
        return "discharging"
    if s["volt"] >= 4.15 and s["cur"] < 150:
        return "full"
    return "charging" if s["cur"] > 20 else "plugged"


def trim():
    """超过上限就砍掉前一半，保留最近的记录。"""
    try:
        if os.path.getsize(LOG) <= MAX_BYTES:
            return
        with open(LOG) as f:
            lines = f.readlines()
        with open(LOG, "w") as f:
            f.writelines(lines[len(lines) // 2:])
    except OSError:
        pass


def write_log(line):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        trim()
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(text):
    """低电量提醒。设备 Python 缺 idna，走 curl。"""
    token = read(TOKEN_FILE)
    if not token:
        return
    payload = {
        "token": token,
        "caller_uri": "sip:power@mu300",
        "caller_name": text,
        "type": "message",
        "call_id": "pw-%d" % int(time.time()),
    }
    try:
        subprocess.run(
            ["curl", "-sS", "-m", "8", "-X", "POST", PUSH_URL,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload, ensure_ascii=False)],
            capture_output=True, timeout=12)
    except Exception:                                  # noqa: BLE001
        pass


def main():
    last = None
    last_state = None
    last_write = 0.0
    warned = False

    while True:
        s = sample()
        st = state_of(s)
        now = time.time()

        cap_changed = last is not None and last["cap"] != s["cap"]
        st_changed = last_state is not None and last_state != st
        if cap_changed or st_changed or (now - last_write) > HEARTBEAT or last is None:
            line = "%s  cap=%3s%%  %.3fV  %6.0fmA  %-11s %s" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), s["cap"], s["volt"],
                s["cur"], st, "AC" if s["plugged"] else "BAT")
            if st_changed:
                line += "   << %s -> %s" % (last_state, st)
            write_log(line)
            last_write = now

        # 未插电且电量偏低：提醒一次，回到 25% 以上再重新武装
        try:
            cap_n = int(s["cap"])
        except (TypeError, ValueError):
            cap_n = 100
        if not s["plugged"] and cap_n <= LOW_WARN and not warned:
            notify("设备电量 %d%%，未插电，随时可能关机" % cap_n)
            write_log("%s  !! 低电量告警 %d%%" % (time.strftime("%Y-%m-%d %H:%M:%S"), cap_n))
            warned = True
        elif s["plugged"] or cap_n > LOW_WARN + 5:
            warned = False

        last, last_state = s, st
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
