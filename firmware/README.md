# firmware · 可能会用到的固件与安装包

这里放的是 U30 Air「开声」所需的**关键材料**，也是整个折腾过程中最难找、最容易卡死的几块。
换机、重刷、换内核时直接取用，不用再满网翻找。

## 下载

| 包 | 大小 | sha256 |
|---|---|---|
| [`mu300-audio-kit-r23.tar.gz`](mu300-audio-kit-r23.tar.gz) | 11 MB | `2df48fb2d8fe88b163c33087ffd208afa8adf3a8fdcc99f8c3485920d0229a6e` |

```sh
curl -LO https://github.com/guaizai2023/u30air-volte-sip/raw/main/firmware/mu300-audio-kit-r23.tar.gz
shasum -a 256 mu300-audio-kit-r23.tar.gz     # 应等于上表
tar xzf mu300-audio-kit-r23.tar.gz
```

## 包里有什么

```
mu300-audio-kit-r23/
├── README.md                        ← 解压后先看这个（用途/顺序/校验/硬约束）
├── agdsp.bin                        ★ AUDCP 固件（6,291,456 B）—— 没它全程无声
├── luci-app-airsim-0.1.0-r23.apk    ★ 社区音频栈（音频模块 + 工具 + LuCI）
├── airsim-install-r23.sh            官方安装脚本（已逐行审查，可用 --check 干跑）
└── audio_params/                    原厂 Android 导出的音频参数（参考用）
```

## 一句话说明每个东西为什么重要

- **`agdsp.bin`**：展锐 AUDCP 固件，DSP 的"操作系统"。原厂 Android 镜像里**根本没有**
  （`super` 全盘搜 `AUDCP` 零命中），只能从社区取。少了它，声卡要么注册失败，要么注册成功但一点声音都没有。
- **`luci-app-airsim-*.apk`**：社区重建的音频栈，含匹配 5.4 内核的音频模块与 `mu300-voice` 等工具。
  没有它就没有 `/dev/snd`，也就没有本文档主角依赖的 `hw:1,53` / `hw:1,16`。
- **`airsim-install-r23.sh`**：官方安装脚本，会校验环境、用硬编码公钥指纹验签 APK，
  再 `apk add --no-upgrade` 安装。比手动装稳。
- **`audio_params/`**：从本机 `odm_a` 分区只读导出的调音参数。一般不用装，留作换内核/调音时的基准。

## 硬约束（务必先看）

1. **语音只在 `5.4.254-gb50db5b6224c` 内核下可用**，装前 `uname -r` 确认。
2. 安装时不能有进行中的通话 / 媒体 / 短信，脚本会直接拒绝。
3. 先 `sh airsim-install-r23.sh --check` 干跑，再 `--install`。
4. 装完确认：`cat /proc/asound/cards` 出现 `sprdphone-sc2730`，`ls -l /lib/firmware/agdsp.bin` 为 6,291,456 B。

详细步骤、每个文件的 sha256、来源与免责声明，都在包里那份 `README.md`。

## 来源

| 材料 | 来源 |
|---|---|
| `agdsp.bin` | `dikeckaan/mu300-audio-firmware` releases v1.0（sha256 与上游 `FW_URL_SHA256` 逐字符一致） |
| APK + 安装脚本 | `kai-wu-cortex/airsim-mu300` releases v0.1.0-r23 |
| `audio_params/` | 本机原厂 Android `super` → LP metadata → erofs 只读导出 |

## 免责

厂商二进制版权归原厂/相应权利人所有，此处仅作**个人设备维修与自用**留档，不构成再分发授权。
刷机有风险，操作前自行备份，后果自负。
