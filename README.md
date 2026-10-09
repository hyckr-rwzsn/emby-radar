<div align="center">

# Emby-Radar

**极轻量的 Telegram Emby 福利信息监听、识别、去重与转发系统**

[![Release](https://img.shields.io/github/v/release/hyckr-rwzsn/emby-radar)](https://github.com/hyckr-rwzsn/emby-radar/releases/latest)
[![CI](https://img.shields.io/github/actions/workflow/status/hyckr-rwzsn/emby-radar/release.yml?label=release-ci)](https://github.com/hyckr-rwzsn/emby-radar/actions/workflows/release.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey)
![Low Spec](https://img.shields.io/badge/1%20Core%20512MB%20VPS-Runs%20Great-success)
[![License](https://img.shields.io/github/license/hyckr-rwzsn/emby-radar)](LICENSE)

[免责声明](#免责声明) · [快速部署](#快速部署) · [功能特性](#功能特性) · [工作流程](#工作流程) · [日常管理](#日常管理) · [配置说明](#配置说明) · [备份](#备份) · [数据与稳定性](#数据与稳定性) · [项目结构](#项目结构) · [安全说明](#安全说明)

</div>

Emby-Radar 基于 Python、Telethon 与 SQLite，监听指定 Telegram 群组或频道，自动识别 Emby 福利信息（注册码 / 抽奖 / 兑换码等），按配置规则去重后转发到指定会话。专为单核、低内存服务器设计，**1 核 512MB 即可稳定运行**。

## 免责声明

> 本项目代码主要由 **AI（大语言模型）生成与实现**。AI 生成的代码可能存在错误、安全漏洞或未预期的行为，请务必自行审查代码、评估风险后再使用。

> 本项目仅为个人学习与技术交流而制作，所有代码、脚本、配置和文档均不构成任何形式的保证。使用者应自行评估风险并承担全部后果，作者不承担因使用、部署、修改或传播本项目而产生的任何直接或间接损失。请勿用于违反 Telegram 服务条款、当地法律法规或侵害他人权益的场景。

> 本项目不提供任何技术支持承诺，仅供学习参考。

## 功能特性

| 功能 | 说明 |
|---|---|
| Telegram 监听 | 主账号静默监听配置的群组与频道，与 Bot 是否在群无关 |
| 三层过滤链 | 斩杀塔（黑名单/死刑词）→ 领域塔（意图分类）→ 信息格式门（硬规定） |
| 文本排除 | 命中排除关键词的消息，在识别前直接忽略 |
| 准确去重 | DNA 指纹 + 跨模板身份，同一活动跨群转发、改期、换模板都不重复推送 |
| 假码护栏 | 占位码 / 示例码自动拦截 |
| 优先转发 | 白名单 / VIP 快速通道，关键内容零延迟 |
| Bot 管理 | `/start` `/status` `/stats` `/test` `/log` 等命令，无需 Web 界面 |
| 克隆兜底 | Bot 不在源群时自动读文字重发 |
| 设备伪装 | 伪装成正常手机登录，支持多设备轮换防封 |
| 限流保护 | 单 worker + 令牌桶限流，避免触发 Telegram FloodWait |
| 自动恢复 | 连接断开自动重连，CPU 长时间高负载且业务停滞时才自愈重启 |
| 自动备份 | 每 12 小时自动打包 DB + 规则配置，发送到备份频道 |

## 工作流程

```mermaid
flowchart LR
    A[Telegram 群组] --> B[主账号监听]
    B --> C[斩杀塔<br/>黑名单 / 死刑词]
    C --> D[领域塔<br/>意图分类]
    D --> E[信息格式门<br/>行首字段判定]
    E --> F[假码护栏 + 时效门]
    F --> G[DNA 去重]
    G --> H[转发队列]
    H --> I[单 worker 限流转发]
    I --> J[目标频道]
```

## 快速部署

支持 Ubuntu、Debian。首次部署**必须先交互式登录一次主账号**（生成登录会话），之后即可用 systemd 无人值守运行。

### 1. 环境要求（极低，专为小 VPS 优化）

- Python 3.10+
- 一台能访问 Telegram 的服务器（Debian/Ubuntu）
- **最低配置：1 核 CPU / 512MB 内存 / 1GB 硬盘** 即可稳定运行

### 2. 获取代码并安装依赖

```bash
git clone https://github.com/hyckr-rwzsn/emby-radar.git
cd Emby-Radar
pip3 install -r requirements.txt
```

### 3. 配置凭证

```bash
cp .env.example .env
# 编辑 .env，至少填入下面四项：
#   API_ID / API_HASH                          （https://my.telegram.org 获取）
#   ADMIN_BOT_TOKEN / FORWARD_BOT_TOKEN        （@BotFather 创建，共两个 Bot）
#   TARGET_ID                                  （转发目标频道 ID）
#   OWNER_ID                                   （你的 Telegram 数字 ID）
```

### 4. 首次交互式登录（必须）

```bash
python3 tg_monitor_v9.py
# 首次运行会引导输入主账号的手机号 + 验证码完成登录
# 登录成功、看到启动日志后按 Ctrl+C 停止
```

> ⚠️ 这一步**必须在终端完成**（需要手动输入验证码）。登录会话保存在 `data/sessions/`，后续 systemd 复用，无需重复登录。

### 5. systemd 托管（可选，长期运行推荐）

```bash
sudo sh deploy_runtime.sh
# 脚本自建 venv 装依赖、安装 systemd 服务并启动，路径自动适配项目目录
sudo systemctl status tg-monitor
```

> 建议把项目放在 `/opt` 或 `/srv` 等系统目录（例如 `/opt/Emby-Radar`）；若放在 `/home` 或 `/root` 下，脚本会自动调整 systemd 安全策略以允许写入，但仍是 `/opt` 更规范。

## 日常管理

向管理 Bot 私聊发送命令：

| 命令 | 用途 |
|---|---|
| `/start` | 主控制台（图形化面板） |
| `/status` | 系统状态（CPU/内存/队列/连接） |
| `/stats` | 转发 / 拦截 / 规则命中统计 |
| `/test <文本>` | 过滤试运行（模拟分类，不触发转发） |
| `/log` | 查看拦截记录（支持翻页） |
| `/reload` | 热更新配置 |
| `/track <ID>` | 设置 VIP 免检 |
| `/mute <ID>` / `/block <ID>` | 屏蔽群组 / 用户 |
| `/backup` | 手动备份 |
| `/restart` | 重启程序 |
| `/help` | 查看全部指令 |

### 测试功能（调规则 / 验链路）

两个测试用途不同，别搞混：

| 功能 | 入口 | 作用 | 是否真发 |
|---|---|---|---|
| 过滤测试 | `/test <文本>` 或主菜单「🧪 过滤测试」 | 只跑规则层，看这段文本命中什么规则、会不会被拦 | ❌ 不发送 |
| 真实转发测试 | 主菜单「📤 真实转发测试」 | 把文本当源消息走完整链路，**真发到目标频道** | ✅ 真发 |

**真实转发测试怎么用**：主菜单点「📤 真实转发测试」→ 按提示发一段文本（120 秒内）→ 看回复。
成功会返回目标消息 ID、发送方式（`native` 原生 / `clone_*` 文本克隆 / `user_native_*` 主账号原生）、命中规则。
⚠️ 内容会真的出现在频道，需你**手动删除**（系统不自动删）；不写入去重占位，不影响正常转发。

## 配置说明

> ⚠️ 本项目开箱**不附带任何过滤规则**。部署后程序能启动、能连接、能进管理面板，
> 但需要你按自己的监控目标自行配置下面三类规则，才会开始抓取 / 转发。
> 改完配置发 `/reload` 热更新（黑白名单 / 死刑词即时生效，无需重启）。

### 一、信息格式（决定「转什么」）

信息格式 = 一组「字段」，消息**命中某个格式**才允许转发。

**在 Bot 里管理**（推荐，全程不碰代码）：发 `/start` → 高级管理 → 信息格式：

| 操作 | 按钮 | 说明 |
|---|---|---|
| 新增格式 | ➕ 新增格式 | 复制模板改成自己的发回去 |
| 编辑格式 | ✏️ 编号 / 🔢 输入序号 | 修改已有格式的关键词 |
| 删除格式 | ➖ 删除自定义 | 删掉自己加的格式 |

**新增格式的模板**（点 ➕ 后，复制改好发回去即可，180 秒内）：

```text
名称: 满人开奖抽奖
必含: 奖品|参与要求
任一: 满人开奖|目标|达到
排除: 已开奖|中奖名单
```

- **必含** = 每条消息都必须出现的字段（照消息原文填，如「奖品」「开奖条件」）
- **任一** = 至少出现一个就行（如「开奖日期」「截止时间」「满人开奖」）
- **排除** = 出现就不匹配（如「已开奖」「中奖名单」）
- **名称**与某个内置格式相同 = 给它增加识别分支（同活动多模板判重的关键）

改完立即热更新生效，用「🧪 过滤测试」贴一条真实消息验证命中。

**底层结构**（进阶，一般不用碰）：格式存在 `tg_monitor_v9.py` 的 `BUILTIN_FORWARD_FORMATS`（内置）与数据目录 `custom_forward_formats.json`（自定义），支持 `regex_any` 行首正则、`regex_min_hits` 等高级写法，照代码内 2 条示例改写即可。

### 二、黑白名单（决定「谁的」）

存 SQLite `blacklist` / `whitelist` 表，用 Bot 命令管理（发 `/start` 也有图形按钮）：

| 命令 | 作用 |
|---|---|
| `/add <用户ID>` | 加**白名单**：该用户消息直接 P0 放行，跳过全部过滤 |
| `/track <用户ID>` / `/untrack <用户ID>` | 加 / 移除 **VIP**（斩杀塔后放行） |
| `/ban <用户ID>` | 加**黑名单**：该用户消息直接丢弃 |
| `/unban <用户ID>` | 移除黑名单 |
| `/block <用户ID>` | 屏蔽用户（等价 ban） |
| `/unblock <用户ID>` | 解除屏蔽 |
| `/mute <群ID>` | 屏蔽**群**：整群消息丢弃 |
| `/unmute <群ID>` | 解除屏蔽群 |
| `/list` | 查看当前黑白名单 |

优先级：**白名单 > 黑名单**。命中白名单直接转发；命中黑名单 / 屏蔽直接丢弃。

**三类屏蔽**（比黑白名单更细，针对具体对象精准丢弃）：

| 类型 | 命令 | 效果 |
|---|---|---|
| 屏蔽用户 | `/block <用户ID>` `/unblock <用户ID>` | 该用户消息直接丢弃 |
| 屏蔽群 | `/mute <群ID>` `/unmute <群ID>` | 整群消息丢弃（垃圾群直接静音） |
| 关键词屏蔽 | 转发消息附带的「🚫 关键词屏蔽」按钮 | 一键把该发送者加黑 |

### 三、死刑词（决定「拦什么垃圾」）

`config/regex_patterns.json` → `KILL_PATTERNS` → `DEATH_WORDS`，一个字符串数组：

```json
{
  "KILL_PATTERNS": {
    "DEATH_WORDS": ["你中奖了", "恭喜你", "兑换成功", "已开奖", "机场", "欢迎.*加入"]
  }
}
```

- 消息**包含任意一个词** → 斩杀塔第一层直接拦截，不再往后看。
- 支持正则（如 `欢迎.*加入`、`套餐[：:]`）；放「通用垃圾特征词」：中奖通知、兑换回执、推广、机场节点等。
- 改完发 `/reload` 热更新，`/list_death` 查看当前死刑词。

同一个 JSON 里还能配：

| 字段 | 作用 |
|---|---|
| `KILL_PATTERNS`（其余 key） | 具体斩杀正则：推广链接、名额枯竭、资源问句等 |
| `DOMAIN_PATTERNS` | 领域分类词：邀请注册 / 抽奖 / 码类 / 注册意图 / 口令意图 |
| `CODE_PATTERNS` | 码的识别正则（决定"什么算一个码"） |

### 四、设备伪装

`config/device_profiles.json` 定义设备指纹（默认 OnePlus 13），可自行增删机型轮换防封。

## 备份

系统把核心数据自动备份到 Telegram 频道，防止服务器数据丢失。**备份频道必须设为私密**（私有频道，仅你自己可见，不要把备份发到公开群）。

### 是否需要备份

- **要备份**：在 `.env` 里配置 `BACKUP_ID`（一个**私有频道** ID），并让管理 Bot 加入该频道、设为管理员。
- **不备份**：不配置 `BACKUP_ID`（留空），系统自动跳过备份，其余功能不受影响。

### 备份方式

- **自动备份**：每 12 小时打包一次，发送到备份频道；发送失败自动回退到管理员私聊。
- **手动备份**：Bot 发 `/backup` 立即备份一次。

### 备份的全部数据内容

| 内容 | 说明 |
|---|---|
| 数据库（radar_core.db） | 转发目标 / 备份频道配置、黑白名单、DNA 去重指纹、转发 / 拦截记录、系统配置 |
| regex_patterns.json | 死刑词、领域正则、码规则 |
| custom_forward_formats.json | 自定义信息格式 |
| device_profiles.json | 设备伪装配置 |

> 数据库里的**消息正文已脱敏**（拦截日志 / 转发日志的正文清空，只留 ID 做审计），防止备份夹带消息内容。

### 不包含的内容（重要）

| 不包含 | 原因 |
|---|---|
| `.env`（API ID / Hash、Bot Token、频道 ID） | 安全，密钥不进 Telegram |
| Session 文件（登录授权） | 安全，密钥不进 Telegram |

⚠️ 因此若服务器**完全损坏**，用备份只能恢复「数据 + 规则」，**仍需手动重填 `.env` 并重新登录账号**。

## 数据与稳定性

- 内存硬限 450MB，触发 85% 自动 GC。
- 数据库上限 50MB，超限自动压缩；日志 5MB×3 自动轮转；历史记录 3 天自动清理。
- 单 worker + 令牌桶限流，触发 FloodWait 时自动退避，不重复发送。
- 连接断开自动重连；Session 失效时进入休眠并告警，不反复重连。
- CPU 看门狗只统计**本进程** CPU，连续 30 分钟高负载**且业务停滞**时才自愈重启，正常业务高峰不会误杀。
- systemd 托管，`Restart=always` 崩溃自动复活。

## 项目结构

```
Emby-Radar/
├── tg_monitor_v9.py          # 主程序（单文件，含过滤链 / 去重 / 转发 / Bot 面板）
├── config/
│   ├── regex_patterns.json   # 正则规则（首次运行自动复制到数据目录）
│   └── device_profiles.json  # 设备伪装配置
├── deploy_runtime.sh          # 部署脚本（venv + systemd + 重启）
├── tg-monitor.service         # systemd 服务文件
├── tg-monitor-journald.conf   # journald 保留上限
├── .github/workflows/release.yml  # 自动打包发布（推 v* 标签）
├── .env.example               # 凭证模板
├── requirements.txt           # Python 依赖
├── README.md
└── LICENSE
```

## 安全说明

仓库不包含 `.env`、密码、Token、Telegram Session、数据库、日志或备份。部署时不要将这些运行时文件提交到 Git。

## 许可证

本项目采用 [MIT License](./LICENSE)。
