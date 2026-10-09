#!/usr/bin/env python3
"""
emby-radar - 极轻量的 Telegram Emby 福利信息监听、识别、去重与转发系统
目标：1核512MB/1GB硬盘 VPS 上的绝对稳定运行
"""

import os
import re
import sys
import unicodedata
import copy
import time
import json
import shutil
import sqlite3
import asyncio
import hashlib
import gc
import faulthandler
import io
import random
import logging
import zipfile
import psutil
import urllib.request
import regex as regex_engine
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from collections import deque
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events, errors, Button
from telethon.tl import types as tl_types
from telethon.tl.functions import PingRequest

# ================= 路径配置 =================
BASE_DIR = Path(__file__).parent
APP_VERSION = "1.0.0"

# 数据目录：环境变量覆盖 > Linux 默认 /app/tg_monitor_data/ > 代码目录下的 data/
MONITOR_DATA_DIR = Path(os.environ.get("TG_MONITOR_DATA_DIR", ""))
if not MONITOR_DATA_DIR.parts:
    MONITOR_DATA_DIR = Path("/app/tg_monitor_data") if Path("/app").exists() else BASE_DIR / "data"

CONFIG_DIR = BASE_DIR / "config"               # 代码目录的 config（正则模板）
DATA_CONFIG_DIR = MONITOR_DATA_DIR / "config"   # 数据目录的 config（运行时副本）
SESSIONS_DIR = MONITOR_DATA_DIR / "sessions"

DB_FILE = MONITOR_DATA_DIR / "radar_core.db"
SESSION_FILE = SESSIONS_DIR / "purifier_session"
ADMIN_SESSION_FILE = SESSIONS_DIR / "admin_session"
FORWARD_SESSION_FILE = SESSIONS_DIR / "forward_session"
LOG_FILE = MONITOR_DATA_DIR / "radar.log"
REGEX_CONFIG_FILE = DATA_CONFIG_DIR / "regex_patterns.json"   # 运行时使用数据目录副本
CUSTOM_FORMAT_CONFIG_FILE = DATA_CONFIG_DIR / "custom_forward_formats.json"
DEVICE_CONFIG_FILE = DATA_CONFIG_DIR / "device_profiles.json"

# ================= 自动创建数据目录 + 首次迁移 =================
for _d in [MONITOR_DATA_DIR, DATA_CONFIG_DIR, SESSIONS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)


def _startup_print(message):
    """Print early startup messages without crashing on non-UTF consoles."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or 'utf-8'
        safe = str(message).encode(encoding, errors='replace').decode(encoding, errors='replace')
        print(safe)


# 首次运行迁移：代码目录有旧文件时自动复制到数据目录
_migration_map = {
    BASE_DIR / ".env": MONITOR_DATA_DIR / ".env",
    CONFIG_DIR / "device_profiles.json": DATA_CONFIG_DIR / "device_profiles.json",
    BASE_DIR / "radar_core.db": DB_FILE,
    BASE_DIR / "purifier_session.session": SESSIONS_DIR / "purifier_session.session",
    BASE_DIR / "admin_session.session": SESSIONS_DIR / "admin_session.session",
    BASE_DIR / "forward_session.session": SESSIONS_DIR / "forward_session.session",
}
for _src, _dst in _migration_map.items():
    if _src.exists() and not _dst.exists():
        shutil.copy2(str(_src), str(_dst))
        _startup_print(f"📦 迁移: {_src.name} → {_dst}")

# 正则模板只在数据目录缺失时初始化；运行中通过 Bot 修改的规则必须跨重启保留
_regex_tpl = CONFIG_DIR / "regex_patterns.json"
if _regex_tpl.exists() and not REGEX_CONFIG_FILE.exists():
    shutil.copy2(str(_regex_tpl), str(REGEX_CONFIG_FILE))


def _secure_runtime_permissions():
    """Best-effort Linux permission hardening for credentials and sessions."""
    if os.name != 'posix':
        return
    for _dir in (MONITOR_DATA_DIR, DATA_CONFIG_DIR, SESSIONS_DIR):
        if _dir.exists():
            os.chmod(_dir, 0o700)

    sensitive_files = [
        MONITOR_DATA_DIR / ".env",
        DB_FILE,
        LOG_FILE,
        REGEX_CONFIG_FILE,
        CUSTOM_FORMAT_CONFIG_FILE,
        DEVICE_CONFIG_FILE,
    ]
    sensitive_files.extend(SESSIONS_DIR.glob("*.session*"))
    for _file in sensitive_files:
        try:
            if _file.exists():
                os.chmod(_file, 0o600)
        except OSError as exc:
            _startup_print(f"权限加固失败: {_file.name}: {exc}")


_secure_runtime_permissions()

# ================= 加载 .env 凭证 =================
load_dotenv(MONITOR_DATA_DIR / ".env")

# ================= 环境变量解析工具 =================
def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(name, default, min_value=None, max_value=None):
    raw = os.environ.get(name)
    try:
        value = int(str(raw if raw is not None else default).strip())
    except (TypeError, ValueError):
        value = int(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _env_float(name, default, min_value=None, max_value=None):
    raw = os.environ.get(name)
    try:
        value = float(str(raw if raw is not None else default).strip())
    except (TypeError, ValueError):
        value = float(default)
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


# ================= 核心密钥（从环境变量读取）=================
NODE_NAME = os.environ.get("NODE_NAME", "TG-Monitor").strip()
API_ID = _env_int("API_ID", 0, 0)
API_HASH = os.environ.get("API_HASH", "").strip()
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
FORWARD_BOT_TOKEN = os.environ.get("FORWARD_BOT_TOKEN", "").strip()
FORWARD_BOT_TOKENS_RAW = os.environ.get("FORWARD_BOT_TOKENS", "").strip()
TRUSTED_SENDER_ID = os.environ.get("TRUSTED_SENDER_ID", "").strip()  # 特权发送者（人工审核训练场）
TG_ANDROID_APP_VERSION = os.environ.get("TG_ANDROID_APP_VERSION", "").strip()
TG_DESKTOP_APP_VERSION = os.environ.get("TG_DESKTOP_APP_VERSION", "").strip()
FORWARD_MODE = os.environ.get("FORWARD_MODE", "bot_native").strip().lower()
ALLOW_BOT_CLONE_FALLBACK = _env_bool("ALLOW_BOT_CLONE_FALLBACK", True)
ALLOW_USER_NATIVE_FALLBACK = _env_bool("ALLOW_USER_NATIVE_FALLBACK", False)
ALLOW_PROTECTED_CLONE = _env_bool("ALLOW_PROTECTED_CLONE", False)
ALLOW_PROTECTED_TEXT_CLONE = _env_bool("ALLOW_PROTECTED_TEXT_CLONE", True)
ALLOW_MEDIA_CLONE = _env_bool("ALLOW_MEDIA_CLONE", False)
ALLOW_SAFE_MEDIA_CLONE_FALLBACK = _env_bool("ALLOW_SAFE_MEDIA_CLONE_FALLBACK", True)
SEND_PORTAL_MESSAGE = _env_bool("SEND_PORTAL_MESSAGE", True)
MAX_FORWARD_RETRIES = _env_int("MAX_FORWARD_RETRIES", 1, 0, 3)
BOT_SOURCE_CACHE_TTL = _env_int("BOT_SOURCE_CACHE_TTL", 1800, 60, 86400)
BOT_SOURCE_NEG_TTL = _env_int("BOT_SOURCE_NEG_TTL", 300, 30, 3600)
ENABLE_DEVICE_ROTATION = _env_bool("ENABLE_DEVICE_ROTATION", False)
ENABLE_OWNER_PRIVATE_FORWARD_TEST = _env_bool("ENABLE_OWNER_PRIVATE_FORWARD_TEST", False)
TG_CONNECT_TIMEOUT = _env_int("TG_CONNECT_TIMEOUT", 30, 5, 120)
TG_STARTUP_TIMEOUT = _env_int("TG_STARTUP_TIMEOUT", 45, 10, 180)
TG_START_RETRIES = _env_int("TG_START_RETRIES", 3, 1, 10)

VALID_FORWARD_MODES = {"bot_native", "bot_native_clone_fallback", "user_native_only"}

if FORWARD_MODE not in VALID_FORWARD_MODES:
    FORWARD_MODE = "bot_native"

if FORWARD_MODE == "bot_native_clone_fallback":
    ALLOW_BOT_CLONE_FALLBACK = True
ALLOW_PROTECTED_CLONE = ALLOW_BOT_CLONE_FALLBACK and ALLOW_PROTECTED_CLONE
ALLOW_PROTECTED_TEXT_CLONE = ALLOW_BOT_CLONE_FALLBACK and ALLOW_PROTECTED_TEXT_CLONE
ALLOW_MEDIA_CLONE = ALLOW_BOT_CLONE_FALLBACK and ALLOW_MEDIA_CLONE
ALLOW_SAFE_MEDIA_CLONE_FALLBACK = ALLOW_BOT_CLONE_FALLBACK and ALLOW_SAFE_MEDIA_CLONE_FALLBACK

def _parse_forward_tokens(primary_token, raw_tokens):
    tokens = []
    if primary_token:
        tokens.append(primary_token)
    if raw_tokens:
        for t in raw_tokens.replace('\n', ',').split(','):
            t = t.strip()
            if t and t not in tokens:
                tokens.append(t)
    return tokens


FORWARD_BOT_TOKENS = _parse_forward_tokens(FORWARD_BOT_TOKEN, FORWARD_BOT_TOKENS_RAW)

if not API_ID or not API_HASH or not ADMIN_BOT_TOKEN or not FORWARD_BOT_TOKENS:
    raise SystemExit("❌ 致命错误：API_ID / API_HASH / ADMIN_BOT_TOKEN / FORWARD_BOT_TOKEN(S) 未设置！请检查 .env 文件。")


# ================= 规则对象（可配置）=================
@dataclass(frozen=True)
class RuleObject:
    name: str
    pattern: str
    action: str = "monitor"    # monitor / exclude
    layer: str = "domain"      # domain / kill / whitelist
    priority: str = "P2-中优"
    enabled: bool = True


DEFAULT_RULE_OBJECTS = (
    RuleObject(name="售卖广告", pattern="KILL_PURCHASE_AD", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="交易市场广告", pattern="KILL_TRADE_MARKET_BOARD", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="金融开卡推广", pattern="KILL_FINANCIAL_CARD_PROMO", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="订阅节点推广", pattern="KILL_SUBSCRIPTION_NODE_PROMO", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="邀请码营销推广", pattern="KILL_INVITE_CODE_PROMO", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="未开放注册通知", pattern="KILL_REGISTRATION_UNAVAILABLE", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="资源兑换卡片", pattern="KILL_RESOURCE_REDEMPTION_CARD", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="资源问句", pattern="KILL_RESOURCE_QUESTION", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="欢迎码广告", pattern="KILL_WELCOME_INVITE_AD", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="抽奖参与回执", pattern="KILL_LOTTERY_JOIN_ACK", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="审查静默回执", pattern="KILL_MODERATION_FEEDBACK", action="exclude", layer="kill", priority="P0-排除"),
    RuleObject(name="邀请注册", pattern="DOMAIN_INVITE_KEYWORD"),
    RuleObject(name="批量发码", pattern="DOMAIN_BULK_CODE"),
    RuleObject(name="码类内容", pattern="DOMAIN_CODE_KEYWORD"),
    RuleObject(name="抽奖", pattern="DOMAIN_LOTTERY_KEYWORD"),
    RuleObject(name="注册意图", pattern="DOMAIN_REG_INTENT"),
    RuleObject(name="口令意图", pattern="DOMAIN_ACTION_INTENT"),
)


def _priority_rank(priority):
    """Parse P0/P1/P2/P3 style priority labels for deterministic rule ordering."""
    m = re.search(r'P(\d+)', str(priority or ''), re.IGNORECASE)
    return int(m.group(1)) if m else 99


def _best_priority(fallback, candidates):
    best = fallback
    best_rank = _priority_rank(fallback)
    for item in candidates:
        rank = _priority_rank(item)
        if rank < best_rank:
            best = str(item)
            best_rank = rank
    return best


BUILTIN_FORWARD_FORMATS = (
    # 示例格式（开源版）—— 仅演示「信息格式」如何配置，真实格式已移除。
    # 每条格式 = 一组「行首字段」正则（regex_any，字段须出现在行首）+ 可选字段（any）+ 排除词（exclude）。
    # 部署后请按自己监控的内容类型，参照这两条示例自行增删。
    {
        "category": "抽奖",
        "name": "口令抽奖（示例）",
        "all": (),
        "regex_any": (
            r"(?m)^[ \t]*[^0-9A-Za-z\u4e00-\u9fa5]{0,6}?抽奖口令[ \t]*[：:]?",
            r"(?m)^[ \t]*[^0-9A-Za-z\u4e00-\u9fa5]{0,6}?奖品[ \t]*[：:]?",
        ),
        "regex_min_hits": 2,
        "any": ("开奖日期", "截止时间", "开奖时间"),
        "exclude": ("已开奖", "中奖名单"),
    },
    {
        "category": "注册码",
        "name": "注册码（示例）",
        "all": (),
        "regex_any": (
            r"(?m)^[ \t]*[^0-9A-Za-z\u4e00-\u9fa5]{0,6}?注册码[ \t]*[：:]?",
        ),
        "regex_min_hits": 1,
        "any": (),
        "exclude": (),
    },
)
LOTTERY_FORWARD_FORMAT_NAMES = frozenset(
    fmt["name"] for fmt in BUILTIN_FORWARD_FORMATS if fmt.get("category") == "抽奖"
)
REGISTRATION_FORWARD_FORMAT_NAMES = frozenset(
    fmt["name"] for fmt in BUILTIN_FORWARD_FORMATS if fmt.get("category") == "注册"
)
CODE_FORWARD_FORMAT_NAMES = frozenset(
    fmt["name"] for fmt in BUILTIN_FORWARD_FORMATS if fmt.get("category") == "注册码"
)

# ================= 资源限制（从环境变量读取，有默认值）=================
MAX_HISTORY_LINES = _env_int("MAX_HISTORY_LINES", 3000, 500, 10000)
LOTTERY_DEDUP_DAYS = _env_int("LOTTERY_DEDUP_DAYS", 10, 7, 14)
MAX_LOTTERY_HISTORY = _env_int("MAX_LOTTERY_HISTORY", 3000, 500, 10000)
MAX_RAM_MB = _env_int("MAX_RAM_MB", 400, 128, 450)
MAX_DB_SIZE_MB = _env_int("MAX_DB_SIZE_MB", 50, 10, 50)
MAX_LOG_SIZE_MB = _env_int("MAX_LOG_SIZE_MB", 5, 1, 5)
LOG_BACKUP_COUNT = _env_int("LOG_BACKUP_COUNT", 3, 1, 3)
DB_CLEANUP_DAYS = _env_int("DB_CLEANUP_DAYS", 3, 1, 30)
CLEANUP_INTERVAL = _env_int("CLEANUP_INTERVAL", 3600, 600, 86400)
MAX_INTERCEPT_LOG = _env_int("MAX_INTERCEPT_LOG", 500, 50, 2000)
MAX_FORWARD_LOG = _env_int("MAX_FORWARD_LOG", 500, 50, 2000)
MAX_MESSAGE_AGE_SECONDS = _env_int("MAX_MESSAGE_AGE_SECONDS", 1800, 60, 86400)
# V11.06：转发来源的"宽松"上限——**只有上游原始发布时间超过它才拦**（防陈旧卡片重放），
# 消息自身时效改用"在本群的出现时间"判断（_message_activity_datetime）。默认 24 小时。
FORWARD_ORIGIN_MAX_AGE_SECONDS = _env_int("FORWARD_ORIGIN_MAX_AGE_SECONDS", 86400, 1800, 604800)
MAX_QUEUE_DELAY_SECONDS = _env_int("MAX_QUEUE_DELAY_SECONDS", 900, 30, MAX_MESSAGE_AGE_SECONDS)
MAX_CLONE_MEDIA_MB = _env_int("MAX_CLONE_MEDIA_MB", 4, 1, 12)
MAX_CLONE_MEDIA_BYTES = MAX_CLONE_MEDIA_MB * 1024 * 1024
SQLITE_MMAP_MB = _env_int("SQLITE_MMAP_MB", 32, 0, 128)
SQLITE_MMAP_BYTES = SQLITE_MMAP_MB * 1024 * 1024

START_TIME = time.monotonic()
FORWARDED_COUNT = 0
FORWARDED_TODAY = 0
INTERCEPTED_TODAY = 0
LAST_DAY_RESET = time.strftime('%Y-%m-%d')
rule_hits = {}  # {pattern_name: hit_count}
RUNNING_DEVICE_PROFILE = {}

# 转发成功日志短缓存；管理面板以 SQLite forward_log 为准，避免重启后丢记录。
forwarded_log = deque(maxlen=100)

# V11.10：统一时区——日志时间戳一律用**北京时间**（UTC+8）。服务器系统时钟是 UTC，
# 此前排查时因时区错位（看日志要减 8 小时）导致搜索窗口全错，故作显式转换，不依赖系统 TZ。
_BJ_TZ = timezone(timedelta(hours=8))

# ================= 日志系统（自动滚动）=================
def setup_logging():
    logger = logging.getLogger('TGMonitor')
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%m-%d %H:%M:%S'
    )
    # V11.10：日志时间戳用北京时间（覆盖 Formatter 默认的 time.localtime，不依赖系统 TZ）
    formatter.converter = lambda secs: datetime.fromtimestamp(secs, _BJ_TZ).timetuple()

    # 文件处理器 - 自动滚动
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_LOG_SIZE_MB * 1024 * 1024,
        backupCount=LOG_BACKUP_COUNT,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    # 控制台处理器
    console_handler = logging.StreamHandler()
    # radar.log keeps full INFO history; journald only needs actionable warnings.
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

logger = setup_logging()
_secure_runtime_permissions()

# 后台任务强引用集合：asyncio 事件循环只持有任务的弱引用，
# 不保存返回值时任务可能在执行途中被 GC 回收（官方文档明确警告）。
_BACKGROUND_TASKS = set()


def _log_background_failure(task):
    """让后台任务的异常落在日志里，而不是只留一条 asyncio 的 never-retrieved 警告。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"❌ 后台任务异常退出: {exc!r}")


def _spawn(coro):
    """启动后台任务并保留强引用，避免任务被提前回收、异常无人接收。"""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    task.add_done_callback(_log_background_failure)
    return task


async def _sleep_forever():
    """致命错误后的永久休眠：不返回，从而不被 supervisor 当作「意外结束」重启。"""
    await asyncio.Event().wait()


def _migrate_regex_config_file():
    """Apply narrow compatibility fixes without overwriting user-edited rules.

    开源版：内置规则迁移已移除（规则由部署者自行配置），本函数保留为空实现，
    避免把作者的历史规则注入空配置。
    """
    return


RPC_ERROR_CN = {
    "AuthKeyUnregisteredError": "会话已失效（Session 被 Telegram 注销）",
    "UnauthorizedError": "授权失败（可能掉线或密码/两步验证问题）",
    "SessionPasswordNeededError": "需要两步验证密码",
    "PhoneCodeInvalidError": "验证码无效",
    "PhoneCodeExpiredError": "验证码已过期",
    "ChannelPrivateError": "目标频道不可见或无访问权限",
    "ChannelInvalidError": "频道 ID 无效",
    "ChatIdInvalidError": "聊天 ID 无效",
    "PeerIdInvalidError": "Peer 无效（目标或来源不可见）",
    "ChatWriteForbiddenError": "没有发言权限",
    "ChatAdminRequiredError": "缺少管理员权限",
    "UserBannedInChannelError": "账号在频道中被限制发言",
    "MessageIdInvalidError": "消息 ID 无效或不可转发",
    "ChatForwardsRestrictedError": "源消息禁止转发",
    # ↓ V11.01 补齐（借鉴 TelegramMonitor 的 TelegramRpcErrors.zh-CN 错误码字典）
    "PeerFloodError": "对同一会话请求过于频繁（易被 Telegram 判定为滥用）",
    "FloodPremiumError": "触发高级账号限流，需长时间等待",
    "SlowModeWaitError": "命中群组慢速模式（发言间隔太短）",
    "AuthKeyInvalidError": "会话密钥失效，需重新登录",
    "SessionRevokedError": "会话被撤销（可能已在别处登录），需重新登录",
    "RpcCallFailError": "RPC 调用失败（多为网络不稳，稍后重试）",
    "InterdcCallError": "跨数据中心调用失败（网络/链路问题）",
    "TimeoutError": "请求超时（网络问题）",
    "ServerError": "Telegram 服务端错误（稍后重试）",
    "UsernameInvalidError": "用户名格式无效",
    "UsernameNotOccupiedError": "用户名不存在",
    "UsernameOccupiedError": "用户名已被占用",
    "PhoneNumberInvalidError": "手机号格式无效",
    "PhoneNumberBannedError": "该手机号被 Telegram 封禁",
    "PhoneNumberUnoccupiedError": "该手机号未注册 Telegram",
    "CodeEmptyError": "验证码为空",
    "FileReferenceExpiredError": "媒体引用已过期，需重新拉取文件",
    "MediaEmptyError": "媒体内容为空，无法转发",
    "MessageNotModifiedError": "内容未变更（重复提交相同编辑）",
    "MessageAuthorRequiredError": "需要消息作者身份才能转发",
    "InputUserDeactivatedError": "目标用户已注销",
    "UserPrivacyRestrictedError": "对方隐私设置禁止该操作",
    "ChatRestrictedError": "该群限制了该操作（禁止转发/发链接等）",
    "ReplyMarkupInvalidError": "按钮结构无效",
    "InputBotDeployError": "Bot 无权在该会话发消息",
}


def _format_rpc_error(exc):
    if exc is None:
        return "未知错误"
    if isinstance(exc, errors.FloodWaitError):
        return f"触发限流，需等待 {exc.seconds}s"

    err_name = type(exc).__name__
    zh = RPC_ERROR_CN.get(err_name)
    raw = str(exc).strip()
    if zh and raw:
        return f"{zh} ({err_name}: {raw})"
    if zh:
        return f"{zh} ({err_name})"
    if raw:
        return f"{err_name}: {raw}"
    return err_name


def _is_deterministic_delivery_error(exc):
    """Return True only when Telegram conclusively rejected the delivery."""
    if exc is None:
        return False
    if type(exc).__name__ in {
        "ChatAdminRequiredError",
        "ChatWriteForbiddenError",
        "UserBannedInChannelError",
        "ChannelPrivateError",
        "ChannelInvalidError",
        "ChatIdInvalidError",
        "PeerIdInvalidError",
        "MessageIdInvalidError",
        "ChatForwardsRestrictedError",
    }:
        return True
    message = str(exc)
    return message.startswith((
        "源消息禁止转发",
        "Bot 原生转发 Peer 无效",
        "Bot 原生转发源不可见",
    ))


class BotSourceUnavailableError(RuntimeError):
    """forward_bot 无法访问源会话，允许主账号原生兜底。"""


def _validate_device_config(data):
    if not isinstance(data, dict):
        return False, "根节点必须是 JSON 对象"
    profiles = data.get('profiles')
    if not isinstance(profiles, dict) or not profiles:
        return False, "profiles 必须是非空对象"
    if any(not isinstance(profile, dict) for profile in profiles.values()):
        return False, "profiles 中每个设备配置都必须是对象"
    active = str(data.get('active_profile', '')).strip()
    if not active or active not in profiles:
        return False, "active_profile 必须指向 profiles 中存在的设备"
    return True, ""


# ================= 配置热更新管理器 =================
class ConfigManager:
    """配置热更新管理器 - 修改JSON即时生效"""

    def __init__(self):
        self.regex_config = {}
        self.custom_forward_formats = []
        self.device_config = {}
        self.last_modified = {}
        self._compiled_cache = {}
        self.rule_objects = list(DEFAULT_RULE_OBJECTS)

        self.load_all()

    def load_all(self):
        statuses = (
            self.load_regex_config(),
            self.load_custom_forward_formats(),
            self.load_device_config(),
        )
        if any(status is None for status in statuses):
            logger.error("❌ 部分配置加载失败，已保留对应的上一份可用配置")
            return False
        logger.info("✅ 配置文件加载完成")
        return True

    def load_regex_config(self):
        source = REGEX_CONFIG_FILE
        try:
            mtime = REGEX_CONFIG_FILE.stat().st_mtime_ns
            if self.last_modified.get('regex') == mtime:
                return False

            with open(source, 'r', encoding='utf-8-sig') as f:
                new_regex_config = json.load(f)

            new_compiled_cache = self._compile_patterns(new_regex_config)
            self.regex_config = new_regex_config
            self._compiled_cache = new_compiled_cache
            self.last_modified['regex'] = mtime
            self._load_rule_objects()
            logger.info(f"🔄 正则配置已热更新 (mtime: {mtime})")
            return True
        except Exception as e:
            logger.error(f"❌ 加载正则配置失败，保留上一份可用规则: {e}")
            if not self.regex_config and _regex_tpl.exists() and _regex_tpl != source:
                try:
                    with open(_regex_tpl, 'r', encoding='utf-8-sig') as f:
                        fallback_config = json.load(f)
                    fallback_cache = self._compile_patterns(fallback_config)
                    self.regex_config = fallback_config
                    self._compiled_cache = fallback_cache
                    self._load_rule_objects()
                    logger.warning("⚠️ 已使用代码目录 regex_patterns.json 模板兜底启动")
                    return True
                except Exception as fallback_error:
                    logger.error(f"❌ 正则模板兜底加载失败: {fallback_error}")
            return None

    def load_device_config(self):
        try:
            with open(DEVICE_CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
                new_device_config = json.load(f)
            ok, error = _validate_device_config(new_device_config)
            if not ok:
                raise ValueError(error)
            self.device_config = new_device_config
            return True
        except Exception as e:
            logger.error(f"❌ 加载设备配置失败: {e}")
            return None

    def load_custom_forward_formats(self):
        try:
            if not CUSTOM_FORMAT_CONFIG_FILE.exists():
                tmp_path = str(CUSTOM_FORMAT_CONFIG_FILE) + '.tmp'
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump({"formats": []}, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(CUSTOM_FORMAT_CONFIG_FILE))
                if os.name == 'posix':
                    os.chmod(CUSTOM_FORMAT_CONFIG_FILE, 0o600)

            mtime = CUSTOM_FORMAT_CONFIG_FILE.stat().st_mtime_ns
            if self.last_modified.get('custom_formats') == mtime:
                return False

            with open(CUSTOM_FORMAT_CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
                raw = json.load(f)

            if isinstance(raw, dict):
                formats = raw.get('formats', [])
            elif isinstance(raw, list):
                formats = raw
            else:
                formats = []
            self.custom_forward_formats = [x for x in formats if isinstance(x, dict)]
            self.last_modified['custom_formats'] = mtime
            logger.info(f"🧩 自定义信息格式已加载: {len(self.custom_forward_formats)} 条")
            return True
        except Exception as e:
            logger.error(f"❌ 加载自定义信息格式失败: {e}")
            return None

    def _compile_patterns(self, regex_config=None):
        """预编译所有正则 - V10.7 三层过滤塔"""
        rc = regex_config if regex_config is not None else self.regex_config
        cache = {}

        # === 通用检测（handler 使用）===
        code_patterns = rc.get('CODE_PATTERNS', [])
        if code_patterns:
            cache['CODE'] = _safe_compile_pattern('|'.join(code_patterns), regex_engine.IGNORECASE)

        strict = rc.get('STRICT_CODE_PATTERN', '')
        if strict:
            cache['STRICT_CODE'] = _safe_compile_pattern(strict, regex_engine.IGNORECASE)

        dna_patterns = rc.get('DNA_SPECIAL_PATTERNS', [])
        if dna_patterns:
            cache['DNA_SPECIAL'] = _safe_compile_pattern('|'.join(dna_patterns), regex_engine.IGNORECASE)

        lottery_id = rc.get('LOTTERY_ID_PATTERN', '')
        if lottery_id:
            cache['LOTTERY_ID'] = _safe_compile_pattern(lottery_id, regex_engine.IGNORECASE)

        # === 第一层：斩杀塔 (KILL) ===
        kill = rc.get('KILL_PATTERNS', {})
        death_words = kill.get('DEATH_WORDS', [])
        if death_words:
            cache['KILL_DEATH'] = _safe_compile_pattern(
                '(' + '|'.join(death_words) + ')', regex_engine.IGNORECASE
            )
        for key, pattern in kill.items():
            if key != 'DEATH_WORDS' and pattern:
                cache[f'KILL_{key}'] = _safe_compile_pattern(pattern, regex_engine.IGNORECASE)

        # === 第二层：领域塔 (DOMAIN) ===
        for key, pattern in rc.get('DOMAIN_PATTERNS', {}).items():
            if pattern:
                cache[f'DOMAIN_{key}'] = _safe_compile_pattern(pattern, regex_engine.IGNORECASE)

        # === 动作信号 ===
        for key, pattern in rc.get('ACTION_SIGNALS', {}).items():
            if pattern:
                cache[f'ACTION_{key}'] = _safe_compile_pattern(pattern, regex_engine.IGNORECASE)

        # === 噪音信号 ===
        for key, pattern in rc.get('NOISE_SIGNALS', {}).items():
            if pattern:
                cache[f'NOISE_{key}'] = _safe_compile_pattern(pattern, regex_engine.IGNORECASE)

        # === 清理工具 ===
        for key, pattern in rc.get('CLEAN_PATTERNS', {}).items():
            if pattern:
                cache[f'CLEAN_{key}'] = _safe_compile_pattern(pattern)
        return cache

    def _load_rule_objects(self):
        raw_rules = self.regex_config.get("RULE_OBJECTS", [])
        parsed_rules = []
        for item in raw_rules:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            pattern = str(item.get("pattern", "")).strip()
            if not name or not pattern:
                continue

            action = str(item.get("action", "monitor")).strip().lower()
            layer = str(item.get("layer", "domain")).strip().lower()
            enabled = bool(item.get("enabled", True))
            priority = str(item.get("priority", "P2-中优")).strip() or "P2-中优"

            if action not in ("monitor", "exclude"):
                continue
            if layer not in ("domain", "kill", "whitelist"):
                continue

            parsed_rules.append(
                RuleObject(
                    name=name,
                    pattern=pattern,
                    action=action,
                    layer=layer,
                    priority=priority,
                    enabled=enabled,
                )
            )

        self.rule_objects = parsed_rules if parsed_rules else list(DEFAULT_RULE_OBJECTS)
        logger.info(f"🧩 规则对象已加载: {len(self.rule_objects)} 条")

    def get_pattern(self, name):
        return self._compiled_cache.get(name)

    def get_rule_objects(self, layer=None, action=None):
        rules = self.rule_objects
        if layer:
            layer = str(layer).strip().lower()
            rules = [r for r in rules if r.layer == layer]
        if action:
            action = str(action).strip().lower()
            rules = [r for r in rules if r.action == action]
        return sorted(
            [r for r in rules if r.enabled],
            key=lambda r: _priority_rank(r.priority)
        )

    def get_device_profile(self):
        profiles = self.device_config.get('profiles', {})
        active = self.device_config.get('active_profile', 'oneplus_13')
        return profiles.get(active, profiles.get('oneplus_13', {}))

    def check_and_reload(self):
        """检查配置文件是否更新，是则重新加载"""
        changed = False
        try:
            mtime = REGEX_CONFIG_FILE.stat().st_mtime_ns
            if self.last_modified.get('regex') != mtime:
                changed = self.load_regex_config() or changed
        except Exception:
            pass
        try:
            mtime = CUSTOM_FORMAT_CONFIG_FILE.stat().st_mtime_ns
            if self.last_modified.get('custom_formats') != mtime:
                changed = self.load_custom_forward_formats() or changed
        except Exception:
            pass
        return changed


config = ConfigManager()


# ================= 请求频率控制器 =================
class RateLimiter:
    """令牌桶限流器 - 防止触发Telegram风控"""

    def __init__(self, rate=2.0, burst=5):
        """
        rate: 每秒产生的令牌数
        burst: 突发容量
        """
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

        # 动态调整：遇到FloodWait自动降速
        self.penalty_until = 0
        self.consecutive_floods = 0

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()

            # 惩罚期等待
            if now < self.penalty_until:
                wait = self.penalty_until - now
                logger.warning(f"⏳ 限流惩罚中，等待 {wait:.1f}s")
                await asyncio.sleep(wait)
                now = time.monotonic()

            # 补充令牌
            elapsed = now - self.last_update
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0
            else:
                self.tokens -= 1

    def report_flood(self, seconds):
        """报告被限流，动态惩罚"""
        self.consecutive_floods += 1
        # 惩罚时间 = 官方等待 + 额外缓冲（连续被限流则指数退避）
        extra = min(30, 5 * (2 ** (self.consecutive_floods - 1)))
        self.penalty_until = time.monotonic() + seconds + extra
        self.tokens = 0
        logger.warning(f"🚨 触发限流，惩罚 {seconds + extra}s (连续{self.consecutive_floods}次)")

    def report_success(self):
        """成功发送，重置连续失败计数"""
        self.consecutive_floods = max(0, self.consecutive_floods - 1)


rate_limiter = RateLimiter(
    rate=_env_float("RATE_LIMIT_RATE", 2.0, 0.2, 5.0),
    burst=_env_int("RATE_LIMIT_BURST", 10, 1, 30)
)


# ================= SQLite数据库管理 =================
class DatabaseManager:
    """数据库管理器 - 自动清理防爆盘"""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.conn = None
        self.cursor = None
        self._init_db()

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()

        # 极限优化参数
        self.cursor.execute("PRAGMA journal_mode=WAL;")
        self.cursor.execute("PRAGMA synchronous=NORMAL;")
        self.cursor.execute("PRAGMA temp_store=MEMORY;")
        self.cursor.execute("PRAGMA cache_size=-2000;")
        self.cursor.execute(f"PRAGMA mmap_size={SQLITE_MMAP_BYTES};")

        # 建表
        self.cursor.executescript('''
            CREATE TABLE IF NOT EXISTS history (
                dna TEXT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS lottery_history (
                dna TEXT PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                word TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS whitelist (
                word TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS sys_config (
                key TEXT PRIMARY KEY,
                val TEXT
            );
            CREATE TABLE IF NOT EXISTS muted_chats (
                chat_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS vip_admins (
                user_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS user_blacklist (
                user_id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS intercept_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                reason TEXT,
                content TEXT,
                chat_id TEXT
            );
            CREATE TABLE IF NOT EXISTS forward_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                preview TEXT,
                src_chat_id TEXT,
                src_msg_id INTEGER,
                target_msg_id INTEGER,
                portal_msg_id INTEGER,
                dna TEXT,
                sender_id TEXT,
                send_bot_idx TEXT,
                send_client TEXT,
                portal_bot_idx TEXT,
                msg_date TEXT,
                is_edited TEXT,
                chat_title TEXT
            );
            CREATE TABLE IF NOT EXISTS forward_delivery (
                src_chat_id TEXT NOT NULL,
                src_msg_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                dna TEXT,
                target_msg_id INTEGER,
                updated_ts INTEGER NOT NULL,
                PRIMARY KEY (src_chat_id, src_msg_id)
            );
            CREATE INDEX IF NOT EXISTS idx_history_ts ON history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_history_dna ON history(dna);
            CREATE INDEX IF NOT EXISTS idx_lottery_history_ts ON lottery_history(timestamp);
            CREATE INDEX IF NOT EXISTS idx_intercept_ts ON intercept_log(ts);
            CREATE INDEX IF NOT EXISTS idx_forward_log_id ON forward_log(id);
            CREATE INDEX IF NOT EXISTS idx_forward_log_source ON forward_log(src_chat_id, src_msg_id);
            CREATE INDEX IF NOT EXISTS idx_forward_delivery_updated ON forward_delivery(updated_ts);
        ''')
        self.cursor.execute("PRAGMA table_info(forward_log)")
        if 'dna' not in {row[1] for row in self.cursor.fetchall()}:
            self.cursor.execute("ALTER TABLE forward_log ADD COLUMN dna TEXT")
        # V11.01 归档可追溯字段（借鉴 TelegramMonitor 的 TelegramMessageRecord：发送时间/编辑标记/群名分开存）
        _fwd_cols = {row[1] for row in self.cursor.execute("PRAGMA table_info(forward_log)").fetchall()}
        for _col, _ddl in (('msg_date', 'TEXT'), ('is_edited', 'TEXT'), ('chat_title', 'TEXT')):
            if _col not in _fwd_cols:
                self.cursor.execute(f"ALTER TABLE forward_log ADD COLUMN {_col} {_ddl}")

        # 默认配置（从 .env 读取）
        _target = os.environ.get("TARGET_ID", "")
        _backup = os.environ.get("BACKUP_ID", "")
        _owner = os.environ.get("OWNER_ID", "")
        if _target:
            self.cursor.execute(
                "INSERT OR IGNORE INTO sys_config (key, val) VALUES ('TARGET_ID', ?)", (_target,)
            )
        if _backup:
            self.cursor.execute(
                "INSERT OR IGNORE INTO sys_config (key, val) VALUES ('BACKUP_ID', ?)", (_backup,)
            )
        if _owner:
            self.cursor.execute(
                "INSERT OR IGNORE INTO sys_config (key, val) VALUES ('OWNER_ID', ?)", (_owner,)
            )
        self.conn.commit()

    def get_config(self, key):
        self.cursor.execute("SELECT val FROM sys_config WHERE key=?", (key,))
        res = self.cursor.fetchone()
        if not res:
            return None
        try:
            return int(res[0])
        except (ValueError, TypeError):
            return res[0]

    def set_config(self, key, val):
        self.cursor.execute(
            "INSERT OR REPLACE INTO sys_config (key, val) VALUES (?, ?)", (key, str(val))
        )
        self.conn.commit()

    def cleanup_old_data(self):
        """自动清理过期数据，防止硬盘爆满"""
        try:
            # 检查数据库大小
            db_size = os.path.getsize(self.db_path) / (1024 * 1024)
            if db_size > MAX_DB_SIZE_MB:
                logger.warning(f"⚠️ 数据库过大: {db_size:.1f}MB，强制清理")

            # 删除过期历史
            self.cursor.execute(
                "DELETE FROM history WHERE timestamp < datetime('now', ?)",
                (f"-{DB_CLEANUP_DAYS} days",)
            )
            deleted = self.cursor.rowcount
            self.cursor.execute(
                "DELETE FROM lottery_history WHERE timestamp < datetime('now', ?)",
                (f"-{LOTTERY_DEDUP_DAYS} days",)
            )
            deleted += self.cursor.rowcount

            # 清理过期拦截记录
            self.cursor.execute(
                "DELETE FROM intercept_log WHERE ts < datetime('now', 'localtime', ?)",
                (f"-{DB_CLEANUP_DAYS} days",)
            )
            deleted += self.cursor.rowcount

            self.cursor.execute(
                "DELETE FROM forward_log WHERE ts < datetime('now', 'localtime', ?)",
                (f"-{DB_CLEANUP_DAYS} days",)
            )
            deleted += self.cursor.rowcount
            self.cursor.execute(
                "DELETE FROM forward_delivery WHERE status='sent' AND updated_ts < ?",
                (int(time.time()) - DB_CLEANUP_DAYS * 86400,)
            )
            deleted += self.cursor.rowcount

            # 限制总行数
            self.cursor.execute(
                "DELETE FROM history WHERE dna NOT IN "
                "(SELECT dna FROM history ORDER BY timestamp DESC LIMIT ?)",
                (MAX_HISTORY_LINES,)
            )
            deleted += self.cursor.rowcount
            self.cursor.execute(
                "DELETE FROM lottery_history WHERE dna NOT IN "
                "(SELECT dna FROM lottery_history ORDER BY timestamp DESC LIMIT ?)",
                (MAX_LOTTERY_HISTORY,)
            )
            deleted += self.cursor.rowcount
            self.cursor.execute(
                "DELETE FROM forward_log WHERE id NOT IN "
                "(SELECT id FROM forward_log ORDER BY id DESC LIMIT ?)",
                (MAX_FORWARD_LOG,)
            )
            deleted += self.cursor.rowcount

            # VACUUM 释放空间（仅在数据库较大时）
            if db_size > MAX_DB_SIZE_MB * 0.7:
                self.cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self.conn.commit()
                self.cursor.execute("VACUUM;")
                logger.info("数据库已压缩")

            self.conn.commit()

            if deleted > 0:
                logger.info(f"🧹 清理了 {deleted} 条过期记录")

        except Exception as e:
            logger.error(f"❌ 数据库清理失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass

    def load_all_caches(self):
        """加载所有缓存"""
        caches = {}

        self.cursor.execute("SELECT word FROM blacklist")
        caches['blacklist'] = set(row[0] for row in self.cursor.fetchall())

        self.cursor.execute("SELECT word FROM whitelist")
        caches['whitelist'] = set(row[0] for row in self.cursor.fetchall())

        self.cursor.execute("SELECT chat_id FROM muted_chats")
        caches['muted'] = set(row[0] for row in self.cursor.fetchall())

        self.cursor.execute("SELECT user_id FROM vip_admins")
        caches['vip'] = set(row[0] for row in self.cursor.fetchall())

        self.cursor.execute("SELECT user_id FROM user_blacklist")
        caches['user_blacklist'] = set(row[0] for row in self.cursor.fetchall())

        caches.update(self.load_dna_caches())

        return caches

    def load_dna_caches(self):
        """Load only persisted deduplication keys for runtime cache refreshes."""
        caches = {}
        self.cursor.execute(
            "SELECT dna FROM history ORDER BY timestamp DESC LIMIT ?",
            (MAX_HISTORY_LINES,)
        )
        rows = self.cursor.fetchall()
        dna_list = [row[0] for row in reversed(rows)]
        self.cursor.execute(
            "SELECT dna FROM forward_delivery WHERE dna <> '' "
            "ORDER BY updated_ts DESC LIMIT ?",
            (MAX_HISTORY_LINES,)
        )
        delivery_dna = [row[0] for row in reversed(self.cursor.fetchall())]
        dna_list = list(dict.fromkeys(dna_list + delivery_dna))[-MAX_HISTORY_LINES:]
        caches['dna_list'] = dna_list
        caches['dna_set'] = set(dna_list)

        self.cursor.execute(
            "SELECT dna FROM lottery_history WHERE timestamp >= datetime('now', ?) "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"-{LOTTERY_DEDUP_DAYS} days", MAX_LOTTERY_HISTORY)
        )
        lottery_rows = self.cursor.fetchall()
        lottery_dna_list = [row[0] for row in reversed(lottery_rows)]
        caches['lottery_dna_list'] = lottery_dna_list
        caches['lottery_dna_set'] = set(lottery_dna_list)

        return caches

    @staticmethod
    def _source_key(chat_id):
        return str(chat_id).removeprefix('-100')

    def claim_forward(self, chat_id, msg_id, dna=""):
        """Atomically reserve a source message before it enters the send queue."""
        source_chat_id = self._source_key(chat_id)
        try:
            self.cursor.execute(
                "INSERT OR IGNORE INTO forward_delivery "
                "(src_chat_id, src_msg_id, status, dna, target_msg_id, updated_ts) "
                "SELECT ?, ?, 'pending', ?, NULL, ? "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM forward_log WHERE src_chat_id=? AND src_msg_id=?"
                ")",
                (
                    source_chat_id,
                    int(msg_id),
                    str(dna or ''),
                    int(time.time()),
                    source_chat_id,
                    int(msg_id),
                )
            )
            claimed = self.cursor.rowcount == 1
            self.conn.commit()
            return claimed
        except Exception as e:
            logger.error(f"❌ 转发占位写库失败，拒绝发送: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def mark_forward_sent(self, chat_id, msg_id, target_msg_id):
        """Persist Telegram delivery before non-critical logs and counters."""
        try:
            self.cursor.execute(
                "UPDATE forward_delivery SET status='sent', target_msg_id=?, updated_ts=? "
                "WHERE src_chat_id=? AND src_msg_id=?",
                (
                    int(target_msg_id),
                    int(time.time()),
                    self._source_key(chat_id),
                    int(msg_id),
                )
            )
            self.conn.commit()
            return self.cursor.rowcount == 1
        except Exception as e:
            logger.error(f"❌ 转发成功状态写库失败，保留 pending 防止重复: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass
            return False

    def release_forward_claim(self, chat_id, msg_id):
        """Release only a known-unsent reservation so a later event may retry."""
        try:
            self.cursor.execute(
                "DELETE FROM forward_delivery "
                "WHERE src_chat_id=? AND src_msg_id=? AND status='pending'",
                (self._source_key(chat_id), int(msg_id))
            )
            self.conn.commit()
        except Exception as e:
            logger.warning(f"⚠️ 转发占位释放失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass

    def close(self):
        if self.conn:
            self.conn.close()


db = DatabaseManager(DB_FILE)


def _safe_db_rollback():
    """Best-effort rollback used after a failed SQLite write."""
    try:
        db.conn.rollback()
    except Exception as exc:
        logger.error(f"❌ SQLite 回滚失败: {exc}")


def _persist_dna(dna_hash, is_lottery):
    """指纹落库：history 限 MAX_HISTORY_LINES 条，抽奖额外进 lottery_history（保留 10 天）。"""
    try:
        db.cursor.execute("INSERT INTO history (dna) VALUES (?)", (dna_hash,))
        if is_lottery:
            db.cursor.execute("INSERT OR IGNORE INTO lottery_history (dna) VALUES (?)", (dna_hash,))
        db.cursor.execute("SELECT COUNT(*) FROM history")
        count = db.cursor.fetchone()[0]
        if count > MAX_HISTORY_LINES:
            db.cursor.execute(
                "DELETE FROM history WHERE dna NOT IN "
                "(SELECT dna FROM history ORDER BY timestamp DESC LIMIT ?)",
                (MAX_HISTORY_LINES,)
            )
        db.conn.commit()
    except Exception as e:
        logger.warning(f"⚠️ DNA 历史写库失败: {e}")
        _safe_db_rollback()


def _record_activity(kind):
    """持久化最近活动时间，避免重启/跨天后心跳误报。"""
    now = int(time.time())
    key = 'LAST_FORWARD_TS' if kind == 'forward' else 'LAST_INTERCEPT_TS'
    try:
        db.cursor.execute(
            "INSERT OR REPLACE INTO sys_config (key, val) VALUES (?, ?)",
            (key, str(now))
        )
        db.cursor.execute(
            "INSERT OR REPLACE INTO sys_config (key, val) VALUES ('LAST_ACTIVITY_TS', ?)",
            (str(now),)
        )
        db.conn.commit()
    except Exception as e:
        logger.warning(f"⚠️ 最近活动时间写入失败: {e}")
        try:
            db.conn.rollback()
        except Exception as rollback_err:
            logger.error(f"❌ SQLite 回滚失败: {rollback_err}")


def _config_epoch(key):
    try:
        value = db.get_config(key)
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_local_epoch(ts):
    if not ts:
        return 0
    try:
        return int(time.mktime(time.strptime(str(ts), '%Y-%m-%d %H:%M:%S')))
    except Exception:
        return 0


def _db_last_forward_epoch():
    try:
        db.cursor.execute("SELECT strftime('%s', MAX(timestamp)) FROM history")
        row = db.cursor.fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def _db_last_intercept_epoch():
    try:
        db.cursor.execute("SELECT MAX(ts) FROM intercept_log")
        row = db.cursor.fetchone()
        return _parse_local_epoch(row[0]) if row else 0
    except Exception:
        return 0


def _last_activity_snapshot():
    """读取最近转发/拦截时间；优先用持久化时间，兼容旧 DB 历史记录。"""
    last_forward = max(_config_epoch('LAST_FORWARD_TS'), _db_last_forward_epoch())
    last_intercept = max(_config_epoch('LAST_INTERCEPT_TS'), _db_last_intercept_epoch())
    last_activity = max(_config_epoch('LAST_ACTIVITY_TS'), last_forward, last_intercept)
    return {
        'last_forward': last_forward,
        'last_intercept': last_intercept,
        'last_activity': last_activity,
    }


def _format_age(ts, now=None):
    if not ts:
        return '无记录'
    now = now or time.time()
    age_seconds = max(0, int(now - ts))
    hours = age_seconds // 3600
    minutes = (age_seconds % 3600) // 60
    if hours >= 1:
        return f"{hours}小时{minutes}分钟前"
    return f"{minutes}分钟前"


def _message_activity_datetime(message):
    """取消息的活动时间：优先上游原始发布时间（防陈旧卡片被当作新活动重放）。"""
    forwarded_date = getattr(getattr(message, 'fwd_from', None), 'date', None)
    if forwarded_date:
        return forwarded_date
    return getattr(message, 'edit_date', None) or getattr(message, 'date', None)

def _build_word_pattern(word):
    """构建关键词正则模式：支持 ? 分隔的模糊匹配（所有片段都出现即命中，不要求顺序）
    例如：'注册?邀请' → '(?=.*注册)(?=.*邀请)'
    普通关键词：'注册码' → '注册码'（精确转义）
    """
    word = str(word or "").strip()
    if not word:
        return r'(?!x)x'

    if '?' in word:
        fragments = [f.strip() for f in word.split('?') if f.strip()]
        if len(fragments) >= 2:
            lookaheads = ''.join(f'(?=.*{_build_word_fragment_pattern(f)})' for f in fragments)
            return f'{lookaheads}.*'
    return _build_word_fragment_pattern(word)


def _build_word_fragment_pattern(fragment):
    """纯英文/数字关键词按独立词匹配，避免 AI 误伤 Mirai/OpenAI。"""
    fragment = str(fragment or "").strip()
    escaped = re.escape(fragment)
    if re.fullmatch(r'[A-Za-z0-9]+', fragment):
        return rf'(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])'
    return escaped


def _find_matching_word(words, text):
    """返回实际命中的原始关键词；仅在总黑名单已命中后调用。"""
    for word in sorted((str(x).strip() for x in words if str(x).strip()), key=len, reverse=True):
        if re.search(_build_word_pattern(word), text or "", re.IGNORECASE):
            return word
    return ""


def _find_matching_regex_source(patterns, text):
    """返回实际命中的原始正则/关键词；仅用于诊断已命中的配置规则。"""
    for pattern in sorted((str(x).strip() for x in patterns if str(x).strip()), key=len, reverse=True):
        try:
            compiled = regex_engine.compile(pattern, regex_engine.IGNORECASE)
            if compiled.search(text or "", timeout=_REGEX_TIMEOUT):
                return pattern
        except (regex_engine.error, TimeoutError):
            continue
    return ""


# ================= 缓存管理 =================
class CacheManager:
    """内存缓存管理器"""

    def __init__(self):
        self.blacklist = set()
        self.whitelist = set()
        self.muted_chats = set()
        self.vip_admins = set()
        self.user_blacklist = set()
        self.dna_set = set()
        self.dna_queue = deque(maxlen=MAX_HISTORY_LINES)
        self.lottery_dna_set = set()
        self.lottery_dna_queue = deque(maxlen=MAX_LOTTERY_HISTORY)
        self.forwarded_map = {}
        self.forwarded_queue = deque(maxlen=MAX_HISTORY_LINES)
        self.intercept_log = deque(maxlen=200)

        self.compiled_blacklist = None
        self.compiled_whitelist = None

        self.reload_all()

    def reload_all(self):
        caches = db.load_all_caches()
        self.blacklist = {str(w).strip() for w in caches['blacklist'] if str(w).strip()}
        self.whitelist = {str(w).strip() for w in caches['whitelist'] if str(w).strip()}
        self.muted_chats = {str(w).strip() for w in caches['muted'] if str(w).strip()}
        self.vip_admins = {str(w).strip() for w in caches['vip'] if str(w).strip()}
        self.user_blacklist = {str(w).strip() for w in caches.get('user_blacklist', set()) if str(w).strip()}
        self.dna_set = caches['dna_set']
        self.dna_queue = deque(caches.get('dna_list', []), maxlen=MAX_HISTORY_LINES)
        self.lottery_dna_set = caches.get('lottery_dna_set', set())
        self.lottery_dna_queue = deque(caches.get('lottery_dna_list', []), maxlen=MAX_LOTTERY_HISTORY)

        if self.blacklist:
            self.compiled_blacklist = regex_engine.compile(
                '(' + '|'.join(_build_word_pattern(w) for w in self.blacklist) + ')',
                regex_engine.IGNORECASE
            )
        else:
            self.compiled_blacklist = None

        if self.whitelist:
            self.compiled_whitelist = regex_engine.compile(
                '(' + '|'.join(_build_word_pattern(w) for w in self.whitelist) + ')',
                regex_engine.IGNORECASE
            )
        else:
            self.compiled_whitelist = None

        logger.info(f"📦 缓存已加载: 黑{len(self.blacklist)} 白{len(self.whitelist)} "
                     f"屏蔽{len(self.muted_chats)} VIP{len(self.vip_admins)} "
                     f"DNA{len(self.dna_set)} 抽奖DNA{len(self.lottery_dna_set)}")

    def refresh_dna_history(self):
        """Keep in-memory deduplication windows aligned with DB cleanup."""
        caches = db.load_dna_caches()
        self.dna_set = caches['dna_set']
        self.dna_queue = deque(caches['dna_list'], maxlen=MAX_HISTORY_LINES)
        self.lottery_dna_set = caches['lottery_dna_set']
        self.lottery_dna_queue = deque(caches['lottery_dna_list'], maxlen=MAX_LOTTERY_HISTORY)

    def add_dna(self, dna_hash, is_lottery=False):
        if dna_hash in self.dna_set or (is_lottery and dna_hash in self.lottery_dna_set):
            return False
        self.dna_set.add(dna_hash)
        self.dna_queue.append(dna_hash)
        # 内存限制
        while len(self.dna_set) > MAX_HISTORY_LINES:
            old = self.dna_queue.popleft()
            self.dna_set.discard(old)
        if is_lottery:
            self.lottery_dna_set.add(dna_hash)
            self.lottery_dna_queue.append(dna_hash)
            while len(self.lottery_dna_set) > MAX_LOTTERY_HISTORY:
                old = self.lottery_dna_queue.popleft()
                self.lottery_dna_set.discard(old)
        return True

    def remove_dna(self, dna_hash):
        self.dna_set.discard(dna_hash)
        self.lottery_dna_set.discard(dna_hash)
        self.dna_queue = deque(
            (item for item in self.dna_queue if item != dna_hash),
            maxlen=MAX_HISTORY_LINES,
        )
        self.lottery_dna_queue = deque(
            (item for item in self.lottery_dna_queue if item != dna_hash),
            maxlen=MAX_LOTTERY_HISTORY,
        )

    def log_intercept(self, text, reason, chat_id=""):
        ts = time.strftime('%m/%d %H:%M')
        self.intercept_log.appendleft((ts, reason, text[:100]))
        # V11.04：**无条件**写文件日志（V11.19 移除了已失效的 `journal` 开关参数及全部调用点实参）。
        # 原先只在 journal=True 时打印，导致「未命中信息格式」「抽奖字段不完整」等路径的拦截
        # 在**文件日志里完全不可见**（用户排查漏转时 grep 一片空白，只能去翻 DB）。
        # 调用点本就只在 should_audit（domain!=none 或 is_candidate）时才进来，噪声已被前置过滤；
        # 日志增长由 5MB×3 轮转兜底。
        logger.info(f"🧾 拦截记录: chat={chat_id} reason={reason} preview={text[:80]}")
        # 持久化到 DB
        try:
            db.cursor.execute(
                "INSERT INTO intercept_log (ts, reason, content, chat_id) VALUES (?, ?, ?, ?)",
                (time.strftime('%Y-%m-%d %H:%M:%S'), reason, text[:200], str(chat_id))
            )
            now = str(int(time.time()))
            db.cursor.execute(
                "INSERT OR REPLACE INTO sys_config (key, val) VALUES ('LAST_INTERCEPT_TS', ?)",
                (now,)
            )
            db.cursor.execute(
                "INSERT OR REPLACE INTO sys_config (key, val) VALUES ('LAST_ACTIVITY_TS', ?)",
                (now,)
            )
            # 限制总行数
            db.cursor.execute(
                "DELETE FROM intercept_log WHERE id NOT IN "
                "(SELECT id FROM intercept_log ORDER BY id DESC LIMIT ?)",
                (MAX_INTERCEPT_LOG,)
            )
            db.conn.commit()
        except Exception as e:
            logger.warning(f"⚠️ 拦截日志写库失败: {e}")
            try:
                db.conn.rollback()
            except Exception:
                pass

    def get_intercepted_page(self, offset=0, limit=10, search=""):
        """分页查询拦截记录，返回 (records, total)"""
        try:
            if search:
                db.cursor.execute(
                    "SELECT COUNT(*) FROM intercept_log WHERE reason LIKE ? OR content LIKE ?",
                    (f"%{search}%", f"%{search}%")
                )
                total = db.cursor.fetchone()[0]
                db.cursor.execute(
                    "SELECT ts, reason, content FROM intercept_log "
                    "WHERE reason LIKE ? OR content LIKE ? "
                    "ORDER BY id DESC LIMIT ? OFFSET ?",
                    (f"%{search}%", f"%{search}%", limit, offset)
                )
            else:
                db.cursor.execute("SELECT COUNT(*) FROM intercept_log")
                total = db.cursor.fetchone()[0]
                db.cursor.execute(
                    "SELECT ts, reason, content FROM intercept_log "
                    "ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                )
            return db.cursor.fetchall(), total
        except Exception as e:
            logger.warning(f"⚠️ 拦截日志查询失败: {e}")
            return [], 0

    def log_forward(self, entry):
        """记录成功转发日志，持久化给管理 Bot 查询。"""
        safe_entry = {
            'ts': str(entry.get('ts') or time.strftime('%Y-%m-%d %H:%M:%S')),
            'preview': str(entry.get('preview') or '')[:500],
            'src_chat_id': str(entry.get('src_chat_id') or ''),
            'src_msg_id': int(entry.get('src_msg_id') or 0),
            'target_msg_id': int(entry.get('target_msg_id') or 0),
            'portal_msg_id': int(entry.get('portal_msg_id') or 0) if entry.get('portal_msg_id') else None,
            'dna': str(entry.get('dna') or ''),
            'sender_id': str(entry.get('sender_id') or ''),
            'send_bot_idx': '' if entry.get('send_bot_idx') is None else str(entry.get('send_bot_idx')),
            'send_client': str(entry.get('send_client') or ''),
            'portal_bot_idx': '' if entry.get('portal_bot_idx') is None else str(entry.get('portal_bot_idx')),
            'msg_date': str(entry.get('msg_date') or ''),
            'is_edited': str(entry.get('is_edited') or ''),
            'chat_title': str(entry.get('chat_title') or ''),
        }
        forwarded_log.appendleft(safe_entry)
        try:
            db.cursor.execute(
                "INSERT INTO forward_log "
                "(ts, preview, src_chat_id, src_msg_id, target_msg_id, portal_msg_id, "
                "dna, sender_id, send_bot_idx, send_client, portal_bot_idx, "
                "msg_date, is_edited, chat_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    safe_entry['ts'],
                    safe_entry['preview'],
                    safe_entry['src_chat_id'],
                    safe_entry['src_msg_id'],
                    safe_entry['target_msg_id'],
                    safe_entry['portal_msg_id'],
                    safe_entry['dna'],
                    safe_entry['sender_id'],
                    safe_entry['send_bot_idx'],
                    safe_entry['send_client'],
                    safe_entry['portal_bot_idx'],
                    safe_entry['msg_date'],
                    safe_entry['is_edited'],
                    safe_entry['chat_title'],
                )
            )
            now = str(int(time.time()))
            db.cursor.execute(
                "INSERT OR REPLACE INTO sys_config (key, val) VALUES ('LAST_FORWARD_TS', ?)",
                (now,)
            )
            db.cursor.execute(
                "INSERT OR REPLACE INTO sys_config (key, val) VALUES ('LAST_ACTIVITY_TS', ?)",
                (now,)
            )
            db.cursor.execute(
                "DELETE FROM forward_log WHERE id NOT IN "
                "(SELECT id FROM forward_log ORDER BY id DESC LIMIT ?)",
                (MAX_FORWARD_LOG,)
            )
            db.conn.commit()
        except Exception as e:
            logger.warning(f"⚠️ 转发日志写库失败: {e}")
            try:
                db.conn.rollback()
            except Exception:
                pass

    def get_forwarded_records(self, limit=20):
        """读取最近转发记录，返回 (records, total)。"""
        try:
            limit = max(1, min(100, int(limit)))
            db.cursor.execute("SELECT COUNT(*) FROM forward_log")
            total = db.cursor.fetchone()[0]
            if total == 0 and forwarded_log:
                return list(forwarded_log)[:limit], len(forwarded_log)
            db.cursor.execute(
                "SELECT ts, preview, src_chat_id, src_msg_id, target_msg_id, portal_msg_id, "
                "sender_id, send_bot_idx, send_client, portal_bot_idx "
                "FROM forward_log ORDER BY id DESC LIMIT ?",
                (limit,)
            )
            rows = db.cursor.fetchall()
            records = []
            for row in rows:
                records.append({
                    'ts': row[0],
                    'preview': row[1],
                    'src_chat_id': row[2],
                    'src_msg_id': row[3],
                    'target_msg_id': row[4],
                    'portal_msg_id': row[5],
                    'sender_id': row[6],
                    'send_bot_idx': row[7],
                    'send_client': row[8],
                    'portal_bot_idx': row[9],
                })
            return records, total
        except Exception as e:
            logger.warning(f"⚠️ 转发日志查询失败: {e}")
            return list(forwarded_log)[:limit], len(forwarded_log)

    def get_stats_summary(self):
        """统计拦截分布"""
        stats = {'domains': {}, 'priorities': {}, 'reasons': {}}
        try:
            db.cursor.execute(
                "SELECT reason, COUNT(*) FROM intercept_log GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 15"
            )
            stats['reasons'] = dict(db.cursor.fetchall())
        except Exception as e:
            logger.warning(f"⚠️ 统计查询失败: {e}")
        return stats

    def has_forwarded_source(self, chat_id, msg_id):
        """Keep source-message deduplication after a process restart."""
        try:
            source_chat_id = str(chat_id).removeprefix('-100')
            db.cursor.execute(
                "SELECT 1 FROM forward_log WHERE src_chat_id=? AND src_msg_id=? "
                "UNION ALL "
                "SELECT 1 FROM forward_delivery WHERE src_chat_id=? AND src_msg_id=? "
                "LIMIT 1",
                (source_chat_id, int(msg_id), source_chat_id, int(msg_id))
            )
            return db.cursor.fetchone() is not None
        except Exception as e:
            logger.warning(f"⚠️ 源消息去重查询失败: {e}")
            return False

    def get_forward_mapping(self, chat_id, msg_id):
        """Recover a recent forward mapping after a process restart."""
        try:
            source_chat_id = str(chat_id).removeprefix('-100')
            db.cursor.execute(
                "SELECT target_msg_id, portal_msg_id, dna, send_bot_idx, send_client, portal_bot_idx "
                "FROM forward_log WHERE src_chat_id=? AND src_msg_id=? ORDER BY id DESC LIMIT 1",
                (source_chat_id, int(msg_id))
            )
            row = db.cursor.fetchone()
            if not row:
                db.cursor.execute(
                    "SELECT target_msg_id, dna FROM forward_delivery "
                    "WHERE src_chat_id=? AND src_msg_id=? AND status='sent' "
                    "AND target_msg_id IS NOT NULL LIMIT 1",
                    (source_chat_id, int(msg_id))
                )
                delivery_row = db.cursor.fetchone()
                if not delivery_row:
                    return None
                return {
                    'target_msg_id': delivery_row[0],
                    'portal_msg_id': None,
                    'dna': delivery_row[1] or None,
                    'send_bot_idx': None,
                    'send_client': 'bot',
                    'portal_bot_idx': None,
                }

            def _bot_index(value):
                try:
                    return int(value) if value not in (None, '') else None
                except (TypeError, ValueError):
                    return None

            return {
                'target_msg_id': row[0],
                'portal_msg_id': row[1],
                'dna': row[2] or None,
                'send_bot_idx': _bot_index(row[3]),
                'send_client': row[4] or 'bot',
                'portal_bot_idx': _bot_index(row[5]),
            }
        except Exception as e:
            logger.warning(f"⚠️ 源消息映射恢复失败: {e}")
            return None

    def record_rule_hit(self, pattern_name):
        """记录规则命中次数（内存累计）"""
        rule_hits[pattern_name] = rule_hits.get(pattern_name, 0) + 1


cache = CacheManager()


def remember_forward(src_uid, payload):
    """登记已转发映射，并按有界队列淘汰最旧条目，防止无界增长。"""
    if len(cache.forwarded_queue) >= MAX_HISTORY_LINES:
        cache.forwarded_map.pop(cache.forwarded_queue.popleft(), None)
    cache.forwarded_map[src_uid] = payload
    cache.forwarded_queue.append(src_uid)


# ================= GUI 辅助组件 =================

FORWARDING_PAUSED = str(db.get_config('FORWARDING_PAUSED') or '').strip() == '1'  # 紧急停止转发开关（持久化到 DB）

# --- 权限校验 ---
async def _check_owner(event):
    """校验回调发送者是否为 OWNER，返回 True=通过"""
    sender_id = str(event.sender_id) if event.sender_id else ""
    OWNER_ID = db.get_config('OWNER_ID')
    if not OWNER_ID or sender_id != str(OWNER_ID):
        logger.warning(f"⚠️ 回调权限校验失败: sender={sender_id}, owner={OWNER_ID}")
        await event.answer("⛔ 无权限", alert=True)
        return False
    return True


# --- 面板文本构建 ---

def _get_alive_count():
    """检测存活账号数"""
    alive = 1  # admin_bot 自身
    try:
        if client.is_connected():
            alive += 1
    except Exception:
        pass
    for bot in forward_bots:
        try:
            if bot.is_connected():
                alive += 1
        except Exception:
            pass
    return alive


def _get_config_device_profile():
    return config.get_device_profile()


def _build_version_status_line():
    running_ver = RUNNING_DEVICE_PROFILE.get('app_version') or device_profile.get('app_version', '-')
    config_ver = _get_config_device_profile().get('app_version', '-')
    if running_ver == config_ver:
        return f"├ 版本状态　运行/配置 `{running_ver}`\n"
    return f"├ 版本状态　运行 `{running_ver}` / 配置 `{config_ver}`（待重启）\n"


def _build_security_status_lines():
    if FORWARD_MODE == "user_native_only":
        return (
            "├ 主账号职责　`监听+原生转发`\n"
            "├ 目标发送　`主账号原生`\n"
            "├ Bot克隆　`已禁用`\n"
            "├ 传送门　`已禁用`\n"
            f"└ 自动轮换　`{'已开启' if ENABLE_DEVICE_ROTATION else '已关闭'}`"
        )
    if ALLOW_BOT_CLONE_FALLBACK:
        clone_state = "文本兜底"
        if ALLOW_MEDIA_CLONE:
            clone_state += "+媒体"
        elif ALLOW_SAFE_MEDIA_CLONE_FALLBACK:
            clone_state += "+小媒体"
        if ALLOW_PROTECTED_CLONE:
            clone_state += "+受保护"
        elif ALLOW_PROTECTED_TEXT_CLONE:
            clone_state += "+受保护文本"
    else:
        clone_state = "已关闭"
    user_role = "监听+原生兜底" if ALLOW_USER_NATIVE_FALLBACK else "只读监听"
    portal_state = "已开启" if SEND_PORTAL_MESSAGE else "已关闭"
    rotation_state = "已开启" if ENABLE_DEVICE_ROTATION else "已关闭"
    fb_total = len(forward_bots)
    fb_alive = sum(1 for b in forward_bots if b.is_connected())
    return (
        f"├ 主账号职责　`{user_role}`\n"
        f"├ 目标发送　`forward_bot池({fb_alive}/{fb_total})`\n"
        f"├ 受限兜底　`{clone_state}`\n"
        f"├ 传送门　`{portal_state}`\n"
        f"└ 自动轮换　`{rotation_state}`"
    )


def _build_start_panel():
    """构建主菜单面板 — 四区分层布局"""
    uptime_seconds = int(time.monotonic() - START_TIME)
    s = uptime_seconds % 60
    m = (uptime_seconds // 60) % 60
    h = (uptime_seconds // 3600) % 24
    d = uptime_seconds // 86400
    device_name = device_profile.get('device_model', 'OnePlus 13')
    app_ver = device_profile.get('app_version', '')
    ver_display = f"`{device_name}` v{app_ver}" if app_ver else f"`{device_name}`"
    status_icon = "⏸ 已暂停" if FORWARDING_PAUSED else "🟢 运行中"
    node_tag = f" `{NODE_NAME}`" if NODE_NAME != "TG-Monitor" else ""
    mode_map = {
        "bot_native": "Bot原生",
        "bot_native_clone_fallback": "Bot原生+克隆兜底",
        "user_native_only": "主账号原生专用",
    }
    mode_display = mode_map.get(FORWARD_MODE, FORWARD_MODE)
    if ALLOW_USER_NATIVE_FALLBACK:
        mode_display += "+主号兜底"
    return (
        f"🛡️ **雷达监控中心** `V{APP_VERSION}`{node_tag}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 **运行状态**　{status_icon}\n"
        f"⏱️ **在线时长**　`{d}天{h}时{m}分{s}秒`\n"
        f"📡 **转发总量**　`{FORWARDED_COUNT:,}` 条\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚙️ **系统核心**\n"
        f"├ 伪装设备　{ver_display}\n"
        f"{_build_version_status_line()}"
        f"├ 转发模式　`{mode_display}`\n"
        f"├ 过滤引擎　意图识别三层塔\n"
        f"├ 去重指纹　`{len(cache.dna_set):,}` / `{MAX_HISTORY_LINES:,}`\n"
        f"└ 消息队列　`{msg_queue.qsize()}` / `{msg_queue.maxsize}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_start_buttons():
    """构建主菜单按钮阵列"""
    return [
        [Button.inline("🖥️ 系统诊断", data=b"m_d"),
         Button.inline("🧪 过滤测试", data=b"m_test")],
        [Button.inline("📤 真实转发测试", data=b"m_forward_test")],
        [Button.inline("📈 数据统计", data=b"m_s"),
         Button.inline("⚡ 快捷设置", data=b"m_q")],
        [Button.inline("🔧 高级管理", data=b"m_adv"),
         Button.inline("⚙️ 系统与安全", data=b"m_sys")],
    ]


def _build_sys_panel():
    """构建系统与安全子面板"""
    status_text = "⏸ 已暂停" if FORWARDING_PAUSED else "🟢 运行中"
    security_lines = _build_security_status_lines()
    return (
        f"⚙️ **系统与安全**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔄 **转发状态**　{status_text}\n"
        f"{security_lines}\n\n"
        f"_**强制轮换**_ = 切换设备伪装指纹\n"
        f"_**重启雷达**_ = 断开全部连接后重启进程\n"
        f"_**紧急停止**_ = 暂停/恢复消息转发\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_sys_buttons():
    """构建系统与安全按钮"""
    stop_text = "✅ 恢复转发" if FORWARDING_PAUSED else "🛑 紧急停止"
    return [
        [Button.inline("♻️ 强制轮换", data=b"m_rotate"),
         Button.inline("🔄 重启雷达", data=b"m_restart")],
        [Button.inline(stop_text, data=b"m_stop")],
        [Button.inline("🔙 返回主菜单", data=b"m_back")],
    ]


def _build_diag_panel(stats, latency_ms):
    """构建系统诊断面板"""
    if latency_ms < 300:
        lat_emoji, lat_text = "🟢", "极佳"
    elif latency_ms < 1000:
        lat_emoji, lat_text = "🟡", "良好"
    else:
        lat_emoji, lat_text = "🔴", "较差"

    mem_pct = stats['sys_pct']
    cpu_pct = stats['cpu']
    disk_pct = stats['disk_pct']
    mem_emoji = "🟢" if mem_pct < 70 else ("🟡" if mem_pct < 85 else "🔴")
    cpu_emoji = "🟢" if cpu_pct < 50 else ("🟡" if cpu_pct < 80 else "🔴")
    disk_emoji = "🟢" if disk_pct < 70 else ("🟡" if disk_pct < 85 else "🔴")
    alive = _get_alive_count()
    total_accounts = 2 + len(forward_bots)  # admin + main + forward bots
    now_str = time.strftime('%Y-%m-%d %H:%M:%S')

    return (
        f"🖥️ **系统诊断报告**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{cpu_emoji} **整机CPU**　`{cpu_pct:.1f}%`\n"
        f"{mem_emoji} **内存**　　`{stats['sys_used_mb']:.0f}` / `{stats['sys_total_mb']:.0f}` MB"
        f"　(`{mem_pct:.1f}%`)\n"
        f"{disk_emoji} **磁盘**　　`{stats['disk_total_mb'] - stats['disk_free_mb']:.0f}` / `{stats['disk_total_mb']:.0f}` MB"
        f"　(`{disk_pct:.1f}%`)\n"
        f"{lat_emoji} **API延迟**　`{latency_ms:.0f}ms` ({lat_text})\n"
        f"👥 **存活账号**　`{alive}` / `{total_accounts}`\n"
        f"📦 **进程内存**　`{stats['proc_mb']:.1f}` MB\n"
        f"🧮 **进程CPU**　`{stats['proc_cpu']:.1f}%`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 `{now_str}`"
    )


def _build_quick_panel():
    """构建快捷设置面板"""
    target = db.get_config('TARGET_ID') or '未设置'
    backup = db.get_config('BACKUP_ID') or '未设置'
    return (
        f"⚡ **快捷设置面板**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"_修改转发目标和备份频道后，新消息将发往新地址。_\n"
        f"_VIP/白名单只能提高优先级，消息仍必须命中信息格式。_\n"
        f"_被屏蔽的用户/群组，其消息将被直接丢弃。_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📡 **转发目标**　`{target}`\n"
        f"🛡️ **备份频道**　`{backup}`\n"
        f"👑 **VIP 管理员**　`{len(cache.vip_admins)}` 人\n"
        f"🚫 **用户屏蔽**　`{len(cache.user_blacklist)}` 人\n"
        f"🚷 **群组屏蔽**　`{len(cache.muted_chats)}` 个\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_quick_buttons():
    """构建快捷设置按钮"""
    return [
        [Button.inline("📡 修改目标", data=b"m_q_t"),
         Button.inline("🛡️ 修改备份", data=b"m_q_b")],
        [Button.inline("👑 VIP 管理", data=b"m_q_v"),
         Button.inline("🚫 用户屏蔽", data=b"m_q_ub")],
        [Button.inline("🚷 群组屏蔽", data=b"m_q_um"),
         Button.inline("🔙 返回主菜单", data=b"m_back")],
    ]


def _build_adv_panel():
    """构建高级管理面板"""
    death_count = len(config.regex_config.get('KILL_PATTERNS', {}).get('DEATH_WORDS', []))
    custom_format_count = len(_get_custom_forward_formats())
    exclude_rule_count = len(config.get_rule_objects(action='exclude'))
    return (
        f"🔧 **高级管理面板**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"_**死刑词**_ = 命中即拦截的关键词\n"
        f"_**黑白名单**_ = 手动放行/拦截名单\n"
        f"_**信息格式**_ = 最终转发模板门\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"☠️ 死刑词：`{death_count}` 个\n"
        f"🛑 DB 黑名单：`{len(cache.blacklist)}` 个\n"
        f"🎯 DB 白名单：`{len(cache.whitelist)}` 个\n"
        f"🧩 自定义格式：`{custom_format_count}` 个\n"
        f"📐 排除规则：`{exclude_rule_count}` 条\n"
        f"📡 转发模式：`Bot 原生转发 + 自动兜底`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_adv_buttons():
    """构建高级管理按钮"""
    return [
        [Button.inline("☠️ 死刑词", data=b"m_adv_kill"),
         Button.inline("📋 黑白名单", data=b"m_adv_list")],
        [Button.inline("🧩 信息格式", data=b"m_adv_fmt"),
         Button.inline("📐 排除规则", data=b"m_adv_exclude")],
        [Button.inline("📋 拦截日志", data=b"m_log"),
         Button.inline("📑 转发日志", data=b"m_fwd_log")],
        [Button.inline("💾 手动备份", data=b"m_adv_bak"),
         Button.inline("🔄 热更新", data=b"m_adv_reload")],
        [Button.inline("🔙 返回主菜单", data=b"m_back")],
    ]


def _format_feature_preview(items, empty="-", max_items=4):
    values = [str(x).strip() for x in _coerce_feature_list(items) if str(x).strip()]
    if not values:
        return empty
    shown = " / ".join(values[:max_items])
    if len(values) > max_items:
        shown += f" / +{len(values) - max_items}"
    return shown


def _safe_regex_min_hits(value, default=0):
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def _format_validation_preview(fmt):
    regex_any = _coerce_feature_list(fmt.get('regex_any'))
    if not regex_any:
        return ""
    min_hits = _safe_regex_min_hits(fmt.get('regex_min_hits'), 1)
    return f"真实码格式 x{min_hits}" if min_hits > 1 else "真实码格式"


def _format_edit_template(fmt):
    def join_values(values):
        return "|".join(str(x).strip() for x in _coerce_feature_list(values) if str(x).strip())

    lines = [f"名称: {fmt.get('name', '未命名')}"]
    all_text = join_values(fmt.get('all'))
    any_text = join_values(fmt.get('any'))
    exclude_text = join_values(fmt.get('exclude'))
    if all_text:
        lines.append(f"必含: {all_text}")
    if any_text:
        lines.append(f"任一: {any_text}")
    if exclude_text:
        lines.append(f"排除: {exclude_text}")
    if len(lines) < 3:
        lines.append("必含: 在这里写必须出现的关键词")
        lines.append("任一: 在这里写任一出现即可的关键词")
    return "\n".join(lines)


def _build_format_edit_prompt(fmt):
    validation = _format_validation_preview(fmt)
    validation_line = f"\n\n隐藏校验：`{validation}` 会保留。" if validation else ""
    return (
        f"✏️ **修改信息格式：{fmt.get('name', '未命名')}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"复制下面这段，改关键词后发给我：\n\n"
        f"```text\n{_format_edit_template(fmt)}\n```\n"
        f"必含 = 每条消息都必须有。\n"
        f"任一 = 有一个就行。\n"
        f"排除 = 当前分支遇到这些就不匹配。"
        f"{validation_line}\n\n"
        f"修改内置格式会增加同名识别分支，系统默认格式仍保留。\n"
        f"要全局阻止某类消息，请使用黑名单或死刑词。\n"
        f"180 秒内发送。"
    )


def _get_format_by_index(index):
    formats = _get_effective_forward_formats()
    if 0 <= index < len(formats):
        return formats[index]
    return None


def _build_format_panel():
    """构建信息格式管理面板。"""
    formats = _get_effective_forward_formats()
    lines = [
        "🧩 **信息格式管理**",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "系统按下面这些格式判断是否转发。点编号按钮可直接修改，也可以点“输入序号”。",
        "",
    ]

    current_category = ""
    for idx, fmt in enumerate(formats, 1):
        category = str(fmt.get('category') or '其他')
        if category != current_category:
            current_category = category
            lines.append(f"**{category}**")

        source = fmt.get('source', '内置')
        flag = "✏️" if source == '自定义扩展' else ("➕" if source == '自定义' else "")
        validation = _format_validation_preview(fmt)
        lines.append(
            f"{idx}. `{fmt.get('name', '未命名')}` {flag}\n"
            f"   必含：{_format_feature_preview(fmt.get('all'), max_items=6)}\n"
            f"   任一：{_format_feature_preview(fmt.get('any'), max_items=6)}\n"
            f"   排除：{_format_feature_preview(fmt.get('exclude'), max_items=5)}"
            + (f"\n   校验：{validation}" if validation else "")
        )

    lines.extend([
        "",
        "✏️ = 已添加同名扩展；➕ = 你新增的格式。",
        "内置标准格式始终保底；要阻止某类消息请使用黑名单或死刑词。",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ])
    text = "\n".join(lines)
    return text[:3900] + "\n..." if len(text) > 3900 else text


def _build_format_buttons():
    formats = _get_effective_forward_formats()
    buttons = []
    row = []
    for idx in range(1, len(formats) + 1):
        row.append(Button.inline(f"✏️{idx}", data=f"m_adv_fmt_e_{idx}".encode()))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        Button.inline("🔢 输入序号修改", data=b"m_adv_fmt_pick"),
        Button.inline("➕ 新增格式", data=b"m_adv_fmt_add"),
    ])
    buttons.append([
        Button.inline("➖ 删除自定义", data=b"m_adv_fmt_del"),
        Button.inline("🔙 返回高级管理", data=b"m_adv_back"),
    ])
    return buttons


def _build_adv_kill_panel():
    """构建死刑词子面板"""
    words = config.regex_config.get('KILL_PATTERNS', {}).get('DEATH_WORDS', [])
    if words:
        display = '、'.join(words[:40])
        overflow = f"\n... 共 `{len(words)}` 个" if len(words) > 40 else ""
        body = f"`{display}`{overflow}"
    else:
        body = "📭 死刑词列表为空"
    return (
        f"☠️ **死刑词管理** (`{len(words)}` 个)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{body}\n\n"
        f"ℹ️ 死刑词与 DB 黑名单是两套规则。\n"
        f"测试显示“DB黑名单”时，请进入「黑白名单」处理。\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_adv_kill_buttons():
    """构建死刑词管理按钮"""
    return [
        [Button.inline("➕ 添加死刑词", data=b"m_adv_kill_add"),
         Button.inline("➖ 删除死刑词", data=b"m_adv_kill_del")],
        [Button.inline("📋 黑白名单", data=b"m_adv_list"),
         Button.inline("🔙 返回高级管理", data=b"m_adv_back")],
    ]


def _get_raw_exclude_rules():
    rules = config.regex_config.get('RULE_OBJECTS', [])
    return [
        item for item in rules
        if isinstance(item, dict)
        and str(item.get('action', '')).strip().lower() == 'exclude'
        and str(item.get('name', '')).strip()
    ]


def _build_adv_exclude_panel():
    """构建排除规则管理面板。"""
    rules = _get_raw_exclude_rules()
    enabled = [item for item in rules if item.get('enabled', True)]
    disabled = [item for item in rules if not item.get('enabled', True)]
    lines = [
        "📐 **排除规则管理**",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"✅ 生效中：`{len(enabled)}` 条 | ⏸ 已停用：`{len(disabled)}` 条",
        "",
    ]
    for item in enabled:
        lines.append(f"✅ `{item.get('name')}`")
    for item in disabled:
        lines.append(f"⏸ `{item.get('name')}`")
    if not rules:
        lines.append("📭 暂无排除规则")
    lines.extend([
        "",
        "删除会持久停用规则，重启后不会恢复；再次添加同名规则可重新启用。",
        "新增格式：`规则名|正则`",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ])
    text = "\n".join(lines)
    return text[:3900] + "\n..." if len(text) > 3900 else text


def _build_adv_exclude_buttons():
    return [
        [Button.inline("➕ 添加排除规则", data=b"m_adv_exclude_add"),
         Button.inline("➖ 删除排除规则", data=b"m_adv_exclude_del")],
        [Button.inline("🔙 返回高级管理", data=b"m_adv_back")],
    ]


def _build_adv_list_panel():
    """构建黑白名单子面板"""
    bl_text = '、'.join(list(cache.blacklist)[:30]) if cache.blacklist else '空'
    wl_text = '、'.join(list(cache.whitelist)[:30]) if cache.whitelist else '空'
    ub_text = '、'.join(list(cache.user_blacklist)[:30]) if cache.user_blacklist else '空'
    return (
        f"📋 **黑白名单管理**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛑 **DB 黑名单** (`{len(cache.blacklist)}` 个)\n`{bl_text}`\n\n"
        f"🎯 **DB 白名单** (`{len(cache.whitelist)}` 个)\n`{wl_text}`\n\n"
        f"🚫 **用户黑名单** (`{len(cache.user_blacklist)}` 个)\n`{ub_text}`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


def _build_adv_list_buttons():
    """构建黑白名单管理按钮"""
    return [
        [Button.inline("➕ 加黑关键词", data=b"m_adv_bl_add"),
         Button.inline("➖ 删黑关键词", data=b"m_adv_bl_del")],
        [Button.inline("➕ 加白关键词", data=b"m_adv_wl_add"),
         Button.inline("➖ 删白关键词", data=b"m_adv_wl_del")],
        [Button.inline("🔙 返回高级管理", data=b"m_adv_back")],
    ]


# --- /log 分页面板构建 ---
def _build_log_panel(records, total, offset, limit, search=""):
    """构建 /log 分页面板"""
    if not records:
        return "📭 **没有拦截记录。**", []

    page_num = offset // limit + 1
    total_pages = max(1, (total + limit - 1) // limit)

    header = f"🗑️ **拦截记录** (共 {total} 条)"
    if search:
        header += f" 🔍 `{search}`"
    header += f"\n第 {page_num}/{total_pages} 页\n━━━━━━━━━━━━━━━━━━━━\n\n"

    msg = header
    for ts, reason, content in records:
        short = content[:40].replace('\n', ' ') + "..." if len(content) > 40 else content.replace('\n', ' ')
        msg += f"⏱ `{ts}` 🛑 `{reason}`\n📝 _{short}_\n\n"

    # 构建翻页按钮
    buttons = []
    nav_row = []
    if offset > 0:
        prev_off = max(0, offset - limit)
        nav_row.append(Button.inline("⬅️ 上一页", data=f"m_log_{prev_off}_{search}".encode()))
    if offset + limit < total:
        next_off = offset + limit
        nav_row.append(Button.inline("下一页 ➡️", data=f"m_log_{next_off}_{search}".encode()))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([Button.inline("🔙 返回主菜单", data=b"m_back")])

    return msg, buttons


# --- /stats 面板文本构建 ---
def _build_stats_text(top_n=10):
    stats_data = cache.get_stats_summary()
    reasons = stats_data.get('reasons', {})
    reason_lines = ""
    if reasons:
        for reason, count in list(reasons.items())[:top_n]:
            reason_lines += f"  🛑 `{reason}` — `{count}` 次\n"
    else:
        reason_lines = "  📭 暂无数据\n"

    hit_lines = ""
    if rule_hits:
        sorted_hits = sorted(rule_hits.items(), key=lambda x: x[1], reverse=True)[:top_n]
        for name, count in sorted_hits:
            hit_lines += f"  🎯 `{name}` — `{count}` 次\n"
    else:
        hit_lines = "  📭 暂无数据\n"

    return (
        f"📊 **数据统计面板**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📈 **今日概览**\n"
        f"  ✅ 已转发：`{FORWARDED_TODAY}` 条\n"
        f"  🚫 已拦截：`{INTERCEPTED_TODAY}` 条\n"
        f"  📦 累计转发：`{FORWARDED_COUNT}` 条\n\n"
        f"🛑 **拦截原因分布 (近 500 条)**\n"
        f"{reason_lines}\n"
        f"🎯 **规则命中排行**\n"
        f"{hit_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def _build_fwd_log_list(count):
    """构建转发日志列表（代码块排版 + 管理按钮）"""
    entries, total = cache.get_forwarded_records(count)
    if not entries:
        return "📭 暂无转发记录。", [[Button.inline("🔙 返回主菜单", data=b"m_back")]]

    lines = [
        f"📑 **最近 {len(entries)} / {total} 条转发记录**",
        "━━━━━━━━━━━━━━━━━━━━\n",
        "```",
        f"{'序号':>4}  {'时间':<12}  {'摘要':<30}",
        f"{'─'*4}  {'─'*12}  {'─'*30}",
    ]
    for i, entry in enumerate(entries, 1):
        preview = entry['preview'][:28].replace('\n', ' ')
        ts = str(entry.get('ts') or '')[5:16]
        lines.append(f"{i:>4}  {ts:<12}  {preview:<30}")
    lines.append("```")

    msg = "\n".join(lines)
    buttons = []
    for i, entry in enumerate(entries):
        target_msg = entry.get('target_msg_id')
        portal_msg = entry.get('portal_msg_id') or 'none'
        sender_id = entry.get('sender_id') or 'none'
        btn_data = f"fmg_{target_msg}_{entry['src_chat_id']}_{entry['src_msg_id']}_{portal_msg}_{sender_id}"
        buttons.append([Button.inline(f"⚙️ #{i+1} 管理", data=btn_data.encode())])

    buttons.append([Button.inline("🔙 返回主菜单", data=b"m_back")])
    return msg, buttons


# --- /test 模拟事件对象 ---
class _MockMessage:
    def __init__(self):
        self.media = None
        self.reply_markup = None

class _MockEvent:
    def __init__(self):
        self.message = _MockMessage()


def _build_filter_verdict_paths(result):
    """把过滤结果渲染成三层塔命中路径，供 /test 与过滤试运行共用。"""
    path_lines = []
    if result.get('pass'):
        if '白名单' in result.get('detail', ''):
            path_lines.append("① 排除层 → ✅ 白名单命中，继续检查信息格式")
        elif 'VIP' in result.get('detail', ''):
            path_lines.append("① 排除层 → 跳过")
            path_lines.append("② 斩杀层 → 跳过")
            path_lines.append("③ VIP 命中 → ✅ 继续检查信息格式")
        else:
            path_lines.append("① 排除层 → 跳过")
            path_lines.append("② 斩杀层 → 跳过")
            path_lines.append(f"③ 领域层 → ✅ `{result.get('domain', '-')}`")
            path_lines.append(f"④ 特征层 → 优先级 `{result.get('priority', '-')}`")
    else:
        detail = result.get('detail', '')
        if '用户黑名单' in detail:
            path_lines.append("① 排除层 → 🚫 用户黑名单命中")
        elif 'DB黑名单' in detail:
            path_lines.append(f"① 排除层 → 🚫 `{detail}`")
        elif '推广链接' in detail:
            path_lines.append("① 排除层 → 跳过")
            path_lines.append("② 斩杀层 → 🚫 推广链接")
        elif '名额枯竭' in detail:
            path_lines.append("① 排除层 → 跳过")
            path_lines.append("② 斩杀层 → 🚫 名额枯竭")
        elif '死刑词' in detail:
            path_lines.append("① 排除层 → 跳过")
            path_lines.append(f"② 斩杀层 → 🚫 `{detail}`")
        elif '闲聊' in detail or '噪声' in detail:
            path_lines.append("① 排除层 → 跳过")
            path_lines.append("② 斩杀层 → 跳过")
            path_lines.append("③ 领域层 → 🚫 无领域信号（闲聊/噪声）")
        elif '无领域' in detail:
            path_lines.append("① 排除层 → 跳过")
            path_lines.append("② 斩杀层 → 跳过")
            path_lines.append("③ 领域层 → 🚫 无领域信号")
        else:
            path_lines.append(f"拦截原因：`{detail}`")
    return path_lines


# --- TTL 状态机（/add 白名单授权）---
_pending_whitelist = {}  # {user_id: {'expiry': timestamp, 'msg': Message}}
_filter_test_active = set()  # 正在等待过滤测试输入的 user_id 集合
_pending_review = {}  # {review_id: {'event': event, 'text': str, 'result': dict}} 待审核卡片
_fwd_log_active = set()  # 正在等待转发日志条数输入的 user_id
PENDING_REVIEW_TTL_SECONDS = 600
MAX_PENDING_REVIEWS = 100


def _prune_pending_reviews():
    """Keep manual-review state bounded and reject stale forwarding actions."""
    cutoff = time.time() - PENDING_REVIEW_TTL_SECONDS
    for review_id, review in list(_pending_review.items()):
        if review.get('ts', 0) < cutoff:
            _pending_review.pop(review_id, None)

    overflow = len(_pending_review) - MAX_PENDING_REVIEWS
    if overflow > 0:
        oldest_ids = sorted(_pending_review, key=lambda rid: _pending_review[rid].get('ts', 0))
        for review_id in oldest_ids[:overflow]:
            _pending_review.pop(review_id, None)


async def _run_keyword_block(uid):
    """等待用户输入关键词，自动加入持久化 DB 黑名单。"""
    try:
        async with admin_bot.conversation(int(uid), timeout=60) as conv:
            await conv.send_message(
                "🚫 **关键词屏蔽**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "请输入要屏蔽的关键词（将加入 DB 黑名单，重启后仍生效）："
            )
            response = await conv.get_response()
            keyword = response.text.strip()

            if not keyword or len(keyword) > 100:
                await conv.send_message("❌ 关键词长度需在 1-100 字符之间。")
                return

            already = keyword in cache.blacklist
            db.cursor.execute("INSERT OR IGNORE INTO blacklist (word) VALUES (?)", (keyword,))
            db.conn.commit()
            cache.reload_all()
            title = "关键词已存在" if already else "关键词已屏蔽"
            await conv.send_message(
                f"✅ **{title}**\n\n"
                f"🚫 `{keyword}` 已加入 DB 黑名单\n"
                f"📋 当前 DB 黑名单 `{len(cache.blacklist)}` 个"
            )

    except asyncio.TimeoutError:
        try:
            await admin_bot.send_message(int(uid), "⏰ **输入超时**，关键词屏蔽已取消。")
        except Exception:
            pass
    except Exception as e:
        try:
            await admin_bot.send_message(int(uid), f"❌ **关键词屏蔽失败:** `{e}`")
        except Exception:
            pass


async def _run_fwd_log_query(uid):
    """等待用户输入日志条数，返回对应日志列表"""
    _fwd_log_active.add(uid)
    try:
        async with admin_bot.conversation(int(uid), timeout=60) as conv:
            await conv.send_message(
                f"📑 **转发成功日志**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 当前共 `{cache.get_forwarded_records(1)[1]}` 条记录\n\n"
                f"请输入要查看的条数（1-100）："
            )
            response = await conv.get_response()
            text = response.text.strip()

            try:
                count = int(text)
                count = max(1, min(100, count))
            except ValueError:
                await conv.send_message("❌ 请输入有效数字（1-100）")
                return

            msg, buttons = _build_fwd_log_list(count)
            await conv.send_message(msg, buttons=buttons)

    except asyncio.TimeoutError:
        try:
            await admin_bot.send_message(int(uid), "⏰ **输入超时**，请重新点击「📑 转发日志」。")
        except Exception:
            pass
    except Exception as e:
        try:
            await admin_bot.send_message(int(uid), f"❌ **日志查询失败:** `{e}`")
        except Exception:
            pass
    finally:
        _fwd_log_active.discard(uid)


async def _run_filter_test(uid, chat_id):
    """等待用户发送测试文本，运行 L1/L2/L3 判定并返回报告"""
    _filter_test_active.add(uid)
    try:
        async with admin_bot.conversation(int(uid), timeout=120) as conv:
            # 第一步：必须用 conv.send_message 发送提示，初始化对话上下文
            await conv.send_message(
                "🧪 **过滤试运行**\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "⏳ **等待输入测试文本...** (120s)\n\n"
                "请直接发送一段文本，系统将模拟过滤引擎\n"
                "展示完整的命中路径和最终结论。\n\n"
                "💡 不会触发转发，纯模拟。"
            )

            # 第二步：等待用户回复
            response = await conv.get_response()
            text = response.text.strip() if response.text else ""

            if not text or len(text) > 2000:
                await conv.send_message("❌ **输入无效**，文本长度需在 1-2000 字符之间。")
                return

            # 运行 L1/L2/L3 判定
            mock_event = _MockEvent()
            code_match = config.get_pattern('CODE')
            code_m = _safe_regex_search(code_match, text)
            strict_code = config.get_pattern('STRICT_CODE')
            strict_codes = _safe_regex_findall(strict_code, text)

            allow_raw_single_code = _is_strict_single_registration_code(text)
            result = classify_intent(
                mock_event, text, None, False,
                code_m, len(strict_codes), "", allow_raw_single_code
            )
            result = _apply_required_forward_format_gate(
                text, result, allow_raw_single_code
            )

            path_lines = _build_filter_verdict_paths(result)

            verdict = "✅ **规则层通过**" if result['pass'] else "🚫 **规则层拦截**"
            dispatch_state = "⏸ 已暂停" if FORWARDING_PAUSED else "🟢 已启用"
            resp = (
                f"🧪 **过滤测试结果**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 测试文本：\n`{text[:200]}`\n\n"
                f"{''.join(chr(10) + l for l in path_lines)}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 结论：{verdict}\n"
                f"📂 领域：`{result.get('domain', '-')}` | 优先级：`{result.get('priority', '-')}`\n"
                f"📋 详情：`{result.get('detail', '-')}`\n"
                f"🔌 转发开关：`{dispatch_state}`\n\n"
                "⚠️ 这里只模拟规则分类，不代表实际发送。真实转发还会检查来源权限、屏蔽群组、消息时效、重复占位、Bot 可见性、队列和限流。"
            )
            if len(resp) > 4000:
                resp = resp[:4000] + "..."
            await conv.send_message(
                resp,
                buttons=[[Button.inline("🔙 返回主菜单", data=b"m_back")]]
            )

    except asyncio.TimeoutError:
        try:
            await admin_bot.send_message(int(uid), "⏰ **过滤测试已超时**\n\n请重新点击「🧪 过滤测试」按钮。")
        except Exception:
            pass
    except Exception as e:
        try:
            await admin_bot.send_message(int(uid), f"❌ **测试组件崩溃:** `{e}`")
        except Exception:
            pass
    finally:
        _filter_test_active.discard(uid)


# ================= JSON配置编辑工具 =================


async def _silent_delete_message(message):
    """尽力删除旧提示消息；失败不阻断后续流程。"""
    try:
        await message.delete()
    except Exception:
        pass


def _set_pending(uid, msg, ttl=60):
    # 覆盖旧通道前先清掉它的提示消息：旧 watcher 因 token 失配不会删它，不清理会残留。
    stale = _pending_whitelist.pop(uid, None)
    if stale and stale.get('msg') is not None:
        _spawn(_silent_delete_message(stale['msg']))
    token = f"{time.time_ns()}_{random.randint(1000, 9999)}"
    _pending_whitelist[uid] = {'expiry': time.time() + ttl, 'msg': msg, 'token': token}
    return token


def _get_pending(uid):
    entry = _pending_whitelist.get(uid)
    if entry and time.time() < entry['expiry']:
        return entry
    _pending_whitelist.pop(uid, None)
    return None


def _clear_pending(uid):
    _pending_whitelist.pop(uid, None)


async def _prompt_expiry_watcher(uid, prompt_msg, ttl):
    """TTL 到期后自动删除提示消息"""
    entry = _pending_whitelist.get(uid)
    token = entry.get('token') if entry else None
    await asyncio.sleep(ttl + 1)
    entry = _pending_whitelist.get(uid)
    if entry and entry.get('token') == token:
        _clear_pending(uid)
        try:
            await prompt_msg.delete()
        except Exception:
            pass


async def _add_expiry_watcher(uid, chat_id, msg_id):
    """60秒后自动过期白名单授权通道"""
    entry = _pending_whitelist.get(uid)
    token = entry.get('token') if entry else None
    await asyncio.sleep(61)
    entry = _pending_whitelist.get(uid)
    if entry and entry.get('token') == token and time.time() >= entry.get('expiry', 0):
        expire_text = entry.get(
            'expire_text',
            "⏰ **授权已过期**\n\n"
            "60 秒内未收到有效输入，通道已自动关闭。\n"
            "请重新发送 `/add` 开启授权。"
        )
        _clear_pending(uid)
        try:
            await admin_bot.edit_message(chat_id, msg_id, expire_text)
        except Exception:
            pass


# ================= TG 版本自动获取 =================

# GitHub API endpoints（Telegram 官方客户端仓库）
_TG_DESKTOP_RELEASES = "https://api.github.com/repos/telegramdesktop/tdesktop/releases/latest"
_TG_ANDROID_RELEASES = "https://api.github.com/repos/DrKLO/Telegram/releases/latest"

_VERSION_CHECK_INTERVAL = 6 * 3600  # 每 6 小时检查一次

# 仅作为空配置兜底，不用于覆盖已有版本。
_FALLBACK_DESKTOP = "6.7.8 x64"
_FALLBACK_ANDROID = "12.6.4"


def _version_tuple(value):
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', value or '')
    if not match:
        return ()
    return tuple(int(part) for part in match.groups())


def _is_newer_version(candidate, current):
    candidate_tuple = _version_tuple(candidate)
    if not candidate_tuple:
        return False
    current_tuple = _version_tuple(current)
    if not current_tuple:
        return True
    return candidate_tuple > current_tuple


def _fetch_latest_tg_versions():
    """从 GitHub 获取最新 TG 客户端版本号，返回 (android_ver, desktop_ver)"""
    headers = {"User-Agent": "TGMonitor/1.0", "Accept": "application/vnd.github.v3+json"}
    android_ver = None
    desktop_ver = None

    # Desktop: telegramdesktop/tdesktop releases/latest（tag 格式 vX.Y.Z → 加 x64 后缀）
    try:
        req = urllib.request.Request(_TG_DESKTOP_RELEASES, headers=headers)
        # The URL is a fixed HTTPS GitHub API constant, never user-controlled.
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            data = json.loads(resp.read().decode())
            tag = data.get("tag_name", "")
            ver = tag.lstrip("vV")
            if re.match(r'^\d+\.\d+\.\d+', ver):
                desktop_ver = ver + " x64"
                logger.info(f"🖥 TG Desktop 最新版本: {desktop_ver}")
    except Exception as e:
        logger.warning(f"⚠️ 获取 Desktop 版本失败: {e}")

    # Android: DrKLO/Telegram releases/latest（tag 格式 release-X.Y.Z-BUILD → 提取 X.Y.Z）
    try:
        req = urllib.request.Request(_TG_ANDROID_RELEASES, headers=headers)
        # The URL is a fixed HTTPS GitHub API constant, never user-controlled.
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            data = json.loads(resp.read().decode())
            tag = data.get("tag_name", "")
            m = re.search(r'(\d+\.\d+\.\d+)', tag)
            if m:
                android_ver = m.group(1)
                logger.info(f"📱 TG Android 最新版本: {android_ver}")
    except Exception as e:
        logger.warning(f"⚠️ 获取 Android 版本失败: {e}")

    return android_ver, desktop_ver


def _select_version_update(current, candidate, manual, label):
    if manual:
        if manual != current:
            return manual, f"{label} 手动覆盖"
        logger.info(f"{label} 手动版本与当前一致: {current}")
        return None, ""

    if candidate:
        if _is_newer_version(candidate, current):
            return candidate, f"{label} 自动升级"
        logger.info(f"{label} 自动版本未高于当前，保持不变: current={current} candidate={candidate}")

    return None, ""


def _update_device_versions(android_ver=None, desktop_ver=None, source="auto"):
    """更新设备配置文件中的版本号：手动覆盖优先，自动只升不降。"""
    try:
        with open(str(DEVICE_CONFIG_FILE), 'r', encoding='utf-8-sig') as f:
            data = json.load(f)

        profiles = data.get('profiles', {})
        changed = False

        if android_ver and 'oneplus_13' in profiles:
            old = profiles['oneplus_13'].get('app_version', '')
            selected, reason = _select_version_update(
                old, android_ver, TG_ANDROID_APP_VERSION if source == "manual" else "", "Android"
            )
            if selected:
                profiles['oneplus_13']['app_version'] = selected
                changed = True
                logger.info(f"📱 {reason}: {old} → {selected}")

        if desktop_ver and 'r9000p' in profiles:
            old = profiles['r9000p'].get('app_version', '')
            selected, reason = _select_version_update(
                old, desktop_ver, TG_DESKTOP_APP_VERSION if source == "manual" else "", "Desktop"
            )
            if selected:
                profiles['r9000p']['app_version'] = selected
                changed = True
                logger.info(f"🖥 {reason}: {old} → {selected}")

        if changed:
            # 原子写入
            tmp_path = str(DEVICE_CONFIG_FILE) + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(DEVICE_CONFIG_FILE))
            _secure_runtime_permissions()
            config.device_config = data
            # 同步更新运行时 device_profile 引用
            global device_profile
            device_profile = config.get_device_profile()
            return True
    except Exception as e:
        logger.error(f"❌ 版本更新写入失败: {e}")
    return False


async def auto_version_check_task():
    """后台任务：每 6 小时从 GitHub 获取版本号，只写入更高版本。"""
    # 首次启动延迟 30 秒，避免启动时网络未就绪
    await asyncio.sleep(30)
    while True:
        try:
            android_ver, desktop_ver = await asyncio.get_event_loop().run_in_executor(
                None, _fetch_latest_tg_versions
            )
            if android_ver or desktop_ver:
                if _update_device_versions(android_ver, desktop_ver, source="auto"):
                    logger.info("📦 TG 版本配置已更新，当前连接需重启后才会使用新版本号")
            else:
                logger.warning("⚠️ 未获取到 TG 最新版本，保留当前设备配置")
        except Exception as e:
            logger.error(f"❌ 版本检查异常: {e}")
        await asyncio.sleep(_VERSION_CHECK_INTERVAL)


def _apply_manual_version_overrides():
    if TG_ANDROID_APP_VERSION or TG_DESKTOP_APP_VERSION:
        changed = _update_device_versions(
            TG_ANDROID_APP_VERSION or None,
            TG_DESKTOP_APP_VERSION or None,
            source="manual",
        )
        if changed:
            logger.info("📦 手动 TG 版本覆盖已写入，本次启动将使用更新后的设备配置")


# ================= 消息队列 =================
msg_queue = asyncio.Queue(maxsize=200)  # V11.07: 100->200（多群同时爆发时 100 条易满 -> 丢消息；200 条约 1~2MB，安全）


# ================= DNA指纹提取 =================
def _flatten_regex_hits(items):
    tokens = []
    for item in items or []:
        if isinstance(item, tuple):
            tokens.extend(str(x) for x in item if x)
        elif item:
            tokens.append(str(item))
    return tokens


_START_PAYLOAD_RE = re.compile(r'(?i)[?&]start(?:app|group)?=([a-z0-9_\-]+)')
_REGISTER_CODE_RE = re.compile(r'(?i)[a-z0-9\-]*?(?:register|renew)_[a-z0-9_\-\*\?]{5,}')


def _append_markup_urls(text, markup):
    full_content = text or ""
    if markup:
        for row in getattr(markup, 'rows', []):
            for btn in row.buttons:
                if hasattr(btn, 'url') and btn.url:
                    full_content += " " + btn.url
    return full_content


def _normalize_dna_text(text, limit=1200):
    normalized = "".join(re.findall(r'[\w一-龥]', text or ''))
    return normalized[:limit]


def _normalize_dedup_text(text, limit=160):
    """去重归一化（借鉴 SlowLink normalize_for_text_dedup）：NFKC → 剥零宽 →
    逐行删动态元信息（已参与人数/中奖概率/参与人数/消耗碎片/满员/种子哈希/转发自/时间戳）→
    大数字占位符归一化（UUID/HASH/DATE/TIME/FRAC/PCT/NUM）→ 只保留文字。
    让「同一活动在不同人数/概率/倒计时状态下」算出同一指纹；
    4 位以下的小数字保留（如活动名「双11」不被抹平）。"""
    raw = unicodedata.normalize("NFKC", text or "")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", raw)

    kept = []
    for line in (x.strip() for x in raw.splitlines() if x.strip()):
        low = line.lower()
        if (
            re.search(r"已参与[：:\s]*\d+人", line)
            or re.search(r"中奖概率[：:\s]*[\d.]+%?", line)
            or re.search(r"(?:当前)?参与人数[：:\s]*\d+", line)
            or re.search(r"消耗\s*[\d.]+\s*碎片", line)
            or re.search(r"(?:满|满员|已满|满额)\s*\d*人?", line)
            or low.startswith("随机种子哈希") or low.startswith("random seed")
            or re.search(r'(?i)^(forwarded from|转发自)\b', line)
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", line)
        ):
            continue
        kept.append(line)
    base = "\n".join(kept) if kept else raw

    # 数字占位符归一化（顺序重要：先长格式后短格式）。
    # 用 (?<!\d)/(?!\d) 而非 \b：Python 的 \b 把中文当 \w，「3600秒」这类数字后跟中文时 \b 会失效。
    base = re.sub(r"(?<![a-f0-9])[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}(?![a-f0-9])", "UUID", base, flags=re.I)
    base = re.sub(r"(?<![a-f0-9])[a-f0-9]{40,}(?![a-f0-9])", "HASH", base, flags=re.I)
    base = re.sub(r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)", "DATE", base)
    base = re.sub(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)", "TIME", base)
    base = re.sub(r"(?<!\d)\d+/\d+(?!\d)", "FRAC", base)
    base = re.sub(r"(?<!\d)\d+\.\d+%?(?!\w)", "PCT", base)
    base = re.sub(r"(?<!\d)\d+%(?!\w)", "PCT", base)
    base = re.sub(r"(?<!\d)\d{4,}(?!\d)", "NUM", base)

    base = base.lower()
    return "".join(re.findall(r'[\w一-龥]', base))[:limit]


def _normalize_match_text(text):
    """识别/去重用归一化：NFKC + 剥零宽/不可见字符（借鉴 SlowLink normalize_text）。
    防止「购\\u200b买」「全角数字」这类防封变体绕过关键词与正则；
    只喂给匹配和指纹，原文仍用于转发与预览。"""
    raw = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", raw)


def _normalize_lottery_deadline_text(value):
    """Round noisy bot countdown seconds to a stable five-minute draw slot."""
    def _round_time(match):
        hour, minute, second = (int(item or 0) for item in match.groups())
        total_seconds = hour * 3600 + minute * 60 + second
        rounded_seconds = ((total_seconds + 150) // 300) * 300
        rounded_seconds = min(rounded_seconds, 23 * 3600 + 55 * 60)
        rounded_hour, remainder = divmod(rounded_seconds, 3600)
        rounded_minute = remainder // 60
        return f"{rounded_hour:02d}:{rounded_minute:02d}:00"

    return re.sub(
        r'(?<!\d)([01]?\d|2[0-3])\s*(?::|时)\s*([0-5]?\d)'
        # 尾随的「分/秒」单位字兜底消费：无秒写法「20时00分」若不吃掉「分」，
        # 归一结果会残留「分」字（`..2000分`），与「20:00」写法打散成两个指纹（V10.89 自检发现）。
        r'(?:\s*(?::|分)\s*([0-5]?\d)(?:\s*秒)?)?\s*(?:分|秒)?',
        _round_time,
        value or '',
    )


def _parse_lottery_deadline(text, now=None):
    """Parse the stated lottery draw deadline; None when absent or unparseable."""
    if not text:
        return None

    tz = timezone(timedelta(hours=8))
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    label_match = re.search(
        r'(?:截止时间|开奖日期|开奖时间|自动开奖时间|抽奖日期)[\s：:]*([\s\S]{0,100})',
        text,
        re.IGNORECASE,
    )
    if not label_match:
        return None
    value = label_match.group(1)

    relative = re.search(r'(?P<day>今天|今日|明天|明日|后天)', value)
    if relative:
        offset = {'今天': 0, '今日': 0, '明天': 1, '明日': 1, '后天': 2}[relative.group('day')]
        date_value = now.date() + timedelta(days=offset)
        date_start = relative.end()
        year, month, day = date_value.year, date_value.month, date_value.day
    else:
        date_match = re.search(
            r'(?:(?P<year>20\d{2})\s*(?:年|[-/.])\s*)?'
            r'(?P<month>\d{1,2})\s*(?:月|[-/.])\s*'
            r'(?P<day>\d{1,2})\s*(?:日|号)?',
            value,
        )
        if not date_match:
            return None
        year = int(date_match.group('year') or now.year)
        month = int(date_match.group('month'))
        day = int(date_match.group('day'))
        date_start = date_match.end()

    time_match = re.search(
        r'(\d{1,2})\s*(?:时|点|:)\s*(\d{1,2})?'
        r'(?:\s*(?:分|:)\s*(\d{1,2}))?',
        value[date_start:],
    )
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2) or 0)
        second = int(time_match.group(3) or 0)
    else:
        # A date-only draw is no longer joinable after that local calendar day.
        hour, minute, second = 23, 59, 59

    timezone_match = re.search(
        r'\b(?:UTC|GMT)\s*(?P<sign>[+-])?\s*(?P<hour>\d{1,2})?'
        r'(?:\s*[:.]?\s*(?P<minute>\d{2}))?\b',
        value,
        re.IGNORECASE,
    )
    deadline_tz = tz
    if timezone_match:
        if timezone_match.group('sign'):
            offset_minutes = (
                int(timezone_match.group('hour') or 0) * 60
                + int(timezone_match.group('minute') or 0)
            )
            if timezone_match.group('sign') == '-':
                offset_minutes = -offset_minutes
        else:
            offset_minutes = 0
        if -23 * 60 <= offset_minutes <= 23 * 60 + 59:
            deadline_tz = timezone(timedelta(minutes=offset_minutes))

    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=deadline_tz)
    except ValueError:
        return None


def _lottery_draw_expired_at(text, now=None):
    """Return a past lottery deadline, including date-only and relative formats."""
    tz = timezone(timedelta(hours=8))
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    deadline = _parse_lottery_deadline(text, now=now)
    return deadline if deadline is not None and deadline < now else None


def _forward_safety_rejection(event, text, now_ts=None):
    """Reject stale or expired content at every path that can send a message."""
    message = getattr(event, 'message', None)
    if message is None:
        return "[drop] 源消息不可用"

    now_ts = time.time() if now_ts is None else now_ts
    now_dt = datetime.fromtimestamp(now_ts, timezone(timedelta(hours=8)))
    is_lottery = re.search(r'(?:抽奖|抽獎|giveaway)', text or '', re.IGNORECASE) is not None

    # 转发消息的原帖时间可能远早于本轮抽奖，不能据此判老：
    # 只要卡片声明了尚未到期的截止时间，就认定其仍然有效，跳过年龄门。
    lottery_still_open = False
    if is_lottery and getattr(getattr(message, 'fwd_from', None), 'date', None):
        deadline = _parse_lottery_deadline(text, now=now_dt)
        lottery_still_open = deadline is not None and deadline >= now_dt

    if not lottery_still_open:
        activity_time = _message_activity_datetime(message)
        if activity_time:
            try:
                age = now_ts - activity_time.timestamp()
            except (AttributeError, TypeError, ValueError, OSError):
                age = None
            # V11.06: forwarded messages use a looser limit (default 24h) - the original post time
            # should only identify truly stale content, not block content that was just forwarded in
            # (live case: a forwarded "shrekpublic_bot generated 20 codes" post, original 76 min old,
            #  was blocked by the 1800s limit while the rule layer / filter test passed).
            age_limit = MAX_MESSAGE_AGE_SECONDS
            if getattr(message, 'fwd_from', None):
                age_limit = max(age_limit, FORWARD_ORIGIN_MAX_AGE_SECONDS)
            if age is not None and age > age_limit:
                return f"[drop] 消息已过期 {int(age)}s>{age_limit}s"

    if is_lottery and not lottery_still_open and getattr(message, 'edit_date', None):
        original_time = (
            getattr(getattr(message, 'fwd_from', None), 'date', None)
            or getattr(message, 'date', None)
        )
        if original_time:
            try:
                original_age = now_ts - original_time.timestamp()
            except (AttributeError, TypeError, ValueError, OSError):
                original_age = None
            if original_age is not None and original_age > MAX_MESSAGE_AGE_SECONDS:
                return (
                    f"[drop] 编辑抽奖原消息已过期 "
                    f"{int(original_age)}s>{MAX_MESSAGE_AGE_SECONDS}s"
                )

    if is_lottery:
        expired_at = _lottery_draw_expired_at(text, now=now_dt)
        if expired_at:
            return f"[drop] 开奖日期已过期:{expired_at.strftime('%Y-%m-%d %H:%M:%S %z')}"
    return ""


def _queue_delay_rejection(queued_at, now_ts=None):
    """Reject a candidate that waited too long after entering the forwarding queue."""
    try:
        queued_at = float(queued_at)
    except (TypeError, ValueError):
        return "[drop] 队列入队时间无效"

    now_ts = time.time() if now_ts is None else now_ts
    delay = now_ts - queued_at
    if delay > MAX_QUEUE_DELAY_SECONDS:
        return f"[drop] 队列等待超时 {int(delay)}s>{MAX_QUEUE_DELAY_SECONDS}s"
    return ""


def _lottery_card_dna_signature(text):
    """Return a stable per-activity signature without collapsing later lottery rounds."""
    stable_text = re.sub(r'(?im)^\s*(?:转发自|forwarded from)\s*[^\r\n]*\r?\n?', '', text or '')
    flat = re.sub(r'\s+', ' ', stable_text)
    title = re.split(
        # 切分标签（借鉴 SlowLink `lottery_section_labels`，15 个）。
        # 关键：**不含「抽奖活动已开始」**——它是卡片开头的标志语，若作为切分点会把 title 切成空，
        # 导致整卡指纹为空、退回对动态字段敏感的通用指纹（重复转发隐患）。
        # 另补「截止」（无「时间」二字的写法）、「奖品」（单数）、「发布群组/口令/活动详情/活动说明/开奖条件/定时开奖/创建者/发起人」。
        r'(?:抽奖条件|奖品内容|奖品|开奖日期|截止时间|截止|开奖时间|奖项设置|参与方式|当前状态|参与设置|参与要求|参与关键词'
        r'|发布群组|口令|活动详情|活动说明|开奖条件|定时开奖|创建者|发起人)',
        stable_text, maxsplit=1
    )[0]
    group = re.search(r'发布群组\s*[：:]\s*(?:▸\s*)?([^\s(]{2,80})', flat)
    deadline = re.search(r'(?:截止时间|截止|开奖日期|开奖时间)\s*[：:]\s*([^\r\n]*)(?:\r?\n\s*([^\r\n]+))?', stable_text)
    # 口令可能紧跟在标签后，也可能换行 + 引号包裹（「」『』""），两种写法都要吃。
    cmd = re.search(
        r'(?:抽奖口令|口令)\s*[：:]\s*[「『"\']?([^\r\n「」『』"\']{2,80})',
        stable_text
    )

    # 标题区含动态字段（参与人数/已参与/概率/倒计时等），这些变化不代表活动变化，必须剔除：
    # 否则同一活动在不同人数状态下指纹不同 → 去重失效、重复转发（V10.48 事故根因）。
    # 采用 SlowLink 的数字占位符归一化：逐行删动态元信息 + 大数字抹平（比字段名枚举更通用）。
    # V10.91：剔除「抽奖活动已开始」这类**状态标语**——它在标题区（切分标签之前）出现与否
    # 会让同一活动的 title 不同（真实案例：某群 两模板 → 2 个指纹），是本次回归集抓到的漏。
    # 注意：若整段标题都是状态语（如卡片开头直接是「抽奖活动已开始！」），剔除后会变空 → 回退原值，
    # 否则指纹为空会退回动态敏感的通用指纹（旧断言「以『抽奖活动已开始』开头的卡指纹不得为空」即为此设）。
    _title_stripped = re.sub(r'(?:抽奖)?活动(?:已|即将|马上)?开始[！!。～~·\s]*', '', title)
    title = _title_stripped if _title_stripped.strip() else title
    title_sig = _normalize_dedup_text(title, limit=160)
    deadline_text = deadline.group(1).strip() if deadline else ''
    if deadline and not re.search(r'(?:\d{4}[-年/.]|\d{1,2}:\d{2})', deadline_text):
        deadline_text = f"{deadline_text} {deadline.group(2) or ''}".strip()
    # 统一开奖时间的写法差异（V10.88「同一活动不同模板」重复事故）：
    #   图1「开奖日期:(UTC+8)\n2026年10月04日 20时00分00秒」 vs 图2「定时开奖日期: 2026-10-04 20:00」
    # 旧逻辑只剥**行尾**的 UTC → 图1 的 (UTC+8) 残留在开头；且中文年月日 / 冒号秒未统一
    # → 同一活动 deadline_sig 不同 → 指纹打散 → 重复转发。
    # ① 任意位置剥时区（含括号写法）；② 时分秒→冒号并取整；③ 年月日→短横线；④ 去掉秒。
    deadline_text = re.sub(r'(?i)[（(]?\s*(?:UTC|GMT)\s*[+-]\s*\d{1,2}\s*[)）]?', ' ', deadline_text)
    deadline_text = _normalize_lottery_deadline_text(deadline_text)
    deadline_text = re.sub(
        r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
        lambda m: f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",   # 月日补零：10月4日 = 10月04日
        deadline_text,
    )
    deadline_text = re.sub(r'(\d{1,2}:\d{2}):\d{2}', r'\1', deadline_text)
    deadline_text = re.sub(r'\s+', ' ', deadline_text).strip()
    deadline_sig = _normalize_dna_text(deadline_text) if deadline_text else ''

    # 抽奖口令是本类活动的身份键：跨群转发、重新排版后依然一致。
    # 有口令时指纹只认「口令 + 开奖时间」，不再拼 160 字长标题 ——
    # 标题里的排版/表情/按钮差异会把同一活动打散成多个指纹，导致同卡被反复转发。
    cmd_sig = _normalize_dna_text(cmd.group(1), limit=80) if cmd else ''
    if len(cmd_sig) >= 4:
        return '|'.join((cmd_sig, deadline_sig)) if deadline_sig else cmd_sig

    if not title_sig:
        return ''

    if not deadline_sig:
        # Person-count draws may omit a deadline.  Use the text body only, never
        # inline-button URLs, so reposts of the same activity share one key.
        body_sig = _normalize_dedup_text(stable_text, limit=900)
        return '|'.join((title_sig, body_sig)) if body_sig else ''

    parts = [title_sig, deadline_sig]
    if group:
        parts.append(_normalize_dna_text(group.group(1)))
    return '|'.join(parts)


def _lottery_extra_identity_hashes(text):
    """结构化抽奖卡的『跨模板身份』（借鉴 SlowLink 多身份去重，bot_runner `code_identities`）。

    同一活动常被多个机器人用不同模板转发（官方卡带口令、镜像卡无口令），单一指纹无法归一。
    这里提取三类额外身份，**任一命中即判重**：
      ① 「奖品名|开奖时间」——奖品名与开奖时间是跨模板最稳定的公共字段（数量 x100/×5 会被剥掉）；
      ② 「随机种子哈希」——有种子时是活动的唯一标识，最稳（借鉴 SlowLink `lottery_seed_pattern`）；
      ③ 「口令|首个奖品」（**不含时间**）——同一活动「改期」时截止时间会变，前两类都会被打散，
         而口令是活动专属、奖品是活动内容，组合足以识别活动且不受改期影响（V11.02 线上重复事故）。
    开奖时间字段覆盖「截止时间/截止/开奖日期/开奖时间/**定时开奖**/开启报名」，
    日期格式兼容 `2026年10月4日 20:00`、`2026-10-04 20:00`、`2026/10/04 20:00`。
    返回额外身份的 md5 列表（可能为空）。"""
    if not text:
        return []
    stable_text = _normalize_match_text(text)
    if not stable_text:
        return []
    ids = []
    # ── 身份①：随机种子哈希（有则最强） ──
    seed_m = re.search(r'(?:随机种子哈希|种子哈希|随机种子)\s*[：:]?\s*([0-9a-fA-F]{8,})', stable_text)
    if seed_m:
        ids.append(
            hashlib.md5(
                ("LOTTERY_SEED_V1:" + seed_m.group(1).lower()[:16]).encode(),
                usedforsecurity=False,
            ).hexdigest()
        )
    # ── 身份②：奖品名|开奖时间 ──
    prize_m = re.search(
        # 「奖品列表:」也是常见写法（V10.88：漏了它会让镜像卡的跨模板身份为空）
        r'(?:奖品内容|奖品列表|奖品)\s*[：:]?\s*\r?\n?\s*[·•]?\s*([^\r\n]{2,60})', stable_text
    )
    deadline_m = re.search(
        # 日期可能写在标签的**下一行**（「开奖日期:(UTC+8)\n2026年10月04日 20时00分00秒」），
        # 故用 group(1)=同行 + group(2)=下一行 拼接后再解析（V10.88）。
        r'(?:截止时间|截止|开奖日期|开奖时间|定时开奖|开启报名)\s*[：:]\s*([^\r\n]*)(?:\r?\n\s*([^\r\n]+))?',
        stable_text,
    )
    if prize_m and deadline_m:
        prize_raw = re.sub(r'[xX×✕*]\s*\d+', '', prize_m.group(1))       # 剥数量 x100 / ×5
        # 同一行可能并列多个奖品（「• A ×2 • B ×2」）→ 只取第一项做身份，否则镜像卡身份不同（V10.88）
        prize_raw = re.split(r'\s*[•·|]\s*', prize_raw)[0]
        prize_norm = ''.join(re.findall(r'[一-龥a-zA-Z]', prize_raw))     # 只留中文与字母
        # 日期兼容「2026年10月4日 / 2026-10-04 / 2026/10/04」+ 时:分（精确到分钟，不四舍五入到 5 分钟）
        core_m = re.search(
            # 兼容「2026年10月04日 20时00分00秒」/「2026-10-04 20:00」/「2026/10/04 20:00」（时|分 也算分隔符）
            r'(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?[\s,]*(\d{1,2})\s*[:：时]\s*(\d{1,2})',
            f"{deadline_m.group(1) or ''} {deadline_m.group(2) or ''}",
        )
        if len(prize_norm) >= 4 and core_m:
            dl_key = (
                f"{core_m.group(1)}-{int(core_m.group(2)):02d}-{int(core_m.group(3)):02d} "
                f"{int(core_m.group(4)):02d}:{int(core_m.group(5)):02d}"
            )
            ids.append(
                hashlib.md5(
                    ("LOTTERY_PRIZE_V1:" + prize_norm + "|" + dl_key).encode(),
                    usedforsecurity=False,
                ).hexdigest()
            )
    # ── 身份③：口令|首个奖品（**不含时间**）──
    # 同一活动「改期」时两条卡的截止时间不同（实例 2026-10-03：22:14:24 vs 17:59:59），
    # 主键(cmd|deadline) 与身份②(奖品|截止) **都会被打散** → 重复转发。
    # 口令是活动专属、奖品是活动内容，两者组合足以识别活动，且**不受改期影响**（V11.02 线上重复事故）。
    cmd_m = re.search(r'(?:抽奖口令|口令)\s*[：:]\s*[「『"\']?([^\r\n「」『』"\']{2,80})', stable_text)
    if cmd_m:
        cmd_norm = ''.join(re.findall(r'[一-龥a-zA-Z0-9]', cmd_m.group(1)))
        if len(cmd_norm) >= 2 and prize_m:
            _pk = re.split(r'\s*[•·|]\s*', re.sub(r'[xX×✕*]\s*\d+', '', prize_m.group(1)))[0]
            pk_norm = ''.join(re.findall(r'[一-龥a-zA-Z]', _pk))
            if len(pk_norm) >= 4:
                ids.append(
                    hashlib.md5(
                        ("LOTTERY_CMD_PRIZE_V1:" + cmd_norm + "|" + pk_norm).encode(),
                        usedforsecurity=False,
                    ).hexdigest()
                )
    # ── 身份④：满人开奖卡（无口令 / 无开奖时间）的跨模板身份 ──
    # 场景：同一活动的两种模板（官方卡「奖品 + 发布群组 / 参与要求」vs 机器人卡「方式 / 订阅 / 奖品」）
    #   都没有口令与开奖时间 → 主指纹退化为「标题|全文归一化」，两模板文本必然不同 → 重复转发
    #   （用户 2026-10-09 线上案例：某兑换码 · 满人开奖 目标 500 人）。
    # 取「目标人数 + 首个奖品 + 订阅群集合」三元组：跨模板稳定；三者必须**同时**相同才判重，
    #   避免把「同群但不同奖品」的多个活动误判为一个。仅在无口令、无时间时启用，影响面最小。
    if not deadline_m and not cmd_m and prize_m:
        count_m = re.search(r'(?:达到|目标|满)\s*(\d{1,6})\s*人', stable_text)
        sub_groups = set()
        for sub in re.findall(r'订阅\s*[：:]?\s*([^\r\n]{2,120})', stable_text):
            for part in re.split(r'[，,、；;|/]', sub):
                g_norm = ''.join(re.findall(r'[一-龥a-zA-Z]', part)).lower()
                if len(g_norm) >= 2:
                    sub_groups.add(g_norm)
        _prize_core = re.split(r'[（(]', prize_m.group(1))[0]      # 先截断括号补充说明（跨模板一致）
        _prize_core = re.sub(r'[xX×✕*]\s*\d+', '', _prize_core)   # 剥数量 x3 / ×5
        _prize_core = re.split(r'\s*[•·|]\s*', _prize_core)[0]    # 同行并列多个奖品时只取第一项
        prize_norm = ''.join(re.findall(r'[一-龥a-zA-Z]', _prize_core))
        if count_m and len(prize_norm) >= 4 and sub_groups:
            payload = '|'.join([count_m.group(1), prize_norm] + sorted(sub_groups))
            ids.append(
                hashlib.md5(
                    ("LOTTERY_FULL_OPEN_V1:" + payload).encode(),
                    usedforsecurity=False,
                ).hexdigest()
            )
    return ids


# ── 掩码码去重（同一码的「完整版」与「掩码版」只发一次，可开关回退）──
# 场景：同一注册码完整版先发、掩码版（* 挡住部分字符）后发（或反之），只转一次；
# 但**不同的码（即使前缀相同）都要照发**。回退：设 GUESS_CHAIN_DEDUP_ENABLED=false。
# 掩码通配符：* 与 ?（各代表 1 个未知字符）。汉字/字母/数字等其它占位无法可靠区分「占位 vs 真实字符」，不自动识别。
GUESS_CHAIN_DEDUP_ENABLED = os.environ.get("GUESS_CHAIN_DEDUP_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
_SEEN_FULL_SEGMENTS = {}     # 已转发的完整码段 -> 过期时间戳
_SEEN_MASKED_SEGMENTS = {}   # 已转发的掩码码段 -> 过期时间戳
_CODE_SEGMENT_TTL_SECONDS = 6 * 3600
_CODE_SEGMENT_MAX = 1000


_PLACEHOLDER_CHARS = set("xX*?·•")


def _is_fake_code_body(body):
    """判定「占位码 / 示例码」——它们不是真码，不得因此放行（2026-10-04 线上误放事故）。

    规则（任一命中即视为假码）：
      ① 码体全是占位符字符（x / * / ? / · / •）——典型「解密后长这样 Register_xxxxxxx」；
      ② 码体高度规律：全同字符（aaaaaa）或**交替递增**（123456 / abcdef / 1A2B3C / 4D5E6F）。
    短码（<4 字符）不做规律判定，避免误伤真实短码。
    """
    if not body:
        return False
    if all(ch in _PLACEHOLDER_CHARS for ch in body):          # ① 纯占位符
        return True
    if len(body) < 4:
        return False
    if len(set(body)) == 1:                                   # ② 全同
        return True
    # 按字符类型分组：数字组与字母组**各自**单调递增（或全同）→ 规律串
    # （覆盖 123456 / abcdef / 1A2B3C / 4D5E6F / aaaaaa 等；真码是随机的，不会两组同时递增）
    digits = [c for c in body if c.isdigit()]
    alphas = [c.lower() for c in body if c.isalpha()]
    if len(digits) + len(alphas) != len(body):
        return False                                          # 含其它符号 → 交给占位符规则判断

    def _monotonic(seq):
        if len(seq) < 2:
            return True
        if len(set(seq)) == 1:                                # 全同
            return True
        vals = [ord(c) for c in seq]
        return all(b - a == 1 for a, b in zip(vals, vals[1:], strict=False))  # 严格 +1 递增

    return _monotonic(digits) and _monotonic(alphas)


def _all_codes_are_fake(text):
    """消息里出现的 Register_/Renew_ 码**全部**是占位/示例码 → 视为不含有效码。

    只拦「全假」：只要有一个真码就放行（真实卡常混排多个码）。
    """
    bodies = re.findall(
        r'(?:Register|Renew)_([A-Za-z0-9*?!@#$%^&()_+=~\-]{2,})', text or '', re.I
    )
    if not bodies:
        return False
    return all(_is_fake_code_body(b) for b in bodies)


def _register_code_segment(text):
    """提取 Register_/Renew_ 后的码段（到空白/换行边界为止），无码返回空串。"""
    if not text or ('Register' not in text and 'Renew' not in text):
        return ""
    m = re.search(r'(?:Register|Renew)_([A-Za-z0-9*?!@#$%^&()_+=~\-]{2,})', text, re.I)
    return m.group(1) if m else ""


def _segment_as_mask_pattern(segment):
    """码段 → 掩码匹配正则：* 与 ? 视为「1 个未知字符」，其余字符按字面转义。"""
    return re.compile(re.escape(segment).replace(r'\*', '.').replace(r'\?', '.'))


def _remember_code_segment(segment):
    """登记已转发的码段（按是否含掩码分桶）；桶超阈值时惰性清理过期项。"""
    now = time.time()
    bucket = _SEEN_MASKED_SEGMENTS if ('*' in segment or '?' in segment) else _SEEN_FULL_SEGMENTS
    if len(bucket) >= _CODE_SEGMENT_MAX:
        for k, t in list(bucket.items()):
            if t <= now:
                del bucket[k]
    bucket[segment] = now + _CODE_SEGMENT_TTL_SECONDS


def build_dna_fingerprint(text, markup):
    text = _normalize_match_text(text)
    if not text:
        return "", ""

    full_content = _append_markup_urls(text, markup)
    start_payloads = _START_PAYLOAD_RE.findall(full_content)
    start_sig = "|".join(sorted(set(start_payloads)))
    register_codes = _REGISTER_CODE_RE.findall(full_content)
    register_sig = "|".join(sorted(set(register_codes)))

    # 批量发码必须优先保留每个动态 payload。旧版泛化 DNA 容易把不同批次误判重复。
    if _is_bulk_registration_code_notice(text):
        if start_sig:
            return "BULK_STARTS_V2", start_sig
        if register_sig:
            return "BULK_CODES_V2", register_sig

    # 抽奖 ID 是最稳定的去重键；没有 ID 时保留结构化正文，避免相同按钮导致误杀。
    lottery_id = config.get_pattern('LOTTERY_ID')
    if lottery_id:
        lid = _safe_regex_search(lottery_id, full_content)
        if lid:
            return "LOTTERY_ID_V2", lid.group(1).strip()

    if _is_structured_lottery_notice(text):
        # Include the deadline so recurring cards with the same title/group remain distinct.
        card_sig = _lottery_card_dna_signature(text)
        if card_sig:
            return "LOTTERY_CARD_V6", card_sig
        # Fallback intentionally excludes button URLs: activity links can vary
        # between reposts of the same lottery.
        sig = _normalize_dna_text(re.sub(r'(?im)^\s*(?:转发自|forwarded from)\s*[^\r\n]*\r?\n?', '', text))
        if len(sig) > 5:
            return "LOTTERY_STRUCT_V3", sig

    # 开放注册卡片经常共用同一个按钮入口，必须把正文统计和站点名一起纳入指纹。
    # V10.87：注册状态卡的「剩余可注册 / bot使用人数」变化是**有效更新**（名额在减少 = 该抢了），
    # 旧版剥掉全部数字 → 98→89 被当成重复吞掉（用户明确要求：新消息 + 名额有变动 + 格式正确 就必须转发）。
    # 故指纹额外拼入正文的数字序列 → 任何名额/人数变化都会得到新指纹并重新转发；
    # 完全相同的卡（同数字）仍会被拦，不会重复刷屏。
    if _is_structured_registration_notice(text):
        sig = _normalize_dna_text(full_content)
        if len(sig) > 5:
            nums = "|".join(re.findall(r'\d{1,7}', text))
            return "REG_STRUCT_V3", sig + "|" + nums

    # 非结构化 Bot start payload 才单独作为去重依据。
    if start_sig:
        return "BOT_STARTS_V2", start_sig

    if register_sig:
        return "REGISTER_CODES_V2", register_sig

    # 抽奖语义指纹（去掉数字，避免"x10" vs "x5"或"30天" vs "7天"导致不同指纹）
    intent_lottery = config.get_pattern('DOMAIN_LOTTERY_KEYWORD')
    if intent_lottery and _safe_regex_search(intent_lottery, text):
        flat_text = text.replace('\n', ' ')
        prize = re.search(r'(?:奖品|抽奖内容|奖项|Prize\s*content)(.{3,25})', flat_text, re.IGNORECASE)
        draw_time = re.search(r'(?:时间|日期|开奖|Draw\s*date)(.{5,20})', flat_text, re.IGNORECASE)
        if prize and draw_time:
            p_clean = "".join(re.findall(r'[一-龥a-zA-Z]', prize.group(1)))
            t_clean = "".join(re.findall(r'[一-龥a-zA-Z]', draw_time.group(1)))
            if len(p_clean) >= 2 and len(t_clean) >= 4:
                return "LOTTERY_SEMANTIC_V2", p_clean + "|" + t_clean

    # 特殊DNA
    dna_special = config.get_pattern('DNA_SPECIAL')
    if dna_special:
        sp = _safe_regex_findall(dna_special, full_content)
        tokens = _flatten_regex_hits(sp)
        if tokens:
            return "DNA_SPECIAL_V2", "|".join(tokens)

    # 通用指纹
    url_strip = config.get_pattern('CLEAN_URL_STRIP')
    clean_text = config.get_pattern('CLEAN_CLEAN_TEXT')

    if url_strip:
        clean = re.sub(r'\d+', '', _safe_regex_sub(url_strip, '', text))
    else:
        clean = re.sub(r'\d+', '', text)

    if clean_text:
        s = "".join(_safe_regex_findall(clean_text, clean))
    else:
        s = "".join(re.findall(r'[一-龥a-zA-Z0-9]', clean))

    return ("GENERIC_V2", s) if len(s) > 5 else ("", "")


# ================= 意图识别阵列 =================
_REGEX_TIMEOUT = 0.1

# 定期保存 Telethon update 状态（pts/qts）的间隔秒数，0 表示关闭；默认 5 分钟
SAVE_STATE_INTERVAL_SECONDS = _env_int("SAVE_STATE_INTERVAL_SECONDS", 300, 0, 86400)


def _safe_compile_pattern(pattern, flags=0):
    """编译正则；**坏规则只跳过这一条并告警**，不再让整批编译抛异常拖垮过滤链。

    借鉴 TelegramMonitor 的 `KeywordPatternBuilder.EnsureValidRegex`（保存前验证 + 单条失败不扩散）。
    配合 `_REGEX_TIMEOUT`（0.1s 可中断引擎）构成两层 ReDoS 防护。
    """
    if not pattern:
        return None
    try:
        return regex_engine.compile(pattern, flags)
    except Exception as e:
        preview = (pattern[:48] + '…') if len(pattern) > 48 else pattern
        logger.error(f"❌ 正则编译失败，已跳过该规则（不影响其它规则）: {preview!r} → {type(e).__name__}: {e}")
        return None


def _safe_regex_search(compiled_pattern, text):
    """使用可中断的正则引擎，避免线程超时后危险匹配继续占用 CPU。"""
    if not compiled_pattern:
        return None
    try:
        return compiled_pattern.search(text or "", timeout=_REGEX_TIMEOUT)
    except TimeoutError:
        logger.warning(f"⚠️ 正则匹配超时（{_REGEX_TIMEOUT}s），疑似 ReDoS 攻击")
        return None
    except Exception:
        return None


def _safe_regex_findall(compiled_pattern, text):
    """使用可中断的正则 findall，避免入口检测绕过 ReDoS 防护。"""
    if not compiled_pattern:
        return []
    try:
        return compiled_pattern.findall(text or "", timeout=_REGEX_TIMEOUT)
    except TimeoutError:
        logger.warning(f"⚠️ 正则 findall 超时（{_REGEX_TIMEOUT}s），疑似 ReDoS 攻击")
        return []
    except Exception:
        return []


def _safe_regex_sub(compiled_pattern, replacement, text):
    """带真正超时的正则替换，超时或异常时返回原文本。"""
    if not compiled_pattern:
        return text or ""
    try:
        return compiled_pattern.sub(replacement, text or "", timeout=_REGEX_TIMEOUT)
    except TimeoutError:
        logger.warning(f"⚠️ 正则 sub 超时（{_REGEX_TIMEOUT}s），疑似 ReDoS 攻击")
        return text or ""
    except Exception:
        return text or ""


def _match_rule_objects(text, layer=None, action=None):
    """Match configured rule objects against text using compiled pattern keys."""
    hits = []
    for rule in config.get_rule_objects(layer=layer, action=action):
        pat = config.get_pattern(rule.pattern)
        if pat and _safe_regex_search(pat, text):
            hits.append(rule)
            cache.record_rule_hit(rule.pattern)
    return hits


def classify_intent(event, text, markup, is_vip, code_match, code_count, sender_id="", allow_raw_single_code=False):
    """
    V10.7 三层过滤塔：排除优先 → 斩杀 → 领域 → 特征补正
    返回 {'pass': bool, 'domain': str, 'priority': str, 'detail': str}
    """
    text = _normalize_match_text(text)
    has_any_code = bool(code_match)
    has_media = getattr(event.message, 'media', None) is not None

    # ══════════════════════════════════════
    #  第零层：排除优先（黑名单 > 白名单）
    # ══════════════════════════════════════

    # 用户级黑名单（最高排除优先级）
    if sender_id and sender_id in cache.user_blacklist:
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '用户黑名单'}

    # DB 黑名单（排除优先于白名单）
    if _safe_regex_search(cache.compiled_blacklist, text):
        matched_word = _find_matching_word(cache.blacklist, text)
        safe_word = matched_word.replace('`', '＇')[:80]
        detail = f'DB黑名单命中:{safe_word}' if safe_word else 'DB黑名单'
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': detail}

    # JSON 规则对象排除：借鉴 TelegramMonitor 的 Exclude 优先模型
    exclude_hits = _match_rule_objects(text, action="exclude")
    if exclude_hits:
        first = exclude_hits[0]
        return {'pass': False, 'domain': first.layer or 'kill', 'priority': '-',
                'detail': f'规则排除:{first.name}'}

    # 白名单快速通道（黑名单未命中后才检查）
    if _safe_regex_search(cache.compiled_whitelist, text):
        return {'pass': True, 'domain': 'whitelist', 'priority': 'P0-白名单',
                'detail': '白名单命中'}

    structured_lottery_notice = _is_structured_lottery_notice(text)
    structured_registration_notice = _is_structured_registration_notice(text)
    bulk_registration_code_notice = _is_bulk_registration_code_notice(text)
    custom_forward_format = _match_custom_forward_format(text)

    # ══════════════════════════════════════
    #  第一层：斩杀塔（死刑词 + 垃圾信号）
    # ══════════════════════════════════════

    # 推广链接
    kill_invite = config.get_pattern('KILL_INVITE_LINK')
    if kill_invite and _safe_regex_search(kill_invite, text):
        cache.record_rule_hit('KILL_INVITE_LINK')
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '推广链接'}

    # 名额枯竭
    kill_quota = config.get_pattern('KILL_QUOTA_EXHAUSTED')
    if kill_quota:
        m = _safe_regex_search(kill_quota, text)
        if m:
            try:
                if int(m.group(1)) <= 0:
                    return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '名额枯竭'}
            except (ValueError, IndexError):
                pass

    # 抽奖参与回执（垃圾通知，非资源投放）
    kill_lottery_join_ack = config.get_pattern('KILL_LOTTERY_JOIN_ACK')
    if kill_lottery_join_ack and _safe_regex_search(kill_lottery_join_ack, text):
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '抽奖参与回执'}
    # 配置未更新时的保底判定：避免“你已成功参加抽奖”漏过
    if ("你已成功参加抽奖" in text
            and ("当前参与人数" in text or "创建消息链接" in text or "抽奖创建者" in text)):
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '抽奖参与回执'}

    # 审查/静默回执（垃圾通知，非资源消息）
    kill_moderation_feedback = config.get_pattern('KILL_MODERATION_FEEDBACK')
    if kill_moderation_feedback and _safe_regex_search(kill_moderation_feedback, text):
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '审查静默回执'}
    # 配置未更新时的保底判定：避免“已静默 + score + 理由”漏过
    if (("已静默" in text or "静默:" in text)
            and "score" in text.lower()
            and "理由" in text
            and ("引流" in text or "售卖" in text or "交易" in text or "违规" in text)):
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '审查静默回执'}

    # 资源问句：例如“注册码 xxx 请问可以用多久”，是讨论/求助，不是资源投放。
    kill_resource_question = config.get_pattern('KILL_RESOURCE_QUESTION')
    if kill_resource_question and _safe_regex_search(kill_resource_question, text):
        if not (structured_lottery_notice or structured_registration_notice):
            return {'pass': False, 'domain': 'noise', 'priority': '-', 'detail': '资源问句'}

    # 积分/兑换商品卡片：例如“注册码 / 注册bot / 数量 / 已兑换 / 所需积分”，不是可直接转发资源。
    kill_resource_redemption = config.get_pattern('KILL_RESOURCE_REDEMPTION_CARD')
    if kill_resource_redemption and _safe_regex_search(kill_resource_redemption, text):
        if not structured_lottery_notice:
            return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '资源兑换卡片'}

    # 库存/购买入口广告：例如“抽奖码库存状态 / 当前库存 / 可直接购买 / 购买入口”。
    kill_purchase_ad = config.get_pattern('KILL_PURCHASE_AD')
    if kill_purchase_ad and _safe_regex_search(kill_purchase_ad, text):
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '售卖广告'}

    # 二手/闲转交易市场看板：含注册码/月卡码/公益白等交易项，不是资源投放。
    kill_trade_market = config.get_pattern('KILL_TRADE_MARKET_BOARD')
    if kill_trade_market and _safe_regex_search(kill_trade_market, text):
        return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '交易市场广告'}

    # 死刑词
    kill_death = config.get_pattern('KILL_DEATH')
    if kill_death:
        safe_text = text.split("💡 活动说明")[0].split("活动说明")[0].split("频道简介")[0]
        if _safe_regex_search(kill_death, safe_text):
            if not (structured_lottery_notice or structured_registration_notice or custom_forward_format):
                death_words = config.regex_config.get('KILL_PATTERNS', {}).get('DEATH_WORDS', [])
                matched_word = _find_matching_regex_source(death_words, safe_text)
                safe_word = matched_word.replace('`', '｀')[:80]
                detail = f'死刑词命中:{safe_word}' if safe_word else '死刑词'
                return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': detail}

    # 结构化抽奖参与确认（"你已成功参加抽奖"等噪音通知，非可参与的抽奖资源）
    # 注意：正常抽奖卡片常含“允许普通用户参加/建议先私聊机器人”，不能用“参加抽奖”泛拦截。
    if structured_lottery_notice:
        if "你已成功参加抽奖" in text or "已成功参加抽奖" in text or "成功参加抽奖" in text:
            return {'pass': False, 'domain': 'kill', 'priority': '-', 'detail': '抽奖参与确认'}

    # ── VIP 直通 ──
    if is_vip:
        return {'pass': True, 'domain': 'vip', 'priority': 'P0-VIP', 'detail': '管理特权'}

    # ══════════════════════════════════════
    #  第二层：领域塔（核心意图匹配）
    # ══════════════════════════════════════

    domain_tags = []
    domain_priorities = []
    for rule in _match_rule_objects(text, layer="domain", action="monitor"):
        domain_tags.append(rule.name)
        domain_priorities.append(rule.priority)

    if has_any_code:
        domain_tags.append('含激活码')
    if code_count >= 2:
        domain_tags.append('多码并发')
    lid = config.get_pattern('LOTTERY_ID')
    if lid and _safe_regex_search(lid, text):
        domain_tags.append('抽奖ID')
    if structured_lottery_notice:
        domain_tags.append('抽奖结构')
    if structured_registration_notice:
        domain_tags.append('注册结构')
    if bulk_registration_code_notice:
        domain_tags.append('注册码结构')
    if custom_forward_format:
        domain_tags.append(f'信息格式:{custom_forward_format}')

    if has_any_code and not (
            bulk_registration_code_notice
            or structured_registration_notice
            or custom_forward_format
            or allow_raw_single_code
    ):
        return {'pass': False, 'domain': 'code', 'priority': '-', 'detail': '注册码字段不完整'}

    if ("抽奖" in text
            and not structured_lottery_notice
            and not custom_forward_format
            and ('抽奖ID' in domain_tags or '口令意图' in domain_tags)):
        return {'pass': False, 'domain': 'lottery', 'priority': '-', 'detail': '抽奖字段不完整'}

    # Bot 深度链接（?start=）不算独立领域，仅作为连击加分信号
    bot_start = config.get_pattern('ACTION_BOT_START')
    has_bot_start = bool(bot_start and _safe_regex_search(bot_start, text))

    if not domain_tags:
        noise_chat = config.get_pattern('NOISE_CHAT')
        noise_fb = config.get_pattern('NOISE_FEEDBACK')
        is_noise = (_safe_regex_search(noise_chat, text)
                    or _safe_regex_search(noise_fb, text))
        if is_noise:
            return {'pass': False, 'domain': 'noise', 'priority': '-', 'detail': '闲聊/求助'}
        return {'pass': False, 'domain': 'none', 'priority': '-', 'detail': '无领域信号'}

    domain_str = '+'.join(domain_tags) if domain_tags else 'media'

    # ══════════════════════════════════════
    #  第三层：特征补正（信号 → 优先级 + 连击 + 实质入口锁）
    # ══════════════════════════════════════

    signal_hits = 0
    signal_tags = []
    if has_media:
        signal_hits += 1
        signal_tags.append('媒体')
    if has_any_code:
        signal_hits += 1
        signal_tags.append('含码')
    if code_count >= 2:
        signal_hits += 1
        signal_tags.append('多码')

    # ── 组合连击：领域意图 + 实质入口 → 额外加分 ──
    has_link = _safe_regex_search(config.get_pattern('CLEAN_URL_STRIP'), text) is not None

    # Bot 指令检测（/register、/start、/create 等），先剥离 URL 再匹配，防止误判 URL 路径
    text_no_urls = re.sub(r'https?://\S+|t\.me/\S+', '', text)
    has_bot_cmd = bool(re.search(r'(?<!\w)/[a-zA-Z0-9_]{2,}', text_no_urls))

    is_reg_domain = ('邀请注册' in domain_tags
                     or '注册意图' in domain_tags
                     or '注册结构' in domain_tags
                     or '注册码结构' in domain_tags)
    is_lot_domain = ('抽奖ID' in domain_tags
                     or '抽奖结构' in domain_tags
                     or '抽奖' in ' '.join(domain_tags))
    is_act_domain = '口令意图' in domain_tags

    if is_reg_domain and (has_link or has_any_code or has_bot_start):
        signal_hits += 2
        signal_tags.append('注册连击')
    if is_lot_domain and (has_any_code or has_media):
        signal_hits += 2
        signal_tags.append('抽奖连击')
    if is_act_domain and has_any_code:
        signal_hits += 2
        signal_tags.append('口令连击')

    # 指令入口连击：注册/抽奖意图 + Bot 指令 → 极高权重
    if (is_reg_domain or is_lot_domain) and has_bot_cmd:
        signal_hits += 3
        signal_tags.append('指令入口连击')

    signal_str = '|'.join(signal_tags) if signal_tags else '-'

    if signal_hits >= 3:
        priority = 'P1-高优'
    elif signal_hits >= 1:
        priority = 'P2-中优'
    else:
        priority = 'P3-常规'
    priority = _best_priority(priority, domain_priorities)

    # ── 格式入口锁：按钮/命令只加分，不能单独构成放行依据 ──
    has_structured_lottery_entry = '抽奖结构' in domain_tags
    has_structured_registration_entry = '注册结构' in domain_tags
    has_custom_forward_entry = bool(custom_forward_format)
    # 实质入口：真实注册码、结构化条目、内置/自定义信息格式。
    # 普通按钮/跳转很容易来自商城卡片或 Bot 帮助，不能绕过信息格式白名单。
    is_actionable = (has_any_code
                     or has_structured_lottery_entry
                     or has_structured_registration_entry or has_custom_forward_entry)

    if not is_actionable and priority in ('P3-常规', 'P2-中优'):
        return {'pass': False, 'domain': domain_str, 'priority': '-', 'detail': f'{domain_str} 未命中信息格式'}

    detail = f"{domain_str} [{signal_str}]"
    return {'pass': True, 'domain': domain_str, 'priority': priority, 'detail': detail}


# ================= 硬件状态监控 (psutil) =================
def get_system_stats():
    """使用 psutil 获取 VPS 实时硬件数据"""
    # CPU：整机值 + 本进程值分开报 —— 只报整机时，共享 VPS 上会把邻居/同机
    # 其他脚本的负载误读成「雷达占用 100%」（2026-10-09 误杀事故的认知源头）。
    proc = psutil.Process(os.getpid())
    proc.cpu_percent(None)                       # 预热本进程基线（首次调用返回 0，不计入）
    cpu_pct = psutil.cpu_percent(interval=0.3)   # 阻塞 0.3s 采整机值
    proc_cpu = proc.cpu_percent(None)            # 同一时间窗内的本进程平均占用

    # 内存
    vm = psutil.virtual_memory()
    proc_mem = proc.memory_info().rss / (1024 * 1024)

    # 硬盘
    du = psutil.disk_usage(str(MONITOR_DATA_DIR))
    disk_total_mb = du.total / (1024 * 1024)
    disk_free_mb = du.free / (1024 * 1024)
    disk_pct = du.percent

    return {
        'cpu': cpu_pct,
        'proc_cpu': proc_cpu,
        'sys_total_mb': vm.total / (1024 * 1024),
        'sys_used_mb': vm.used / (1024 * 1024),
        'sys_avail_mb': vm.available / (1024 * 1024),
        'sys_pct': vm.percent,
        'proc_mb': proc_mem,
        'disk_total_mb': disk_total_mb,
        'disk_free_mb': disk_free_mb,
        'disk_pct': disk_pct,
    }


# ================= 设备伪装 =================
_apply_manual_version_overrides()
device_profile = config.get_device_profile()
active_profile_key = config.device_config.get('active_profile', 'oneplus_13')
default_app_version = _FALLBACK_DESKTOP if active_profile_key == 'r9000p' else _FALLBACK_ANDROID
RUNNING_DEVICE_PROFILE = dict(device_profile)
TG_CLIENT_COMMON_KWARGS = dict(
    request_retries=3,
    connection_retries=3,
    retry_delay=1,
    auto_reconnect=True,
    timeout=TG_CONNECT_TIMEOUT,
    use_ipv6=False,
)

client = TelegramClient(
    str(SESSION_FILE).replace('.session', ''), API_ID, API_HASH,
    device_model=device_profile.get('device_model', 'OnePlus 13 (PJZ110)'),
    system_version=device_profile.get('system_version', 'ColorOS 16.0.5'),
    app_version=device_profile.get('app_version', default_app_version),
    lang_code=device_profile.get('lang_code', 'zh-Hans'),
    system_lang_code=device_profile.get('system_lang_code', 'zh-Hans'),
    **TG_CLIENT_COMMON_KWARGS
)

admin_bot = TelegramClient(str(ADMIN_SESSION_FILE).replace('.session', ''), API_ID, API_HASH, **TG_CLIENT_COMMON_KWARGS)
forward_bots = []
for i, _ in enumerate(FORWARD_BOT_TOKENS):
    if i == 0:
        stem = str(FORWARD_SESSION_FILE).replace('.session', '')
    else:
        stem = str(SESSIONS_DIR / f"forward_session_{i}")
    forward_bots.append(TelegramClient(stem, API_ID, API_HASH, **TG_CLIENT_COMMON_KWARGS))

forward_bot_tokens_active = list(FORWARD_BOT_TOKENS)
_forward_rr_index = -1

TARGET_ID = db.get_config('TARGET_ID')
BACKUP_ID = db.get_config('BACKUP_ID')


# ================= JSON配置编辑工具 =================
MAX_REGEX_LEN = 200  # 正则最大长度，防 ReDoS


def _validate_regex(pattern):
    """测试正则是否合法且无灾难回溯风险，返回 (ok, error_msg)"""
    if len(pattern) > MAX_REGEX_LEN:
        return False, f"正则过长（{len(pattern)} > {MAX_REGEX_LEN}字符）"
    try:
        regex_engine.compile(pattern, regex_engine.IGNORECASE)
    except regex_engine.error as e:
        return False, f"正则语法错误: {e}"
    return True, ""


def update_regex_json(section, key, action, value=None):
    """
    安全修改 regex_patterns.json 并触发热重载
    action: 'add' | 'remove' | 'set'
    使用原子写入（临时文件 + os.replace）防止写入中途崩溃损坏配置
    """
    try:
        # 正则合法性校验（防 ReDoS 注入）
        if action in ('add', 'set') and value:
            patterns_to_check = value if isinstance(value, list) else [value]
            for p in patterns_to_check:
                ok, err = _validate_regex(p)
                if not ok:
                    return False, err

        with open(REGEX_CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        original_data = copy.deepcopy(data)

        target = data.get(section, {})
        if key:
            target = target.get(key, [])

        if isinstance(target, str):
            if action == 'add' and value and value not in target:
                target = f"(?:{target})|(?:{value})" if target else value
            elif action == 'remove' and value:
                new_target = target.replace(f"|(?:{value})", "")
                new_target = new_target.replace(f"(?:{value})|", "")
                target = "" if new_target == target and target == str(value) else new_target.strip("|")
            elif action == 'set':
                target = value
        else:
            if not isinstance(target, list):
                target = []
            if action == 'add' and value and value not in target:
                target.append(value)
            elif action == 'remove' and value:
                target = [x for x in target if x != value]
            elif action == 'set':
                target = value

        if key:
            if section not in data:
                data[section] = {}
            data[section][key] = target
        else:
            data[section] = target

        # 原子写入：先写临时文件，再 replace
        tmp_path = str(REGEX_CONFIG_FILE) + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(REGEX_CONFIG_FILE))

        config.last_modified.pop('regex', None)
        if config.load_regex_config() is None:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(original_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(REGEX_CONFIG_FILE))
            config.last_modified.pop('regex', None)
            config.load_regex_config()
            return False, "热加载失败，已自动恢复写入前的规则配置"
        cache.reload_all()
        return True, target
    except Exception as e:
        logger.error(f"❌ JSON配置修改失败: {e}")
        return False, str(e)


def update_exclude_rule(action, name, pattern=None):
    """新增、重新启用或持久停用 RULE_OBJECTS 排除规则。"""
    name = str(name or '').strip()
    pattern = str(pattern or '').strip()
    if not name or len(name) > 40:
        return False, "规则名长度需在 1-40 字符之间"
    if action not in ('add', 'remove'):
        return False, "不支持的操作"
    if action == 'add' and pattern:
        ok, err = _validate_regex(pattern)
        if not ok:
            return False, err

    try:
        with open(REGEX_CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        original_data = copy.deepcopy(data)
        rules = data.setdefault('RULE_OBJECTS', [])
        if not isinstance(rules, list):
            return False, "RULE_OBJECTS 配置格式异常"

        matched = next((
            item for item in rules
            if isinstance(item, dict)
            and str(item.get('action', '')).strip().lower() == 'exclude'
            and str(item.get('name', '')).strip().casefold() == name.casefold()
        ), None)

        if action == 'remove':
            if not matched or not matched.get('enabled', True):
                return False, f"未找到生效中的排除规则：{name}"
            matched['enabled'] = False
            operation = 'disabled'
        elif matched:
            if matched.get('enabled', True):
                return False, f"排除规则已存在：{matched.get('name')}"
            matched['enabled'] = True
            operation = 'enabled'
        else:
            if not pattern:
                return False, "新增规则请使用：规则名|正则"
            pattern_key = 'CUSTOM_EXCLUDE_' + hashlib.sha256(
                name.casefold().encode('utf-8')
            ).hexdigest()[:12].upper()
            kill_patterns = data.setdefault('KILL_PATTERNS', {})
            if not isinstance(kill_patterns, dict):
                return False, "KILL_PATTERNS 配置格式异常"
            if pattern_key in kill_patterns:
                return False, "规则标识冲突，请更换规则名"
            kill_patterns[pattern_key] = pattern
            matched = {
                'name': name,
                'pattern': f'KILL_{pattern_key}',
                'action': 'exclude',
                'layer': 'kill',
                'priority': 'P0-排除',
                'enabled': True,
            }
            rules.append(matched)
            operation = 'added'

        tmp_path = str(REGEX_CONFIG_FILE) + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(REGEX_CONFIG_FILE))

        config.last_modified.pop('regex', None)
        if config.load_regex_config() is None:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(original_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(REGEX_CONFIG_FILE))
            config.last_modified.pop('regex', None)
            config.load_regex_config()
            return False, "热加载失败，已自动恢复写入前的规则配置"
        cache.reload_all()
        return True, {'name': matched.get('name', name), 'operation': operation}
    except Exception as e:
        logger.error(f"❌ 排除规则修改失败: {e}")
        return False, str(e)


def _write_custom_forward_formats(formats):
    """原子写入用户自定义信息格式，并热加载到内存。"""
    payload = {
        "updated_at": time.strftime('%Y-%m-%d %H:%M:%S'),
        "formats": formats,
    }
    tmp_path = str(CUSTOM_FORMAT_CONFIG_FILE) + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, str(CUSTOM_FORMAT_CONFIG_FILE))
    if os.name == 'posix':
        try:
            os.chmod(CUSTOM_FORMAT_CONFIG_FILE, 0o600)
        except OSError:
            pass
    config.last_modified.pop('custom_formats', None)
    if config.load_custom_forward_formats() is None:
        raise RuntimeError("自定义信息格式已写入，但热加载失败")


def _get_custom_forward_formats():
    formats = getattr(config, 'custom_forward_formats', [])
    return formats if isinstance(formats, list) else []


def _get_effective_forward_formats():
    """用于面板编辑的格式视图；同名自定义格式替换展示内容。"""
    effective = []
    by_name = {}
    for fmt in BUILTIN_FORWARD_FORMATS:
        item = {
            'name': fmt.get('name', ''),
            'category': fmt.get('category', '其他'),
            'all': list(fmt.get('all') or []),
            'any': list(fmt.get('any') or []),
            'regex_any': list(fmt.get('regex_any') or []),
            'regex_min_hits': int(fmt.get('regex_min_hits') or 0),
            'exclude': list(fmt.get('exclude') or []),
            'enabled': True,
            'source': '内置',
        }
        by_name[str(item['name']).casefold()] = len(effective)
        effective.append(item)

    for fmt in _get_custom_forward_formats():
        if not isinstance(fmt, dict):
            continue
        name = str(fmt.get('name', '')).strip()
        if not name:
            continue
        old_idx = by_name.get(name.casefold())
        old_category = effective[old_idx].get('category', '其他') if old_idx is not None else '自定义'
        item = {
            'name': name,
            'category': fmt.get('category') or old_category,
            'all': _coerce_feature_list(fmt.get('all')),
            'any': _coerce_feature_list(fmt.get('any')),
            'regex_any': _coerce_feature_list(fmt.get('regex_any')),
            'regex_min_hits': _safe_regex_min_hits(fmt.get('regex_min_hits')),
            'exclude': _coerce_feature_list(fmt.get('exclude')),
            'enabled': bool(fmt.get('enabled', True)),
            'source': '自定义扩展' if name.casefold() in by_name else '自定义',
        }
        idx = old_idx
        if idx is None:
            by_name[name.casefold()] = len(effective)
            effective.append(item)
        else:
            effective[idx] = item
    return effective


def _clean_custom_feature(value):
    value = re.sub(r'^[\s\-•·*]+', '', str(value or '').strip())
    value = value.strip('`"\'「」[]（）()')
    return value[:80]


def _split_custom_feature_values(value):
    parts = re.split(r'[|｜]', str(value or ''))
    return [_clean_custom_feature(p) for p in parts if _clean_custom_feature(p)]


def _parse_custom_forward_format(raw_text):
    """解析用户提交的自定义转发格式。只支持普通文本特征，不支持正则。"""
    if not raw_text or len(raw_text) > 1500:
        return None, "请输入 1-1500 字符内的格式特征。"

    name = ""
    all_features = []
    any_features = []
    exclude_features = []

    for raw_line in raw_text.splitlines():
        line = _clean_custom_feature(raw_line)
        if not line:
            continue

        m = re.match(r'^(名称|名字|name)\s*[:：]\s*(.+)$', line, re.IGNORECASE)
        if m:
            name = _clean_custom_feature(m.group(2))[:30]
            continue

        m = re.match(r'^(必含|必须|all|required)\s*[:：]\s*(.+)$', line, re.IGNORECASE)
        if m:
            all_features.extend(_split_custom_feature_values(m.group(2)))
            continue

        m = re.match(r'^(任一|任意|any|optional)\s*[:：]\s*(.+)$', line, re.IGNORECASE)
        if m:
            any_features.extend(_split_custom_feature_values(m.group(2)))
            continue

        m = re.match(r'^(排除|不要|exclude|not)\s*[:：]\s*(.+)$', line, re.IGNORECASE)
        if m:
            exclude_features.extend(_split_custom_feature_values(m.group(2)))
            continue

        all_features.append(line)

    def dedupe(items):
        seen = set()
        result = []
        for item in items:
            key = item.casefold()
            if item and key not in seen:
                seen.add(key)
                result.append(item)
        return result

    all_features = dedupe(all_features)[:8]
    any_features = dedupe(any_features)[:10]
    exclude_features = dedupe(exclude_features)[:8]

    positive_count = len(all_features) + (1 if any_features else 0)
    if positive_count < 2:
        return None, "至少需要 2 个正向特征。可以每行写一个必含特征，或用 `任一: A|B` 增加可选特征组。"

    existing_count = len(_get_custom_forward_formats())
    if not name:
        name = f"自定义格式{existing_count + 1}"

    return {
        'name': name,
        'all': all_features,
        'any': any_features,
        'exclude': exclude_features,
        'enabled': True,
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }, ""


def _custom_match_contains(text, feature):
    text = str(text or "")
    feature = str(feature or "")
    if not feature:
        return True
    t_norm = re.sub(r'\s+', ' ', text).casefold()
    f_norm = re.sub(r'\s+', ' ', feature).strip().casefold()
    if f_norm and f_norm in t_norm:
        return True
    t_compact = re.sub(r'\s+', '', text).casefold()
    f_compact = re.sub(r'\s+', '', feature).casefold()
    return bool(f_compact and f_compact in t_compact)


def _coerce_feature_list(value):
    """Normalize custom-format JSON fields; tolerate manual JSON edits."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [x for x in value if x]
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


_REGEX_COMPILE_CACHE = {}  # pattern_str -> compiled regex（None 表示编译失败）
_REGEX_COMPILE_CACHE_MAX = 512


def _compile_regex_cached(pattern):
    """缓存 regex 编译结果，避免热路径对同一批格式正则反复 compile。"""
    key = str(pattern)
    if key in _REGEX_COMPILE_CACHE:
        return _REGEX_COMPILE_CACHE[key]
    try:
        compiled = regex_engine.compile(key, regex_engine.IGNORECASE)
    except regex_engine.error:
        compiled = None
    if len(_REGEX_COMPILE_CACHE) >= _REGEX_COMPILE_CACHE_MAX:
        _REGEX_COMPILE_CACHE.clear()
    _REGEX_COMPILE_CACHE[key] = compiled
    return compiled


def _regex_hit_count(pattern, text):
    compiled = _compile_regex_cached(pattern)
    if compiled is None:
        return 0
    return len(_safe_regex_findall(compiled, text or ""))


def _format_matches_text(fmt, text):
    if not isinstance(fmt, dict) or not fmt.get('enabled', True):
        return False

    all_features = _coerce_feature_list(fmt.get('all'))
    any_features = _coerce_feature_list(fmt.get('any'))
    regex_any = _coerce_feature_list(fmt.get('regex_any'))
    exclude_features = _coerce_feature_list(fmt.get('exclude'))
    if any(_custom_match_contains(text, f) for f in exclude_features):
        return False
    if all_features and not all(_custom_match_contains(text, f) for f in all_features):
        return False

    regex_hits = 0
    for pattern in regex_any:
        regex_hits += _regex_hit_count(pattern, text)

    min_hits = _safe_regex_min_hits(fmt.get('regex_min_hits'))
    if min_hits and regex_hits < min_hits:
        return False
    if regex_any and not regex_hits:
        return False

    text_any_hit = any(_custom_match_contains(text, f) for f in any_features)
    if any_features and not text_any_hit:
        return False
    if regex_any and not regex_hits:
        return False

    return bool(all_features or any_features or regex_any or min_hits)


def _match_forward_format_names(text, names):
    wanted = {str(name).casefold() for name in names}
    for fmt in _get_effective_forward_formats():
        name = str(fmt.get('name') or '')
        if name.casefold() in wanted and _format_matches_text(fmt, text):
            return name[:30]
    # 同名自定义格式只能扩展识别范围，不能让稳定的内置格式失效。
    for fmt in BUILTIN_FORWARD_FORMATS:
        name = str(fmt.get('name') or '')
        if name.casefold() in wanted and _format_matches_text(fmt, text):
            return name[:30]
    return ""


def _match_custom_forward_format(text):
    """返回命中的信息格式名称；未命中返回空字符串。"""
    for fmt in _get_effective_forward_formats():
        if _format_matches_text(fmt, text):
            return str(fmt.get('name') or '自定义格式')[:30]
    for fmt in BUILTIN_FORWARD_FORMATS:
        if _format_matches_text(fmt, text):
            return str(fmt.get('name') or '内置格式')[:30]
    return ""


def add_custom_forward_format(raw_text):
    fmt, err = _parse_custom_forward_format(raw_text)
    if err:
        return False, err
    try:
        formats = list(_get_custom_forward_formats())
        replace_idx = None
        for idx, old in enumerate(formats):
            if isinstance(old, dict) and str(old.get('name', '')).casefold() == fmt['name'].casefold():
                replace_idx = idx
                break

        builtin_by_name = {
            str(x.get('name', '')).casefold(): x
            for x in BUILTIN_FORWARD_FORMATS
        }
        builtin_fmt = builtin_by_name.get(fmt['name'].casefold())
        is_builtin_override = builtin_fmt is not None
        if builtin_fmt:
            fmt['category'] = builtin_fmt.get('category', '其他')
            if builtin_fmt.get('regex_any'):
                fmt['regex_any'] = list(builtin_fmt.get('regex_any') or [])
                fmt['regex_min_hits'] = int(builtin_fmt.get('regex_min_hits') or 0)
        if replace_idx is None and len(formats) >= 50:
            return False, "自定义格式最多保留 50 条，请先删除旧格式。"

        if replace_idx is None:
            fmt['updated_at'] = fmt['created_at']
            formats.append(fmt)
            op = "扩展内置格式" if is_builtin_override else "新增格式"
        else:
            old_created_at = formats[replace_idx].get('created_at') if isinstance(formats[replace_idx], dict) else None
            if old_created_at:
                fmt['created_at'] = old_created_at
            fmt['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            formats[replace_idx] = fmt
            op = "更新格式"

        _write_custom_forward_formats(formats)
        return True, {'format': fmt, 'op': op}
    except Exception as e:
        logger.error(f"❌ 自定义格式写入失败: {e}")
        return False, str(e)


def remove_custom_forward_format(selector):
    selector = str(selector or '').strip()
    if not selector:
        return False, "请输入要删除的编号或名称。"
    try:
        formats = list(_get_custom_forward_formats())

        idx_to_remove = None
        if selector.isdigit():
            idx = int(selector) - 1
            if 0 <= idx < len(formats):
                idx_to_remove = idx
        else:
            for idx, fmt in enumerate(formats):
                if isinstance(fmt, dict) and str(fmt.get('name', '')).casefold() == selector.casefold():
                    idx_to_remove = idx
                    break

        if idx_to_remove is None:
            return False, "未找到对应的自定义格式。"

        removed = formats.pop(idx_to_remove)
        _write_custom_forward_formats(formats)
        return True, removed
    except Exception as e:
        logger.error(f"❌ 自定义格式删除失败: {e}")
        return False, str(e)


# ================= 白名单授权输入处理（非命令消息）=================
@admin_bot.on(events.NewMessage(func=lambda e: not e.raw_text.startswith('/')))
async def ttl_input_handler(event):
    """处理 TTL 状态下的各类输入（白名单/目标ID）"""
    global TARGET_ID, BACKUP_ID
    sender = await event.get_sender()
    sender_id = str(sender.id) if sender else ""
    OWNER_ID = db.get_config('OWNER_ID')
    if not OWNER_ID or sender_id != str(OWNER_ID):
        return

    pending = _get_pending(sender_id)
    if not pending:
        return

    action = pending.get('action', 'add_whitelist')
    text = event.raw_text.strip()

    # ── 过滤测试 ──
    if action == 'test_filter':
        if not text or len(text) > 2000:
            await event.reply("❌ **输入无效**，文本长度需在 1-2000 字符之间。")
            return
        _clear_pending(sender_id)
        mock_event = _MockEvent()
        code_match = config.get_pattern('CODE')
        code_m = _safe_regex_search(code_match, text)
        strict_code = config.get_pattern('STRICT_CODE')
        strict_codes = _safe_regex_findall(strict_code, text)

        allow_raw_single_code = _is_strict_single_registration_code(text)
        result = classify_intent(
            mock_event, text, None, False,
            code_m, len(strict_codes), "", allow_raw_single_code
        )
        result = _apply_required_forward_format_gate(
            text, result, allow_raw_single_code
        )

        path_lines = _build_filter_verdict_paths(result)

        verdict = "✅ **规则层通过**" if result['pass'] else "🚫 **规则层拦截**"
        resp = (
            f"🧪 **过滤测试结果**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 测试文本：\n`{text[:200]}`\n\n"
            f"{''.join(chr(10) + l for l in path_lines)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 结论：{verdict}\n"
            f"📂 领域：`{result.get('domain', '-')}` | 优先级：`{result.get('priority', '-')}`\n"
            f"📋 详情：`{result.get('detail', '-')}`"
        )
        if len(resp) > 4000:
            resp = resp[:4000] + "..."
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(
                    resp,
                    buttons=[[Button.inline("🔙 返回主菜单", data=b"m_back")]]
                )
        except Exception:
            await event.reply(resp)
        return

    # ── 真实转发测试 ──
    if action == 'forward_test':
        if not text or _utf16_len(text) > _MSG_LIMIT:
            await event.reply("❌ **输入无效**，文本长度需在 1-4096 个 Telegram 字符以内。")
            return

        safety_rejection = _forward_safety_rejection(event, text)
        if safety_rejection:
            _clear_pending(sender_id)
            await event.reply(f"🚫 **测试未发送**\n\n`{safety_rejection}`")
            return

        code_match = _safe_regex_search(config.get_pattern('CODE'), text)
        strict_codes = _safe_regex_findall(config.get_pattern('STRICT_CODE'), text)
        result = classify_intent(
            event, text, None, False, code_match, len(strict_codes),
            sender_id, _is_strict_single_registration_code(text)
        )
        result = _apply_required_forward_format_gate(
            text, result, _is_strict_single_registration_code(text)
        )
        if not result.get('pass'):
            _clear_pending(sender_id)
            await event.reply(
                f"🚫 **测试未发送**\n\n规则层拦截：`{result.get('detail', '-')}`"
            )
            return
        if FORWARDING_PAUSED:
            _clear_pending(sender_id)
            await event.reply("⏸ **测试未发送**\n\n当前转发已暂停。")
            return
        if not TARGET_ID:
            _clear_pending(sender_id)
            await event.reply("❌ **测试未发送**\n\nTARGET_ID 未配置。")
            return

        _clear_pending(sender_id)
        try:
            await rate_limiter.acquire()
            sent_msg, send_bot_idx, send_client, forward_method = await _dispatch_forward_event(event, text)
            if not sent_msg:
                raise RuntimeError("未返回目标消息")
            log_preview = ' '.join(str(text or '').split())[:80]
            logger.info(
                f"✅ 真实转发测试成功: src={event.chat_id}_{event.message.id} "
                f"target={sent_msg.id} method={forward_method} preview={log_preview}"
            )
            await event.reply(
                f"✅ **真实转发测试成功**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"目标消息：`{sent_msg.id}`\n"
                f"方式：`{forward_method}`\n"
                f"规则：`{result.get('detail', '-')}`\n\n"
                f"ℹ️ 这条是**真发**到目标频道的，系统**不会自动删除**，\n"
                f"确认无误后请去目标频道手动删掉它。\n\n"
                f"💡 `方式` 释义：`native`=Bot原生 / `clone_*`=文本克隆 / `user_native_*`=主账号原生\n"
                f"💡 想调规则用「🧪 过滤测试」；想验证能否发出去用本功能。"
            )
        except errors.FloodWaitError as exc:
            rate_limiter.report_flood(exc.seconds)
            await event.reply(f"🚨 **真实转发测试触发限流**\n\n需等待 `{exc.seconds}` 秒。")
        except Exception as exc:
            logger.error(
                f"❌ 真实转发测试失败: src={event.chat_id}_{event.message.id} "
                f"{_format_rpc_error(exc)}"
            )
            await event.reply(f"❌ **真实转发测试失败**\n\n`{_format_rpc_error(exc)}`")
        return

    # ── 修改转发目标 ──
    if action == 'set_target':
        try:
            new_id = int(text)
            if new_id >= 0 or abs(new_id) < 10**9:
                await event.reply("❌ ID 格式异常，频道 ID 应为 `-100xxxxxxxxxx` 格式")
                return
            ok, check_msg = await _check_forward_target_across_bots(new_id, "转发目标")
            if not ok:
                await event.reply(f"❌ 目标校验失败：{check_msg}")
                return
            TARGET_ID = new_id
            db.set_config('TARGET_ID', TARGET_ID)
            _clear_pending(sender_id)
            # 删除用户输入的消息（干净 UX）
            try:
                await event.delete()
            except Exception:
                pass
            # 编辑提示消息为成功状态
            msg = pending.get('msg')
            if msg:
                try:
                    await msg.edit(
                        f"✅ **转发目标已更新**\n\n"
                        f"📡 新目标：`{TARGET_ID}`\n"
                        f"🧪 预检：`{check_msg}`",
                        buttons=[[Button.inline("🔙 返回快捷设置", data=b"m_q")]]
                    )
                except Exception:
                    await event.reply(
                        f"✅ **转发目标已更新**\n📡 新目标：`{TARGET_ID}`\n🧪 预检：`{check_msg}`",
                        buttons=[[Button.inline("🔙 返回快捷设置", data=b"m_q")]]
                    )
        except ValueError:
            await event.reply("❌ 格式错误，请输入纯数字频道 ID（如 `-1001234567890`）")
        return

    # ── 修改备份频道 ──
    if action == 'set_backup':
        try:
            new_id = int(text)
            if new_id >= 0 or abs(new_id) < 10**9:
                await event.reply("❌ ID 格式异常，频道 ID 应为 `-100xxxxxxxxxx` 格式")
                return
            ok, check_msg = await _check_bot_target_permission(admin_bot, new_id, "备份频道")
            if not ok:
                await event.reply(f"❌ 目标校验失败：{check_msg}")
                return
            BACKUP_ID = new_id
            db.set_config('BACKUP_ID', BACKUP_ID)
            _clear_pending(sender_id)
            try:
                await event.delete()
            except Exception:
                pass
            msg = pending.get('msg')
            if msg:
                try:
                    await msg.edit(
                        f"✅ **备份频道已更新**\n\n"
                        f"🛡️ 新备份：`{BACKUP_ID}`\n"
                        f"🧪 预检：`{check_msg}`",
                        buttons=[[Button.inline("🔙 返回快捷设置", data=b"m_q")]]
                    )
                except Exception:
                    await event.reply(
                        f"✅ **备份频道已更新**\n🛡️ 新备份：`{BACKUP_ID}`\n🧪 预检：`{check_msg}`",
                        buttons=[[Button.inline("🔙 返回快捷设置", data=b"m_q")]]
                    )
        except ValueError:
            await event.reply("❌ 格式错误，请输入纯数字频道 ID（如 `-1009876543210`）")
        return

    # ── 选择信息格式编号后进入修改 ──
    if action == 'select_custom_format':
        if not text.isdigit():
            await event.reply("❌ 请输入数字编号，比如 `3`。")
            return
        fmt = _get_format_by_index(int(text) - 1)
        if not fmt:
            await event.reply("❌ 没有这个编号，请返回信息格式面板查看。")
            return
        try:
            await event.delete()
        except Exception:
            pass
        _pending_whitelist[sender_id]['action'] = 'add_custom_format'
        prompt = _build_format_edit_prompt(fmt)
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(prompt)
        except Exception:
            try:
                new_msg = await event.reply(prompt)
                _pending_whitelist[sender_id]['msg'] = new_msg
            except Exception:
                pass
        return

    # ── 自定义信息格式添加/修改 ──
    if action == 'add_custom_format':
        ok, result = add_custom_forward_format(text)
        if not ok:
            await event.reply(f"❌ **格式保存失败**\n\n{result}")
            return

        _clear_pending(sender_id)
        try:
            await event.delete()
        except Exception:
            pass

        fmt = result.get('format', {}) if isinstance(result, dict) else {}
        op = result.get('op', '保存格式') if isinstance(result, dict) else '保存格式'
        body = (
            f"✅ **{op}成功**\n\n"
            f"`{fmt.get('name', '-')}` 已生效。\n"
            f"后续消息会按这个格式判断是否转发。"
        )
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(body, buttons=_build_format_buttons())
        except Exception:
            await event.reply(body, buttons=_build_format_buttons())
        return

    # ── 自定义信息格式删除 ──
    if action == 'remove_custom_format':
        ok, result = remove_custom_forward_format(text)
        if not ok:
            await event.reply(f"❌ **删除失败**\n\n{result}")
            return

        _clear_pending(sender_id)
        try:
            await event.delete()
        except Exception:
            pass

        removed_name = result.get('name', '-') if isinstance(result, dict) else str(result)
        body = (
            f"✅ **已删除**\n\n"
            f"`{removed_name}` 已移除。"
        )
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(body, buttons=_build_format_buttons())
        except Exception:
            await event.reply(body, buttons=_build_format_buttons())
        return

    # ── 排除规则添加/删除 ──
    if action in ('add_exclude_rule', 'remove_exclude_rule'):
        if action == 'add_exclude_rule':
            if '|' in text:
                name, pattern = (part.strip() for part in text.split('|', 1))
            else:
                name, pattern = text, ''
            ok, result = update_exclude_rule('add', name, pattern)
        else:
            name = text
            ok, result = update_exclude_rule('remove', name)
        if not ok:
            await event.reply(f"❌ **操作失败：** {result}")
            return

        _clear_pending(sender_id)
        try:
            await event.delete()
        except Exception:
            pass
        operation = result.get('operation')
        title = {
            'added': '添加成功',
            'enabled': '重新启用成功',
            'disabled': '停用成功',
        }.get(operation, '操作成功')
        body = f"📐 **{title}**\n\n规则：`{result.get('name', name)}`\n配置已热更新。"
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(body, buttons=_build_adv_exclude_buttons())
        except Exception:
            await event.reply(body, buttons=_build_adv_exclude_buttons())
        return

    # ── 死刑词添加/删除 ──
    if action in ('add_death', 'remove_death'):
        if not text or len(text) > 100:
            await event.reply("❌ **输入无效**，死刑词长度需在 1-100 字符之间。")
            return

        operation = 'add' if action == 'add_death' else 'remove'
        try:
            current_words = config.regex_config.get(
                'KILL_PATTERNS', {}
            ).get('DEATH_WORDS', [])
            existed = text in current_words
            ok, result = update_regex_json(
                'KILL_PATTERNS', 'DEATH_WORDS', operation, text
            )
            if not ok:
                await event.reply(f"❌ **操作失败：** {result}")
                return
            if action == 'add_death':
                title = "死刑词已存在" if existed else "添加成功"
            else:
                title = "删除成功" if existed else "死刑词中未找到"
            body = (
                f"☠️ **{title}**\n\n"
                f"关键词：`{text}`\n"
                f"当前死刑词共 `{len(result)}` 个。"
            )
        except Exception as e:
            await event.reply(f"❌ **操作失败：** {e}")
            return

        _clear_pending(sender_id)
        try:
            await event.delete()
        except Exception:
            pass
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(body, buttons=_build_adv_kill_buttons())
        except Exception:
            await event.reply(body, buttons=_build_adv_kill_buttons())
        return

    if action in ('add_blacklist', 'remove_blacklist'):
        if not text or len(text) > 100:
            await event.reply("❌ **输入无效**，关键词长度需在 1-100 字符之间。")
            return

        try:
            if action == 'add_blacklist':
                already = text in cache.blacklist
                db.cursor.execute("INSERT OR IGNORE INTO blacklist (word) VALUES (?)", (text,))
                db.conn.commit()
                cache.reload_all()
                title = "黑名单已存在" if already else "加黑成功"
                body = (
                    f"🛑 **{title}**\n\n"
                    f"关键词：`{text}`\n"
                    f"当前 DB 黑名单共 `{len(cache.blacklist)}` 个。\n\n"
                    f"命中该关键词的消息会在排除层直接拦截。"
                )
            else:
                db.cursor.execute("DELETE FROM blacklist WHERE word = ?", (text,))
                removed = db.cursor.rowcount
                db.conn.commit()
                cache.reload_all()
                title = "删黑成功" if removed else "黑名单中未找到"
                body = (
                    f"✅ **{title}**\n\n"
                    f"关键词：`{text}`\n"
                    f"当前 DB 黑名单共 `{len(cache.blacklist)}` 个。"
                )
        except Exception as e:
            _clear_pending(sender_id)
            await event.reply(f"❌ **操作失败：** {e}")
            return

        _clear_pending(sender_id)
        try:
            await event.delete()
        except Exception:
            pass
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(body, buttons=_build_adv_list_buttons())
        except Exception:
            await event.reply(body, buttons=_build_adv_list_buttons())
        return

    # ── DB 白名单删除 ──
    if action == 'remove_whitelist':
        if not text or len(text) > 100:
            await event.reply("❌ **输入无效**，关键词长度需在 1-100 字符之间。")
            return
        try:
            db.cursor.execute("DELETE FROM whitelist WHERE word = ?", (text,))
            removed = db.cursor.rowcount
            db.conn.commit()
            cache.reload_all()
            title = "删白成功" if removed else "白名单中未找到"
            body = (
                f"✅ **{title}**\n\n"
                f"关键词：`{text}`\n"
                f"当前 DB 白名单共 `{len(cache.whitelist)}` 个。"
            )
        except Exception as e:
            _clear_pending(sender_id)
            await event.reply(f"❌ **操作失败：** {e}")
            return
        _clear_pending(sender_id)
        try:
            await event.delete()
        except Exception:
            pass
        try:
            msg = pending.get('msg')
            if msg:
                await msg.edit(body, buttons=_build_adv_list_buttons())
        except Exception:
            await event.reply(body, buttons=_build_adv_list_buttons())
        return

    # ── 白名单添加（默认行为）──
    if not text or len(text) > 100:
        await event.reply("❌ **输入无效**，关键词长度需在 1-100 字符之间。")
        return

    try:
        db.cursor.execute("INSERT OR IGNORE INTO whitelist (word) VALUES (?)", (text,))
        db.conn.commit()
        cache.reload_all()
    except Exception as e:
        _clear_pending(sender_id)
        await event.reply(f"❌ **添加失败：** {e}")
        return

    _clear_pending(sender_id)
    try:
        msg = pending.get('msg')
        if msg:
            await msg.edit(
                f"✅ **授权完成**\n\n"
                f"🎯 白名单已添加：`{text}`\n"
                f"📋 当前白名单共 `{len(cache.whitelist)}` 个关键词\n\n"
                f"该关键词命中的消息会优先放行，但仍必须命中信息格式。",
                buttons=[[Button.inline("🔙 返回黑白名单", data=b"m_adv_list")]]
            )
    except Exception:
        await event.reply(f"🎯 **白名单已添加：** `{text}`")


# ================= 管理员命令处理 (Bot端) =================
@admin_bot.on(events.NewMessage(pattern='/'))
async def bot_admin_handler(event):
    global TARGET_ID, BACKUP_ID, FORWARDED_COUNT, FORWARDING_PAUSED
    text = event.raw_text.strip()
    sender = await event.get_sender()
    sender_id = str(sender.id) if sender else ""

    # 权限校验：仅 OWNER 可操作（强制校验，OWNER_ID 未设置时拒绝所有人）
    OWNER_ID = db.get_config('OWNER_ID')
    if not OWNER_ID or sender_id != str(OWNER_ID):
        return

    if text == '/start':
        panel = _build_start_panel()
        await event.reply(panel, buttons=_build_start_buttons())

    elif text == '/help':
        await event.reply(
            f"📋 **全部可用指令** `[V{APP_VERSION}]`\n\n"
            "🖥️ **/start** — 战舰主控制台（图形化面板）\n"
            "📡 **/ping** — 服务器状态探测（带刷新按钮）\n"
            "🛡 **/add** — 白名单授权通道（60秒TTL）\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📊 /status — 实时监控面板\n"
            "📈 /stats — 数据统计（转发/拦截/规则命中）\n"
            "🧪 /test <文本> — 过滤试运行（模拟分类，不发送）\n"
            "📤 /start → 📤 真实转发测试 — 真发一条到目标频道验证发送链路\n"
            "⚙️ /config — 配置总览\n"
            "📋 /list — 查看黑白名单\n"
            "🗑️ /log — 查看拦截记录（支持翻页）\n"
            "🎯 /track <ID> — 设为 VIP 优先级\n"
            "🚷 /mute <ID> — 屏蔽群组\n"
            "📡 /settarget <ID> — 修改转发目标\n"
            "🛡 /setbackup <ID> — 修改备份频道\n"
            "🛑 /ban <词> — 加DB黑名单\n"
            "✅ /unban <词> — 删DB黑名单\n"
            "🎯 /add <词> — 加DB白名单（直接添加）\n"
            "🚫 /block <ID> — 屏蔽用户（按发送者ID）\n"
            "✅ /unblock <ID> — 解除用户屏蔽\n"
            "📋 /blocklist — 查看用户黑名单\n"
            "☠️ /add_death <词> — 添加死刑词\n"
            "♻️ /remove_death <词> — 移除死刑词\n"
            "📋 /list_death — 查看死刑词\n"
            "➕ /add_exclude <名称>|<正则> — 添加排除规则\n"
            "➖ /remove_exclude <名称> — 停用排除规则\n"
            "📐 /list_exclude — 查看排除规则\n"
            "🧩 信息格式 — /start → 高级管理 → 信息格式\n"
            "📋 /list_domain — 只读查看底层领域正则\n"
            "💾 /backup — 手动备份\n"
            "🔄 /reload — 热更新配置\n"
            "⚡ /restart — 重启程序"
        )

    elif text == '/status':
        ping_start = time.time()
        stats = await asyncio.get_event_loop().run_in_executor(None, get_system_stats)
        ping_ms = (time.time() - ping_start) * 1000
        panel = _build_diag_panel(stats, ping_ms)
        buttons = [
            [Button.inline("🔄 刷新", data=b"m_d_r"),
             Button.inline("🔙 返回主菜单", data=b"m_back")]
        ]
        await event.reply(panel, buttons=buttons)

    elif text.startswith('/settarget'):
        parts = text.split()
        if len(parts) > 1:
            try:
                raw = parts[1].strip()
                new_id = int(raw)
                # 校验：Telegram 频道/群组 ID 必须为负数且绝对值 >= 10^9
                if new_id >= 0 or abs(new_id) < 10**9:
                    await event.reply("❌ ID 格式异常，频道 ID 应为 `-100xxxxxxxxxx` 格式")
                else:
                    ok, check_msg = await _check_forward_target_across_bots(new_id, "转发目标")
                    if not ok:
                        await event.reply(f"❌ 目标校验失败：{check_msg}")
                        return
                    TARGET_ID = new_id
                    db.set_config('TARGET_ID', TARGET_ID)
                    await event.reply(
                        f"✅ **转发频道已切换为：** `{TARGET_ID}`\n"
                        f"🧪 预检：`{check_msg}`"
                    )
            except ValueError:
                await event.reply("❌ 格式错误，请输入纯数字频道ID")
        else:
            await event.reply("❌ 正确格式：/settarget <纯数字频道ID>")

    elif text.startswith('/setbackup'):
        parts = text.split()
        if len(parts) > 1:
            try:
                raw = parts[1].strip()
                new_id = int(raw)
                if new_id >= 0 or abs(new_id) < 10**9:
                    await event.reply("❌ ID 格式异常，频道 ID 应为 `-100xxxxxxxxxx` 格式")
                else:
                    ok, check_msg = await _check_bot_target_permission(admin_bot, new_id, "备份频道")
                    if not ok:
                        await event.reply(f"❌ 目标校验失败：{check_msg}")
                        return
                    BACKUP_ID = new_id
                    db.set_config('BACKUP_ID', BACKUP_ID)
                    await event.reply(
                        f"✅ **备份频道已切换为：** `{BACKUP_ID}`\n"
                        f"🧪 预检：`{check_msg}`"
                    )
            except ValueError:
                await event.reply("❌ 格式错误，请输入纯数字频道ID")
        else:
            await event.reply("❌ 正确格式：/setbackup <纯数字频道ID>")

    elif text == '/list':
        bl_text = "、".join(list(cache.blacklist)[:50]) if cache.blacklist else "空"
        wl_text = "、".join(list(cache.whitelist)[:50]) if cache.whitelist else "空"
        mt_text = "、".join(cache.muted_chats) if cache.muted_chats else "空"
        vip_text = "、".join(cache.vip_admins) if cache.vip_admins else "空"
        ub_text = "、".join(list(cache.user_blacklist)[:50]) if cache.user_blacklist else "空"
        msg = (
            f"📋 **名单详细列表**\n\n"
            f"🛑 **黑名单 ({len(cache.blacklist)}个):**\n`{bl_text}`\n\n"
            f"🎯 **白名单 ({len(cache.whitelist)}个):**\n`{wl_text}`\n\n"
            f"🚫 **用户黑名单 ({len(cache.user_blacklist)}个):**\n`{ub_text}`\n\n"
            f"🚷 **已屏蔽群组 ({len(cache.muted_chats)}个):**\n`{mt_text}`\n\n"
            f"👑 **VIP 优先级 ({len(cache.vip_admins)}个):**\n`{vip_text}`"
        )
        await event.reply(msg)

    elif text == '/track':
        await event.reply("❌ 用法：`/track <用户ID>`\n\n💡 将用户设为 VIP；可提高优先级，但仍必须命中信息格式。")

    elif text.startswith('/track '):
        uid = text[7:].strip()
        if uid:
            try:
                db.cursor.execute("INSERT OR IGNORE INTO vip_admins (user_id) VALUES (?)", (uid,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"🎯 **成功设置 VIP 优先级：** `{uid}`")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 设置 VIP 失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/track <用户ID>`")

    elif text == '/untrack':
        await event.reply("❌ 用法：`/untrack <用户ID>`\n\n💡 取消用户的 VIP 优先级。")

    elif text.startswith('/untrack '):
        uid = text[9:].strip()
        if uid:
            try:
                db.cursor.execute("DELETE FROM vip_admins WHERE user_id = ?", (uid,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"✅ **已取消 VIP 优先级：** `{uid}`")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 取消 VIP 失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/untrack <用户ID>`")

    elif text == '/mute':
        await event.reply("❌ 用法：`/mute <群组ID>`\n\n💡 屏蔽指定群组，该群组的所有消息将被忽略。")

    elif text.startswith('/mute '):
        chat_id_str = text[6:].strip()
        if chat_id_str:
            clean_id = chat_id_str.removeprefix('-100')
            try:
                db.cursor.execute("INSERT OR IGNORE INTO muted_chats (chat_id) VALUES (?)", (clean_id,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"🚷 **成功屏蔽群组：** `{chat_id_str}`")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 屏蔽群组失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/mute <群组ID>`")

    elif text == '/unmute':
        await event.reply("❌ 用法：`/unmute <群组ID>`\n\n💡 解除群组屏蔽。")

    elif text.startswith('/unmute '):
        chat_id_str = text[8:].strip()
        if chat_id_str:
            clean_id = chat_id_str.removeprefix('-100')
            try:
                db.cursor.execute("DELETE FROM muted_chats WHERE chat_id = ?", (clean_id,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"✅ **已解除屏蔽：** `{chat_id_str}`")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 解除群组屏蔽失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/unmute <群组ID>`")

    elif text.startswith('/log'):
        parts = text.split(maxsplit=1)
        search_query = parts[1].strip() if len(parts) > 1 else ""

        records, total = cache.get_intercepted_page(0, 10, search_query)
        if total == 0:
            await event.reply("📭 **没有拦截记录。**" if not search_query else f"📭 **没有包含 `{search_query}` 的记录。**")
            return

        msg, buttons = _build_log_panel(records, total, 0, 10, search_query)
        await event.reply(msg, buttons=buttons)

    elif text.startswith('/test'):
        test_text = text[5:].strip()
        if not test_text:
            await event.reply("❌ 用法：`/test <要测试的文本>`\n\n模拟过滤引擎，查看消息会被如何分类。")
            return

        mock_event = _MockEvent()
        code_match = config.get_pattern('CODE')
        code_m = _safe_regex_search(code_match, test_text)
        strict_code = config.get_pattern('STRICT_CODE')
        strict_codes = _safe_regex_findall(strict_code, test_text)

        allow_raw_single_code = _is_strict_single_registration_code(test_text)
        result = classify_intent(
            mock_event, test_text, None, False,
            code_m, len(strict_codes), "", allow_raw_single_code
        )
        result = _apply_required_forward_format_gate(
            test_text, result, allow_raw_single_code
        )

        # 构建命中路径
        path_lines = _build_filter_verdict_paths(result)

        verdict = "✅ **规则层通过**" if result['pass'] else "🚫 **规则层拦截**"
        domain = result.get('domain', '-')
        priority = result.get('priority', '-')
        detail = result.get('detail', '-')

        resp = (
            f"🧪 **过滤测试结果**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 测试文本：\n`{test_text[:200]}`\n\n"
            f"{''.join(chr(10) + l for l in path_lines)}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 结论：{verdict}\n"
            f"📂 领域：`{domain}` | 优先级：`{priority}`\n"
            f"📋 详情：`{detail}`\n"
        )

        # 显示今日累计（含本次测试的拦截计数不增加，因为 /test 不走 handler）
        resp += f"\n📊 今日统计：转发 `{FORWARDED_TODAY}` | 拦截 `{INTERCEPTED_TODAY}`"

        if len(resp) > 4000:
            resp = resp[:4000] + "..."
        await event.reply(resp)

    elif text == '/stats':
        buttons = [
            [Button.inline("🔄 刷新", data=b"m_s_r"),
             Button.inline("🔙 返回主菜单", data=b"m_back")]
        ]
        await event.reply(_build_stats_text(), buttons=buttons)

    elif text == '/backup':
        if not BACKUP_ID:
            await event.reply("❌ **备份频道未设置！**\n请先执行 `/setbackup <频道ID>` 配置备份频道。")
            return
        msg = await event.reply("⏳ **正在打包备份数据...**\n\n📦 DB + JSON 配置文件")
        try:
            tmp_zip, size_bytes = await admin_bot.loop.run_in_executor(
                None, _build_backup_zip
            )
        except Exception as e:
            await msg.edit(f"❌ 备份打包失败: {e}")
            return
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        caption = (
            f"🛠️ **【手动备份】**\n"
            f"⏱ `{now_str}`\n"
            f"📦 {_format_size(size_bytes)} | DB + JSON 配置"
        )
        send_target = int(BACKUP_ID)
        fallback = False
        try:
            await admin_bot.get_entity(send_target)
            await admin_bot.send_file(send_target, str(tmp_zip), caption=caption)
        except Exception:
            owner_id = db.get_config('OWNER_ID')
            if owner_id:
                fallback = True
                send_target = int(owner_id)
                fallback_caption = caption + (
                    f"\n\n⚠️ **警告：无法识别备份频道 `{BACKUP_ID}` 实体，"
                    f"已回退发送至私聊。**\n"
                    f"请确认 Bot 是否已加入该频道并设为管理员。"
                )
                try:
                    await admin_bot.get_entity(send_target)
                    await admin_bot.send_file(send_target, str(tmp_zip), caption=fallback_caption)
                except Exception as e2:
                    await msg.edit(f"❌ 备份发送失败: {e2}")
                    try:
                        tmp_zip.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return
            else:
                await msg.edit("❌ 备份发送失败: 实体不可达且无 OWNER_ID 兜底")
                try:
                    tmp_zip.unlink(missing_ok=True)
                except Exception:
                    pass
                return
        try:
            tmp_zip.unlink(missing_ok=True)
        except Exception:
            pass
        if fallback:
            await msg.edit(
                f"✅ **手动备份完成**（⚠️ 已回退至私聊）\n\n"
                f"⏱ `{now_str}`\n"
                f"📦 {_format_size(size_bytes)} | DB + JSON 配置\n\n"
                f"⚠️ 无法识别备份频道 `{BACKUP_ID}`，请确认 Bot 已加入该频道。"
            )
        else:
            await msg.edit(
                f"✅ **手动备份完成**，已发送至备份频道\n\n"
                f"⏱ `{now_str}`\n"
                f"📦 {_format_size(size_bytes)} | DB + JSON 配置"
            )

    elif text == '/ban':
        await event.reply("❌ 用法：`/ban <关键词>`\n\n💡 将关键词加入 DB 黑名单，命中即拦截。")

    elif text.startswith('/ban '):
        word = text[5:].strip()
        if word:
            try:
                already = word in cache.blacklist
                db.cursor.execute("INSERT OR IGNORE INTO blacklist (word) VALUES (?)", (word,))
                db.conn.commit()
                cache.reload_all()
                title = "黑名单已存在" if already else "加黑成功"
                await event.reply(f"🛑 **{title}：** `{word}`\n当前 DB 黑名单 `{len(cache.blacklist)}` 个。")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 加黑失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/ban <关键词>`")

    elif text == '/unban':
        await event.reply("❌ 用法：`/unban <关键词>`\n\n💡 从 DB 黑名单中移除关键词。")

    elif text.startswith('/unban '):
        word = text[7:].strip()
        if word:
            try:
                db.cursor.execute("DELETE FROM blacklist WHERE word = ?", (word,))
                removed = db.cursor.rowcount
                db.conn.commit()
                cache.reload_all()
                title = "删黑成功" if removed else "黑名单中未找到"
                await event.reply(f"✅ **{title}：** `{word}`\n当前 DB 黑名单 `{len(cache.blacklist)}` 个。")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 删黑失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/unban <关键词>`")

    elif text == '/add' or text.startswith('/add '):
        word = text[4:].strip()
        if word:
            # /add <word> → 直接添加
            try:
                db.cursor.execute("INSERT OR IGNORE INTO whitelist (word) VALUES (?)", (word,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"🎯 **加白成功：** `{word}`")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 加白失败：`{e}`")
        else:
            # /add → 显示授权面板
            panel = (
                f"🛡️ **白名单授权通道**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📋 当前白名单：`{len(cache.whitelist)}` 个关键词\n"
                f"🎯 白名单消息优先放行，但仍必须命中信息格式\n\n"
                f"💡 **使用方法：**\n"
                f"  • 点击下方按钮开启授权\n"
                f"  • 60秒内输入目标关键词\n"
                f"  • 超时自动关闭通道\n\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            buttons = [[Button.inline("➕ 确认添加", data=b"add_confirm")]]
            await event.reply(panel, buttons=buttons)

    elif text == '/del':
        await event.reply("❌ 用法：`/del <关键词>`\n\n💡 从 DB 白名单中移除关键词。")

    elif text.startswith('/del '):
        word = text[5:].strip()
        if word:
            try:
                db.cursor.execute("DELETE FROM whitelist WHERE word = ?", (word,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"✅ **删白成功：** `{word}`")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 删白失败：`{e}`")

    elif text == '/block':
        await event.reply("❌ 用法：`/block <用户ID>`\n\n💡 屏蔽指定用户，该用户发送的所有消息将被直接拦截。")

    elif text.startswith('/block '):
        uid = text[7:].strip().lstrip('@')
        if uid:
            try:
                db.cursor.execute("INSERT OR IGNORE INTO user_blacklist (user_id) VALUES (?)", (uid,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"🚫 **用户已屏蔽：** `{uid}`\n该用户发送的消息将被直接拦截。")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 添加用户屏蔽失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/block <用户ID>`")

    elif text == '/unblock':
        await event.reply("❌ 用法：`/unblock <用户ID>`\n\n💡 解除用户屏蔽。")

    elif text.startswith('/unblock '):
        uid = text[9:].strip().lstrip('@')
        if uid:
            try:
                db.cursor.execute("DELETE FROM user_blacklist WHERE user_id = ?", (uid,))
                db.conn.commit()
                cache.reload_all()
                await event.reply(f"✅ **用户已解除屏蔽：** `{uid}`")
            except Exception as e:
                _safe_db_rollback()
                await event.reply(f"❌ 解除用户屏蔽失败：`{e}`")
        else:
            await event.reply("❌ 用法：`/unblock <用户ID>`")

    elif text == '/blocklist':
        if cache.user_blacklist:
            lines = [f"• `{uid}`" for uid in sorted(cache.user_blacklist)]
            await event.reply(f"🚫 **用户黑名单** ({len(cache.user_blacklist)} 个)\n\n" + "\n".join(lines))
        else:
            await event.reply("🚫 用户黑名单为空")

    elif text == '/ping':
        ping_start = time.time()
        msg = await event.reply("📡 **正在探测服务器状态...**")
        ping_ms = (time.time() - ping_start) * 1000
        stats = await asyncio.get_event_loop().run_in_executor(None, get_system_stats)
        panel = _build_diag_panel(stats, ping_ms)
        buttons = [
            [Button.inline("🔄 刷新", data=b"m_d_r"),
             Button.inline("🔙 返回主菜单", data=b"m_back")]
        ]
        await msg.edit(panel, buttons=buttons)

    elif text == '/restart':
        await event.reply("🔄 **雷达系统正在重启...**\n\n⏳ 请稍候。")
        for c in [client, admin_bot, *forward_bots]:
            try:
                await c.disconnect()
            except Exception:
                pass
        db.close()
        os.execl(sys.executable, sys.executable, os.path.abspath(__file__))

    elif text == '/stack':
        # 诊断假死：把所有线程的调用栈打进日志（journalctl -u tg-monitor 可查）
        faulthandler.dump_traceback()
        await event.reply("🧵 线程栈已转储到日志，用 `journalctl -u tg-monitor -n 200` 查看「Current thread」段")

    elif text == '/reload':
        if config.load_all():
            cache.reload_all()
            await event.reply("✅ **配置已热更新完成！**")
        else:
            await event.reply("❌ **配置热更新失败**\n\n运行中已保留上一份可用配置，请检查日志。")

    # ── 配置热管理指令（脱离SSH直连管理）──

    elif text.startswith('/add_death '):
        word = text[11:].strip()
        if word:
            ok, result = update_regex_json('KILL_PATTERNS', 'DEATH_WORDS', 'add', word)
            if ok:
                await event.reply(f"☠️ **死刑词已添加：** `{word}`\n当前共 `{len(result)}` 个")
            else:
                await event.reply(f"❌ 添加失败: {result}")

    elif text.startswith('/remove_death '):
        word = text[14:].strip()
        if word:
            ok, result = update_regex_json('KILL_PATTERNS', 'DEATH_WORDS', 'remove', word)
            if ok:
                await event.reply(f"♻️ **死刑词已移除：** `{word}`\n剩余 `{len(result)}` 个")
            else:
                await event.reply(f"❌ 移除失败: {result}")

    elif text == '/list_death':
        words = config.regex_config.get('KILL_PATTERNS', {}).get('DEATH_WORDS', [])
        if words:
            display = '、'.join(words[:60])
            overflow = f"\n... 共 `{len(words)}` 个" if len(words) > 60 else ""
            await event.reply(f"☠️ **死刑词列表：**\n`{display}`{overflow}")
        else:
            await event.reply("📭 死刑词列表为空。")

    elif text.startswith('/add_exclude '):
        value = text[13:].strip()
        if '|' not in value:
            await event.reply("❌ 格式错误，请使用：`/add_exclude 规则名|正则`")
        else:
            name, pattern = (part.strip() for part in value.split('|', 1))
            ok, result = update_exclude_rule('add', name, pattern)
            if ok:
                await event.reply(f"📐 **排除规则已生效：** `{result.get('name')}`")
            else:
                await event.reply(f"❌ 添加失败: {result}")

    elif text.startswith('/remove_exclude '):
        name = text[16:].strip()
        ok, result = update_exclude_rule('remove', name)
        if ok:
            await event.reply(f"✅ **排除规则已停用：** `{result.get('name')}`")
        else:
            await event.reply(f"❌ 停用失败: {result}")

    elif text == '/list_exclude':
        await event.reply(_build_adv_exclude_panel(), buttons=_build_adv_exclude_buttons())

    elif text.startswith('/add_domain ') or text.startswith('/remove_domain '):
        await event.reply(
            "⚠️ **领域正则直改已停用**\n\n"
            "这个入口容易把过滤范围放宽，已经改为只读。\n"
            "要放行新类型，请用：`/start` → 高级管理 → 信息格式。\n"
            "要拦截垃圾，请用黑名单或 `/add_death <词>`。"
        )

    elif text == '/list_domain':
        domains = config.regex_config.get('DOMAIN_PATTERNS', {})
        if domains:
            msg = "🎯 **底层领域正则（只读）：**\n\n"
            for cat, patterns in domains.items():
                if isinstance(patterns, list):
                    msg += f"**{cat}** ({len(patterns)}个):\n"
                    for p in patterns[:5]:
                        msg += f"  `{p}`\n"
                    if len(patterns) > 5:
                        msg += f"  ... 共 {len(patterns)} 个\n"
                elif isinstance(patterns, str):
                    msg += f"**{cat}**:\n  `{patterns}`\n"
                msg += "\n"
            if len(msg) > 4000:
                msg = msg[:4000] + "\n...(截断)"
            await event.reply(msg)
        else:
            await event.reply("📭 领域模式为空。")

    elif text == '/config':
        death_count = len(config.regex_config.get('KILL_PATTERNS', {}).get('DEATH_WORDS', []))
        rule_count = len(config.get_rule_objects())
        exclude_rule_count = len(config.get_rule_objects(action='exclude'))
        custom_format_count = len(_get_custom_forward_formats())
        await event.reply(
            f"⚙️ **配置总览** `[V{APP_VERSION}]`\n\n"
            f"☠️ 死刑词：`{death_count}` 个\n"
            f"🧩 信息格式：`{len(_get_effective_forward_formats())}` 个（自定义 `{custom_format_count}` 个）\n"
            f"📐 规则对象：`{rule_count}` 条（排除 `{exclude_rule_count}` 条）\n"
            f"🛑 DB黑名单：`{len(cache.blacklist)}` 个\n"
            f"🎯 DB白名单：`{len(cache.whitelist)}` 个\n"
            f"🚫 用户黑名单：`{len(cache.user_blacklist)}` 个\n\n"
            f"**可用管理指令：**\n"
            f"`/add_death <词>` `/remove_death <词>` `/list_death`\n"
            f"`/ban <词>` `/unban <词>` `/block <ID>` `/unblock <ID>`\n"
            f"`/list_domain` 只读查看底层领域正则\n"
            f"`/reload` — 重新加载全部配置"
        )


# ================= Inline 按钮回调处理 =================
@admin_bot.on(events.CallbackQuery)
async def callback_handler(event):
    """处理 Inline 按钮回调 — 仅 OWNER_ID 可触发"""
    global FORWARDING_PAUSED, FORWARDED_COUNT, FORWARDED_TODAY
    try:
        if not await _check_owner(event):
            return
    except Exception:
        return

    data = event.data.decode('utf-8') if isinstance(event.data, bytes) else str(event.data)
    logger.info(f"🔘 回调收到: data={data}, sender={event.sender_id}")

    # ── 转发消息来源/删除按钮 ──
    if data.startswith('src_'):
        parts = data.split('_', 2)
        if len(parts) == 3:
            chat_id = parts[1]
            msg_id = parts[2]
            link = f"https://t.me/c/{chat_id}/{msg_id}"
            await event.answer(f"来源: {link}", alert=True)

    elif data.startswith('del_'):
        try:
            await event.delete()
            await event.answer("✅ 已删除", alert=False)
        except Exception as e:
            await event.answer(f"❌ 删除失败: {e}", alert=True)

    # ── 一键屏蔽发送者 ──
    elif data.startswith('blk_'):
        uid = data[4:]
        if uid:
            try:
                db.cursor.execute("INSERT OR IGNORE INTO user_blacklist (user_id) VALUES (?)", (uid,))
                db.conn.commit()
                cache.reload_all()
                await event.answer(f"🚫 已屏蔽用户 {uid}", alert=True)
            except Exception as e:
                await event.answer(f"❌ 屏蔽失败: {e}", alert=True)

    # ══════════════════════════════════════
    #  主菜单导航系统 (m_ 前缀)
    # ══════════════════════════════════════

    # ── 返回主菜单 ──
    elif data == 'm_back':
        try:
            await event.edit(_build_start_panel(), buttons=_build_start_buttons())
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 系统诊断 ──
    elif data in ('m_d', 'm_d_r'):
        try:
            ping_start = time.time()
            stats = await asyncio.get_event_loop().run_in_executor(None, get_system_stats)
            ping_ms = (time.time() - ping_start) * 1000
            panel = _build_diag_panel(stats, ping_ms)
            buttons = [
                [Button.inline("🔄 刷新", data=b"m_d_r"),
                 Button.inline("🔙 返回主菜单", data=b"m_back")]
            ]
            await event.edit(panel, buttons=buttons)
            await event.answer(f"📡 延迟: {ping_ms:.0f}ms")
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 系统与安全子面板 ──
    elif data == 'm_sys':
        try:
            await event.edit(_build_sys_panel(), buttons=_build_sys_buttons())
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 强制设备轮换 ──
    elif data == 'm_rotate':
        try:
            now_ts = int(time.time())
            db.set_config('NEXT_DEVICE_ROTATION', str(now_ts))
            profiles = list(config.device_config.get('profiles', {}).keys())
            active = config.device_config.get('active_profile', profiles[0] if profiles else 'unknown')
            idx = (profiles.index(active) + 1) % len(profiles) if active in profiles and profiles else 0
            new_profile = profiles[idx] if profiles else 'unknown'
            config.device_config['active_profile'] = new_profile
            tmp_path = str(DEVICE_CONFIG_FILE) + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(config.device_config, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(DEVICE_CONFIG_FILE))
            _secure_runtime_permissions()

            ver_info = ""
            try:
                android_ver, desktop_ver = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_latest_tg_versions
                )
                if android_ver or desktop_ver:
                    changed = _update_device_versions(android_ver, desktop_ver, source="auto")
                    state = "已写入，重启后生效" if changed else "已检查，无需更新"
                    ver_info = (
                        f"\n🔄 版本号{state}："
                        f"Android `{android_ver or '未获取'}` / Desktop `{desktop_ver or '未获取'}`"
                    )
                else:
                    ver_info = "\n⚠️ 未获取到版本号，保留当前配置"
            except Exception:
                ver_info = "\n⚠️ 版本号刷新失败，使用当前值"

            await event.answer("🔄 已触发设备轮换！", alert=True)
            await event.edit(
                f"🔄 **设备轮换已执行**\n\n"
                f"📱 当前：`{active}` → `{new_profile}`{ver_info}\n\n"
                f"⚠️ 需要重启才能生效。",
                buttons=[
                    [Button.inline("🔄 立即重启", data=b"m_restart")],
                    [Button.inline("🔙 返回系统与安全", data=b"m_sys")],
                ]
            )
        except Exception as e:
            await event.answer(f"❌ 轮换失败: {e}", alert=True)

    # ── 紧急停止/恢复转发 ──
    elif data == 'm_stop':
        FORWARDING_PAUSED = not FORWARDING_PAUSED
        db.set_config('FORWARDING_PAUSED', '1' if FORWARDING_PAUSED else '0')
        state_text = "⏸ 已暂停" if FORWARDING_PAUSED else "🟢 运行中"
        await event.answer(f"转发状态: {state_text}", alert=True)
        try:
            await event.edit(_build_sys_panel(), buttons=_build_sys_buttons())
        except Exception:
            pass

    # ── 重启雷达 ──
    elif data == 'm_restart':
        await event.answer("🔄 正在重启...", alert=True)
        try:
            await event.edit("🔄 **雷达系统正在重启...**\n\n⏳ 请稍候。")
        except Exception:
            pass
        for c in [client, admin_bot, *forward_bots]:
            try:
                await c.disconnect()
            except Exception:
                pass
        db.close()
        os.execl(sys.executable, sys.executable, os.path.abspath(__file__))

    # ── 拦截日志 ──
    elif data == 'm_log':
        try:
            records, total = cache.get_intercepted_page(0, 10)
            if total == 0:
                await event.answer("📭 暂无拦截记录", alert=True)
                return
            msg, buttons = _build_log_panel(records, total, 0, 10)
            await event.edit(msg, buttons=buttons)
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 拦截日志翻页 ──
    elif data.startswith('m_log_'):
        try:
            parts = data.split('_', 3)
            offset = int(parts[2]) if len(parts) > 2 else 0
            search = parts[3] if len(parts) > 3 else ""
            records, total = cache.get_intercepted_page(offset, 10, search)
            if total == 0:
                await event.answer("📭 无记录", alert=True)
                return
            msg, buttons = _build_log_panel(records, total, offset, 10, search)
            await event.edit(msg, buttons=buttons)
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 数据统计 ──
    elif data in ('m_s', 'm_s_r'):
        try:
            buttons = [
                [Button.inline("🔄 刷新", data=b"m_s_r"),
                 Button.inline("🔙 返回主菜单", data=b"m_back")]
            ]
            await event.edit(_build_stats_text(8), buttons=buttons)
            await event.answer("✅ 已刷新")
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ══════════════════════════════════════
    #  快捷设置面板 (m_q_ 前缀)
    # ══════════════════════════════════════

    # ── 快捷设置主面板 ──
    elif data == 'm_q':
        try:
            await event.edit(_build_quick_panel(), buttons=_build_quick_buttons())
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 修改转发目标 (TTL) ──
    elif data == 'm_q_t':
        await event.answer()
        uid = str(event.sender_id)
        logger.info(f"=> 修改目标按钮点击: uid={uid}")
        prompt_text = (
            "📡 **修改转发目标**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ **等待输入新目标 ID...** (60s)\n\n"
            "请发送频道 ID（如 `-1001234567890`）\n"
            "超时将自动取消。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=60)
        _pending_whitelist[uid]['action'] = 'set_target'
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 60))

    # ── 修改备份频道 (TTL) ──
    elif data == 'm_q_b':
        await event.answer()
        uid = str(event.sender_id)
        logger.info(f"=> 修改备份按钮点击: uid={uid}")
        prompt_text = (
            "🛡️ **修改备份频道**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ **等待输入新备份 ID...** (60s)\n\n"
            "请发送频道 ID（如 `-1009876543210`）\n"
            "超时将自动取消。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=60)
        _pending_whitelist[uid]['action'] = 'set_backup'
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 60))

    # ── 取消快捷设置输入 ──
    elif data == 'm_q_cancel':
        await event.answer("已取消")
        uid = str(event.sender_id)
        _clear_pending(uid)
        try:
            await event.edit(_build_quick_panel(), buttons=_build_quick_buttons())
        except Exception:
            pass

    # ── VIP 管理 ──
    elif data == 'm_q_v':
        try:
            if cache.vip_admins:
                lines = [f"• `{uid}`" for uid in sorted(cache.vip_admins)]
                body = "\n".join(lines)
            else:
                body = "📭 VIP 列表为空"
            await event.edit(
                f"👑 **VIP 管理员** (`{len(cache.vip_admins)}` 人)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{body}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 使用 `/track <ID>` 添加 | `/untrack <ID>` 移除",
                buttons=[[Button.inline("🔙 返回快捷设置", data=b"m_q")]]
            )
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 用户屏蔽管理 ──
    elif data == 'm_q_ub':
        try:
            if cache.user_blacklist:
                lines = [f"• `{uid}`" for uid in sorted(cache.user_blacklist)]
                body = "\n".join(lines)
            else:
                body = "📭 用户黑名单为空"
            await event.edit(
                f"🚫 **用户黑名单** (`{len(cache.user_blacklist)}` 人)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{body}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 使用 `/block <ID>` 添加 | `/unblock <ID>` 移除",
                buttons=[[Button.inline("🔙 返回快捷设置", data=b"m_q")]]
            )
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 群组屏蔽管理 ──
    elif data == 'm_q_um':
        try:
            if cache.muted_chats:
                lines = [f"• `{cid}`" for cid in sorted(cache.muted_chats)]
                body = "\n".join(lines)
            else:
                body = "📭 群组屏蔽列表为空"
            await event.edit(
                f"🚷 **群组屏蔽** (`{len(cache.muted_chats)}` 个)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{body}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 使用 `/mute <ID>` 添加 | `/unmute <ID>` 移除",
                buttons=[[Button.inline("🔙 返回快捷设置", data=b"m_q")]]
            )
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ══════════════════════════════════════
    #  高级管理面板 (m_adv_ 前缀)
    # ══════════════════════════════════════

    # ── 高级管理主面板 ──
    elif data == 'm_adv':
        try:
            await event.edit(_build_adv_panel(), buttons=_build_adv_buttons())
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 返回高级管理 ──
    elif data == 'm_adv_back':
        try:
            await event.edit(_build_adv_panel(), buttons=_build_adv_buttons())
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 死刑词管理 ──
    elif data == 'm_adv_kill':
        try:
            await event.edit(
                _build_adv_kill_panel(),
                buttons=_build_adv_kill_buttons()
            )
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    elif data in ('m_adv_kill_add', 'm_adv_kill_del'):
        await event.answer()
        uid = str(event.sender_id)
        is_add = data == 'm_adv_kill_add'
        action = 'add_death' if is_add else 'remove_death'
        title = "添加死刑词" if is_add else "删除死刑词"
        hint = "请直接发送要命中即拦截的关键词。" if is_add else "请直接发送要移除的死刑词。"
        prompt_text = (
            f"☠️ **{title}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ **等待输入中...** (60s)\n\n"
            f"{hint}\n"
            f"死刑词与 DB 黑名单独立保存，超时将自动取消。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=60)
        _pending_whitelist[uid]['action'] = action
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 60))

    # ── 排除规则管理 ──
    elif data == 'm_adv_exclude':
        try:
            await event.edit(
                _build_adv_exclude_panel(),
                buttons=_build_adv_exclude_buttons()
            )
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    elif data in ('m_adv_exclude_add', 'm_adv_exclude_del'):
        await event.answer()
        uid = str(event.sender_id)
        is_add = data == 'm_adv_exclude_add'
        action = 'add_exclude_rule' if is_add else 'remove_exclude_rule'
        title = "添加排除规则" if is_add else "删除排除规则"
        hint = (
            "请发送：`规则名|正则`。若同名规则已停用，只发送规则名即可重新启用。"
            if is_add else "请发送要停用的完整规则名，例如：`订阅节点推广`。"
        )
        prompt_msg = await event.reply(
            f"📐 **{title}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ **等待输入中...** (120s)\n\n"
            f"{hint}\n"
            f"超时将自动取消。"
        )
        _set_pending(uid, prompt_msg, ttl=120)
        _pending_whitelist[uid]['action'] = action
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 120))

    # ── 黑白名单管理 ──
    elif data == 'm_adv_list':
        try:
            await event.edit(
                _build_adv_list_panel(),
                buttons=_build_adv_list_buttons()
            )
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ── 信息格式管理 ──
    elif data == 'm_adv_fmt':
        try:
            await event.edit(
                _build_format_panel(),
                buttons=_build_format_buttons()
            )
            await event.answer()
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    elif data == 'm_adv_fmt_add':
        await event.answer()
        uid = str(event.sender_id)
        prompt_text = (
            "➕ **新增信息格式**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "复制下面这段，改成你要的新格式后发给我：\n\n"
            "```text\n"
            "名称: 新格式名称\n"
            "必含: 必须出现的关键词1|必须出现的关键词2\n"
            "任一: 任一出现即可1|任一出现即可2\n"
            "排除: 不想转发的关键词1|不想转发的关键词2\n"
            "```\n"
            "名称如果和上面列表相同，就是为该内置格式增加识别分支。\n"
            "必含 = 每条消息都必须有。\n"
            "任一 = 有一个就行。\n"
            "排除 = 当前分支遇到这些就不匹配。\n"
            "要全局阻止某类消息，请使用黑名单或死刑词。\n\n"
            "180 秒内发送。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=180)
        _pending_whitelist[uid]['action'] = 'add_custom_format'
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 180))

    elif data.startswith('m_adv_fmt_e_'):
        await event.answer()
        uid = str(event.sender_id)
        try:
            idx = int(data.rsplit('_', 1)[1]) - 1
        except (TypeError, ValueError, IndexError):
            await event.answer("❌ 编号异常", alert=True)
            return
        fmt = _get_format_by_index(idx)
        if not fmt:
            await event.answer("❌ 没有这个格式", alert=True)
            return
        prompt_msg = await event.reply(_build_format_edit_prompt(fmt))
        _set_pending(uid, prompt_msg, ttl=180)
        _pending_whitelist[uid]['action'] = 'add_custom_format'
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 180))

    elif data == 'm_adv_fmt_pick':
        await event.answer()
        uid = str(event.sender_id)
        prompt_text = (
            "🔢 **输入序号修改**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "请发送要修改的格式编号，比如：`3`\n"
            "我会把该格式的当前关键词发给你复制修改。\n\n"
            "180 秒内发送。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=180)
        _pending_whitelist[uid]['action'] = 'select_custom_format'
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 180))

    elif data == 'm_adv_fmt_del':
        await event.answer()
        uid = str(event.sender_id)
        custom_formats = _get_custom_forward_formats()
        if custom_formats:
            items = []
            for idx, fmt in enumerate(custom_formats[:30], 1):
                if isinstance(fmt, dict):
                    items.append(f"{idx}. `{fmt.get('name', '未命名')}`")
            current = "\n".join(items) if items else "暂无自定义模板。"
        else:
            current = "暂无自定义模板。"
        prompt_text = (
            "➖ **删除信息格式**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{current}\n\n"
            "发送编号即可删除，比如：`1`\n"
            "内置格式不能删除；如果你改过内置格式，这里删除的是你的扩展版本。\n\n"
            "120 秒内发送。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=120)
        _pending_whitelist[uid]['action'] = 'remove_custom_format'
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 120))

    elif data in ('m_adv_bl_add', 'm_adv_bl_del'):
        await event.answer()
        uid = str(event.sender_id)
        is_add = data == 'm_adv_bl_add'
        action = 'add_blacklist' if is_add else 'remove_blacklist'
        title = "添加黑名单关键词" if is_add else "删除黑名单关键词"
        hint = "请直接发送要拦截的关键词。" if is_add else "请直接发送要移除的黑名单关键词。"
        prompt_text = (
            f"🛑 **{title}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ **等待输入中...** (60s)\n\n"
            f"{hint}\n"
            f"超时将自动取消。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=60)
        _pending_whitelist[uid]['action'] = action
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 60))

    elif data == 'm_adv_bl_cancel':
        uid = str(event.sender_id)
        _clear_pending(uid)
        try:
            await event.edit(_build_adv_list_panel(), buttons=_build_adv_list_buttons())
            await event.answer("已取消")
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    elif data in ('m_adv_wl_add', 'm_adv_wl_del'):
        await event.answer()
        uid = str(event.sender_id)
        is_add = data == 'm_adv_wl_add'
        action = 'add_whitelist' if is_add else 'remove_whitelist'
        title = "添加白名单关键词" if is_add else "删除白名单关键词"
        hint = "请直接发送要放行的关键词。" if is_add else "请直接发送要移除的白名单关键词。"
        prompt_text = (
            f"🎯 **{title}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ **等待输入中...** (60s)\n\n"
            f"{hint}\n"
            f"超时将自动取消。"
        )
        prompt_msg = await event.reply(prompt_text)
        _set_pending(uid, prompt_msg, ttl=60)
        _pending_whitelist[uid]['action'] = action
        _spawn(_prompt_expiry_watcher(uid, prompt_msg, 60))

    # ── 手动备份 ──
    elif data == 'm_adv_bak':
        if not BACKUP_ID:
            await event.answer("❌ 备份频道未设置！请先执行 /setbackup", alert=True)
            return
        await event.answer("⏳ 正在打包备份数据...", alert=False)
        try:
            await event.edit("⏳ **正在打包备份数据...**\n\n📦 DB + JSON 配置文件")
        except Exception:
            pass
        try:
            tmp_zip, size_bytes = await admin_bot.loop.run_in_executor(
                None, _build_backup_zip
            )
            logger.info(f"📦 备份打包完成: {_format_size(size_bytes)}")
        except Exception as e:
            logger.error(f"❌ 备份打包失败: {e}")
            await event.edit(f"❌ **备份打包失败**\n\n`{e}`")
            return
        now_str = time.strftime('%Y-%m-%d %H:%M:%S')
        caption = (
            f"🛠️ **【手动备份】**\n"
            f"⏱ `{now_str}`\n"
            f"📦 {_format_size(size_bytes)} | DB + JSON 配置"
        )
        send_target = int(BACKUP_ID)
        fallback = False
        try:
            await admin_bot.get_entity(send_target)
            await admin_bot.send_file(send_target, str(tmp_zip), caption=caption)
            logger.info(f"✅ 备份已发送至 {send_target}")
        except Exception as e:
            # Entity 缓存丢失 → 兜底发送至 OWNER_ID 私聊
            owner_id = db.get_config('OWNER_ID')
            if owner_id:
                fallback = True
                send_target = int(owner_id)
                fallback_caption = caption + (
                    f"\n\n⚠️ **警告：无法识别备份频道 `{BACKUP_ID}` 实体，"
                    f"已回退发送至私聊。**\n"
                    f"请确认 Bot 是否已加入该频道并设为管理员。"
                )
                try:
                    await admin_bot.get_entity(send_target)
                    await admin_bot.send_file(send_target, str(tmp_zip), caption=fallback_caption)
                    logger.warning(f"⚠️ 备份已回退发送至 OWNER_ID={send_target}")
                except Exception as e2:
                    logger.error(f"❌ 备份回退也失败: {e2}")
                    await event.edit(f"❌ **备份发送失败**\n\n`{e2}`")
                    try:
                        tmp_zip.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return
            else:
                logger.error(f"❌ 备份发送失败且无 OWNER_ID 兜底: {e}")
                await event.edit(f"❌ **备份发送失败**\n\n`{e}`")
                try:
                    tmp_zip.unlink(missing_ok=True)
                except Exception:
                    pass
                return
        try:
            tmp_zip.unlink(missing_ok=True)
        except Exception:
            pass
        if fallback:
            await event.edit(
                f"✅ **手动备份完成**（⚠️ 已回退至私聊）\n\n"
                f"⏱ `{now_str}`\n"
                f"📦 {_format_size(size_bytes)} | DB + JSON 配置\n\n"
                f"⚠️ 无法识别备份频道 `{BACKUP_ID}`，请确认 Bot 已加入该频道。"
            )
        else:
            await event.edit(
                f"✅ **手动备份完成**，已发送至备份频道\n\n"
                f"⏱ `{now_str}`\n"
                f"📦 {_format_size(size_bytes)} | DB + JSON 配置"
            )

    # ── 热更新配置 ──
    elif data == 'm_adv_reload':
        try:
            if not config.load_all():
                await event.answer("❌ 配置热更新失败，已保留旧配置", alert=True)
                return
            cache.reload_all()
            await event.answer("✅ 配置已热更新！", alert=True)
            await event.edit(_build_adv_panel(), buttons=_build_adv_buttons())
        except Exception as e:
            await event.answer(f"❌ 热更新失败: {e}", alert=True)

    # ══════════════════════════════════════
    #  转发成功日志面板 (m_fwd_log / fsrc_ / fdel_ / fblk_)
    # ══════════════════════════════════════

    # ── 转发成功日志面板（触发对话式输入）──
    elif data == 'm_fwd_log':
        await event.answer("⏳ 请输入条数...")
        uid = str(event.sender_id)
        try:
            _, total_forward_logs = cache.get_forwarded_records(1)
            await event.edit(
                f"📑 **转发成功日志**\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 当前共 `{total_forward_logs}` 条记录\n\n"
                f"⏳ 请输入要查看的条数（1-100）...",
                buttons=[]
            )
        except Exception:
            pass
        _spawn(_run_fwd_log_query(uid))

    # ── 二级管理菜单 ──
    elif data.startswith('fmg_'):
        parts = data.split('_')
        # fmg_{target_msg}_{src_chat}_{src_msg}_{portal_msg}_{sender}
        if len(parts) >= 4:
            target_msg = parts[1]
            src_chat = parts[2]
            src_msg = parts[3]
            portal_msg = parts[4] if len(parts) > 4 and parts[4].lower() != 'none' else None
            sender_id = parts[5] if len(parts) > 5 and parts[5].lower() != 'none' else None

            menu_text = (
                f"⚙️ **消息管理**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📋 源消息: `{src_chat}/{src_msg}`\n"
                f"📤 频道消息: `{target_msg}`\n"
                f"🚀 去看看: `{portal_msg or '无'}`\n\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
            buttons = [
                [Button.inline("📍 信息来源", data=f"fsrc_{src_chat}_{src_msg}".encode())],
                [Button.inline("🗑️ 彻底删除", data=f"fdel2_{target_msg}_{portal_msg or 'none'}".encode())],
            ]
            if sender_id:
                buttons.append([Button.inline("🚫 关键词屏蔽", data=f"fkw_{sender_id}".encode())])
            buttons.append([Button.inline("🔙 返回主菜单", data=b"m_back")])

            try:
                await event.edit(menu_text, buttons=buttons)
                await event.answer()
            except Exception as e:
                await event.answer(f"❌ {e}", alert=True)

    # ── 成对删除（消息体 + 传送门）──
    elif data.startswith('fdel2_'):
        parts = data.split('_')
        target_msg = parts[1] if len(parts) > 1 else 'none'
        portal_msg = parts[2] if len(parts) > 2 else 'none'

        deleted = []
        if target_msg != 'none':
            try:
                await _delete_target_messages([int(target_msg)])
                deleted.append("消息体")
            except Exception as e:
                logger.warning(f"⚠️ 删除消息体失败: {_format_rpc_error(e)}")

        if portal_msg != 'none':
            try:
                await _delete_target_messages([int(portal_msg)])
                deleted.append("传送门")
            except Exception as e:
                logger.warning(f"⚠️ 删除传送门失败: {_format_rpc_error(e)}")

        if deleted:
            await event.answer(f"✅ 已删除: {' + '.join(deleted)}", alert=True)
            try:
                await event.edit("🗑️ **已彻底删除**", buttons=[])
            except Exception:
                pass
        else:
            await event.answer("❌ 删除失败", alert=True)

    # ── 转发日志 → 来源详情 ──
    elif data.startswith('fsrc_'):
        parts = data.split('_', 2)
        if len(parts) == 3:
            chat_id = parts[1]
            msg_id = parts[2]
            link = f"https://t.me/c/{chat_id}/{msg_id}"
            await event.answer(f"来源: {link}", alert=True)

    # ── 转发日志 → 屏蔽发送者 ──
    elif data.startswith('fblk_'):
        uid = data[5:]
        if uid:
            try:
                db.cursor.execute("INSERT OR IGNORE INTO user_blacklist (user_id) VALUES (?)", (uid,))
                db.conn.commit()
                cache.reload_all()
                await event.answer(f"🚫 已屏蔽用户 {uid}", alert=True)
            except Exception as e:
                await event.answer(f"❌ 屏蔽失败: {e}", alert=True)

    # ── 关键词屏蔽（触发对话式输入）──
    elif data.startswith('fkw_'):
        await event.answer("⏳ 请输入关键词...")
        uid = str(event.sender_id)
        _spawn(_run_keyword_block(uid))

    # ── 过滤测试 ──
    elif data == 'm_test':
        await event.answer("⏳ 请在 120 秒内发送测试文本...")
        uid = str(event.sender_id)
        try:
            await event.edit("🧪 **过滤试运行已启动**\n\n⏳ 请在下方对话框中发送测试文本...", buttons=[])
        except Exception:
            pass
        _spawn(_run_filter_test(uid, event.chat_id))

    # ── 真实转发测试 ──
    elif data == 'm_forward_test':
        uid = str(event.sender_id)
        if _get_pending(uid):
            await event.answer("⚠️ 已有输入通道在等待中", alert=True)
            return
        await event.answer("⚠️ 将真实发送到目标频道", alert=True)
        _set_pending(uid, event.message, ttl=120)
        _pending_whitelist[uid]['action'] = 'forward_test'
        prompt = (
            "📤 **真实转发测试**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "**它是什么**：把你发的一段文字当作「源消息」走**完整发送链路**，\n"
            "**真实发到当前目标频道** —— 用来验证「过了规则层到底能不能发出去」。\n"
            "（`/test` 只跑规则层，通过 ≠ 能发；这个才是真发。）\n\n"
            "**怎么用**\n"
            "  1️⃣ 现在直接发一段文本给我（最多 4096 个 Telegram 字符）\n"
            "  2️⃣ 依次过：时效门 → 规则层 → 暂停开关 → 目标配置 → 限流 → 真实发送\n"
            "  3️⃣ 看回复：✅ 成功（含目标消息 ID / 发送方式 / 命中规则）或 🚫 拦截原因\n\n"
            "**注意**\n"
            "· 内容会真的出现在目标频道，需你**手动删除**（不会自动删）\n"
            "· 不写入去重占位，因此不影响正常转发\n"
            "· 仅 OWNER 可用；同一时间只允许一个输入通道\n\n"
            "⏳ 请在 120 秒内发送；点下方「❌ 取消」可随时退出。"
        )
        try:
            await event.edit(prompt, buttons=[[Button.inline("❌ 取消", data=b"m_forward_test_cancel")]])
        except Exception:
            await admin_bot.send_message(uid, prompt)
        _spawn(_prompt_expiry_watcher(uid, event.message, 120))

    elif data == 'm_forward_test_cancel':
        await event.answer("已取消")
        _clear_pending(str(event.sender_id))
        try:
            await event.edit(_build_start_panel(), buttons=_build_start_buttons())
        except Exception:
            pass

    # ── 取消过滤测试 ──
    elif data == 'm_test_cancel':
        await event.answer("已取消")
        _filter_test_active.discard(str(event.sender_id))
        try:
            await event.edit(_build_start_panel(), buttons=_build_start_buttons())
        except Exception:
            pass

    # ── /add → 确认添加（开启 TTL 等待）──
    elif data == 'add_confirm':
        uid = str(event.sender_id)
        if _get_pending(uid):
            await event.answer("⚠️ 已在等待输入中", alert=True)
            return

        await event.answer("✅ 通道已开启，请输入关键词")
        _set_pending(uid, event.message, ttl=60)
        _pending_whitelist[uid]['action'] = 'add_whitelist'
        prompt = (
            "🛡️ **白名单授权通道** — 已开启\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ **等待输入中...** (剩余 60s)\n\n"
            "请直接发送要添加的关键词。\n"
            "超时将自动关闭通道。"
        )
        try:
            await event.edit(prompt, buttons=[[Button.inline("❌ 取消", data=b"add_cancel")]])
        except Exception:
            await admin_bot.send_message(uid, prompt)
        _spawn(
            _add_expiry_watcher(uid, event.chat_id, event.message.id)
        )

    # ── /add → 取消授权 ──
    elif data == 'add_cancel':
        uid = str(event.sender_id)
        _clear_pending(uid)
        try:
            await event.edit(
                "❌ **授权已取消**\n\n"
                "通道已关闭。发送 `/add` 重新开启。"
            )
            await event.answer("已取消")
        except Exception as e:
            await event.answer(f"❌ {e}", alert=True)

    # ══════════════════════════════════════
    #  人工审核训练场回调 (rv_ok_ / rv_no_)
    # ══════════════════════════════════════

    # ── 确认转发（forward_bot 执行）──
    elif data.startswith('rv_ok_'):
        _prune_pending_reviews()
        review_id = data[6:]
        review = _pending_review.get(review_id)
        if not review:
            await event.answer("❌ 审核记录已过期", alert=True)
            return
        if review.get('dispatching'):
            await event.answer("⏳ 该审核正在转发", alert=True)
            return
        if not review.get('result', {}).get('pass'):
            _pending_review.pop(review_id, None)
            await event.answer("🚫 未命中信息格式，不能确认转发", alert=True)
            try:
                await event.edit("🚫 **已拦截** — 未命中信息格式，不能转发。", buttons=[])
            except Exception:
                pass
            return
        review['dispatching'] = True
        await event.answer("🔄 正在转发...", alert=True)

        src_event = review['event']
        src_msg = src_event.message
        src_text = (
            getattr(src_event, 'raw_text', None)
            or getattr(src_msg, 'message', None)
            or review['text']
        )
        safety_rejection = _forward_safety_rejection(src_event, src_text)
        if safety_rejection:
            _pending_review.pop(review_id, None)
            await event.answer("❌ 源消息已过期，不能转发", alert=True)
            try:
                await event.edit(
                    f"🚫 **已拦截**\n\n`{safety_rejection}`",
                    buttons=[]
                )
            except Exception:
                pass
            cache.log_intercept(src_text, safety_rejection, src_event.chat_id)
            return

        code_match = _safe_regex_search(config.get_pattern('CODE'), src_text)
        strict_codes = _safe_regex_findall(config.get_pattern('STRICT_CODE'), src_text)
        allow_raw_single_code = (
            not getattr(src_msg, 'edit_date', None)
            and _is_strict_single_registration_code(
                src_text,
                getattr(src_msg, 'reply_markup', None),
                getattr(src_msg, 'media', None) is not None,
            )
        )
        fresh_result = classify_intent(
            src_event,
            src_text,
            getattr(src_msg, 'reply_markup', None),
            False,
            code_match,
            len(strict_codes),
            str(src_event.sender_id) if getattr(src_event, 'sender_id', None) else "",
            allow_raw_single_code,
        )
        fresh_result = _apply_required_forward_format_gate(
            src_text, fresh_result, allow_raw_single_code
        )
        if not fresh_result.get('pass'):
            _pending_review.pop(review_id, None)
            await event.answer("❌ 源消息已变化，不能转发", alert=True)
            try:
                await event.edit(
                    f"🚫 **已拦截**\n\n重新校验失败：`{fresh_result.get('detail', '-')}`",
                    buttons=[]
                )
            except Exception:
                pass
            return

        if cache.has_forwarded_source(src_event.chat_id, src_msg.id):
            _pending_review.pop(review_id, None)
            await event.answer("❌ 源消息已转发", alert=True)
            try:
                await event.edit("🚫 **已拦截**\n\n该源消息已经转发过。", buttons=[])
            except Exception:
                pass
            return

        if not db.claim_forward(src_event.chat_id, src_msg.id):
            _pending_review.pop(review_id, None)
            await event.answer("❌ 源消息已占位，不能重复转发", alert=True)
            try:
                await event.edit("🚫 **已拦截**\n\n该源消息已进入转发流程。", buttons=[])
            except Exception:
                pass
            return

        sent_msg = None
        send_bot_idx = None
        send_client = 'bot'
        forward_method = '-'
        approval_started_at = time.time()
        await asyncio.sleep(random.uniform(0.05, 0.15))
        try:
            await rate_limiter.acquire()
            queue_rejection = _queue_delay_rejection(approval_started_at)
            safety_rejection = _forward_safety_rejection(src_event, src_text)
            if queue_rejection or safety_rejection:
                rejection = queue_rejection or safety_rejection
                db.release_forward_claim(src_event.chat_id, src_msg.id)
                _pending_review.pop(review_id, None)
                cache.log_intercept(src_text, rejection, src_event.chat_id)
                logger.info(f"⏭ 审核等待后安全门丢弃: {src_event.chat_id}_{src_msg.id} reason={rejection}")
                try:
                    await event.edit(f"🚫 **已拦截**\n\n`{rejection}`", buttons=[])
                except Exception as edit_error:
                    logger.warning(f"⚠️ 审核超时拦截提示更新失败: {_format_rpc_error(edit_error)}")
                return
            sent_msg, send_bot_idx, send_client, forward_method = await _dispatch_forward_event(src_event, src_text)
        except errors.FloodWaitError as e:
            db.release_forward_claim(src_event.chat_id, src_msg.id)
            review['dispatching'] = False
            rate_limiter.report_flood(e.seconds)
            logger.warning(f"⚠️ 审核确认触发限流: {e.seconds}s")
            await event.edit(
                f"🚨 **触发限流**\n\n需等待 `{e.seconds}` 秒后再试。",
                buttons=[[Button.inline("🔄 重试", data=f"rv_ok_{review_id}".encode()),
                          Button.inline("🗑️ 丢弃", data=f"rv_no_{review_id}".encode())]]
            )
            return
        except Exception as e:
            _pending_review.pop(review_id, None)
            logger.warning(f"⚠️ 审核确认转发失败: {_format_rpc_error(e)}")
            await event.edit(
                f"⚠️ **转发结果不确定**\n\n"
                f"`{_format_rpc_error(e)}`\n\n"
                "已保留发送占位，防止重复转发。",
                buttons=[]
            )
            return

        if sent_msg:
            _pending_review.pop(review_id, None)
            log_preview = ' '.join(str(src_text or '').split())[:80]
            logger.info(
                f"✅ 审核确认转发成功: src={src_event.chat_id}_{src_msg.id} "
                f"target={sent_msg.id} method={forward_method} preview={log_preview}"
            )
            db.mark_forward_sent(src_event.chat_id, src_msg.id, sent_msg.id)
            # 审核确认路径同样登记内容指纹：否则人工转过的卡被别的群再转发时，
            # DNA 层拦不住会重复进频道。只登记、不反向拦截人工确认本身。
            review_kind, review_fp = build_dna_fingerprint(src_text, getattr(src_msg, 'reply_markup', None))
            if review_fp:
                review_dna = hashlib.md5(
                    (review_kind + ":" + review_fp).encode(), usedforsecurity=False
                ).hexdigest()
                is_review_lottery = review_kind.startswith("LOTTERY_")
                cache.add_dna(review_dna, is_lottery=is_review_lottery)
                _persist_dna(review_dna, is_review_lottery)
            # 传送门（私聊/测试流/私密群组 → 纯文本降级）
            portal_msg = None
            src_link = "内部源，无直连"
            portal_bot_idx = None
            if FORWARD_MODE != "user_native_only":
                try:
                    portal_msg, src_link, portal_bot_idx = await _send_portal_message(
                        src_event.chat_id, src_msg.id, src_event.is_private, preferred_bot_idx=send_bot_idx
                    )
                except Exception:
                    pass

            FORWARDED_COUNT += 1
            FORWARDED_TODAY += 1
            _record_activity('forward')
            src_uid = f"{src_event.chat_id}_{src_msg.id}"
            remember_forward(src_uid, {
                'ts': time.time(),
                'dna': None,
                'target_msg_id': sent_msg.id,
                'portal_msg_id': portal_msg.id if portal_msg else None,
                'send_bot_idx': send_bot_idx,
                'send_client': send_client,
                'portal_bot_idx': portal_bot_idx,
            })
            cache.log_forward({
                'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                'preview': src_text[:500],
                'src_chat_id': str(src_event.chat_id).removeprefix('-100'),
                'src_msg_id': src_msg.id,
                'target_msg_id': sent_msg.id,
                'portal_msg_id': portal_msg.id if portal_msg else None,
                'sender_id': str(src_event.sender_id) if getattr(src_event, 'sender_id', None) else '',
                'msg_date': str(getattr(src_msg, 'date', '') or ''),
                'is_edited': '1' if getattr(src_msg, 'edit_date', None) else '',
                'chat_title': str(getattr(getattr(src_event, 'chat', None), 'title', '') or ''),
                'send_bot_idx': send_bot_idx,
                'send_client': send_client,
                'portal_bot_idx': portal_bot_idx,
            })

            await event.edit(
                f"✅ **已确认转发至频道**\n\n"
                f"📝 `{src_text[:50]}...`\n"
                f"📎 {src_link}",
                buttons=[]
            )
        else:
            db.release_forward_claim(src_event.chat_id, src_msg.id)
            review['dispatching'] = False
            await event.edit("❌ **转发失败**", buttons=[])

    # ── 丢弃 ──
    elif data.startswith('rv_no_'):
        _prune_pending_reviews()
        review_id = data[6:]
        _pending_review.pop(review_id, None)
        await event.answer("🗑️ 已丢弃", alert=False)
        try:
            await event.edit("🗑️ **已丢弃** — 该消息不会转发。", buttons=[])
        except Exception:
            pass


# ================= UTF-16 安全实体截断 =================
_CAPTION_LIMIT = 1024  # send_file 的 caption 上限（UTF-16 代码单元）
_MSG_LIMIT = 4096      # send_message 的 text 上限


def _utf16_len(s):
    """以 UTF-16 代码单元为单位获取字符串长度"""
    return len(s.encode('utf-16-le')) // 2


def _truncate_utf16(s, max_units):
    """按 UTF-16 代码单元数截断字符串，不乱拆 surrogate pair。单次遍历 O(n)。"""
    units = 0
    for i, ch in enumerate(s):
        units += 1 if ord(ch) <= 0xFFFF else 2
        if units > max_units:
            return s[:i]
    return s


def _sanitize_for_send(text, entities, limit):
    """UTF-16 安全的截断，防止 emoji 导致实体偏移错位"""
    if not text:
        return text, entities or []
    truncated = _truncate_utf16(text, limit)
    if not entities:
        return truncated, []
    safe = []
    trunc_utf16 = _utf16_len(truncated)
    for ent in entities:
        if ent.offset >= trunc_utf16:
            continue
        new_len_utf16 = min(ent.length, trunc_utf16 - ent.offset)
        if new_len_utf16 > 0:
            new_ent = copy.copy(ent)
            new_ent.offset = ent.offset
            new_ent.length = new_len_utf16
            safe.append(new_ent)
    return truncated, safe


def _extract_msg_text_entities(msg):
    """优先使用 raw_text，避免 message.text 携带 Markdown 标记字符。"""
    return (getattr(msg, 'raw_text', None) or ''), (getattr(msg, 'entities', None) or [])


def _is_structured_lottery_notice(text):
    """高置信抽奖通知：统一读取“信息格式”模板。"""
    return bool(text and _match_forward_format_names(text, LOTTERY_FORWARD_FORMAT_NAMES))


def _is_structured_registration_notice(text):
    """高置信开放注册/自由注册通知：统一读取“信息格式”模板。"""
    return bool(text and _match_forward_format_names(text, REGISTRATION_FORWARD_FORMAT_NAMES))


def _is_bulk_registration_code_notice(text):
    """高置信注册码消息：统一读取“信息格式”模板。"""
    return bool(text and _match_forward_format_names(text, CODE_FORWARD_FORMAT_NAMES))


_SINGLE_REGISTRATION_CODE_RE = re.compile(
    r'(?i)^(?:(?:注册码|激活码|兑换码|续期码)\s*[:：]\s*)?'
    r'(?:[\w\u4e00-\u9fff][\w\u4e00-\u9fff-]{0,63}-)?'
    r'(?:(?:register|renew)_[A-Za-z0-9*?!@#$%^&()_+=~\-]{6,}'
    r'|(?:register|renew)-[A-Za-z0-9]{1,24}(?:-[A-Za-z0-9]{1,24}){1,4}'
    r'|whitelist_[A-Za-z0-9]{6,})$'
)


def _is_strict_single_registration_code(text, markup=None, has_media=False):
    """Allow only a standalone Register_/Renew_ code, never a card or promotion."""
    if not text or markup is not None or has_media:
        return False
    return _SINGLE_REGISTRATION_CODE_RE.fullmatch(text.strip()) is not None


def _has_required_forward_format(text, result, allow_raw_single_code=False):
    """最终格式门：只有内置/自定义信息格式能进入转发链路。"""
    if not text or not result:
        return False
    marker = f"{result.get('domain', '')} {result.get('detail', '')}"
    # _match_custom_forward_format 已覆盖所有内置格式（含抽奖/注册/注册码结构），
    # 无需再分别调 _is_structured_*_notice —— 等价但少遍历三轮。
    return bool(
        '抽奖结构' in marker
        or '注册结构' in marker
        or '注册码结构' in marker
        or '信息格式:' in marker
        or _match_custom_forward_format(text)
        or allow_raw_single_code
    )


def _apply_required_forward_format_gate(text, result, allow_raw_single_code=False):
    """把所有入口统一收口到“必须命中信息格式”。"""
    if not result or not result.get('pass'):
        return result
    if _has_required_forward_format(text, result, allow_raw_single_code):
        return result
    gated = dict(result)
    domain = gated.get('domain', 'none')
    gated.update({'pass': False, 'priority': '-', 'detail': f'{domain} 未命中信息格式'})
    return gated


def _looks_like_forward_candidate(text):
    """仅用于诊断日志：识别疑似应进入过滤管道的高价值消息。"""
    text = _normalize_match_text(text)
    if not text:
        return False
    lower_text = text.lower()
    markers = (
        "注册码", "激活码", "邀请码", "兑换码", "续期码",
        "开放注册", "自由注册", "开启注册", "总注册限制",
        "已注册人数", "剩余可注册", "bot使用人数",
        "抽奖", "?start=", "register_", "renew_"
    )
    return any(k in lower_text for k in markers)


async def _is_private_bot_lottery(event, text):
    """Allow only strict lottery cards from a private Bot source, never ordinary DMs."""
    if not event.is_private or not _is_structured_lottery_notice(text):
        return False
    try:
        sender = await event.get_sender()
    except Exception as e:
        logger.warning(f"⚠️ 私聊抽奖发送方识别失败，按安全策略跳过: {_format_rpc_error(e)}")
        return False
    return bool(getattr(sender, 'bot', False))


async def _build_portal_payload(chat_id, msg_id, is_private):
    """构建传送门文案和可展示链接。"""
    if is_private:
        return "🚀 去看看：[私聊源，无直连]", "私聊源，无直连"

    src_chat_entity = None
    try:
        src_chat_entity = await client.get_entity(chat_id)
    except Exception:
        pass

    has_username = hasattr(src_chat_entity, 'username') and src_chat_entity.username
    if has_username:
        src_link = f"https://t.me/{src_chat_entity.username}/{msg_id}"
        return f'🚀 去看看：<a href="{src_link}">点击跳转</a>', src_link

    cid = str(chat_id)
    if cid.startswith("-100") and len(cid) > 4:
        # 私有频道/超级群回退链接，目标用户需在源群内才可打开
        c_link = f"https://t.me/c/{cid[4:]}/{msg_id}"
        return f'🚀 去看看：<a href="{c_link}">点击跳转</a>', c_link

    return "🚀 去看看：[内部源，无直连]", "内部源，无直连"


async def _send_portal_message(chat_id, msg_id, is_private, preferred_bot_idx=None, attempts=2):
    portal_text, src_link = await _build_portal_payload(chat_id, msg_id, is_private)
    last_err = None
    for bot_idx, bot_client in _iter_forward_bots(start_idx=preferred_bot_idx):
        for attempt in range(1, attempts + 1):
            try:
                target_peer = await _resolve_bot_target_peer(bot_idx, bot_client)
                await rate_limiter.acquire()
                sent = await bot_client.send_message(
                    target_peer,
                    portal_text,
                    parse_mode='html',
                    link_preview=False
                )
                return sent, src_link, bot_idx
            except errors.FloodWaitError as e:
                rate_limiter.report_flood(e.seconds)
                logger.warning(f"⚠️ 传送门触发限流，跳过本次传送门: {e.seconds}s")
                return None, src_link, None
            except Exception as e:
                last_err = e
                if attempt < attempts:
                    await asyncio.sleep(0.8)
    logger.warning(f"⚠️ 传送门发送失败: {_format_rpc_error(last_err)}")
    return None, src_link, None


def _is_invalid_peer_error(exc):
    """识别 Telegram Peer 无效类异常，避免打成通用转发崩溃。"""
    if exc is None:
        return False
    name = type(exc).__name__
    if "PeerIdInvalid" in name or "ChannelInvalid" in name or "ChatIdInvalid" in name:
        return True
    msg = str(exc).lower()
    return "invalid peer" in msg or "peer_id_invalid" in msg


def _is_source_visibility_error(exc):
    """Bot 看不到源会话/源消息时，允许切到主账号原生兜底。"""
    if exc is None:
        return False
    if isinstance(exc, BotSourceUnavailableError):
        return True
    err_name = type(exc).__name__
    if err_name in ("ChannelPrivateError", "MessageIdInvalidError"):
        return True
    msg = str(exc)
    lower_msg = msg.lower()
    return (
        "对源消息不可见" in msg
        or "无法访问源聊天" in msg
        or "source peer" in lower_msg
        or "source chat" in lower_msg
        or "message id invalid" in lower_msg
        or "message_id_invalid" in lower_msg
        or "channelprivate" in lower_msg
    )


_bot_source_peer_cache = {}   # {bot_idx: {chat_key: {'peer': x, 'expires': ts}}}
_bot_target_peer_cache = {}   # {bot_idx: {'key': target_id, 'peer': x, 'expires': ts}}
_user_target_peer_cache = {}  # {'key': target_id, 'peer': x, 'expires': ts}
MAX_BOT_SOURCE_PEER_CACHE = 256
_bot_native_skip_cache = {}   # {source_chat_id: expires}; all Bots recently lacked source access


def _is_bot_native_source_skipped(event):
    chat_id = getattr(event, 'chat_id', None)
    if chat_id is None:
        return False
    key = str(chat_id)
    now = time.monotonic()
    expires = _bot_native_skip_cache.get(key, 0)
    if expires > now:
        return True
    _bot_native_skip_cache.pop(key, None)
    return False


def _mark_bot_native_source_skipped(event):
    chat_id = getattr(event, 'chat_id', None)
    if chat_id is None:
        return
    now = time.monotonic()
    for key, expires in list(_bot_native_skip_cache.items()):
        if expires <= now:
            _bot_native_skip_cache.pop(key, None)
    if len(_bot_native_skip_cache) >= MAX_BOT_SOURCE_PEER_CACHE:
        oldest_key = min(_bot_native_skip_cache, key=_bot_native_skip_cache.get)
        _bot_native_skip_cache.pop(oldest_key, None)
    _bot_native_skip_cache[str(chat_id)] = now + BOT_SOURCE_NEG_TTL
    logger.info(
        "⚡ 所有转发 Bot 均不可访问源聊天，%ss 内直接克隆: chat=%s",
        BOT_SOURCE_NEG_TTL,
        chat_id,
    )


def _next_forward_bot_index():
    global _forward_rr_index
    if not forward_bots:
        return 0
    _forward_rr_index = (_forward_rr_index + 1) % len(forward_bots)
    return _forward_rr_index


def _iter_forward_bots(start_idx=None):
    if not forward_bots:
        return []
    n = len(forward_bots)
    if start_idx is None:
        start_idx = _next_forward_bot_index()
    return [((start_idx + off) % n, forward_bots[(start_idx + off) % n]) for off in range(n)]


def _raise_forward_error(last_err, fallback_msg):
    if isinstance(last_err, errors.FloodWaitError):
        raise last_err
    if isinstance(last_err, BotSourceUnavailableError):
        raise last_err
    raise RuntimeError(_format_rpc_error(last_err) if last_err else fallback_msg)


async def _resolve_user_target_peer():
    """缓存主账号视角下的目标频道实体，用于原生转发兜底和联动删除。"""
    if not TARGET_ID:
        raise RuntimeError("TARGET_ID 未配置")

    key = str(TARGET_ID)
    now = time.monotonic()
    if (_user_target_peer_cache.get('key') == key
            and _user_target_peer_cache.get('peer') is not None
            and _user_target_peer_cache.get('expires', 0) > now):
        return _user_target_peer_cache['peer']

    peer = await client.get_input_entity(TARGET_ID)
    _user_target_peer_cache.clear()
    _user_target_peer_cache.update({
        'key': key,
        'peer': peer,
        'expires': now + BOT_SOURCE_CACHE_TTL,
    })
    return peer


async def _resolve_user_source_peer(event):
    """用主账号自身解析源会话；主账号能监听到的消息通常都能解析。"""
    try:
        return await event.get_input_chat()
    except Exception:
        chat_id = getattr(event, 'chat_id', None)
        if chat_id is not None:
            return await client.get_input_entity(chat_id)
        return getattr(event.message, 'peer_id', None)


async def _forward_native_by_user(event, acquire_rate_limit=True):
    """Bot 看不到源消息时，由监听主账号执行原生转发兜底。"""
    target_peer = await _resolve_user_target_peer()
    source_peer = await _resolve_user_source_peer(event)
    if not source_peer:
        raise RuntimeError("主账号无法解析源聊天")

    if acquire_rate_limit:
        await rate_limiter.acquire()
    await asyncio.sleep(random.uniform(0.08, 0.25))
    sent_msg = await client.forward_messages(
        target_peer,
        event.message.id,
        from_peer=source_peer
    )
    if isinstance(sent_msg, list):
        sent_msg = sent_msg[0] if sent_msg else None
    return sent_msg


async def _delete_target_messages_by_user(message_ids):
    """主账号兜底转发的消息，必要时也用主账号执行联动删除。"""
    ids = [int(x) for x in message_ids if x]
    if not ids:
        return
    target_peer = await _resolve_user_target_peer()
    await client.delete_messages(target_peer, ids)


async def _check_bot_target_permission(bot_client, target_id, label):
    """预检查 Bot 对目标会话是否可见且具备基础发送权限。"""
    try:
        entity = await bot_client.get_entity(target_id)
    except Exception as e:
        return False, f"{label}不可访问: {_format_rpc_error(e)}"

    is_broadcast = bool(getattr(entity, "broadcast", False))
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or str(target_id)

    perms_err = None
    try:
        perms = await bot_client.get_permissions(entity, 'me')
    except Exception as e:
        perms = None
        perms_err = e

    if perms is None and (is_broadcast or bool(getattr(entity, "megagroup", False))):
        return False, f"{label}权限读取失败: {_format_rpc_error(perms_err)}"

    if perms is not None:
        if getattr(perms, "is_banned", False):
            return False, f"{label}已被限制发言: {title}"
        if is_broadcast:
            can_post = bool(
                getattr(perms, "is_creator", False)
                or getattr(perms, "post_messages", False)
                or getattr(perms, "is_admin", False)
            )
            if not can_post:
                return False, f"{label}缺少发帖权限: {title}"
        else:
            can_send = getattr(perms, "send_messages", None)
            if can_send is False:
                return False, f"{label}缺少发言权限: {title}"

    return True, f"{label}校验通过: {title}"


async def _check_forward_target_across_bots(target_id, label):
    passed = []
    failed = []
    for idx, bot in enumerate(forward_bots):
        ok, msg = await _check_bot_target_permission(bot, target_id, f"{label}#Bot{idx + 1}")
        if ok:
            passed.append(msg)
        else:
            failed.append(msg)

    if passed:
        summary = f"通过 {len(passed)}/{len(forward_bots)}"
        if failed:
            summary += f"；失败 {len(failed)} 个（可继续但会降低兜底能力）"
        return True, summary
    fail_msg = failed[0] if failed else "全部转发 Bot 校验失败"
    return False, fail_msg


async def _precheck_targets():
    """启动时预检查转发目标和备份频道权限，提前暴露配置问题。"""
    if TARGET_ID:
        if FORWARD_MODE == "user_native_only":
            ok, msg = await _check_bot_target_permission(client, int(TARGET_ID), "主账号原生目标")
            if ok:
                logger.info(f"✅ {msg}")
            else:
                logger.warning(f"⚠️ {msg}")
        else:
            ok, msg = await _check_forward_target_across_bots(int(TARGET_ID), "转发目标")
            if ok:
                logger.info(f"✅ {msg}")
            else:
                logger.warning(f"⚠️ {msg}")

        if ALLOW_USER_NATIVE_FALLBACK and FORWARD_MODE != "user_native_only":
            ok, msg = await _check_bot_target_permission(client, int(TARGET_ID), "主账号兜底目标")
            if ok:
                logger.info(f"✅ {msg}")
            else:
                logger.warning(f"⚠️ {msg}")

    if BACKUP_ID:
        ok, msg = await _check_bot_target_permission(admin_bot, int(BACKUP_ID), "备份频道")
        if ok:
            logger.info(f"✅ {msg}")
        else:
            logger.warning(f"⚠️ {msg}")


async def _delete_target_messages(message_ids, preferred_bot_idx=None, prefer_user=False):
    """删除目标频道消息；主账号兜底发送的消息优先由主账号删除。"""
    ids = [int(x) for x in message_ids if x]
    if not ids:
        return

    last_err = None
    if prefer_user and (ALLOW_USER_NATIVE_FALLBACK or FORWARD_MODE == "user_native_only"):
        try:
            await _delete_target_messages_by_user(ids)
            return
        except Exception as e:
            last_err = e

    for bot_idx, bot_client in _iter_forward_bots(start_idx=preferred_bot_idx):
        try:
            target_peer = await _resolve_bot_target_peer(bot_idx, bot_client)
            await bot_client.delete_messages(target_peer, ids)
            return
        except Exception as e:
            last_err = e
            continue

    if ALLOW_USER_NATIVE_FALLBACK or FORWARD_MODE == "user_native_only":
        try:
            await _delete_target_messages_by_user(ids)
            return
        except Exception as e:
            last_err = e

    _raise_forward_error(last_err, "删除失败")


async def _resolve_bot_target_peer(bot_idx, bot_client):
    """缓存每个 forward_bot 视角下的目标频道实体，避免 raw ID 偶发实体解析失败。"""
    if not TARGET_ID:
        raise RuntimeError("TARGET_ID 未配置")

    key = str(TARGET_ID)
    now = time.monotonic()
    cache = _bot_target_peer_cache.get(bot_idx) or {}
    if (cache.get('key') == key
            and cache.get('peer') is not None
            and cache.get('expires', 0) > now):
        return cache['peer']

    peer = await bot_client.get_input_entity(TARGET_ID)
    _bot_target_peer_cache[bot_idx] = {
        'key': key,
        'peer': peer,
        'expires': now + BOT_SOURCE_CACHE_TTL,
    }
    return peer


async def _resolve_bot_source_peer(bot_idx, bot_client, event):
    """
    用指定 forward_bot 的会话解析源实体。
    只有 Bot 也能访问源聊天时，Telegram 才允许 Bot 原生转发该消息。
    """
    chat_id = getattr(event, 'chat_id', None)
    if chat_id is None:
        return None

    key = str(chat_id)
    now = time.monotonic()
    bot_cache = _bot_source_peer_cache.setdefault(bot_idx, {})
    cached = bot_cache.get(key)
    if cached and cached['expires'] > now:
        return cached['peer']

    for stale_key, entry in list(bot_cache.items()):
        if entry.get('expires', 0) <= now:
            bot_cache.pop(stale_key, None)
    if len(bot_cache) >= MAX_BOT_SOURCE_PEER_CACHE:
        oldest_key = min(bot_cache, key=lambda cache_key: bot_cache[cache_key].get('expires', 0))
        bot_cache.pop(oldest_key, None)

    candidates = []
    try:
        chat = getattr(event, 'chat', None) or await event.get_chat()
        username = getattr(chat, 'username', None)
        if username:
            candidates.append(username if str(username).startswith('@') else f"@{username}")
    except Exception:
        pass

    candidates.extend([chat_id, str(chat_id)])

    seen = set()
    last_err = None
    for candidate in candidates:
        ckey = str(candidate)
        if ckey in seen:
            continue
        seen.add(ckey)
        try:
            peer = await bot_client.get_input_entity(candidate)
            bot_cache[key] = {
                'peer': peer,
                'expires': now + BOT_SOURCE_CACHE_TTL,
            }
            return peer
        except Exception as e:
            last_err = e

    bot_cache[key] = {
        'peer': None,
        'expires': now + BOT_SOURCE_NEG_TTL,
    }
    logger.warning(
        f"⚠️ forward_bot#{bot_idx + 1} 无法访问源聊天 {chat_id}，无法执行 Bot 原生转发: "
        f"{_format_rpc_error(last_err)}"
    )
    return None


async def _forward_native_by_bot(event):
    """由 forward_bot 池执行原生转发，失败自动切换下一只。"""
    if _is_bot_native_source_skipped(event):
        raise BotSourceUnavailableError("源聊天近期已确认对全部转发 Bot 不可见")

    last_err = None
    source_unavailable = 0
    for bot_idx, bot_client in _iter_forward_bots():
        try:
            target_peer = await _resolve_bot_target_peer(bot_idx, bot_client)
            source_peer = await _resolve_bot_source_peer(bot_idx, bot_client, event)
            if not source_peer:
                source_unavailable += 1
                last_err = BotSourceUnavailableError(f"forward_bot#{bot_idx + 1} 无法访问源聊天")
                continue

            sent_msg = await bot_client.forward_messages(
                target_peer,
                event.message.id,
                from_peer=source_peer
            )
            if isinstance(sent_msg, list):
                sent_msg = sent_msg[0] if sent_msg else None
            return sent_msg, bot_idx
        except errors.ChatForwardsRestrictedError:
            raise
        except errors.FloodWaitError:
            raise
        except Exception as e:
            last_err = e
            continue

    if forward_bots and source_unavailable == len(forward_bots):
        _mark_bot_native_source_skipped(event)
    _raise_forward_error(last_err, "无可用转发 Bot")


def _has_downloadable_media(msg):
    """区分真实附件和链接预览；WebPage 预览不能按文件下载克隆。"""
    media = getattr(msg, 'media', None)
    if media is None:
        return False
    media_type = type(media).__name__
    if media_type in ('MessageMediaWebPage', 'MessageMediaUnsupported', 'MessageMediaPoll', 'MessageMediaDice'):
        return False
    return bool(getattr(msg, 'file', None) or getattr(msg, 'photo', None) or getattr(msg, 'document', None))


def _has_media_payload(msg):
    """目标消息完整性检查：只要有真实媒体载荷就算保留成功。"""
    media = getattr(msg, 'media', None)
    if media is None:
        return False
    media_type = type(media).__name__
    return media_type not in ('MessageMediaWebPage', 'MessageMediaUnsupported', 'MessageMediaPoll', 'MessageMediaDice')


def _get_media_size_bytes(msg):
    file_obj = getattr(msg, 'file', None)
    size = getattr(file_obj, 'size', None)
    if size:
        return int(size)

    media = getattr(msg, 'media', None)
    document = getattr(media, 'document', None)
    size = getattr(document, 'size', None)
    return int(size) if size else 0


def _get_message_document(msg):
    media = getattr(msg, 'media', None)
    return getattr(media, 'document', None) or getattr(msg, 'document', None)


def _is_animated_media(msg):
    if getattr(msg, 'gif', None):
        return True
    document = _get_message_document(msg)
    for attr in getattr(document, 'attributes', []) or []:
        if isinstance(attr, tl_types.DocumentAttributeAnimated):
            return True
    return False


def _clone_media_send_kwargs(msg):
    """Preserve Telegram GIF/animation metadata when media has to be cloned."""
    document = _get_message_document(msg)
    file_obj = getattr(msg, 'file', None)
    mime_type = (
        getattr(file_obj, 'mime_type', None)
        or getattr(document, 'mime_type', None)
    )
    is_animated = _is_animated_media(msg)
    kwargs = {'force_document': False}
    if mime_type:
        kwargs['mime_type'] = mime_type
    if is_animated:
        attrs = [copy.copy(x) for x in (getattr(document, 'attributes', []) or [])]
        if not any(isinstance(x, tl_types.DocumentAttributeAnimated) for x in attrs):
            attrs.append(tl_types.DocumentAttributeAnimated())
        kwargs['attributes'] = attrs
        kwargs['supports_streaming'] = True
    elif getattr(msg, 'video', None):
        kwargs['supports_streaming'] = True
    return kwargs


def _allow_media_clone_for_event(event, allow_safe_media=False):
    """媒体克隆开关：全量显式开关优先；安全小媒体仅用于原生转发失败后的兜底。"""
    if ALLOW_MEDIA_CLONE:
        return True
    if not allow_safe_media or not ALLOW_SAFE_MEDIA_CLONE_FALLBACK:
        return False
    return not getattr(event.message, 'noforwards', False)


async def _send_text_clone(text, entities, buttons, preferred_bot_idx=None):
    last_err = None
    for bot_idx, bot_client in _iter_forward_bots(start_idx=preferred_bot_idx):
        try:
            target_peer = await _resolve_bot_target_peer(bot_idx, bot_client)
            sent = await bot_client.send_message(
                target_peer,
                text,
                formatting_entities=entities,
                buttons=buttons,
                link_preview=False,
                parse_mode=None
            )
            return sent, bot_idx
        except errors.FloodWaitError:
            raise
        except Exception as e:
            last_err = e
            if buttons:
                try:
                    logger.warning(f"⚠️ 克隆文本带按钮发送失败，去按钮重试: {_format_rpc_error(e)}")
                    await rate_limiter.acquire()
                    target_peer = await _resolve_bot_target_peer(bot_idx, bot_client)
                    sent = await bot_client.send_message(
                        target_peer,
                        text,
                        formatting_entities=entities,
                        buttons=None,
                        link_preview=False,
                        parse_mode=None
                    )
                    return sent, bot_idx
                except errors.FloodWaitError:
                    raise
                except Exception as e2:
                    last_err = e2
                    continue
    _raise_forward_error(last_err, "克隆文本发送失败")


async def _clone_send_by_bot(
    event,
    src_buttons,
    preferred_bot_idx=None,
    allow_safe_media=False,
    require_media=False,
    force_text_only=False,
):
    """
    Bot 克隆兜底：默认只克隆文本；原生转发失败时可允许小媒体安全兜底。
    这不是原生转发，但所有发送仍由 forward_bot 执行，主账号不产生上行发送。
    """
    has_media = _has_downloadable_media(event.message)
    msg_text, msg_entities = _extract_msg_text_entities(event.message)
    send_text, send_entities = _sanitize_for_send(msg_text, msg_entities, _MSG_LIMIT)

    if not send_text.strip() and not has_media:
        logger.info("⏭ 克隆兜底跳过：无文本无媒体")
        return None, preferred_bot_idx

    await asyncio.sleep(random.uniform(0.05, 0.15))
    media_type = type(getattr(event.message, 'media', None)).__name__ if getattr(event.message, 'media', None) else 'None'
    logger.info(
        f"🔁 克隆兜底开始: src={getattr(event, 'chat_id', '')}_{getattr(event.message, 'id', '')} "
        f"media={media_type} downloadable={has_media} buttons={bool(src_buttons)} text_len={len(send_text)}"
    )

    if has_media and not force_text_only:
        if not _allow_media_clone_for_event(event, allow_safe_media=allow_safe_media):
            logger.warning("⚠️ 媒体克隆未启用或当前场景不允许，仅发送文本内容")
            if require_media:
                raise RuntimeError("源消息含媒体，但当前安全策略不允许媒体克隆")
            if send_text.strip():
                await rate_limiter.acquire()
                return await _send_text_clone(send_text, send_entities, src_buttons, preferred_bot_idx)
            await rate_limiter.acquire()
            return await _send_text_clone("🚀 去看看：源消息为媒体，媒体克隆已关闭", [], None, preferred_bot_idx)

        media_size = _get_media_size_bytes(event.message)
        if allow_safe_media and not ALLOW_MEDIA_CLONE and not media_size:
            logger.warning("⚠️ 克隆媒体大小未知，安全兜底模式下跳过媒体仅转文本")
            if require_media:
                raise RuntimeError("源媒体大小未知，安全策略禁止媒体克隆")
            if send_text.strip():
                await rate_limiter.acquire()
                return await _send_text_clone(send_text, send_entities, src_buttons, preferred_bot_idx)
            await rate_limiter.acquire()
            return await _send_text_clone("🚀 去看看：源媒体大小未知，已按安全策略跳过媒体克隆", [], None, preferred_bot_idx)
        if media_size and media_size > MAX_CLONE_MEDIA_BYTES:
            logger.warning(
                f"⚠️ 克隆媒体过大，跳过媒体仅转文本: "
                f"{media_size / 1024 / 1024:.1f}MB>{MAX_CLONE_MEDIA_MB}MB"
            )
            if require_media:
                raise RuntimeError(f"源媒体超过安全克隆上限: {media_size}>{MAX_CLONE_MEDIA_BYTES}")
            if send_text.strip():
                await rate_limiter.acquire()
                return await _send_text_clone(send_text, send_entities, src_buttons, preferred_bot_idx)
            await rate_limiter.acquire()
            return await _send_text_clone("🚀 去看看：点击跳转", [], None, preferred_bot_idx)

        media_bytes = io.BytesIO()
        try:
            await asyncio.wait_for(
                client.download_media(event.message, file=media_bytes),
                timeout=30.0
            )
            media_bytes.seek(0)
            ext = getattr(event.message.file, 'ext', None)
            if not ext:
                if _is_animated_media(event.message):
                    mime_type = _clone_media_send_kwargs(event.message).get('mime_type', '')
                    ext = '.mp4' if mime_type == 'video/mp4' else '.gif'
                else:
                    ext = '.mp4' if getattr(event.message, 'video', None) else '.jpg'
            media_bytes.name = f'media{ext}'
            cap_text, cap_entities = _sanitize_for_send(msg_text, msg_entities, _CAPTION_LIMIT)
            media_send_kwargs = _clone_media_send_kwargs(event.message)
            last_err = None
            for bot_idx, bot_client in _iter_forward_bots(start_idx=preferred_bot_idx):
                try:
                    await rate_limiter.acquire()
                    media_bytes.seek(0)
                    target_peer = await _resolve_bot_target_peer(bot_idx, bot_client)
                    sent_msg = await bot_client.send_file(
                        target_peer,
                        file=media_bytes,
                        caption=cap_text if cap_text.strip() else None,
                        formatting_entities=cap_entities if cap_text.strip() else None,
                        buttons=src_buttons,
                        parse_mode=None,
                        **media_send_kwargs
                    )
                    if require_media and not _has_media_payload(sent_msg):
                        try:
                            await bot_client.delete_messages(target_peer, [sent_msg.id])
                        except Exception as del_err:
                            logger.warning(f"⚠️ 删除克隆后的无媒体残缺消息失败: {_format_rpc_error(del_err)}")
                        raise RuntimeError("克隆媒体发送后目标消息仍无媒体")
                    return sent_msg, bot_idx
                except errors.FloodWaitError:
                    raise
                except Exception as e:
                    last_err = e
                    if src_buttons:
                        try:
                            logger.warning(f"⚠️ 克隆媒体带按钮发送失败，去按钮重试: {_format_rpc_error(e)}")
                            await rate_limiter.acquire()
                            media_bytes.seek(0)
                            target_peer = await _resolve_bot_target_peer(bot_idx, bot_client)
                            sent_msg = await bot_client.send_file(
                                target_peer,
                                file=media_bytes,
                                caption=cap_text if cap_text.strip() else None,
                                formatting_entities=cap_entities if cap_text.strip() else None,
                                buttons=None,
                                parse_mode=None,
                                **media_send_kwargs
                            )
                            if require_media and not _has_media_payload(sent_msg):
                                try:
                                    await bot_client.delete_messages(target_peer, [sent_msg.id])
                                except Exception as del_err:
                                    logger.warning(f"⚠️ 删除克隆重试后的无媒体残缺消息失败: {_format_rpc_error(del_err)}")
                                raise RuntimeError("克隆媒体去按钮重试后目标消息仍无媒体")
                            return sent_msg, bot_idx
                        except errors.FloodWaitError:
                            raise
                        except Exception as e2:
                            last_err = e2
                            continue
            _raise_forward_error(last_err, "克隆媒体发送失败")
        except errors.FloodWaitError:
            raise
        except Exception as clone_err:
            logger.warning(f"⚠️ 克隆媒体发送失败: {_format_rpc_error(clone_err)}")
            if require_media:
                raise
            if send_text.strip():
                await rate_limiter.acquire()
                return await _send_text_clone(send_text, send_entities, src_buttons, preferred_bot_idx)
            raise
        finally:
            media_bytes.close()

    if send_text.strip():
        await rate_limiter.acquire()
        return await _send_text_clone(send_text, send_entities, src_buttons, preferred_bot_idx)
    return None, preferred_bot_idx


async def _repair_missing_forward_media(event, sent_msg, send_bot_idx, send_client, forward_method, src_buttons):
    """源消息有媒体但目标缺媒体时，删除残缺转发并强制走安全小媒体克隆。"""
    if not sent_msg or not _has_downloadable_media(event.message):
        return sent_msg, send_bot_idx, send_client, forward_method
    if _has_media_payload(sent_msg):
        return sent_msg, send_bot_idx, send_client, forward_method
    if (
        forward_method == 'clone_protected_text'
        or (getattr(event.message, 'noforwards', False) and not ALLOW_PROTECTED_CLONE)
    ):
        logger.info(
            f"⏭ 受保护消息保持纯文本兜底，不尝试补发媒体: "
            f"src={getattr(event, 'chat_id', '')}_{getattr(event.message, 'id', '')}"
        )
        return sent_msg, send_bot_idx, send_client, forward_method

    media_type = type(getattr(event.message, 'media', None)).__name__ if getattr(event.message, 'media', None) else 'None'
    logger.warning(
        f"⚠️ 转发后媒体缺失，准备删除残缺消息并安全克隆媒体: "
        f"src={getattr(event, 'chat_id', '')}_{getattr(event.message, 'id', '')} "
        f"target={getattr(sent_msg, 'id', '')} method={forward_method} media={media_type}"
    )

    try:
        await _delete_target_messages(
            [sent_msg.id],
            preferred_bot_idx=send_bot_idx,
            prefer_user=(send_client == 'user')
        )
    except Exception as e:
        logger.warning(f"⚠️ 删除缺媒体的残缺转发失败，继续尝试补发媒体: {_format_rpc_error(e)}")

    repaired_msg, repair_bot_idx = await _clone_send_by_bot(
        event,
        src_buttons,
        preferred_bot_idx=send_bot_idx,
        allow_safe_media=True,
        require_media=True
    )
    return repaired_msg, repair_bot_idx, 'bot', f"{forward_method}_media_repair"


async def _dispatch_forward_event(event, text):
    """按配置选择主账号原生专用或兼容模式转发链路。"""
    source_uid = f"{getattr(event, 'chat_id', '')}_{getattr(event.message, 'id', '')}"
    sent_msg = None
    send_bot_idx = None
    send_client = 'bot'
    forward_method = 'native'
    src_buttons = event.message.reply_markup if hasattr(event.message, 'reply_markup') else None
    is_restricted = getattr(event.message, 'noforwards', False)
    protected_text_clone_enabled = bool(
        ALLOW_BOT_CLONE_FALLBACK
        and ALLOW_PROTECTED_TEXT_CLONE
        and (text or '').strip()
    )
    can_clone_protected_text = is_restricted and protected_text_clone_enabled

    if FORWARD_MODE == "user_native_only":
        if is_restricted:
            cache.log_intercept(text, "[drop] 源消息禁止原生转发", event.chat_id)
            return None, None, 'user', 'user_native_only'
        sent_msg = await _forward_native_by_user(event, acquire_rate_limit=False)
        if isinstance(sent_msg, list):
            sent_msg = sent_msg[0] if sent_msg else None
        return sent_msg, None, 'user', 'user_native_only'

    try:
        sent_msg, send_bot_idx = await _forward_native_by_bot(event)
    except errors.ChatForwardsRestrictedError as native_err:
        if ALLOW_BOT_CLONE_FALLBACK and (ALLOW_PROTECTED_CLONE or protected_text_clone_enabled):
            force_text_only = protected_text_clone_enabled and not ALLOW_PROTECTED_CLONE
            clone_scope = "纯文本" if force_text_only else "内容"
            logger.warning(
                f"⚠️ Bot 原生转发受限，按配置启用受保护{clone_scope}克隆: "
                f"src={source_uid}"
            )
            forward_method = 'clone_protected_text' if force_text_only else 'clone_protected'
            sent_msg, send_bot_idx = await _clone_send_by_bot(
                event,
                src_buttons,
                allow_safe_media=False,
                force_text_only=force_text_only,
            )
            logger.info(
                f"✅ 受保护消息克隆完成: src={source_uid} "
                f"target={getattr(sent_msg, 'id', None)} bot={send_bot_idx}"
            )
        else:
            cache.log_intercept(text, "[drop] 源消息禁止转发", event.chat_id)
            raise RuntimeError("源消息禁止转发，已按安全策略跳过") from native_err
    except errors.FloodWaitError:
        raise
    except Exception as native_err:
        can_clone = ALLOW_BOT_CLONE_FALLBACK and (
            not is_restricted or ALLOW_PROTECTED_CLONE or can_clone_protected_text
        )
        can_user_native = ALLOW_USER_NATIVE_FALLBACK and not is_restricted
        if is_restricted and not (ALLOW_PROTECTED_CLONE or can_clone_protected_text):
            cache.log_intercept(text, "[drop] 受保护消息禁止克隆", event.chat_id)

        peer_unusable = _is_source_visibility_error(native_err)
        if peer_unusable and can_user_native:
            try:
                logger.warning(
                    "⚠️ Bot 原生转发不可用（peer/可见性），切换主账号原生兜底: "
                    f"{_format_rpc_error(native_err)}"
                )
                forward_method = 'user_native_peer'
                sent_msg = await _forward_native_by_user(event, acquire_rate_limit=False)
                send_bot_idx = None
                send_client = 'user'
            except errors.ChatForwardsRestrictedError as user_err:
                if ALLOW_BOT_CLONE_FALLBACK and (ALLOW_PROTECTED_CLONE or protected_text_clone_enabled):
                    is_restricted = True
                    can_clone = True
                    can_clone_protected_text = protected_text_clone_enabled
                    logger.warning("⚠️ 主账号原生转发受限，继续切换 Bot 纯文本克隆")
                else:
                    cache.log_intercept(text, "[drop] 源消息禁止转发", event.chat_id)
                    raise RuntimeError("源消息禁止转发，主账号原生兜底也被拒绝") from user_err
            except errors.FloodWaitError:
                raise
            except Exception as user_err:
                logger.warning(
                    "⚠️ 主账号原生兜底失败，继续检查 Bot 克隆兜底: "
                    f"{_format_rpc_error(user_err)}"
                )
        elif peer_unusable and not can_user_native:
            logger.warning(
                f"⚠️ 源消息对 Bot 不可见，主账号原生兜底未启用: "
                f"src={source_uid} reason={_format_rpc_error(native_err)}"
            )

        if not sent_msg and can_clone and peer_unusable:
            logger.warning(
                "⚠️ Bot 原生转发不可用（peer/可见性），自动切换克隆兜底: "
                f"{_format_rpc_error(native_err)}"
            )
            force_text_only = can_clone_protected_text and not ALLOW_PROTECTED_CLONE
            forward_method = 'clone_protected_text' if force_text_only else 'clone_peer'
            send_client = 'bot'
            sent_msg, send_bot_idx = await _clone_send_by_bot(
                event,
                src_buttons,
                allow_safe_media=True,
                force_text_only=force_text_only,
            )
            logger.info(
                f"✅ 源不可见消息克隆完成: src={source_uid} "
                f"target={getattr(sent_msg, 'id', None)} bot={send_bot_idx}"
            )
        elif not sent_msg and can_clone:
            logger.warning(f"⚠️ Bot 原生转发失败，切入 Bot 克隆兜底: {_format_rpc_error(native_err)}")
            force_text_only = can_clone_protected_text and not ALLOW_PROTECTED_CLONE
            forward_method = 'clone_protected_text' if force_text_only else 'clone_fallback'
            send_client = 'bot'
            sent_msg, send_bot_idx = await _clone_send_by_bot(
                event,
                src_buttons,
                allow_safe_media=True,
                force_text_only=force_text_only,
            )
            logger.info(
                f"✅ 转发失败后的克隆完成: src={source_uid} "
                f"target={getattr(sent_msg, 'id', None)} bot={send_bot_idx}"
            )
        elif sent_msg:
            pass
        elif _is_invalid_peer_error(native_err):
            raise RuntimeError(f"Bot 原生转发 Peer 无效: {_format_rpc_error(native_err)}") from native_err
        elif _is_source_visibility_error(native_err):
            raise RuntimeError(f"Bot 原生转发源不可见: {_format_rpc_error(native_err)}") from native_err
        else:
            raise

    if isinstance(sent_msg, list):
        sent_msg = sent_msg[0] if sent_msg else None
    sent_msg, send_bot_idx, send_client, forward_method = await _repair_missing_forward_media(
        event, sent_msg, send_bot_idx, send_client, forward_method, src_buttons
    )
    return sent_msg, send_bot_idx, send_client, forward_method


# ================= 转发工作器（Bot 原生优先 + 主账号原生兜底）=================
async def forward_worker():
    """优先由 forward_bot 原生转发；Bot 不可见源消息时可用主账号原生兜底。"""
    global FORWARDED_COUNT, FORWARDED_TODAY
    OWNER_ID = db.get_config('OWNER_ID')
    while True:
        item = await msg_queue.get()
        try:
            if len(item) == 7:
                event, text, dna_hash, is_lottery_dna, retry_count, queued_at, extra_dna_hashes = item
            elif len(item) == 6:
                event, text, dna_hash, is_lottery_dna, retry_count, queued_at = item
                extra_dna_hashes = []
            elif len(item) == 5:
                event, text, dna_hash, is_lottery_dna, retry_count = item
                queued_at = time.time()
                extra_dna_hashes = []
            elif len(item) == 4:
                event, text, dna_hash, retry_count = item
                is_lottery_dna = False
                queued_at = time.time()
                extra_dna_hashes = []
            else:
                event, text, dna_hash = item
                is_lottery_dna = False
                retry_count = 0
                extra_dna_hashes = []
                queued_at = time.time()
        except (TypeError, ValueError):
            logger.error(f"❌ 队列 item 格式异常: {type(item).__name__}，已跳过")
            msg_queue.task_done()
            continue

        src_uid = f"{event.chat_id}_{event.message.id}"
        success = False
        requeued = False
        release_claim = False
        # 主消息是否已实际发出。一旦发出，任何后续异常（如媒体补充/传送门触发 FloodWait）
        # 都不得再回队重试——否则重试会重新发送主消息，频道出现两条（V10.84 线上重复事故）。
        sent_msg = None

        if FORWARDING_PAUSED:
            cache.log_intercept(text, "[drop] 转发暂停", event.chat_id)
            db.release_forward_claim(event.chat_id, event.message.id)
            cache.forwarded_map.pop(src_uid, None)
            try:
                cache.forwarded_queue.remove(src_uid)
            except ValueError:
                pass
            if dna_hash:
                cache.remove_dna(dna_hash)
                for eh in (extra_dna_hashes or []):
                    cache.remove_dna(eh)
            msg_queue.task_done()
            await asyncio.sleep(1)
            continue

        try:
            if not TARGET_ID:
                release_claim = True
                raise RuntimeError("TARGET_ID 未配置")

            queue_rejection = _queue_delay_rejection(queued_at)
            if queue_rejection:
                release_claim = True
                cache.log_intercept(text, queue_rejection, event.chat_id)
                logger.info(f"⏭ 队列等待安全门丢弃: {src_uid} reason={queue_rejection}")
                continue

            safety_rejection = _forward_safety_rejection(event, text)
            if safety_rejection:
                release_claim = True
                cache.log_intercept(text, safety_rejection, event.chat_id)
                logger.info(f"⏭ 队列发送前安全门丢弃: {src_uid} reason={safety_rejection}")
                continue

            await rate_limiter.acquire()

            queue_rejection = _queue_delay_rejection(queued_at)
            safety_rejection = _forward_safety_rejection(event, text)
            if queue_rejection or safety_rejection:
                release_claim = True
                rejection = queue_rejection or safety_rejection
                cache.log_intercept(text, rejection, event.chat_id)
                logger.info(f"⏭ 限流等待后安全门丢弃: {src_uid} reason={rejection}")
                continue

            src_chat_id = str(event.chat_id).removeprefix('-100')
            sender_id_val = str(event.sender_id) if getattr(event, 'sender_id', None) else ""

            sent_msg, send_bot_idx, send_client, forward_method = await _dispatch_forward_event(event, text)

            # ═══════════════════════════════════════════════
            #  发送成功后处理
            # ═══════════════════════════════════════════════
            if sent_msg:
                success = True
                _clear_pending_duplicates(dna_hash)   # 首条成功 → 丢弃暂存副本（待定去重收尾）
                log_preview = ' '.join(str(text or '').split())[:80]
                logger.info(
                    f"✅ 转发成功: src={src_uid} target={sent_msg.id} "
                    f"method={forward_method} preview={log_preview}"
                )
                db.mark_forward_sent(event.chat_id, event.message.id, sent_msg.id)
                if dna_hash:
                    _persist_dna(dna_hash, is_lottery_dna)
                    for eh in (extra_dna_hashes or []):
                        _persist_dna(eh, True)

                rate_limiter.report_success()
                FORWARDED_COUNT += 1
                FORWARDED_TODAY += 1
                _record_activity('forward')

                cache.forwarded_map[src_uid] = {
                    'ts': time.time(),
                    'dna': dna_hash,
                    'target_msg_id': sent_msg.id,
                    'portal_msg_id': None,
                    'send_bot_idx': send_bot_idx,
                    'send_client': send_client,
                    'portal_bot_idx': None,
                }

                # 传送门链接（私聊/私密群组/测试流 → 纯文本降级）
                portal_msg = None
                portal_bot_idx = None
                if SEND_PORTAL_MESSAGE and FORWARD_MODE != "user_native_only":
                    try:
                        portal_msg, _, portal_bot_idx = await _send_portal_message(
                            event.chat_id, event.message.id, event.is_private, preferred_bot_idx=send_bot_idx
                        )
                        if portal_msg:
                            cache.forwarded_map[src_uid]['portal_msg_id'] = portal_msg.id
                            cache.forwarded_map[src_uid]['portal_bot_idx'] = portal_bot_idx
                    except Exception as e:
                        logger.warning(f"⚠️ 传送门发送失败: {_format_rpc_error(e)}")

                cache.log_forward({
                    'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'preview': text[:100],
                    'src_chat_id': src_chat_id,
                    'src_msg_id': event.message.id,
                    'target_msg_id': sent_msg.id,
                    'portal_msg_id': portal_msg.id if portal_msg else None,
                    'dna': dna_hash,
                    'sender_id': sender_id_val,
                    'send_bot_idx': send_bot_idx,
                    'send_client': send_client,
                    'portal_bot_idx': portal_bot_idx,
                })

            else:
                release_claim = True
                logger.error("❌ 未能发送任何内容")

        except errors.FloodWaitError as e:
            rate_limiter.report_flood(e.seconds)
            queue_rejection = _queue_delay_rejection(queued_at)
            if queue_rejection:
                release_claim = True
                cache.log_intercept(text, queue_rejection, event.chat_id)
                logger.warning(f"⏭ FloodWait 后消息不再回队: {src_uid} reason={queue_rejection}")
            elif retry_count < MAX_FORWARD_RETRIES and sent_msg is None:
                # 仅当「主消息尚未发出」时才回队重试；已发出的绝不重发（V10.84 防重复）。
                try:
                    msg_queue.put_nowait(
                        (event, text, dna_hash, is_lottery_dna, retry_count + 1, queued_at, extra_dna_hashes)
                    )
                    requeued = True
                    logger.warning(
                        f"⚠️ 触发限流 {e.seconds}s，消息已回队重试 "
                        f"({retry_count + 1}/{MAX_FORWARD_RETRIES}) queue_age={int(time.time() - queued_at)}s"
                    )
                except asyncio.QueueFull:
                    logger.error(f"❌ 触发限流 {e.seconds}s，队列已满，消息丢弃")
            else:
                release_claim = True
                logger.error(f"❌ 触发限流 {e.seconds}s，超过重试次数，消息丢弃")
            if not requeued:
                release_claim = True
            if OWNER_ID:
                try:
                    state = "已回队重试" if requeued else "已丢弃"
                    await admin_bot.send_message(int(OWNER_ID), f"🚨 **Bot 限流**\n`{e.seconds}`s\n消息{state}。")
                except Exception as alert_err:
                    logger.warning(f"⚠️ 限流告警发送失败: {_format_rpc_error(alert_err)}")
        except Exception as e:
            failure = _format_rpc_error(e)
            # 非 FloodWait 异常一律丢弃消息（只有限流才回队），因此必须释放占位和 DNA：
            # 否则该内容会在 DNA 生效期内无法再次转发（非确定性失败 → 永久漏转）。
            release_claim = True
            if _is_deterministic_delivery_error(e):
                logger.info(f"⏭ 确定性转发失败，释放占位和 DNA: {src_uid} {failure}")
            else:
                logger.info(f"⏭ 转发失败，释放占位和 DNA 待后续重试: {src_uid} {failure}")
            cache.log_intercept(
                text, f"[drop] 转发失败: {failure[:160]}", event.chat_id
            )
            logger.error(f"❌ 转发异常: src={src_uid} {failure}")
        finally:
            if release_claim and not success and not requeued:
                db.release_forward_claim(event.chat_id, event.message.id)
            if not success and not requeued:
                cache.forwarded_map.pop(src_uid, None)
                try:
                    cache.forwarded_queue.remove(src_uid)
                except ValueError:
                    pass
                if release_claim and dna_hash:
                    cache.remove_dna(dna_hash)                     # 释放去重键（允许补救）
                    for eh in (extra_dna_hashes or []):
                        cache.remove_dna(eh)
                    await _replay_pending_duplicate(dna_hash)      # 待定去重：重放一条暂存副本，立即重新占位 → 漏转被补、不连锁
            msg_queue.task_done()


# ================= 删除同步处理（联动删除目标频道消息）=================
@client.on(events.MessageDeleted)
async def delete_handler(event):
    """client 监听删除事件，按原发送方优先联动删除目标消息。"""
    for msg_id in event.deleted_ids:
        chat_id = getattr(event, 'chat_id', None)
        if chat_id:
            uid = f"{chat_id}_{msg_id}"
            data = cache.forwarded_map.pop(uid, None)
            if data is None:
                data = cache.get_forward_mapping(chat_id, msg_id)
                if data:
                    logger.info(f"📦 已从持久化转发日志恢复删除映射: 源 {msg_id}")
            if data:
                target_msg_id = data.get('target_msg_id')
                portal_msg_id = data.get('portal_msg_id')
                send_bot_idx = data.get('send_bot_idx')
                send_client = data.get('send_client')
                portal_bot_idx = data.get('portal_bot_idx')
                # V10.36 源消息被删除时**保留内容指纹**（不再 remove_dna / 删 history）：
                # 同一内容常在多个群流传，一个群删源消息不应给其它群的相同内容"解锁"，
                # 否则删完 76 秒内其它群的副本就会被当成新内容重复转发。
                # 代价：源删除后原样重发同一内容，在指纹有效期内（普通3天/抽奖10天）不会再次转发。
                try:
                    if target_msg_id:
                        await _delete_target_messages(
                            [target_msg_id],
                            preferred_bot_idx=send_bot_idx,
                            prefer_user=(send_client == 'user')
                        )
                    if portal_msg_id:
                        pref_idx = portal_bot_idx if portal_bot_idx is not None else send_bot_idx
                        await _delete_target_messages([portal_msg_id], preferred_bot_idx=pref_idx)
                    if target_msg_id or portal_msg_id:
                        logger.info(f"🗑️ 联动删除: 源 {msg_id} → 目标 {[x for x in (target_msg_id, portal_msg_id) if x]}")
                except Exception as e:
                    logger.warning(f"⚠️ 联动删除失败: {_format_rpc_error(e)}")
        else:
            # Telegram message IDs are only unique within one source chat.
            # Without the chat ID, guessing could delete an unrelated target post.
            logger.warning(
                "⚠️ 删除事件缺少源会话 ID，跳过联动删除: msg_id=%s", msg_id
            )


# ================= 核心消息处理 =================
_LAST_UPDATE_TS = time.monotonic()   # 最近一次收到监控群消息的时刻（推送僵死检测用）

# ── 按群静默检测（覆盖「账号不在群内 / 被踢」这类静默故障）──
# client_health_watchdog 判的是「全局」静默（所有群都没消息才告警），
# 发现不了「单个群不可读」：其它群照常有消息时它永不告警，只能靠人工发现漏转。
_CHAT_LAST_SEEN = {}          # chat_id -> (last_ts, title)
_CHAT_STALE_ALERTED = set()   # 已告警的群，避免反复打扰


def _note_chat_activity(chat_id, title=None):
    """记录某监控群最近一次收到消息（无论该消息最终是否被转发）。"""
    try:
        key = str(chat_id)
        _CHAT_LAST_SEEN[key] = (time.time(), title or key)
        _CHAT_STALE_ALERTED.discard(key)
    except Exception:
        pass


# ── 待定去重（借鉴 SlowLink `pending dedup`）──
# DNA 重复的副本不直接丢弃，先暂存；首条转发成功则丢弃，失败则释放去重并「重放」一条暂存副本补救。
# 对比旧「隔离区」：隔离区只拦不补（失败后副本仍丢），待定去重既补漏又不连锁。
_PENDING_DUPLICATES = {}   # dna_hash -> [(event, ts)]
_PENDING_DUP_MAX = 5       # 每个 DNA 最多暂存副本数（防内存膨胀）


def _remember_pending_duplicate(dna_hash, event):
    """DNA 重复：暂存该副本（首条若转发失败可用于补救），而非直接丢弃。"""
    if not dna_hash:
        return
    try:
        lst = _PENDING_DUPLICATES.setdefault(str(dna_hash), [])
        if len(lst) < _PENDING_DUP_MAX:
            lst.append((event, time.time()))
    except Exception:
        pass


def _clear_pending_duplicates(dna_hash):
    """首条转发成功 → 丢弃暂存的副本（正常去重生效）。"""
    if dna_hash:
        _PENDING_DUPLICATES.pop(str(dna_hash), None)


async def _replay_pending_duplicate(dna_hash):
    """首条转发失败 → 一次性重放一条暂存副本（重新走完整处理，会重新占位去重键）。
    重放后即清空该 DNA 的暂存；重放的副本若再判重复也不会产生新暂存 → 不会无限循环。"""
    lst = _PENDING_DUPLICATES.pop(str(dna_hash), None)
    if not lst:
        return False
    event = lst[0][0]
    try:
        await handler(event)
        logger.info(
            f"♻️ 待定去重：首条失败，已重放暂存副本 "
            f"chat={getattr(event, 'chat_id', None)} msg={getattr(event, 'id', None)}"
        )
        return True
    except Exception as e:
        logger.warning(f"⚠️ 待定去重重放失败: {_format_rpc_error(e)}")
        return False


async def _save_client_state_once():
    """把 Telethon 的 update 状态（pts/qts）落盘。

    借鉴 TelegramMonitor 的 `StatePath` + 定时 `SaveAllState`：状态不落盘时，进程异常退出后
    重连会**补收断线期间的全部历史消息**（本项目 2026-10-01 事故：9-30 的卡在 10-01 被补收并转发）。
    本函数是「事前根治」，与 V10.90 的「旧消息拦截」事后补救互补。
    """
    saver = getattr(client, "_save_states_and_entities", None)
    try:
        loop = getattr(client, "loop", None) or asyncio.get_event_loop()
        if callable(saver):
            await loop.run_in_executor(None, saver)
        else:
            await loop.run_in_executor(None, client.session.save)
    except Exception as e:
        logger.debug(f"保存 Telegram update 状态失败（可忽略）: {e}")


async def save_state_task():
    """定期保存 update 状态，缩短异常退出后的状态丢失窗口。"""
    while True:
        await asyncio.sleep(SAVE_STATE_INTERVAL_SECONDS)
        await _save_client_state_once()


async def chat_stale_watchdog():
    """按群检测静默：某群超过 CHAT_STALE_HOURS 小时无任何消息 → 告警给 OWNER。"""
    stale_hours = _env_int("CHAT_STALE_HOURS", 168, 2, 720)
    while True:
        await asyncio.sleep(1800)
        try:
            owner_id = db.get_config('OWNER_ID')
            if not owner_id or not _CHAT_LAST_SEEN:
                continue
            now = time.time()
            stale = [
                (key, title, now - ts)
                for key, (ts, title) in list(_CHAT_LAST_SEEN.items())
                if (now - ts) > stale_hours * 3600 and key not in _CHAT_STALE_ALERTED
            ]
            if not stale:
                continue
            listed = "\n".join(
                f"• {title}（静默 {sec / 3600:.0f} 小时）" for _, title, sec in stale
            )
            await send_owner_alert(
                f"⚠️ **监控群静默告警**\n以下群已超过 {stale_hours} 小时无消息，"
                f"可能：不在群内 / 被踢 / 群已停更\n{listed}"
            )
            for key, _, _ in stale:
                _CHAT_STALE_ALERTED.add(key)
            logger.warning(f"⚠️ 监控群静默告警已发送: {[t for _, t, _ in stale]}")
        except Exception as e:
            logger.error(f"❌ 监控群静默检测异常: {_format_rpc_error(e)}")

@client.on(events.NewMessage(func=lambda e: not e.message.edit_date))
@client.on(events.MessageEdited)
async def handler(event):
    global INTERCEPTED_TODAY, _LAST_UPDATE_TS
    _LAST_UPDATE_TS = time.monotonic()
    if not event.is_private:
        _note_chat_activity(event.chat_id, getattr(getattr(event, 'chat', None), 'title', None))
    text, markup = event.raw_text, event.message.reply_markup
    sender_id_str = str(event.sender_id) if getattr(event, 'sender_id', None) else ""
    OWNER_ID = db.get_config('OWNER_ID')
    is_candidate = _looks_like_forward_candidate(text)
    if is_candidate:
        media_type = type(getattr(event.message, 'media', None)).__name__ if getattr(event.message, 'media', None) else 'None'
        forwarded_time = getattr(getattr(event.message, 'fwd_from', None), 'date', None)
        logger.info(
            f"📨 候选消息进入监听: chat={event.chat_id} msg={event.id} "
            f"private={event.is_private} media={media_type} "
            f"noforwards={getattr(event.message, 'noforwards', False)} "
            f"date={getattr(event.message, 'date', None)} "
            f"edit_date={getattr(event.message, 'edit_date', None)} fwd_date={forwarded_time} "
            f"text_len={len(text or '')}"
        )

    # ══════════════════════════════════════
    #  管理员私聊转发测试（默认关闭）
    #  仅在 ENABLE_OWNER_PRIVATE_FORWARD_TEST=true 时，OWNER 私聊非命令文本才走正式转发管道
    # ══════════════════════════════════════
    is_admin_test = (
        ENABLE_OWNER_PRIVATE_FORWARD_TEST
        and
        event.is_private
        and OWNER_ID
        and sender_id_str == str(OWNER_ID)
        and text
        and not text.startswith('/')
    )

    if is_admin_test:
        logger.info(f"🧪 [管理员测试] 收到私聊测试消息: {text[:80]}...")

    # ══════════════════════════════════════
    #  特权发送者人工审核训练场
    #  TRUSTED_SENDER_ID 私聊 → 模拟打分 → 审核卡片
    # ══════════════════════════════════════
    is_trusted_review = (
        event.is_private
        and TRUSTED_SENDER_ID
        and sender_id_str == TRUSTED_SENDER_ID
        and text
        and not text.startswith('/')
        and not is_admin_test
    )

    if is_trusted_review:
        _prune_pending_reviews()
        code_match_t = config.get_pattern('CODE')
        code_m_t = _safe_regex_search(code_match_t, text)
        strict_code_t = config.get_pattern('STRICT_CODE')
        strict_codes_t = _safe_regex_findall(strict_code_t, text)
        allow_raw_single_code_t = (
            not getattr(event.message, 'edit_date', None)
            and _is_strict_single_registration_code(
                text, markup, getattr(event.message, 'media', None) is not None
            )
        )
        result_t = classify_intent(
            event, text, markup, False, code_m_t, len(strict_codes_t),
            sender_id_str, allow_raw_single_code_t
        )
        result_t = _apply_required_forward_format_gate(text, result_t, allow_raw_single_code_t)

        # 构建审核卡片
        verdict = "✅ 会通过" if result_t['pass'] else "🚫 会被拦截"
        reasons = result_t.get('detail', '-')
        domain = result_t.get('domain', '-')
        priority = result_t.get('priority', '-')

        # 生成唯一 review_id
        review_id = f"rv_{int(time.time() * 1000)}_{random.randint(100, 999)}"
        _pending_review[review_id] = {
            'event': event,
            'text': text[:500],
            'result': result_t,
            'ts': time.time(),
        }

        card = (
            f"📋 **人工审核训练场**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 **测试文本：**\n`{text[:300]}`\n\n"
            f"📊 **模拟打分：** `{verdict}`\n"
            f"📂 **领域：** `{domain}` | **优先级：** `{priority}`\n"
            f"🔍 **命中理由：** `{reasons}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        if result_t['pass']:
            buttons = [
                [Button.inline("✅ 确认转发", data=f"rv_ok_{review_id}".encode()),
                 Button.inline("🗑️ 丢弃", data=f"rv_no_{review_id}".encode())]
            ]
        else:
            buttons = [[Button.inline("🗑️ 丢弃", data=f"rv_no_{review_id}".encode())]]
        try:
            OWNER_ID_val = db.get_config('OWNER_ID')
            if OWNER_ID_val:
                await admin_bot.send_message(int(OWNER_ID_val), card, buttons=buttons)
        except Exception as e:
            logger.error(f"❌ 审核卡片发送失败: {e}")
        return

    # ── 私聊丢弃（仅高置信抽奖 Bot 卡片可进入队列）──
    allow_private_bot_lottery = await _is_private_bot_lottery(event, text)
    if event.is_private and not is_admin_test and not allow_private_bot_lottery:
        if is_candidate:
            cache.log_intercept(text, "[drop] 私聊消息", event.chat_id)
        return
    if allow_private_bot_lottery:
        logger.info(f"✅ 私聊抽奖 Bot 卡片放行: chat={event.chat_id} msg={event.id}")

    # ── 目标频道自身消息丢弃 ──
    if not is_admin_test and event.chat_id == TARGET_ID:
        if is_candidate:
            cache.log_intercept(text, "[drop] 目标频道自身消息", event.chat_id)
        return

    # ── 屏蔽群组丢弃 ──
    if not is_admin_test:
        event_chat_id_str = str(event.chat_id)
        if (event_chat_id_str in cache.muted_chats
                or event_chat_id_str.removeprefix('-100') in cache.muted_chats):
            if is_candidate:
                cache.log_intercept(text, "[drop] 屏蔽群组", event.chat_id)
            return

    # ── 最终发送安全门（任何真实转发入口都不能绕过）──
    safety_rejection = _forward_safety_rejection(event, text)
    if safety_rejection:
        if is_candidate:
            cache.log_intercept(text, safety_rejection, event.chat_id)
            logger.info(
                f"⏭ 候选消息安全门丢弃: chat={event.chat_id} msg={event.id} "
                f"reason={safety_rejection} date={getattr(event.message, 'date', None)} "
                f"edit_date={getattr(event.message, 'edit_date', None)} "
                f"fwd_date={getattr(getattr(event.message, 'fwd_from', None), 'date', None)}"
            )
        return

    # ── 无文本丢弃 ──
    if not text:
        logger.info(f"⏭ 无文本消息跳过: chat={event.chat_id} msg={event.id}")
        return

    # 定期检查配置热更新
    if random.random() < 0.01:
        if config.check_and_reload():
            logger.info("🔄 配置已自动热更新")

    # ══════════════════════════════════════
    #  过滤管道（L1/L2/L3）
    # ══════════════════════════════════════
    code_match = config.get_pattern('CODE')
    code_match = _safe_regex_search(code_match, text)
    strict_code = config.get_pattern('STRICT_CODE')
    strict_codes = _safe_regex_findall(strict_code, text)

    is_vip = sender_id_str in cache.vip_admins
    allow_raw_single_code = (
        not getattr(event.message, 'edit_date', None)
        and _is_strict_single_registration_code(
            text, markup, getattr(event.message, 'media', None) is not None
        )
    )

    result = classify_intent(
        event, text, markup, is_vip, code_match, len(strict_codes),
        sender_id_str, allow_raw_single_code
    )
    result = _apply_required_forward_format_gate(text, result, allow_raw_single_code)

    if not result['pass']:
        # Ordinary chat without any candidate signal has no audit value. Keep only
        # real rule hits and candidate misses so the DB/statistics stay actionable.
        should_audit = result['domain'] != 'none' or is_candidate
        if should_audit:
            INTERCEPTED_TODAY += 1
            reason = f"[{result['domain']}] {result['detail']}"
            cache.log_intercept(text, reason, event.chat_id)
        return

    # ── V11.09 假码护栏：只有占位/示例码的消息不得放行 ──
    # 线上实例：①「再来几个注册码 + base64 乱码 + 解密后长这样 某站点-30-Register_xxxxxxx」
    #           ②「喵~ 给你几个新的注册码喵~ / Register_1A2B3C / Register_4D5E6F」
    # 两者都因"形状像码"命中了格式，实际是示例/占位，进频道即垃圾。
    if _all_codes_are_fake(text):
        INTERCEPTED_TODAY += 1
        cache.log_intercept(text, "[drop] 仅含占位/示例码", event.chat_id)
        return

    # ── 结构化信号兜底：通过分类的消息必须至少包含一个真实信息格式 ──
    if not _has_required_forward_format(text, result, allow_raw_single_code):
        INTERCEPTED_TODAY += 1
        cache.log_intercept(text, "[drop] 未命中信息格式", event.chat_id)
        return

    # ── 掩码码去重（同码变体只发一次）──
    # 含 * / ? 的码：通配后匹配到「已转发的码」→ 拦；完整码：被「已转发的掩码码」匹配到 → 拦。
    gc_seg = _register_code_segment(text) if GUESS_CHAIN_DEDUP_ENABLED else ""
    if GUESS_CHAIN_DEDUP_ENABLED and gc_seg:
        now = time.time()
        if '*' in gc_seg or '?' in gc_seg:
            pat = _segment_as_mask_pattern(gc_seg)
            dup = any(exp > now and pat.fullmatch(s) for s, exp in _SEEN_FULL_SEGMENTS.items()) \
                or any(exp > now and pat.fullmatch(s) for s, exp in _SEEN_MASKED_SEGMENTS.items())
        else:
            dup = any(exp > now and _segment_as_mask_pattern(s).fullmatch(gc_seg)
                      for s, exp in _SEEN_MASKED_SEGMENTS.items())
        if dup:
            INTERCEPTED_TODAY += 1
            cache.log_intercept(text, "[drop] 掩码码·同码已转发", event.chat_id)
            return

    if is_candidate:
        logger.info(
            f"✅ 候选过滤通过: chat={event.chat_id} msg={event.id} "
            f"domain={result['domain']} priority={result['priority']}"
        )

    # 旧消息拦截（V10.90）：断线重连会**补收历史消息**（实例：9-30 10:44 发的卡，10-01 10:44 才收到并转发）。
    # 按**消息实际发送时间**（event.message.date，非入队时间）判断，超龄的补收消息不再转发。
    # 阈值可用 .env 的 MESSAGE_MAX_AGE_HOURS 调整（默认 12 小时）。
    _msg_date = getattr(event.message, 'date', None)
    if _msg_date is not None:
        try:
            _msg_age = time.time() - _msg_date.timestamp()
        except (AttributeError, ValueError, OSError, OverflowError):
            _msg_age = 0.0
        if _msg_age > _env_int("MESSAGE_MAX_AGE_HOURS", 12, 1, 720) * 3600:
            INTERCEPTED_TODAY += 1
            cache.log_intercept(
                text, f"[drop] 旧消息(补收 {int(_msg_age / 3600)}h)", event.chat_id
            )
            return

    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in cache.forwarded_map:
        if is_candidate:
            cache.log_intercept(text, "[drop] 消息ID重复", event.chat_id)
        return
    if cache.has_forwarded_source(event.chat_id, event.id):
        if is_candidate:
            cache.log_intercept(text, "[drop] 源消息已转发", event.chat_id)
        return

    dna_kind, dna_fingerprint = build_dna_fingerprint(text, markup)
    dna_hash = (
        hashlib.md5(
            (dna_kind + ":" + dna_fingerprint).encode(),
            usedforsecurity=False,
        ).hexdigest()
        if dna_fingerprint else ""
    )
    is_lottery_dna = dna_kind.startswith("LOTTERY_")
    # 「奖品|截止」跨模板身份：不限于 LOTTERY_* kind——GENERIC_V2 等泛化指纹的抽奖卡
    # （同一活动被不同机器人用不同模板转发）也必须注册，否则三张卡走三种键、去重失效（V10.65 线上重复事故）。
    extra_dna_hashes = _lottery_extra_identity_hashes(text) if (is_lottery_dna or '抽奖' in text) else []
    if is_candidate and dna_hash:
        logger.info(
            f"🧬 候选DNA: chat={event.chat_id} msg={event.id} "
            f"kind={dna_kind} hash={dna_hash[:10]} fp_len={len(dna_fingerprint)}"
            + (f" extra={len(extra_dna_hashes)}" if extra_dna_hashes else "")
        )

    if dna_hash:
        if not cache.add_dna(dna_hash, is_lottery=is_lottery_dna):
            # 待定去重：副本先暂存（首条若转发失败则重放补救），不直接丢弃
            _remember_pending_duplicate(dna_hash, event)
            if is_candidate:
                cache.log_intercept(
                    text,
                    f"[drop] DNA重复 {dna_hash[:10]} kind={dna_kind}",
                    event.chat_id
                )
            return
        # 跨模板归一（借鉴 SlowLink 多身份去重）：额外身份键任一已存在即判重复。
        # 官方卡（带口令）先到会同时注册「口令|截止」与「奖品|截止」两键，
        # 镜像卡（无口令）随后到达时其「奖品|截止」键命中 → 判重，不再重复转发。
        dup_extra = None
        for eh in extra_dna_hashes:
            if not cache.add_dna(eh, is_lottery=True):
                dup_extra = eh
                break
        if dup_extra is not None:
            cache.remove_dna(dna_hash)   # 回滚主键（本消息不转发）
            _remember_pending_duplicate(dna_hash, event)
            if is_candidate:
                cache.log_intercept(
                    text,
                    f"[drop] DNA重复(跨模板) {dup_extra[:10]} kind={dna_kind}",
                    event.chat_id
                )
            return

    if not db.claim_forward(event.chat_id, event.id, dna_hash):
        if dna_hash:
            cache.remove_dna(dna_hash)
            for eh in extra_dna_hashes:
                cache.remove_dna(eh)
        if is_candidate:
            cache.log_intercept(text, "[drop] 源消息已占位或写库失败", event.chat_id)
        return

    remember_forward(msg_id, {'ts': time.time(), 'dna': dna_hash})

    # 掩码码去重：转发成功后登记码段（完整码与掩码码分别入库，供后续掩码匹配）
    if GUESS_CHAIN_DEDUP_ENABLED and gc_seg:
        _remember_code_segment(gc_seg)

    try:
        queued_at = time.time()
        msg_queue.put_nowait((event, text, dna_hash, is_lottery_dna, 0, queued_at, extra_dna_hashes))
        logger.info(
            f"📥 候选入队: {msg_id} domain={result['domain']} "
            f"priority={result['priority']} qsize={msg_queue.qsize()} queued_at={int(queued_at)}"
        )
    except asyncio.QueueFull:
        db.release_forward_claim(event.chat_id, event.id)
        cache.forwarded_map.pop(msg_id, None)
        try:
            cache.forwarded_queue.remove(msg_id)
        except ValueError:
            pass
        if dna_hash:
            cache.remove_dna(dna_hash)
            for eh in extra_dna_hashes:
                cache.remove_dna(eh)
        cache.log_intercept(text, "[drop] 转发队列已满", event.chat_id)
        logger.warning(f"⚠️ 消息队列已满，已丢弃并回滚去重标记: {msg_id}")


# ================= 备份打包（共享） =================
def _build_backup_zip():
    """打包 DB + JSON 配置文件，返回 (zip_path, file_size_bytes)"""
    stamp = f"{int(time.time())}_{os.getpid()}_{time.time_ns()}"
    final_zip = MONITOR_DATA_DIR / f'radar_backup_{stamp}.zip'
    tmp_zip = MONITOR_DATA_DIR / f'.radar_backup_{stamp}.tmp'
    snapshot_db = MONITOR_DATA_DIR / f'.radar_backup_{stamp}.db'
    try:
        packed = []
        with zipfile.ZipFile(str(tmp_zip), 'w', zipfile.ZIP_DEFLATED) as zf:
            if DB_FILE.exists():
                src_conn = sqlite3.connect(str(DB_FILE), timeout=10)
                try:
                    dst_conn = sqlite3.connect(str(snapshot_db))
                    try:
                        src_conn.backup(dst_conn)
                        # Backups leave the host; retain audit IDs but omit message bodies.
                        for statement in (
                            "UPDATE intercept_log SET content = ''",
                            "UPDATE forward_log SET preview = ''",
                        ):
                            try:
                                dst_conn.execute(statement)
                            except sqlite3.OperationalError:
                                pass
                        dst_conn.commit()
                    finally:
                        dst_conn.close()
                finally:
                    src_conn.close()
                zf.write(str(snapshot_db), DB_FILE.name)
                packed.append('DB')
            else:
                logger.warning(f"⚠️ 备份跳过：数据库不存在 {DB_FILE}")
            for cf in [REGEX_CONFIG_FILE, CUSTOM_FORMAT_CONFIG_FILE, DEVICE_CONFIG_FILE]:
                if cf.exists():
                    zf.write(str(cf), f"config/{cf.name}")
                    packed.append(cf.name)
        snapshot_db.unlink(missing_ok=True)
        size_bytes = tmp_zip.stat().st_size
        if size_bytes < 22:  # 空 zip = 22 bytes
            raise FileNotFoundError(f"备份包为空，所有源文件均不存在: DB={DB_FILE.exists()}")
        os.replace(str(tmp_zip), str(final_zip))
        if os.name == 'posix':
            os.chmod(final_zip, 0o600)
        logger.info(f"📦 备份打包完成: {', '.join(packed)} → {_format_size(size_bytes)}")
        return final_zip, size_bytes
    except Exception:
        snapshot_db.unlink(missing_ok=True)
        tmp_zip.unlink(missing_ok=True)
        raise


def _format_size(size_bytes):
    """格式化文件大小"""
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


# ================= 后台任务监督 =================
async def _supervise_background(name, task_func):
    """Keep a failed long-running task from silently disappearing."""
    while True:
        try:
            await task_func()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"❌ 后台任务异常退出: {name}，5s 后重启")
            await asyncio.sleep(5)
        else:
            logger.error(f"⚠️ 后台任务意外结束: {name}，5s 后重启")
            await asyncio.sleep(5)


# ================= 自动备份任务 =================
async def auto_backup_task():
    while True:
        await asyncio.sleep(12 * 3600)  # 每 12 小时
        try:
            if not BACKUP_ID:
                continue

            tmp_zip, size_bytes = await admin_bot.loop.run_in_executor(
                None, _build_backup_zip
            )
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            caption = (
                f"⏱️ **【系统自动备份】**\n"
                f"⏱ `{now_str}`\n"
                f"📦 {_format_size(size_bytes)} | DB + JSON 配置"
            )
            send_target = int(BACKUP_ID)
            sent_ok = False
            try:
                await admin_bot.get_entity(send_target)
                await admin_bot.send_file(send_target, str(tmp_zip), caption=caption)
                sent_ok = True
                logger.info(f"✅ 自动备份已发送至 {send_target}")
            except Exception as e:
                # Entity 缓存丢失 → 兜底发送至 OWNER_ID 私聊
                owner_id = db.get_config('OWNER_ID')
                if owner_id:
                    send_target = int(owner_id)
                    fallback_caption = caption + (
                        f"\n\n⚠️ **警告：无法识别备份频道 `{BACKUP_ID}` 实体，"
                        f"已回退发送至私聊。**\n"
                        f"请确认 Bot 是否已加入该频道并设为管理员。"
                    )
                    try:
                        await admin_bot.get_entity(send_target)
                        await admin_bot.send_file(send_target, str(tmp_zip), caption=fallback_caption)
                        sent_ok = True
                        logger.warning(f"⚠️ 自动备份已回退至 OWNER_ID={send_target}")
                    except Exception as e2:
                        logger.error(f"❌ 自动备份回退也失败: {e2}")
                else:
                    logger.error(f"❌ 自动备份失败且无 OWNER_ID 兜底: {e}")
            if not sent_ok:
                logger.warning("⚠️ 自动备份未成功发送")
            try:
                tmp_zip.unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.warning(f"⚠️ 清理自动备份临时文件失败: {cleanup_err}")
            gc.collect()
        except Exception as e:
            logger.error(f"❌ 自动备份失败: {e}")


# ================= 自动清理任务 =================
async def auto_cleanup_task():
    """定期清理数据库、日志和临时文件"""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        try:
            db.cleanup_old_data()
            cache.refresh_dna_history()

            # 检查日志文件大小
            if LOG_FILE.exists():
                log_size = LOG_FILE.stat().st_size / (1024 * 1024)
                if log_size > MAX_LOG_SIZE_MB:
                    logger.warning(f"⚠️ 日志文件过大: {log_size:.1f}MB")

            # 清理残留备份文件；只删超过 1 小时的旧文件，避免误删正在发送的备份。
            cutoff = time.time() - 3600
            for _pattern in ('backup.zip', 'backup_*.zip', 'radar_backup_*.zip', '.radar_backup_*.tmp', '.radar_backup_*.db'):
                for _bf in MONITOR_DATA_DIR.glob(_pattern):
                    try:
                        if _bf.stat().st_mtime < cutoff:
                            _bf.unlink()
                            logger.info(f"🧹 清理残留 {_bf.name}")
                    except OSError:
                        pass
            for _pycache in BASE_DIR.rglob('__pycache__'):
                try:
                    shutil.rmtree(str(_pycache))
                except OSError:
                    pass

            gc.collect()
            logger.info("🧹 定期清理完成")
        except Exception as e:
            logger.error(f"❌ 清理任务异常: {e}")


# ================= 每周 VACUUM 任务 =================
async def weekly_vacuum_task():
    """每周自动 VACUUM SQLite，回收冗余空间"""
    while True:
        await asyncio.sleep(7 * 86400)  # 每 7 天执行一次
        try:
            db_size_mb = DB_FILE.stat().st_size / (1024 * 1024)
            logger.info(f"🗄️ VACUUM 前数据库大小: {db_size_mb:.1f}MB")
            db.cursor.execute("VACUUM")
            db.conn.commit()
            db_size_mb = DB_FILE.stat().st_size / (1024 * 1024)
            logger.info(f"🗄️ VACUUM 完成，数据库大小: {db_size_mb:.1f}MB")
        except Exception as e:
            logger.error(f"❌ VACUUM 异常: {e}")


# ================= 内存监控任务 =================
async def memory_watchdog():
    """内存监控 - 接近上限时主动GC"""
    proc = psutil.Process(os.getpid())
    while True:
        await asyncio.sleep(60)
        try:
            mb = proc.memory_info().rss / (1024 * 1024)
            if mb > MAX_RAM_MB * 0.85:
                logger.warning(f"⚠️ 内存接近上限: {mb:.1f}MB，触发GC")
                gc.collect()
        except Exception as e:
            logger.warning(f"⚠️ 内存监控异常: {e}")


async def cpu_watchdog():
    """CPU 保护：**本进程**持续高负载时记录诊断并自愈重启。

    1. 采样对象是「本进程 CPU」而非「整机 CPU」——共享 VPS 上整机值会被邻居租户
       的负载污染，只关心自己的进程是否真的失控。
    2. 进程启动后前 GRACE 秒不计数（默认 10 分钟）——启动阶段登录账号并同步全部
       dialogs 本身就会吃满 CPU，避免「重启 → 启动高负载 → 再次凑满判定 → 再重启」死循环。
    """
    interval = _env_int("CPU_WATCHDOG_INTERVAL", 60, 10, 600)
    threshold = _env_float("CPU_WATCHDOG_THRESHOLD", 98.0, 50.0, 100.0)
    strikes = _env_int("CPU_WATCHDOG_STRIKES", 30, 1, 60)
    grace = _env_int("CPU_WATCHDOG_GRACE_SECONDS", 600, 0, 3600)
    stall_window = _env_int("CPU_WATCHDOG_STALL_SECONDS", 1800, 60, 7200)
    proc = psutil.Process(os.getpid())
    hit = 0
    # 预热：psutil 首次调用无基线，返回 0
    try:
        await asyncio.get_event_loop().run_in_executor(None, proc.cpu_percent, 1)
    except Exception:
        pass
    # 启动冷静期：重启后前 N 秒不判定（登录 + 同步 dialogs 的正常启动开销）
    if grace > 0:
        logger.info(f"🧊 CPU 看门狗进入 {grace}s 启动冷静期（本进程 CPU 暂不计数）")
        await asyncio.sleep(grace)
    while True:
        await asyncio.sleep(interval)
        try:
            cpu = await asyncio.get_event_loop().run_in_executor(None, proc.cpu_percent, 5)
            if cpu >= threshold:
                hit += 1
                logger.warning(f"⚠️ 本进程 CPU 5秒均值持续偏高: {cpu:.1f}%（连续 {hit}/{strikes} 次）")
                if hit >= strikes:
                    # 业务停滞判据：CPU 高**且**长时间没有转发/拦截记录 = 真失控（死循环）；
                    # 反之说明是业务高峰在正常干活（大量消息涌入时 CPU 本就吃满），只记日志、不重启。
                    snapshot = _last_activity_snapshot()
                    last_activity = snapshot['last_activity']
                    idle_for = (time.time() - last_activity) if last_activity else None
                    stalled = (idle_for is None) or (idle_for >= stall_window)
                    if not stalled:
                        hit = 0
                        logger.warning(
                            f"⚠️ 本进程 CPU 持续 {cpu:.1f}% 但业务正常推进"
                            f"（{_format_age(last_activity)}有转发/拦截），判定为业务高峰，不重启"
                        )
                        continue
                    proc_mb = proc.memory_info().rss / (1024 * 1024)
                    logger.error(f"❌ 本进程 CPU 长时间高负载({cpu:.1f}%)且业务停滞，触发自愈重启")
                    await send_owner_alert(
                        f"🚨 **本进程 CPU 长时间高负载且业务停滞**\n\n"
                        f"本进程 CPU 5秒均值：`{cpu:.1f}%`（连续 {strikes} 次 ≥ {threshold:.0f}%）\n"
                        f"业务活动：`{_format_age(last_activity)}`有转发/拦截"
                        f"（超过 {stall_window // 60} 分钟无活动）\n"
                        f"内存：`{proc_mb:.0f}MB`\n"
                        f"活跃任务：`{len(asyncio.all_tasks())}`\n\n"
                        f"已自动重启，请观察重启后是否再次出现。"
                    )
                    for c in [client, admin_bot, *forward_bots]:
                        try:
                            await c.disconnect()
                        except Exception:
                            pass
                    db.close()
                    os.execl(sys.executable, sys.executable, os.path.abspath(__file__))
            else:
                hit = 0
        except Exception as e:
            logger.warning(f"⚠️ CPU 监控异常: {e}")


# ================= 活跃心跳监控 =================
async def heartbeat_watchdog():
    """按持久化最近活动时间告警，避免重启/跨天导致内存计数清零误报。"""
    global FORWARDED_TODAY, INTERCEPTED_TODAY, LAST_DAY_RESET
    idle_hours = max(48, _env_int("HEARTBEAT_IDLE_HOURS", 48, 1, 168))
    last_alert_ts = 0
    while True:
        await asyncio.sleep(3600)  # 每小时检查一次
        try:
            # 每日零点重置计数器
            today = time.strftime('%Y-%m-%d')
            if today != LAST_DAY_RESET:
                FORWARDED_TODAY = 0
                INTERCEPTED_TODAY = 0
                LAST_DAY_RESET = today

            # 检查是否长时间无活动（启动后至少 1 小时才开始检测）
            uptime_hours = (time.monotonic() - START_TIME) / 3600
            snapshot = _last_activity_snapshot()
            now = time.time()
            last_activity = snapshot['last_activity']
            has_recent_activity = bool(last_activity and now - last_activity <= idle_hours * 3600)
            if uptime_hours > 1 and not has_recent_activity:
                if now - last_alert_ts > 86400:  # 每天最多告警一次
                    last_alert_ts = now
                    last_forward_text = _format_age(snapshot['last_forward'], now)
                    last_intercept_text = _format_age(snapshot['last_intercept'], now)
                    if last_activity:
                        window_line = f"最近 `{idle_hours}` 小时内未记录到转发或拦截。"
                    else:
                        window_line = "当前数据库还没有转发或拦截活动记录。"
                    await send_owner_alert(
                        "⚠️ **活跃心跳异常**\n\n"
                        f"{window_line}\n\n"
                        f"最近活动：\n"
                        f"  ✅ 最近转发：`{last_forward_text}`\n"
                        f"  🚫 最近拦截：`{last_intercept_text}`\n\n"
                        f"可能原因：\n"
                        f"  1. 所有监控群组静默\n"
                        f"  2. 过滤规则过严导致全量拦截\n"
                        f"  3. 系统逻辑异常\n\n"
                        f"请发送 `/status` 检查系统状态。"
                    )
                    logger.warning(f"⚠️ 活跃心跳异常：最近 {idle_hours} 小时无持久化活动记录")
        except Exception as e:
            logger.warning(f"⚠️ 活跃心跳检查异常: {_format_rpc_error(e)}")


# ================= 设备指纹轮换任务 =================
async def auto_device_rotation():
    """双机随机轮换：基础周期15天 + 随机抖动±5天，需 /restart 重启生效"""
    BASE_DAYS = _env_int("ROTATION_BASE_DAYS", 15, 1, 365)
    JITTER_MIN = _env_int("ROTATION_JITTER_MIN", -5, -30, 30)
    JITTER_MAX = _env_int("ROTATION_JITTER_MAX", 5, -30, 30)
    if JITTER_MIN > JITTER_MAX:
        logger.warning(
            f"⚠️ 设备轮换抖动范围反向，已自动纠正: {JITTER_MIN}..{JITTER_MAX}"
        )
        JITTER_MIN, JITTER_MAX = JITTER_MAX, JITTER_MIN

    # 首次启动：如果没有下次轮换日期，计算一个并写入 DB
    next_rotation = db.get_config('NEXT_DEVICE_ROTATION')
    if not next_rotation:
        jitter = random.randint(JITTER_MIN, JITTER_MAX)
        next_ts = int(time.time()) + max(1, BASE_DAYS + jitter) * 86400
        db.set_config('NEXT_DEVICE_ROTATION', str(next_ts))
        next_rotation = next_ts
        logger.info(f"📅 首次设备轮换时间已计算: {time.strftime('%Y-%m-%d', time.localtime(next_ts))}")

    while True:
        await asyncio.sleep(3600)  # 每小时检查一次
        try:
            now = int(time.time())
            if now < int(next_rotation):
                continue

            # 到达轮换点
            profiles = list(config.device_config.get('profiles', {}).keys())
            if not profiles:
                continue
            active = config.device_config.get('active_profile', profiles[0])
            idx = (profiles.index(active) + 1) % len(profiles) if active in profiles else 0
            new_profile = profiles[idx]
            config.device_config['active_profile'] = new_profile
            tmp_path = str(DEVICE_CONFIG_FILE) + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(config.device_config, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(DEVICE_CONFIG_FILE))
            _secure_runtime_permissions()

            # 轮换时强制刷新版本号（确保新设备用最新版本）
            try:
                android_ver, desktop_ver = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_latest_tg_versions
                )
                if android_ver or desktop_ver:
                    changed = _update_device_versions(android_ver, desktop_ver, source="auto")
                    if changed:
                        logger.info("🔄 轮换后版本号已写入，重启后生效")
                    else:
                        logger.info("🔄 轮换后版本号已检查，无需更新")
                else:
                    logger.warning("⚠️ 轮换后未获取到版本号，保留当前配置")
            except Exception as e:
                logger.warning(f"⚠️ 轮换后版本刷新失败: {e}")

            # 计算下次轮换日期
            jitter = random.randint(JITTER_MIN, JITTER_MAX)
            next_ts = now + max(1, BASE_DAYS + jitter) * 86400
            db.set_config('NEXT_DEVICE_ROTATION', str(next_ts))
            next_rotation = next_ts

            # 获取中文名
            old_name = config.device_config.get('profiles', {}).get(active, {}).get('description', active)
            new_name = config.device_config.get('profiles', {}).get(new_profile, {}).get('description', new_profile)
            next_date = time.strftime('%Y-%m-%d', time.localtime(next_ts))

            logger.info(f"🔄 设备指纹已轮换: {active} → {new_profile}")
            await send_owner_alert(
                f"🔄 **检测到模拟环境已到达随机轮换点**\n\n"
                f"当前：{old_name} → {new_name}\n"
                f"下次轮换：`{next_date}`\n\n"
                f"版本号已自动刷新，请执行 `/restart` 重启生效。"
            )
        except Exception as e:
            logger.error(f"❌ 设备轮换异常: {e}")


# ================= 灾难自愈 =================
async def send_owner_alert(message):
    """通过 Bot 向 owner 发送紧急告警（自动附加服务器标识）"""
    try:
        header = f"🖥 `{NODE_NAME}`\n" if NODE_NAME != "TG-Monitor" else ""
        OWNER_ID = db.get_config('OWNER_ID')
        if not OWNER_ID:
            logger.error("❌ OWNER_ID 未配置，紧急告警无法发送")
            return False
        try:
            target = int(OWNER_ID)
        except (TypeError, ValueError):
            logger.error(f"❌ OWNER_ID 格式无效，紧急告警无法发送: {OWNER_ID}")
            return False
        try:
            await admin_bot.get_entity(target)
        except Exception:
            pass
        await admin_bot.send_message(target, header + message)
        return True
    except Exception as e:
        logger.error(f"❌ 告警发送失败: {e}")
        return False


async def _ensure_bot_connected(bot_client, token, label):
    """检查 Bot 连接状态，断线时用 token 做一次轻量重连。"""
    try:
        if not bot_client.is_connected():
            logger.warning(f"⚠️ {label} 连接断开，尝试重连...")
            await bot_client.connect()
        await bot_client.get_me()
        return True
    except (errors.AuthKeyUnregisteredError, errors.UnauthorizedError) as e:
        logger.warning(f"⚠️ {label} 授权失效，尝试 token 重登: {_format_rpc_error(e)}")
    except Exception as e:
        logger.warning(f"⚠️ {label} 健康检查失败，尝试重启连接: {_format_rpc_error(e)}")

    try:
        try:
            await bot_client.disconnect()
        except Exception:
            pass
        await bot_client.start(bot_token=token)
        await bot_client.get_me()
        logger.info(f"✅ {label} 已恢复连接")
        return True
    except Exception as e:
        logger.error(f"❌ {label} 恢复失败: {_format_rpc_error(e)}")
        return False


async def _start_telegram_client(client_obj, label, *, bot_token=None):
    """统一启动 TelegramClient，避免启动阶段无限卡死。"""
    last_err = None
    for attempt in range(1, TG_START_RETRIES + 1):
        try:
            if client_obj.is_connected():
                await client_obj.disconnect()
            start_coro = client_obj.start(bot_token=bot_token) if bot_token else client_obj.start()
            await asyncio.wait_for(start_coro, timeout=TG_STARTUP_TIMEOUT)
            logger.info(f"✅ {label} 已连接: is_connected={client_obj.is_connected()}")
            return True
        except (errors.AuthKeyUnregisteredError, errors.UnauthorizedError):
            raise
        except asyncio.TimeoutError as e:
            last_err = e
            logger.warning(
                f"⚠️ {label} 启动超时({attempt}/{TG_START_RETRIES})，"
                f"{TG_STARTUP_TIMEOUT}s 内未完成握手。"
            )
        except Exception as e:
            last_err = e
            logger.warning(f"⚠️ {label} 启动失败({attempt}/{TG_START_RETRIES}): {_format_rpc_error(e)}")

        try:
            await client_obj.disconnect()
        except Exception:
            pass
        await asyncio.sleep(min(3 * attempt, 10))

    if last_err:
        raise last_err
    raise RuntimeError(f"{label} 启动失败")


async def _send_startup_notice():
    """发送上线通知；失败必须落日志，避免服务活着但 Bot 无响应时无从排查。"""
    owner_id = db.get_config('OWNER_ID')
    if not owner_id:
        logger.warning("⚠️ OWNER_ID 未配置，跳过上线通知")
        return False

    try:
        target = int(owner_id)
    except (TypeError, ValueError):
        logger.error(f"❌ OWNER_ID 格式无效，无法发送上线通知: {owner_id}")
        return False

    await _ensure_bot_connected(admin_bot, ADMIN_BOT_TOKEN, "管理 Bot")
    for attempt in range(1, 4):
        try:
            try:
                await admin_bot.get_entity(target)
            except Exception:
                pass
            await admin_bot.send_message(
                target,
                "🟢 雷达系统已上线！\n发送 /start 打开控制面板。"
            )
            logger.info("✅ 上线通知已发送")
            return True
        except Exception as e:
            logger.warning(f"⚠️ 上线通知发送失败({attempt}/3): {_format_rpc_error(e)}")
            await asyncio.sleep(2 * attempt)
    return False


async def bot_health_watchdog():
    """Bot 健康看护：管理 Bot 和转发 Bot 池断线后自动恢复。"""
    while True:
        await asyncio.sleep(60)
        try:
            await _ensure_bot_connected(admin_bot, ADMIN_BOT_TOKEN, "管理 Bot")
            for idx, (bot_client, token) in enumerate(zip(forward_bots, forward_bot_tokens_active, strict=True), start=1):
                await _ensure_bot_connected(bot_client, token, f"转发 Bot#{idx}")
        except Exception as e:
            logger.error(f"❌ Bot 健康看护异常: {_format_rpc_error(e)}")


async def client_health_watchdog():
    """主账号健康看护 - 每 60 秒检测连接与 Auth 状态"""
    global _LAST_UPDATE_TS
    stale_threshold = _env_int("UPDATE_STALENESS_HOURS", 4, 1, 72) * 3600
    # Ping 存活但持续静默的兜底——连续 N 轮 Ping 通仍完全无消息，
    # 仍强制重连一次（防 updates 流单独僵死、RPC 仍正常的罕见情况导致永久漏转）
    alive_passes = 0
    alive_passes_limit = 3
    while True:
        await asyncio.sleep(60)
        try:
            # 推送僵死检测（借鉴 SlowLink）：TCP 活着但更新流死了的情况，
            # is_connected() 查不出来；用「多久没收到监控群消息」兜底。
            stale_sec = time.monotonic() - _LAST_UPDATE_TS
            if stale_sec > stale_threshold and client.is_connected():
                # 群静默 ≠ 推送流僵死。先发一次轻量 Ping 验证连接层是否存活：
                # 通 → 只是群里没消息，重置计时器、**不重连**（避免无效重连的风控隐患）；
                # 不通 → 才执行轻量重连。
                ping_alive = False
                try:
                    await asyncio.wait_for(
                        client(PingRequest(ping_id=random.randint(1, 2**31 - 1))),
                        timeout=15,
                    )
                    ping_alive = True
                except Exception as ping_err:
                    logger.warning(f"⚠️ 推送流存活探测失败: {_format_rpc_error(ping_err)}")

                if ping_alive:
                    alive_passes += 1
                    _LAST_UPDATE_TS = time.monotonic()
                else:
                    alive_passes = 0

                if ping_alive and alive_passes < alive_passes_limit:
                    logger.info(
                        f"ℹ️ 已 {stale_sec / 3600:.1f} 小时未收到监控群消息，"
                        f"但 Ping 存活（群静默，非僵死），不重连"
                        f"（静默轮次 {alive_passes}/{alive_passes_limit}）"
                    )
                    continue

                if ping_alive:
                    # 兜底：Ping 一直通但连续多轮完全无消息，防「updates 流单独僵死、RPC 仍正常」
                    # 的罕见情况导致永久漏转 —— 此时强制重连一次（重连会触发离线 catch-up）
                    logger.warning(
                        f"⚠️ 连续 {alive_passes} 轮静默（约 {alive_passes * stale_threshold / 3600:.0f} 小时）"
                        f"但 Ping 均存活，执行一次兜底重连（防 updates 流单独僵死导致永久漏转）"
                    )
                    alive_passes = 0
                else:
                    logger.warning(
                        f"⚠️ 已 {stale_sec / 3600:.1f} 小时未收到任何监控群消息且 Ping 失败，"
                        f"推送流僵死，执行轻量重连"
                    )
                await client.disconnect()
                await asyncio.sleep(1)
                await client.connect()
                _LAST_UPDATE_TS = time.monotonic()
                logger.info("✅ 轻量重连完成，观察消息是否恢复")
                continue
            if not client.is_connected():
                logger.warning("⚠️ 主账号连接断开，尝试重连...")
                try:
                    await client.connect()
                except errors.AuthKeyUnregisteredError:
                    await send_owner_alert(
                        "🚨 **运行时 Session 失效！** `[AuthKeyUnregistered]`\n"
                        "主账号已被 Telegram 踢下线，系统进入休眠。\n"
                        "Bot 控制台仍在线，请删除 `/app/tg_monitor_data/sessions/purifier_session.session` 后重新登录。"
                    )
                    logger.error("❌ 运行时 AuthKey 失效，停止重连")
                    await _sleep_forever()
                except errors.UnauthorizedError:
                    await send_owner_alert(
                        "🚨 **运行时授权失败！** `[Unauthorized]`\n"
                        "主账号授权被撤销，系统进入休眠。"
                    )
                    logger.error("❌ 运行时 Unauthorized，停止重连")
                    await _sleep_forever()

                if not client.is_connected():
                    await send_owner_alert(
                        "🚨 **主账号连接丢失！**\n"
                        "系统已进入休眠，Bot 控制台仍在线。\n"
                        "请检查主账号状态或重新登录。"
                    )
                    logger.error("❌ 主账号重连失败，进入休眠")
                    while not client.is_connected():
                        await asyncio.sleep(300)
                        try:
                            await client.connect()
                        except (errors.AuthKeyUnregisteredError, errors.UnauthorizedError):
                            await send_owner_alert("🚨 **休眠期间 Session 失效！** Bot 仍在线。")
                            await _sleep_forever()
                        except Exception as retry_err:
                            logger.warning(f"⚠️ 主账号休眠重连失败: {_format_rpc_error(retry_err)}")
                    if client.is_connected():
                        await send_owner_alert("🟢 **主账号已恢复连接！**")
                        logger.info("✅ 主账号重连成功")
        except Exception as e:
            logger.error(f"❌ 健康看护异常: {e}")


# ================= 主程序 =================
async def main():
    global _forward_rr_index
    faulthandler.enable()   # 任何段错误/卡死时可触发全线程栈转储（/stack 也可手动触发）
    logger.info("🌍 正在伪装成设备启动探针...")
    logger.info(f"📱 设备: {device_profile.get('device_model', 'Unknown')}")

    logger.info("🤖 正在唤醒管理 Bot...")
    try:
        await _start_telegram_client(admin_bot, "管理 Bot", bot_token=ADMIN_BOT_TOKEN)
    except Exception as e:
        logger.error(f"❌ 管理 Bot 启动失败: {_format_rpc_error(e)}")
        raise

    # 主账号启动（带 Auth 异常捕获）
    try:
        await _start_telegram_client(client, "主账号")
    except errors.AuthKeyUnregisteredError:
        logger.error("❌ 主账号 AuthKey 已失效（Session 被踢）")
        await send_owner_alert(
            "🚨 **主账号已掉线！** `[AuthKeyUnregistered]`\n"
            "Session 已被 Telegram 服务器注销。\n"
            "系统已停止主号轮询，Bot 控制台仍在线。\n\n"
            "👉 请删除 `/app/tg_monitor_data/sessions/purifier_session.session` 后重新登录。"
        )
        logger.info("🤖 Bot 控制台继续在线，等待人工介入")
        await admin_bot.run_until_disconnected()
        return
    except errors.UnauthorizedError:
        logger.error("❌ 主账号授权失败（密码错误或被封）")
        await send_owner_alert(
            "🚨 **主账号授权失败！** `[Unauthorized]`\n"
            "可能原因：密码错误 / 账号被封 / 两步验证问题。\n"
            "系统已停止主号轮询，Bot 控制台仍在线。"
        )
        await admin_bot.run_until_disconnected()
        return

    if FORWARD_MODE == "user_native_only":
        forward_bots[:] = []
        forward_bot_tokens_active[:] = []
        _forward_rr_index = -1
        logger.info("🔒 主账号原生专用模式：跳过启动转发 Bot 池和 Bot 克隆链路")
    else:
        logger.info(f"🤖 正在唤醒转发 Bot 池... 共 {len(FORWARD_BOT_TOKENS)} 个")
        started_forward_bots = []
        started_forward_tokens = []
        for idx, (bot_client, token) in enumerate(zip(forward_bots, FORWARD_BOT_TOKENS, strict=True), start=1):
            try:
                await _start_telegram_client(bot_client, f"转发 Bot#{idx}", bot_token=token)
                started_forward_bots.append(bot_client)
                started_forward_tokens.append(token)
            except Exception as e:
                logger.error(f"❌ 转发 Bot#{idx} 启动失败: {_format_rpc_error(e)}")

        if not started_forward_bots:
            await send_owner_alert("🚨 **所有转发 Bot 启动失败**，系统仅保留管理面板在线。")
            await admin_bot.run_until_disconnected()
            return

        forward_bots[:] = started_forward_bots
        forward_bot_tokens_active[:] = started_forward_tokens
        _forward_rr_index = -1
        logger.info(f"✅ 转发 Bot 池就绪: {len(forward_bots)} 个可用")
    await _precheck_targets()

    # 注册 Bot 指令菜单（用户输入 / 时左下角弹出）
    try:
        from telethon.tl.functions.bots import SetBotCommandsRequest
        from telethon.tl.types import BotCommand, BotCommandScopeDefault
        await admin_bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code='',
            commands=[
                BotCommand("start", "控制面板"),
                BotCommand("status", "系统诊断"),
                BotCommand("list", "黑白名单"),
                BotCommand("log", "拦截日志"),
                BotCommand("test", "过滤试运行"),
                BotCommand("ping", "延迟探测"),
                BotCommand("add", "白名单授权"),
                BotCommand("ban", "添加黑名单"),
                BotCommand("unban", "删除黑名单"),
                BotCommand("stats", "数据统计"),
                BotCommand("help", "全部指令"),
            ]
        ))
        logger.info("✅ Bot 原生菜单指令注册成功")
    except Exception as e:
        logger.warning(f"⚠️ Bot 指令菜单注册失败: {e}")

    await _send_startup_notice()

    logger.info(f"🚀 V{APP_VERSION} 代码与数据分离架构 启动成功！")

    # 启动后台任务
    _spawn(_supervise_background('auto_backup_task', auto_backup_task))
    _spawn(_supervise_background('auto_cleanup_task', auto_cleanup_task))
    _spawn(_supervise_background('memory_watchdog', memory_watchdog))
    _spawn(_supervise_background('cpu_watchdog', cpu_watchdog))
    _spawn(_supervise_background('forward_worker', forward_worker))
    _spawn(_supervise_background('client_health_watchdog', client_health_watchdog))
    _spawn(_supervise_background('bot_health_watchdog', bot_health_watchdog))
    _spawn(_supervise_background('chat_stale_watchdog', chat_stale_watchdog))
    if SAVE_STATE_INTERVAL_SECONDS > 0:
        _spawn(_supervise_background('save_state_task', save_state_task))
    if ENABLE_DEVICE_ROTATION:
        _spawn(_supervise_background('auto_device_rotation', auto_device_rotation))
    else:
        logger.info("🔒 自动设备轮换已关闭，保持主账号设备指纹稳定")
    _spawn(_supervise_background('weekly_vacuum_task', weekly_vacuum_task))
    _spawn(_supervise_background('auto_version_check_task', auto_version_check_task))
    _spawn(_supervise_background('heartbeat_watchdog', heartbeat_watchdog))

    logger.info("🚀 所有后台任务已启动，进入事件监听循环...")
    try:
        await client.run_until_disconnected()
    finally:
        await _save_client_state_once()   # 正常退出也保存一次 update 状态


if __name__ == '__main__':
    client.loop.run_until_complete(main())
