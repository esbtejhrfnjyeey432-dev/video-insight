# -*- coding: utf-8 -*-
"""
VideoInsight 视频解析工具 · 后端服务
流程：视频上传/链接下载 -> ffmpeg 抽关键帧 -> 阿里百炼 Qwen-VL 内容理解 -> 结构化分析 + Remix
"""
import base64
import asyncio
import concurrent.futures
import glob
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests
import imageio_ffmpeg
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import resolver
import quota_identity
from payment_store import PaymentStore

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("video-insight")


def _pick_ffmpeg() -> str:
    """按优先级挑选 ffmpeg：环境变量 → 系统 PATH → imageio-ffmpeg 内置二进制。"""
    env_path = os.environ.get("VI_FFMPEG", "").strip()
    if env_path and os.path.exists(env_path):
        return env_path
    which = shutil.which("ffmpeg")
    if which:
        return which
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = _pick_ffmpeg()

# ------------------------------------------------------------------
# 隐私与访问控制（公网部署用环境变量注入，代码仓库中不含任何密钥）
#   VI_API_KEY     服务端持有的百炼 API Key（部署模式：前端永远看不到、也改不了）
#   VI_ACCESS_CODE 访问码；设置后所有解析接口都需携带正确的码（发给招聘者的是「链接+访问码」）
#   VI_PUBLIC      公开模式开关（默认 1 = 开启）。设为 1/true 时**不需要访问码**，
#                  任何人打开链接即可直接使用；设为 0/false 时才启用 VI_ACCESS_CODE 保护。
# ------------------------------------------------------------------
ENV_API_KEY = os.environ.get("VI_API_KEY", "").strip()
ACCESS_CODE = os.environ.get("VI_ACCESS_CODE", "").strip()
DEPLOY_MODE = bool(ENV_API_KEY)  # 服务端注入 Key 即视为公开部署模式
DAILY_LIMIT = int(os.environ.get("VI_DAILY_LIMIT", "20"))  # 部署模式：每天最多解析次数
_usage: dict = {}  # {访问码: [日期, 已用次数]} · 防止访问码外泄后被刷爆额度
# 每日 API 消耗金额上限（元）——测试期统一总闸。按估算成本累计所有烧 Key 的调用
# （分析、拆解、脚本、分镜、提示词、连贯修复、九宫格生图），超过即拒绝。
# 值为估算非精确计费，测试够了以后改这一个数字即可。
DAILY_COST_LIMIT_CNY = max(0.0, float(os.environ.get("VI_DAILY_COST_CNY", "5.00")))
_daily_cost_state: dict = {"date": time.strftime("%Y-%m-%d"), "cny": 0.0}
_daily_cost_lock = threading.Lock()
# 并发分池：轻任务（本地/浏览器抽帧）允许多人并行；重任务（远程下载、全片拆解）
# 限制并发防止免费层 OOM。所有请求彼此独立（临时目录/令牌隔离），互不覆盖。
MAX_LINK_CONCURRENT = max(1, int(os.environ.get("VI_MAX_LINK_CONCURRENT", "2")))
MAX_UPLOAD_CONCURRENT = max(1, int(os.environ.get("VI_MAX_UPLOAD_CONCURRENT", "4")))
MAX_FRAME_CONCURRENT = max(1, int(os.environ.get("VI_MAX_FRAME_CONCURRENT", "4")))
MAX_AGENT_CONCURRENT = max(1, int(os.environ.get("VI_MAX_AGENT_CONCURRENT", "2")))
MAX_CREATIVE_CONCURRENT = max(1, int(os.environ.get("VI_MAX_CREATIVE_CONCURRENT", "2")))
ANALYSIS_QUEUE_TIMEOUT = max(1, int(os.environ.get("VI_QUEUE_TIMEOUT", "60")))
_link_slots = asyncio.Semaphore(MAX_LINK_CONCURRENT)
_upload_slots = asyncio.Semaphore(MAX_UPLOAD_CONCURRENT)
_frame_slots = asyncio.Semaphore(MAX_FRAME_CONCURRENT)
_agent_slots = asyncio.Semaphore(MAX_AGENT_CONCURRENT)
_creative_slots = asyncio.Semaphore(MAX_CREATIVE_CONCURRENT)
_asr_media: dict[str, tuple[str, float]] = {}
_asr_media_lock = threading.Lock()

# 分析结果缓存：相同视频（文件/链接/帧）在 TTL 内重复解析直接复用上次结果，
# 秒回且不消耗 AI 额度——对「失败后重试」和「同一视频反复点」场景体验提升最大。
ANALYSIS_CACHE_TTL = int(os.environ.get("VI_CACHE_TTL", "1800"))  # 默认 30 分钟
ANALYSIS_CACHE_MAX = int(os.environ.get("VI_CACHE_MAX", "128"))    # 最多缓存条数
_analysis_cache: dict[str, tuple[float, dict]] = {}  # key -> (expire_ts, result)
_analysis_cache_lock = threading.Lock()


# 二创工作台缓存：与通用分析缓存分开，容量更小——
# 拆解响应含几十张关键帧 base64，必须限制条数防止撑爆免费层内存。
CREATIVE_CACHE_MAX = int(os.environ.get("VI_CREATIVE_CACHE_MAX", "10"))
_creative_cache: dict[str, tuple[float, dict]] = {}

# 付费生图体验保护：按访问者累计记录实际发起的九宫格费用，并设置全站预算。
# 临时公开体验使用本机 JSON 存储；正式商业化应替换为登录账号 + 数据库 + 支付回调。
GRID_FREE_LIMIT_CNY = max(0.0, float(os.environ.get("VI_GRID_FREE_LIMIT_CNY", "1.00")))
GRID_DAILY_BUDGET_CNY = max(0.0, float(os.environ.get("VI_GRID_DAILY_BUDGET_CNY", "10.00")))
GRID_SERVICE_FEE_CNY = max(0.0, float(os.environ.get("VI_GRID_SERVICE_FEE_CNY", "0.10")))
GRID_USAGE_PATH = Path(os.environ.get(
    "VI_GRID_USAGE_PATH", str(Path(tempfile.gettempdir()) / "video-insight-grid-usage.json")))
PAYMENT_PROOF_DIR = Path(os.environ.get(
    "VI_PAYMENT_PROOF_DIR", str(Path(tempfile.gettempdir()) / "video-insight-payment-proofs")))
PAYMENT_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PAYMENT_REQUIRE_DURABLE = os.environ.get(
    "VI_REQUIRE_DURABLE_PAYMENT_STORE", "true" if os.environ.get("RENDER") else "false"
).strip().lower() in ("1", "true", "yes", "on")
PAYMENT_ADMIN_TOKEN = os.environ.get("VI_PAYMENT_ADMIN_TOKEN", "").strip()
PAYMENT_AUTO_REVIEW = os.environ.get("VI_PAYMENT_AUTO_REVIEW", "true").strip().lower() in \
    ("1", "true", "yes", "on")
PAYMENT_REVIEW_MODEL = os.environ.get("VI_PAYMENT_REVIEW_MODEL", "qwen3-vl-plus").strip()
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
PAYMENT_ADMIN_URL = os.environ.get(
    "VI_PAYMENT_ADMIN_URL", "https://video-insight-9q9i.onrender.com/payment-admin.html").strip()
_grid_usage_lock = threading.Lock()
PAYMENT_STORE = PaymentStore(PAYMENT_DATABASE_URL, GRID_USAGE_PATH, PAYMENT_PROOF_DIR)


def _load_grid_usage() -> dict:
    default = {"date": time.strftime("%Y-%m-%d"), "daily_cny": 0.0, "clients": {}}
    return PAYMENT_STORE.load_state(default)


_grid_usage = _load_grid_usage()


def _save_grid_usage_locked() -> None:
    try:
        PAYMENT_STORE.state_path = GRID_USAGE_PATH
        PAYMENT_STORE.save_state(_grid_usage)
    except Exception as exc:
        logger.warning("grid_usage_save_failed reason=%s", exc)
        if PAYMENT_REQUIRE_DURABLE:
            raise RuntimeError("付款数据持久化暂不可用") from exc


def _require_durable_payment_store() -> None:
    """Never accept money on Render unless orders and proof images are durable."""
    if PAYMENT_REQUIRE_DURABLE and not PAYMENT_STORE.durable_available():
        logger.error("durable_payment_store_unavailable reason=%s", PAYMENT_STORE.last_error)
        raise HTTPException(503, "付款服务正在安全维护，请稍后再试；不会生成或丢失订单。")
    if PAYMENT_REQUIRE_DURABLE and not PAYMENT_STORE.state_loaded_durable:
        with _grid_usage_lock:
            restored = PAYMENT_STORE.load_state({
                "date": time.strftime("%Y-%m-%d"), "daily_cny": 0.0, "clients": {}})
            if not PAYMENT_STORE.state_loaded_durable:
                raise HTTPException(503, "付款服务正在恢复订单数据，请稍后再试。")
            _grid_usage.clear()
            _grid_usage.update(restored)


def _grid_client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    host = forwarded or (request.client.host if request.client else "unknown")
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:32]


def _grid_model_cost(ref_count: int) -> float:
    return round(0.18 + 0.02 * min(3, max(0, int(ref_count))), 2)


def _grid_quote_locked(client_key: str, model_cost: float) -> dict:
    today = time.strftime("%Y-%m-%d")
    if _grid_usage.get("date") != today:
        _grid_usage["date"], _grid_usage["daily_cny"] = today, 0.0
    used = round(float((_grid_usage.get("clients") or {}).get(client_key, 0.0)), 2)
    daily = round(float(_grid_usage.get("daily_cny") or 0.0), 2)
    paid_credits = max(0, int((_grid_usage.get("credits") or {}).get(client_key, 0)))
    allowed = used + model_cost <= GRID_FREE_LIMIT_CNY + 1e-9
    global_allowed = daily + model_cost <= GRID_DAILY_BUDGET_CNY + 1e-9
    return {
        "allowed": bool((allowed and global_allowed) or paid_credits > 0),
        "reason": "user_limit" if not allowed else ("daily_budget" if not global_allowed else ""),
        "used_cny": used,
        "remaining_cny": round(max(0.0, GRID_FREE_LIMIT_CNY - used), 2),
        "free_limit_cny": round(GRID_FREE_LIMIT_CNY, 2),
        "model_cost_cny": model_cost,
        "service_fee_cny": round(GRID_SERVICE_FEE_CNY, 2),
        "payable_cny": round(model_cost + GRID_SERVICE_FEE_CNY, 2),
        "paid_credits": paid_credits,
    }


def _reserve_grid_cost(client_key: str, model_cost: float) -> dict:
    with _grid_usage_lock:
        quote = _grid_quote_locked(client_key, model_cost)
        free_allowed = quote["used_cny"] + model_cost <= GRID_FREE_LIMIT_CNY + 1e-9 and \
            float(_grid_usage.get("daily_cny") or 0.0) + model_cost <= GRID_DAILY_BUDGET_CNY + 1e-9
        if not free_allowed and quote["paid_credits"] <= 0:
            raise HTTPException(402, detail={
                "code": "grid_payment_required",
                "message": "免费体验额度已用完，完成本次付款核验后即可继续生成。",
                **quote,
            })
        if free_allowed:
            clients = _grid_usage.setdefault("clients", {})
            clients[client_key] = round(float(clients.get(client_key, 0.0)) + model_cost, 2)
            _grid_usage["daily_cny"] = round(float(_grid_usage.get("daily_cny", 0.0)) + model_cost, 2)
            reservation = "free"
        else:
            credits = _grid_usage.setdefault("credits", {})
            credits[client_key] = max(0, int(credits.get(client_key, 0)) - 1)
            reservation = "credit"
        _save_grid_usage_locked()
        result = _grid_quote_locked(client_key, model_cost)
        result["reservation"] = reservation
        return result


def _release_grid_cost(client_key: str, model_cost: float, reservation: str = "free") -> None:
    with _grid_usage_lock:
        if reservation == "credit":
            credits = _grid_usage.setdefault("credits", {})
            credits[client_key] = int(credits.get(client_key, 0)) + 1
            _save_grid_usage_locked()
            return
        clients = _grid_usage.setdefault("clients", {})
        clients[client_key] = round(max(0.0, float(clients.get(client_key, 0.0)) - model_cost), 2)
        _grid_usage["daily_cny"] = round(max(0.0, float(_grid_usage.get("daily_cny", 0.0)) - model_cost), 2)
        _save_grid_usage_locked()


def _require_payment_admin(token: str) -> None:
    if not PAYMENT_ADMIN_TOKEN:
        raise HTTPException(503, "尚未配置付款审核口令")
    if not hmac.compare_digest(token or "", PAYMENT_ADMIN_TOKEN):
        raise HTTPException(403, "审核口令不正确")


def _notify_payment_order(order: dict) -> None:
    if not SERVERCHAN_SENDKEY:
        return
    key = SERVERCHAN_SENDKEY
    match = re.match(r"sctp(\d+)t", key)
    url = (f"https://{match.group(1)}.push.ft07.com/send/{key}.send" if match else
           f"https://sctapi.ftqq.com/{key}.send")
    try:
        requests.post(url, json={"title": "收到新的九宫格付款截图",
            "desp": f"订单：{order['order_id']}\n\n应付：¥{order['amount_cny']:.2f}\n\n[打开审核后台]({PAYMENT_ADMIN_URL})",
            "short": f"订单 {order['order_id']} 待审核"}, timeout=12)
    except requests.RequestException as exc:
        logger.warning("payment_notify_failed reason=%s", exc)


def _grant_payment_credit_locked(order: dict, reviewer: str) -> bool:
    """Idempotently approve one order and grant exactly one generation credit."""
    if order.get("status") == "approved":
        return False
    order["status"] = "approved"
    order["reviewed"] = time.strftime("%Y-%m-%d %H:%M:%S")
    order["reviewer"] = reviewer
    credits = _grid_usage.setdefault("credits", {})
    for key in order.get("keys") or []:
        credits[key] = int(credits.get(key, 0)) + 1
    return True


def _analyze_payment_proof(raw: bytes, content_type: str, order: dict) -> dict:
    """OCR a payment screenshot. This checks visible evidence, not actual settlement."""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(503, "未配置付款截图识别模型")
    data_url = "data:" + content_type + ";base64," + base64.b64encode(raw).decode("ascii")
    prompt = """你是付款截图 OCR Agent。盲提取图片中清晰可见的付款证据；你不知道正确答案。
不要猜测、补全或根据常见格式生成文字。判断画面是否明确显示支付成功/已支付（待支付、输入金额页、失败、退款均为 false），提取实付金额数字，并从备注、附言、商品说明或订单信息中提取完整商户订单号。字段不清晰就返回 null 或空字符串。
只返回 JSON：{"payment_success":false,"amount":null,"order_id":"","confidence":0.0,"reason":"简短说明可见证据"}"""
    result = _creative_asset_prompt(prompt, [data_url],
                                    {**cfg, "model": PAYMENT_REVIEW_MODEL},
                                    max_tokens=1024, attempts=2, timeout=60)
    try:
        amount_matches = abs(float(result.get("amount")) - float(order["amount_cny"])) < 0.001
    except (TypeError, ValueError):
        amount_matches = False
    order_id = str(result.get("order_id") or "").strip().upper()
    confidence = max(0.0, min(1.0, float(result.get("confidence") or 0.0)))
    reasons: list[str] = []
    if not result.get("payment_success"):
        reasons.append("未清晰识别到支付成功状态")
    if not amount_matches:
        reasons.append(f"金额不一致（识别：{result.get('amount')!s}，应付：{float(order['amount_cny']):.2f}）")
    if order_id != order["order_id"]:
        reasons.append(f"订单号不一致（识别：{order_id or '空'}，应为：{order['order_id']}）")
    if confidence < 0.98:
        reasons.append(f"图片识别置信度不足（{confidence:.2f}）")
    verified = bool(result.get("payment_success")) and amount_matches \
        and order_id == order["order_id"] and confidence >= 0.98
    return {"verified": verified, "payment_success": bool(result.get("payment_success")),
            "amount": result.get("amount"), "order_id": order_id,
            "confidence": confidence,
            "reason": "；".join(reasons) if reasons else "支付状态、金额和订单号均匹配",
            "evidence_note": str(result.get("reason") or "")[:300]}


_ORDER_ID_RE = re.compile(r"^VI[A-F0-9]{8}$")


def _cache_get(key: str, store: dict | None = None) -> dict | None:
    cache = _analysis_cache if store is None else store
    now = time.monotonic()
    with _analysis_cache_lock:
        entry = cache.get(key)
        if not entry:
            return None
        expire_ts, result = entry
        if now >= expire_ts:
            cache.pop(key, None)
            return None
        # 命中后刷新过期时间，活跃条目不会被反复淘汰
        cache[key] = (now + ANALYSIS_CACHE_TTL, result)
        return result


def _cache_put(key: str, result: dict, store: dict | None = None,
               max_entries: int | None = None) -> None:
    cache = _analysis_cache if store is None else store
    limit = ANALYSIS_CACHE_MAX if max_entries is None else max_entries
    with _analysis_cache_lock:
        if len(cache) >= limit:
            # 简单逐出：丢弃最接近过期的一条，避免缓存无限膨胀撑爆免费层内存
            oldest_key = min(cache, key=lambda k: cache[k][0])
            cache.pop(oldest_key, None)
        cache[key] = (time.monotonic() + ANALYSIS_CACHE_TTL, result)


def _with_cache_meta(result: dict, **overrides) -> dict:
    """命中缓存时复制结果并标记来源，避免把 mutable 元数据写回缓存。"""
    out = dict(result)
    meta = dict(out.get("_meta") or {})
    meta["cached"] = True
    meta.update(overrides)
    out["_meta"] = meta
    return out

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# 公开模式：默认开启（分享链接给任何人都能直接用）。
# 需要「仅授权人可用」时才在部署平台把 VI_PUBLIC 设为 false 并配置 VI_ACCESS_CODE。
PUBLIC_MODE = _env_flag("VI_PUBLIC", True)
ENABLE_DEBUG_ENDPOINTS = _env_flag("VI_ENABLE_DEBUG", not DEPLOY_MODE)
SERVICE_VERSION = os.environ.get("RENDER_GIT_COMMIT", os.environ.get("VI_VERSION", "dev"))[:12]

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
IMAGE_API_URL = os.environ.get("VI_IMAGE_API_URL", "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation").strip()
IMAGE_MODEL = os.environ.get("VI_IMAGE_MODEL", "qwen-image-3.0").strip()
ASR_SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
ASR_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
ASR_MODEL = os.environ.get(
    "VI_ASR_MODEL", "qwen-audio-3.0-asr-flash-filetrans").strip()
DEFAULT_MODEL = "qwen3-vl-plus"
FAST_VISION_MODEL = os.environ.get("VI_FAST_VISION_MODEL", "qwen-vl-plus").strip()
AGENT_MODEL = os.environ.get("VI_AGENT_MODEL", "qwen-plus").strip()
MODEL_OPTIONS = ["qwen3-vl-plus", "qwen-vl-plus", "qwen-vl-max", "qwen-vl-max-latest"]
# 抽帧数量/宽度可用环境变量下调（云平台免费层内存小，建议 6 帧 / 640px）
MAX_FRAMES = int(os.environ.get("VI_MAX_FRAMES", "12"))
# 帧数硬上限：抽帧越多 AI 推理越慢。6 帧已能均匀覆盖全片关键画面，
# 是「速度 / 质量」的平衡点；需要更精细可提高该上限（最多 12）。
try:
    MAX_FRAMES = min(MAX_FRAMES, int(os.environ.get("VI_FRAME_CAP", "6")))
except Exception:
    pass
FRAME_WIDTH = int(os.environ.get("VI_FRAME_WIDTH", "768"))
MAX_VIDEO_BYTES = int(os.environ.get("VI_MAX_VIDEO_MB", "500")) * 1024 * 1024
MAX_LINK_DURATION = 3 * 3600

PROMPT = """你是专业的视频内容分析师。我会给你一段视频中按时间顺序抽取的关键帧画面，并可能附带自动语音转写，请完成：
重要原则：所有结论必须来自关键帧中确实可见的信息。宁可少写，也不要为了凑数量编造、重复或加入无关内容；无法确认的细节要明确说明无法从画面判断。
语音原则：只有提供了语音转写时，才可引用口头内容；转写可能有错别字，应结合上下文谨慎归纳。未提供转写时，不得臆测画面之外的语音内容。
外文内容：如果关键帧或语音转写中包含英文或其他语言，请识别其语言，并将能够确认的标题、字幕和关键信息准确翻译为中文后输出。
1. 内容理解：判断视频主题、类型（教育培训/知识科普/新闻资讯/娱乐/产品演示/VLOG/其他）与标签；
2. 关键信息提取（核心任务）：逐条提取视频中的关键信息，共 6-8 条，按重要性从高到低排列。覆盖：主题、人物或主体、事件、关键数据、方法步骤、重要结论等，每条一句话，尽量带画面中的具体细节，让没看过视频的人读完就能掌握全部要点；
3. 章节时间轴：按关键帧先后顺序估算时间点划分章节；
4. 如果属于教学/知识类视频，提炼教学观点：核心教学主张、讲解思路、适合人群；
5. Remix 衍生创作：生成 3 张观点卡片（一句话观点+简短说明）、1 个约 60 秒的短视频口播脚本、3 条金句摘录；
6. 语音内容摘要：提供了语音转写时，用 3-6 句话概括口头讲述的重点；未提供时返回空字符串；
7. 总体总结：综合全部内容写一段 100-150 字的总结，概括视频讲了什么、整体结构如何、核心结论与价值、适合什么人看，作为整份报告的收尾。

只输出严格的 JSON，不要输出任何其他文字，不要用 markdown 代码块包裹。JSON 结构：
{"title":"视频标题","category":"视频类型","tags":["标签"],
"key_info":["关键信息1","关键信息2"],
"chapters":[{"time":"MM:SS","label":"章节名称"}],
"teaching":{"is_teaching":true,"viewpoints":["教学观点"],"logic":"讲解思路","audience":"适合人群"},
"remix":{"cards":[{"title":"一句话观点","desc":"简短说明"}],"script":"60秒口播脚本","quotes":["金句"]},
"speech_summary":"语音内容摘要；没有语音转写时为空字符串",
"overall_summary":"150-250字的总体总结"}
若不是教学类视频，teaching.is_teaching 填 false，其余教学字段填空数组或空字符串。"""

app = FastAPI(
    title="VideoInsight",
    docs_url=None if DEPLOY_MODE else "/docs",
    redoc_url=None if DEPLOY_MODE else "/redoc",
    openapi_url=None if DEPLOY_MODE else "/openapi.json",
)


@app.middleware("http")
async def request_observability(request: Request, call_next):
    """轻量请求追踪：不记录用户链接、文件名、请求体或任何密钥。"""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("request_failed id=%s method=%s path=%s",
                         request_id, request.method, request.url.path)
        raise
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if DEPLOY_MODE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    log_path = ("/api/asr-media/<redacted>"
                if request.url.path.startswith("/api/asr-media/")
                else request.url.path)
    logger.info("request id=%s method=%s path=%s status=%s ms=%s",
                request_id, request.method, log_path,
                response.status_code, elapsed_ms)
    return response

# ------------------------------------------------------------------
# 跨域（CORS）：前端页面与后端 API 部署在不同域名时必需。
# 例如界面托管在 Vercel、后端跑在 Render/Koyeb，浏览器会拦截跨域请求。
# 默认放行全部来源，方便直接把链接分享给任何人使用。
# 需要收紧时用环境变量 VI_ALLOW_ORIGINS 指定白名单（多个用逗号分隔）。
# ------------------------------------------------------------------
_origins = os.environ.get("VI_ALLOW_ORIGINS", "").strip()
ALLOW_ORIGINS = [o.strip() for o in _origins.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    if ENV_API_KEY:  # 部署模式：服务端环境变量优先，且不会被配置文件覆盖
        cfg["api_key"] = ENV_API_KEY
    # 小红书登录 Cookie 优先读环境变量（服务端统一配置），否则读配置文件
    env_xhs = os.environ.get("VI_XHS_COOKIE", "").strip()
    if env_xhs:
        cfg["xhs_cookie"] = env_xhs
    return cfg


async def require_code(x_access_code: str = Header(default="")):
    """访问码校验：公网部署时防止链接被陌生人滥用额度。

    公开模式（VI_PUBLIC 开启，默认）下直接放行——任何人打开链接都能用，
    额度由 DAILY_LIMIT 兜底保护。
    """
    if PUBLIC_MODE:
        return
    if ACCESS_CODE and not hmac.compare_digest(str(x_access_code), ACCESS_CODE):
        raise HTTPException(401, "访问码不正确，请输入正确的访问码")


async def optional_code(x_access_code: str = Header(default="")):
    """带访问码会额外增加配额，不带码也能用（公开分享场景）。"""
    return None


async def acquire_analysis_slot(kind: str):
    """按任务类型分池：轻任务多人并行，重任务限制并发防止 OOM。"""
    slots = (_frame_slots if kind == "frames" else
             _agent_slots if kind == "agent" else
             _upload_slots if kind == "upload" else
             _creative_slots if kind == "creative" else _link_slots)
    try:
        await asyncio.wait_for(slots.acquire(), timeout=ANALYSIS_QUEUE_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        raise HTTPException(503, "当前同时分析的任务较多，请稍等片刻后重试")
    return slots


def check_daily_usage():
    """部署模式下限制每日解析次数（本地自用不限），防止访问码泄露后被刷额度"""
    if not DEPLOY_MODE:
        return
    today = time.strftime("%Y-%m-%d")
    # Public deployments may deliberately omit an access code.  Keep a
    # process-local quota in that case so one public link cannot consume an
    # unlimited amount of the owner's model credit.
    usage_key = ACCESS_CODE or "public"
    rec = _usage.setdefault(usage_key, [today, 0])
    if rec[0] != today:
        rec[0], rec[1] = today, 0
    if rec[1] >= DAILY_LIMIT:
        raise HTTPException(429, f"今日解析次数已达上限（每天 {DAILY_LIMIT} 次），明天再来吧～")
    rec[1] += 1


def _analysis_cost_estimate(mode: str, with_asr: bool = False) -> float:
    """估算一次视觉分析的模型成本（元）。不精确，仅用于每日总额兜底。"""
    m = str(mode or "standard").lower()
    base = 0.30 if m == "deep" else (0.08 if m == "quick" else 0.15)
    return round(base + (0.10 if with_asr else 0.0), 2)


def charge_daily_cost(amount: float):
    """按估算金额累计每日 API 消耗；超过测试上限则拒绝，防止刷爆 Key。"""
    if not DEPLOY_MODE or DAILY_COST_LIMIT_CNY <= 0:
        return
    amount = max(0.0, float(amount or 0.0))
    with _daily_cost_lock:
        today = time.strftime("%Y-%m-%d")
        if _daily_cost_state["date"] != today:
            _daily_cost_state["date"], _daily_cost_state["cny"] = today, 0.0
        if _daily_cost_state["cny"] + amount > DAILY_COST_LIMIT_CNY + 1e-9:
            raise HTTPException(
                429, "今日 API 消耗已达测试上限（约 ¥%.2f），明天再来试试吧" % DAILY_COST_LIMIT_CNY)
        _daily_cost_state["cny"] = round(_daily_cost_state["cny"] + amount, 2)


def save_config(cfg: dict) -> None:
    # Atomic replacement avoids a truncated secret file after crashes.  0600
    # prevents other OS users from reading a locally stored API key on Unix.
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, CONFIG_PATH)


def get_duration(path: str, headers: dict | None = None):
    """用 ffmpeg 读取视频时长（imageio-ffmpeg 自带 ffmpeg，无需单独安装）"""
    try:
        cmd = [FFMPEG, "-hide_banner"]
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        cmd += ["-i", path]
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, errors="ignore", timeout=60,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if not m:
            return None
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return None


def _run_ffmpeg(cmd: list, timeout: int = 90):
    try:
        subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=timeout)
    except Exception:
        return False
    return True


def _extract_frames_batch(path: str, tmpdir: str, duration: float):
    """一次 ffmpeg 调用抽完全部帧（serverless 环境下比逐帧调用快数倍）。

    用 fps 滤镜按目标帧率均匀取样，一次解码即可产出全部关键帧，
    避免 N 次 ffmpeg 冷启动开销（serverless 环境下这一步很贵）。
    """
    pattern = os.path.join(tmpdir, "b%03d.jpg")
    fps = max(MAX_FRAMES / max(duration, 0.5), 1 / max(duration, 0.5))
    # 注意：不要叠加 thumbnail 滤镜——它会把多帧压成 1 个代表帧导致产出帧数锐减。
    # 纯 fps 滤镜即可按目标帧率均匀取样，实测比逐帧调用快 6 倍。
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", path,
        "-vf", f"fps={fps:.6f},scale={FRAME_WIDTH}:-2",
        "-q:v", "5", pattern,
    ]
    if not _run_ffmpeg(cmd, timeout=min(int(duration) + 60, 120)):
        return []
    files = sorted(
        f for f in glob.glob(os.path.join(tmpdir, "b*.jpg"))
        if os.path.getsize(f) > 1000
    )
    return files[:MAX_FRAMES]


def _extract_frames_serial(path: str, tmpdir: str, duration: float):
    """逐帧抽取（兜底方案）：依赖 -ss 快速 seek，兼容性最好；并发执行避免逐帧冷启动。"""
    def grab(i: int):
        t = duration * (i + 0.5) / MAX_FRAMES
        out = os.path.join(tmpdir, f"f{i}.jpg")
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-ss", f"{t:.2f}", "-i", path,
            "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "5", out,
        ]
        if _run_ffmpeg(cmd, timeout=60) and os.path.exists(out) and os.path.getsize(out) > 1000:
            return out
        return None

    # 免费层内存有限，最多 3 路并发抽帧，兼顾速度与稳定性
    workers = min(3, MAX_FRAMES)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return [f for f in pool.map(grab, range(MAX_FRAMES)) if f]


def extract_frames(path: str):
    """均匀抽取关键帧并转为 base64（静态画面自动去重，控制 token 消耗）"""
    duration = get_duration(path) or 0.0
    if duration <= 0:
        duration = 60.0
    tmpdir = tempfile.mkdtemp(prefix="vinsight_frames_")
    frames, seen = [], set()
    try:
        files = _extract_frames_batch(path, tmpdir, duration)
        if len(files) < max(2, MAX_FRAMES // 2):
            files = _extract_frames_serial(path, tmpdir, duration)
        for f in files:
            raw = Path(f).read_bytes()
            digest = hashlib.md5(raw).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            frames.append("data:image/jpeg;base64," + base64.b64encode(raw).decode())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return frames, duration


def extract_remote_frames(stream_url: str, duration: float, headers: dict | None = None):
    """从远程媒体均匀抽帧，不把一至三小时的视频完整下载到服务器。"""
    if duration <= 0 or duration > MAX_LINK_DURATION:
        raise HTTPException(400, "视频时长需在 3 小时以内")
    tmpdir = tempfile.mkdtemp(prefix="vinsight_remote_frames_")
    files = []
    try:
        frame_count = 4 if duration >= 2 * 3600 else MAX_FRAMES
        header_blob = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        def grab(i: int):
            t = duration * (i + 0.5) / frame_count
            out = os.path.join(tmpdir, f"r{i:02d}.jpg")
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{t:.2f}"]
            if header_blob:
                cmd += ["-headers", header_blob]
            cmd += ["-i", stream_url, "-frames:v", "1",
                    "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "5", out]
            if _run_ffmpeg(cmd, timeout=75) and os.path.exists(out) and os.path.getsize(out) > 1000:
                return out
            return None
        # 免费实例内存很小。两路 ffmpeg 在部分 B 站长视频上仍可能触发 OOM，
        # 因此单路执行；两小时以上只取 4 帧，稳定优先。
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            files = [f for f in pool.map(grab, range(frame_count)) if f]
        if len(files) < 2:
            raise HTTPException(400, "平台允许读取链接信息，但阻止了远程抽帧；请保存到本地后上传")
        return ["data:image/jpeg;base64," + base64.b64encode(Path(f).read_bytes()).decode()
                for f in files]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _try_json_load(text: str):
    """依次尝试标准解析与 raw_decode（容忍尾部多余文本），失败返回 None。"""
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
        return obj
    except Exception:
        return None


def _balance_json(text: str) -> str:
    """补齐末尾缺失的闭合括号，修复被截断的 JSON 输出。"""
    stack: list[str] = []
    pairs = {"]": "[", "}": "{"}
    closing = {"[": "]", "{": "}"}
    in_str = False
    escape = False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
    return text + "".join(closing[o] for o in reversed(stack))


def _repair_truncated_json(text: str) -> dict:
    """尽力修复不完整/带尾逗号的 JSON，降低大模型偶发输出截断导致的失败率。"""
    obj = _try_json_load(text)
    if obj is not None:
        return obj
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)  # 去尾逗号
    obj = _try_json_load(cleaned)
    if obj is not None:
        return obj
    obj = _try_json_load(_balance_json(cleaned))   # 补齐截断括号
    if obj is not None:
        return obj
    raise HTTPException(500, "JSON 解析失败，请重试一次")


def parse_model_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise HTTPException(500, "模型未返回有效的 JSON 结果")
    try:
        return _repair_truncated_json(text[s:e + 1])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "JSON 解析失败，请重试一次")


def _transcript_text(payload: dict) -> str:
    """从百炼文件转写结果中提取纯文本，兼容整段与逐句两种结构。"""
    transcripts = payload.get("transcripts") or []
    parts: list[str] = []
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        text = str(transcript.get("text") or "").strip()
        if text:
            parts.append(text)
            continue
        sentences = transcript.get("sentences") or []
        sentence_text = "".join(
            str(item.get("text") or "").strip()
            for item in sentences if isinstance(item, dict)
        )
        if sentence_text:
            parts.append(sentence_text)
    return "\n".join(parts).strip()


def _compact_transcript(text: str, limit: int = 40000) -> str:
    """长课程转写可能很大；均匀保留全文片段，避免只留下开头。"""
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    blocks = 8
    width = max(1, limit // blocks)
    max_start = max(0, len(text) - width)
    starts = [round(i * max_start / (blocks - 1)) for i in range(blocks)]
    return "\n……\n".join(text[start:start + width] for start in starts)


def transcribe_remote_audio_details(media_url: str, cfg: dict, timeout: int = 150) -> dict:
    """调用百炼文件转写，保留句级时间戳供视频拆解使用。"""
    if not media_url or not cfg.get("api_key") or not ASR_MODEL:
        return {"text": "", "segments": []}
    headers = {
        "Authorization": "Bearer " + cfg["api_key"],
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    is_qwen3 = ASR_MODEL.startswith("qwen3-")
    asr_input = ({"file_url": media_url} if is_qwen3
                 else {"file_urls": [media_url]})
    response = requests.post(
        ASR_SUBMIT_URL,
        headers=headers,
        json={
            "model": ASR_MODEL,
            "input": asr_input,
            "parameters": {"channel_id": [0], "enable_itn": True,
                           "enable_words": False},
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"语音转写任务提交失败（HTTP {response.status_code}）")
    task_id = str((response.json().get("output") or {}).get("task_id") or "")
    if not task_id:
        raise RuntimeError("语音转写服务未返回任务编号")

    deadline = time.monotonic() + timeout
    task_payload: dict = {}
    while time.monotonic() < deadline:
        task_response = requests.get(
            ASR_TASK_URL.format(task_id=task_id),
            headers={"Authorization": "Bearer " + cfg["api_key"]},
            timeout=30,
        )
        if task_response.status_code != 200:
            raise RuntimeError(f"查询语音转写任务失败（HTTP {task_response.status_code}）")
        task_payload = task_response.json()
        output = task_payload.get("output") or {}
        status = str(output.get("task_status") or "").upper()
        if status == "SUCCEEDED":
            break
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            raise RuntimeError(str(output.get("message") or "语音转写任务未成功"))
        time.sleep(1.5)
    else:
        raise RuntimeError("语音转写等待超时")

    output = task_payload.get("output") or {}
    result = output.get("result") or {}
    result_url = str(result.get("transcription_url") or "")
    # 兼容 Qwen-Audio/Fun-ASR 的旧版 results 数组返回结构。
    results = output.get("results") or []
    if not result_url:
        result_url = next((
            str(item.get("transcription_url") or "")
            for item in results
            if isinstance(item, dict) and item.get("transcription_url")
        ), "")
    if not result_url.startswith("https://"):
        raise RuntimeError("语音转写服务未返回有效结果地址")
    transcript_response = requests.get(result_url, timeout=45)
    transcript_response.raise_for_status()
    payload = transcript_response.json()
    segments: list[dict] = []
    for transcript in payload.get("transcripts") or []:
        if not isinstance(transcript, dict):
            continue
        for sentence in transcript.get("sentences") or []:
            if not isinstance(sentence, dict):
                continue
            text = str(sentence.get("text") or "").strip()
            if not text:
                continue
            try:
                begin = max(0, int(sentence.get("begin_time") or 0))
                end = max(begin, int(sentence.get("end_time") or begin))
            except (TypeError, ValueError):
                continue
            speaker = str(sentence.get("speaker_id") or sentence.get("speaker") or "").strip()
            if speaker.isdigit():
                speaker = "人物" + str(int(speaker) + 1)
            segments.append({"start_ms": begin, "end_ms": end, "text": text,
                             "speaker": speaker or "待校准",
                             "speaker_source": "asr" if speaker else "unknown"})
    return {
        "text": _compact_transcript(_transcript_text(payload)),
        "segments": segments[:2000],
    }


def transcribe_remote_audio(media_url: str, cfg: dict, timeout: int = 150) -> str:
    """兼容原有分析流程，仅返回转写正文。"""
    return transcribe_remote_audio_details(media_url, cfg, timeout).get("text", "")


def _parse_srt(content: bytes) -> list[dict]:
    """解析用户提供的 SRT；只信任时间和文字，人物名缺失时明确待校准。"""
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(413, "SRT 文件不能超过 2MB")
    text = content.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    clock = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})")
    segments: list[dict] = []
    for block in re.split(r"\n\s*\n", text):
        match = clock.search(block)
        if not match:
            continue
        values = [int(value) for value in match.groups()]
        start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
        end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
        lines = [re.sub(r"<[^>]+>", "", line).strip() for line in block[match.end():].splitlines() if line.strip()]
        body = " ".join(lines).strip()
        if not body:
            continue
        named = re.match(r"^([\w\u4e00-\u9fff·]{1,16})[：:]\s*(.+)$", body)
        segments.append({"start_ms": start, "end_ms": max(start, end),
                         "text": named.group(2) if named else body,
                         "speaker": named.group(1) if named else "待校准",
                         "speaker_source": "srt" if named else "unknown"})
    return segments[:3000]


def _creative_understanding(shots: list[dict], transcript: list[dict], cfg: dict,
                            base_analysis: dict | None = None) -> dict:
    compact = [{k: item.get(k) for k in ("start_ms", "end_ms", "speaker", "text")}
               for item in transcript[:1200]]
    if len(shots) <= 12:
        primary_shots = list(shots)
    else:
        indexes = sorted({round(i * (len(shots) - 1) / 11) for i in range(12)})
        primary_shots = [shots[index] for index in indexes]
    prompt = """你是短剧情原片分析导演。结合按时间排列且均匀覆盖全片的关键帧、字幕和已有基础分析，重新完成可核验的结构化理解，不得只复制基础分析。逐条校准说话人：SRT/ASR明确人物、清晰可见的口型对应或连续对话关系可以作为证据；直接证据充分时 confidence 应为0.90到0.99，只有弱上下文时为0.60到0.89，无法判断才写“待确认”且低于0.60。必须结合画面和台词填写前三秒、核心冲突、节奏和有效原因，不得在已有画面/台词证据时返回空字符串。只输出 JSON：
{"speaker_calibration":[{"start_ms":0,"end_ms":1000,"speaker":"人物A/旁白/待确认","text":"","confidence":0.0,"evidence":"画面口型/SRT标注/仅上下文推断"}],
"characters":[{"id":"char-1","name":"人物A","identity":"","personality":"","appearance":"","wardrobe_by_unit":[{"unit":"剧情单元1","wardrobe":""}]}],
"relationships":[{"from":"char-1","to":"char-2","relationship":"","changes":""}],
"scenes":[{"name":"","time_range":"","props":[]}],
"story_units":[{"unit":1,"time_range":"","summary":"","characters":[],"emotion_changes":[{"character":"人物名/整体氛围","from":"此前情绪","to":"当前情绪","cause":"画面或台词依据"}],"transition":"本单元如何承接上一单元并推动下一单元"}],
"shot_analysis":[{"shot":1,"shot_size":"远景/全景/中景/近景/特写","composition":"人物站位与构图","action":"人物或产品动作","visual_value":"该镜头可复用的拍摄特点"}],
"product_placements":[{"time_range":"","product":"","method":"","plot_function":""}],
"appeal_logic":{"first_3_seconds":"","conflict":"","payoffs":[],"pace":"","why_it_works":""},
"review_required":["需要人工确认的说话人或事实"]}。
每个剧情单元的 emotion_changes 和 transition 均为必填；每张参考关键帧都必须在 shot_analysis 中给出景别、构图、动作和实用价值。已有基础分析：""" + json.dumps(_analysis_for_agent(base_analysis or {}), ensure_ascii=False)[:10000] + "\n镜头索引：" + json.dumps([{"shot": x.get("shot"), "start": x.get("start"), "end": x.get("end")} for x in primary_shots], ensure_ascii=False) + "\n字幕数据：" + json.dumps(compact, ensure_ascii=False)[:34000]
    images = [item.get("image", "") for item in primary_shots]
    # 原片理解一次要看 12 张图并输出长结构化 JSON，属于最重的视觉调用，
    # 给 180 秒余量；宁可慢也不能降级成空壳理解。
    result = _creative_asset_prompt(prompt, images, cfg, timeout=180)
    units = result.get("story_units") or []
    incomplete = (not result.get("shot_analysis") or not units or
                  any(not x.get("transition") or not x.get("emotion_changes")
                      for x in units if isinstance(x, dict)))
    if incomplete:
        repair = prompt + "\n上一次结果缺少情绪、剧情承接或原片景别分析。请重新输出完整 JSON；这些字段不得为空。"
        result = _creative_asset_prompt(repair, images[:6], cfg, timeout=180)
    # 首轮最多查看 12 张均匀样本；其余镜头按小批量并行补齐视觉字段。
    # 这样长视频不会出现“前面有分析、后面全部待识别”。
    existing = {int(item.get("shot") or 0): item
                for item in (result.get("shot_analysis") or [])
                if isinstance(item, dict) and item.get("shot")}
    missing = [shot for shot in shots if int(shot.get("shot") or 0) not in existing]
    batches = [missing[index:index + 10] for index in range(0, len(missing), 10)]
    if batches:
        workers = min(3, len(batches))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_analyze_shot_feature_batch, batch, cfg)
                       for batch in batches]
            for future in futures:
                try:
                    for item in future.result():
                        if isinstance(item, dict) and item.get("shot"):
                            existing[int(item["shot"])] = item
                except Exception as exc:
                    logger.warning("shot_feature_batch_failed reason=%s", type(exc).__name__)
    result["shot_analysis"] = [existing[number] for number in sorted(existing)]
    return result


def _analyze_shot_feature_batch(shots: list[dict], cfg: dict) -> list[dict]:
    """仅分析一小批镜头的可拍摄视觉字段，避免重复生成全片剧情理解。"""
    indexes = [{"shot": item.get("shot"), "start": item.get("start"),
                "end": item.get("end")} for item in shots]
    prompt = """你是影视镜头分析员。下列图片与镜头索引严格一一对应。逐张观察并返回全部镜头，不得漏项，不得使用“待识别”“未知”或空字符串。景别只能从远景/全景/中景/近景/特写中选择；构图写人物或主体位置及画面关系；动作写当前画面确实可见的动作或状态；参考价值写该镜头对二创拍摄可直接复用的构图、动作或氛围特点。不要猜测画外内容。只输出 JSON：{"shot_analysis":[{"shot":1,"shot_size":"近景","composition":"人物居中，手机位于画面右侧","action":"人物低头查看手机","visual_value":"用手机特写承接来电冲突"}]}。
镜头索引：""" + json.dumps(indexes, ensure_ascii=False)
    result = _creative_asset_prompt(
        prompt, [item.get("image", "") for item in shots], cfg,
        max_tokens=max(1400, len(shots) * 220), attempts=2, timeout=150)
    rows = [item for item in (result.get("shot_analysis") or [])
            if isinstance(item, dict)]
    expected = {int(item.get("shot") or 0) for item in shots}
    valid = {int(item.get("shot") or 0): item for item in rows
             if int(item.get("shot") or 0) in expected and
             all(str(item.get(key) or "").strip() for key in
                 ("shot_size", "composition", "action", "visual_value"))}
    if set(valid) != expected:
        raise HTTPException(502, "部分镜头视觉字段缺失")
    return [valid[number] for number in sorted(valid)]


def _understanding_from_analysis(analysis: dict, transcript: list[dict]) -> dict:
    """复用首轮解析结果，避免二创入口再次完整调用视觉模型。"""
    chapters = analysis.get("chapters") if isinstance(analysis, dict) else []
    chapters = chapters if isinstance(chapters, list) else []
    calibration = []
    names: list[str] = []
    for item in transcript[:1200]:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker") or "待确认")
        explicit = speaker not in {"", "待确认", "待校准", "unknown"}
        if explicit and speaker not in names:
            names.append(speaker)
        calibration.append({
            "start_ms": int(item.get("start_ms") or 0),
            "end_ms": int(item.get("end_ms") or 0),
            "speaker": speaker if explicit else "待确认",
            "text": str(item.get("text") or ""),
            "confidence": 0.9 if explicit else 0.0,
            "evidence": "字幕人物标注" if explicit else "需要人工确认",
        })
    units = []
    scenes = []
    for index, chapter in enumerate(chapters[:40]):
        if not isinstance(chapter, dict):
            continue
        title = str(chapter.get("label") or chapter.get("title") or f"内容段落 {index + 1}")
        time_range = str(chapter.get("time") or "")
        summary = str(chapter.get("summary") or chapter.get("content") or title)
        lowered = summary.lower()
        if any(word in lowered for word in ("冲突", "爆雷", "突发", "争吵", "危机")):
            emotion_from, emotion_to = "平静", "紧张/震惊"
        elif any(word in lowered for word in ("调查", "确认", "追问", "真相")):
            emotion_from, emotion_to = "疑惑", "警觉/确认"
        elif any(word in lowered for word in ("行动", "执行", "决定", "指令")):
            emotion_from, emotion_to = "犹豫", "坚定"
        else:
            emotion_from, emotion_to = "中性", "专注"
        units.append({"unit": index + 1, "time_range": time_range, "summary": summary,
                      "characters": [], "emotion_changes": [{"character": "整体氛围",
                      "from": emotion_from, "to": emotion_to,
                      "cause": "根据章节标题与内容摘要推断，建议结合原片表情复核"}],
                      "transition": ""})
        scenes.append({"name": title, "time_range": time_range, "props": []})
    remix = analysis.get("remix") if isinstance(analysis, dict) else {}
    cards = remix.get("cards") if isinstance(remix, dict) else []
    cards = cards if isinstance(cards, list) else []
    appeal_text = "；".join(str(x.get("content") or x.get("text") or x.get("title") or "")
                            for x in cards[:6] if isinstance(x, dict)).strip("；")
    key_info = analysis.get("key_info") if isinstance(analysis, dict) else []
    key_info = key_info if isinstance(key_info, list) else []
    first_hook = (str(units[0].get("summary") or units[0].get("time_range") or "")
                  if units else (str(key_info[0]) if key_info else ""))
    conflict = str(key_info[1] if len(key_info) > 1 else analysis.get("overall_summary") or "")[:500]
    pace = (f"根据 {len(units)} 个章节节点推断内容推进节奏；需结合原片镜头复核"
            if units else "")
    for index, unit in enumerate(units):
        if index == 0:
            unit["transition"] = ("作为开场建立事件与人物目标" +
                                  (f"，随后进入“{units[index + 1]['summary']}”" if len(units) > 1 else ""))
        elif index + 1 < len(units):
            unit["transition"] = f"承接上一单元结果，并推动到“{units[index + 1]['summary']}”"
        else:
            unit["transition"] = "承接上一单元冲突，完成本段收束或进入下一阶段"
    return {
        "speaker_calibration": calibration,
        "characters": [{"id": f"char-{i + 1}", "name": name, "identity": "",
                        "personality": "", "appearance": "", "wardrobe_by_unit": []}
                       for i, name in enumerate(names)],
        "relationships": [], "scenes": scenes, "story_units": units, "shot_analysis": [],
        "product_placements": [],
        "appeal_logic": {"first_3_seconds": first_hook, "conflict": conflict, "payoffs": [], "pace": pace,
                         "why_it_works": appeal_text or str(analysis.get("overall_summary") or "")[:500]},
        "review_required": [] if names else ["说话人需要人工确认"],
    }


def _merge_speaker_calibration(segments: list[dict], understanding: dict) -> list[dict]:
    calibrated = understanding.get("speaker_calibration") if isinstance(understanding, dict) else []
    if not isinstance(calibrated, list):
        return segments
    result = []
    for segment in segments:
        best = None
        for item in calibrated:
            if not isinstance(item, dict):
                continue
            overlap = min(segment["end_ms"], int(item.get("end_ms") or 0)) - max(segment["start_ms"], int(item.get("start_ms") or 0))
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, item)
        merged = dict(segment)
        if best:
            item = best[1]
            confidence = max(0.0, min(1.0, float(item.get("confidence") or 0)))
            speaker = str(item.get("speaker") or "待确认")
            merged.update({"speaker": speaker if confidence >= 0.6 else "待确认",
                           "speaker_candidate": speaker, "speaker_confidence": confidence,
                           "speaker_evidence": str(item.get("evidence") or "")[:160]})
        result.append(merged)
    return result


def _validate_creative_phase(
    phase: str, result: dict, expected_groups: int | list[int] | None = None
) -> dict:
    """拒绝把空壳模型输出当成成功。"""
    if not isinstance(result, dict):
        raise HTTPException(502, "AI 没有返回结构化结果")
    if phase == "script":
        script = result.get("script") or {}
        shots = script.get("shots") or []
        if not script.get("story_units") or not shots:
            raise HTTPException(502, "新脚本缺少剧情单元或镜头，已阻止残缺结果进入下一步")
        empty_dialogue = sum(1 for x in shots if isinstance(x, dict)
                             and not str(x.get("dialogue") or "").strip())
        if empty_dialogue > max(1, len(shots) // 2):
            raise HTTPException(502, "新脚本多数镜头缺少新台词，已阻止残缺结果进入下一步")
        if script.get("target_seconds") is not None:
            previous_end = 0.0
            for index, shot in enumerate(shots):
                try:
                    start, end = float(shot.get("start")), float(shot.get("end"))
                except (TypeError, ValueError):
                    raise HTTPException(502, "新脚本存在无效时间，已阻止进入下一步")
                if abs(start - previous_end) > 0.05 or end <= start:
                    raise HTTPException(502, "新脚本时间轴不连续，已阻止进入下一步")
                if int(shot.get("shot") or 0) != index + 1:
                    raise HTTPException(502, "新脚本镜头顺序异常，已阻止进入下一步")
                previous_end = end
            target_seconds = float(script.get("target_seconds"))
            if abs(previous_end - target_seconds) > 0.05:
                raise HTTPException(502, "新脚本没有完整覆盖目标时长，已阻止进入下一步")
        result.setdefault("role_profiles", [])
        result.setdefault("product_profiles", [])
    elif phase == "storyboard":
        rows = result.get("storyboard") or []
        if not rows or any(not row.get("panels") or not row.get("time") for row in rows if isinstance(row, dict)):
            raise HTTPException(502, "分镜组缺少时间或画面规划，已阻止残缺结果进入下一步")
        wanted = list(range(1, int(expected_groups) + 1)) if isinstance(expected_groups, int) else []
        actual = [int(row.get("group") or 0) for row in rows if isinstance(row, dict)]
        if wanted and actual != wanted:
            raise HTTPException(502, "分镜组数量不完整，已阻止残缺结果进入下一步")
        previous_end = 0
        for row in rows:
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)s?\s*",
                                 str(row.get("time") or ""))
            if not match:
                raise HTTPException(502, "分镜组时间格式无效，已阻止进入下一步")
            start, end = float(match.group(1)), float(match.group(2))
            if abs(start - previous_end) > 0.05 or end <= start:
                raise HTTPException(502, "分镜组时间存在跳段或重叠，已阻止进入下一步")
            previous_end = end
            panels = row.get("panels") if isinstance(row, dict) else None
            if not isinstance(panels, list) or len(panels) != 9:
                raise HTTPException(502, "每个二创九宫格必须完整包含 9 个画面")
            if any(not isinstance(panel, dict) or not str(panel.get("visual") or "").strip()
                   for panel in panels):
                raise HTTPException(502, "九宫格存在空画面，已阻止半成品进入下一步")
            for index, panel in enumerate(panels, 1):
                panel["panel"] = index
    elif phase == "prompts":
        rows = result.get("video_prompts") or []
        if not rows or any(not str(row.get("prompt") or "").strip()
                           for row in rows if isinstance(row, dict)):
            raise HTTPException(502, "视频提示词为空，已阻止残缺结果进入下一步")
        wanted = ({int(x) for x in expected_groups}
                  if isinstance(expected_groups, list) else set())
        covered = {int(x) for row in rows if isinstance(row, dict)
                   for x in (row.get("source_groups") or []) if str(x).isdigit()}
        if wanted and not wanted.issubset(covered):
            raise HTTPException(502, "视频提示词未覆盖全部分镜组，已阻止残缺结果进入下一步")
        ordered = [int(x) for row in rows if isinstance(row, dict)
                   for x in (row.get("source_groups") or []) if str(x).isdigit()]
        if wanted and ordered != list(expected_groups):
            raise HTTPException(502, "视频提示词顺序与分镜时间轴不一致，已阻止乱序结果")
        if wanted and (len(rows) != len(expected_groups) or any(
                [int(x) for x in (row.get("source_groups") or []) if str(x).isdigit()]
                != [expected_groups[index]] for index, row in enumerate(rows))):
            raise HTTPException(502, "每个视频提示词必须只对应一个同编号分镜组")
    return result


def _apply_explicit_speaker_evidence(segments: list[dict], understanding: dict) -> dict:
    """SRT/ASR 已明确给出说话人时，以原始证据覆盖模型猜测和零置信度。"""
    if not isinstance(understanding, dict):
        understanding = {}
    calibrated = [dict(x) for x in (understanding.get("speaker_calibration") or [])
                  if isinstance(x, dict)]
    for segment in segments:
        speaker = str(segment.get("speaker") or "").strip()
        source = str(segment.get("speaker_source") or "unknown")
        if speaker in {"", "待确认", "待校准", "unknown"} or source not in {"srt", "asr"}:
            continue
        best = None
        for item in calibrated:
            overlap = min(int(segment.get("end_ms") or 0), int(item.get("end_ms") or 0)) - max(int(segment.get("start_ms") or 0), int(item.get("start_ms") or 0))
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, item)
        target = best[1] if best else {"start_ms": int(segment.get("start_ms") or 0),
                                      "end_ms": int(segment.get("end_ms") or 0),
                                      "text": str(segment.get("text") or "")}
        if not best:
            calibrated.append(target)
        target.update({"speaker": speaker, "confidence": 0.98 if source == "srt" else 0.92,
                       "evidence": "SRT明确标注" if source == "srt" else "语音模型声纹分离"})
    calibrated.sort(key=lambda x: (int(x.get("start_ms") or 0), int(x.get("end_ms") or 0)))
    understanding["speaker_calibration"] = calibrated
    return understanding


def _sanitize_creative_script(result: dict, assets: list[dict],
                              target_seconds: int | None = None) -> dict:
    """保证模型只引用本轮真实上传的素材。"""
    allowed = {str(item.get("id")) for item in assets
               if isinstance(item, dict) and item.get("id") and not item.get("hidden")}
    has_product = any(isinstance(item, dict) and item.get("type") == "product"
                      and not item.get("hidden") for item in assets)
    script = result.get("script") if isinstance(result, dict) else None
    if isinstance(script, dict):
        shots = [shot for shot in (script.get("shots") or []) if isinstance(shot, dict)]
        for shot in shots:
            if isinstance(shot, dict):
                shot["asset_ids"] = [str(value) for value in (shot.get("asset_ids") or [])
                                     if str(value) in allowed]
        if target_seconds and shots:
            # 数组顺序就是剧情顺序；仅重排时间，不重排内容。按模型给出的镜头时长
            # 比例压缩/拉伸到目标总时长，确保从 0 开始、无重叠、无空档。
            durations = []
            for shot in shots:
                try:
                    duration = float(shot.get("end")) - float(shot.get("start"))
                except (TypeError, ValueError):
                    duration = 0
                durations.append(max(0.5, duration))
            scale = float(target_seconds) / sum(durations)
            cursor = 0.0
            previous_unit = 1
            for index, (shot, duration) in enumerate(zip(shots, durations), 1):
                start = cursor
                cursor = float(target_seconds) if index == len(shots) else cursor + duration * scale
                shot["shot"] = index
                shot["start"] = round(start, 2)
                shot["end"] = round(cursor, 2)
                try:
                    unit = max(previous_unit, int(shot.get("unit") or previous_unit))
                except (TypeError, ValueError):
                    unit = previous_unit
                shot["unit"] = unit
                previous_unit = unit
            script["target_seconds"] = int(target_seconds)
        if not has_product:
            for unit in script.get("story_units") or []:
                if isinstance(unit, dict):
                    unit["product_placement"] = ""
    if not has_product and isinstance(result, dict):
        result["product_profiles"] = []
    if isinstance(result, dict):
        copy = result.get("post_copy") if isinstance(result.get("post_copy"), dict) else {}
        script = result.get("script") if isinstance(result.get("script"), dict) else {}
        if not copy.get("titles"):
            copy["titles"] = [str(script.get("title") or "新视频")]
        if not str(copy.get("body") or "").strip():
            copy["body"] = str(script.get("creative_angle") or "新视频脚本已整理完成，欢迎观看。")
        if not copy.get("tags"):
            copy["tags"] = ["短视频", "二创"]
        result["post_copy"] = copy
    return result


def _creative_scene_frames(video_path: str, out_dir: str, duration: float) -> list[dict]:
    """用 FFmpeg 场景分数寻找镜头切换点并输出低分辨率关键帧。"""
    pattern = os.path.join(out_dir, "scene-%03d.jpg")
    command = [
        FFMPEG, "-hide_banner", "-i", video_path,
        # 先缩到 480 宽再做场景比较：场景判断对分辨率不敏感，
        # 却能把解码后的逐帧计算量降一个量级，长视频拆镜提速明显。
        "-vf", "scale=480:-2,select='gt(scene,0.28)',showinfo",
        "-fps_mode", "vfr", "-frames:v", "39", "-q:v", "4", pattern,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=240)
    times = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", completed.stderr)]

    # 场景检测可能遇到静态视频；首帧始终保留，保证工作台仍可继续。
    first_path = os.path.join(out_dir, "scene-000.jpg")
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", "0",
        "-i", video_path, "-frames:v", "1", "-vf", "scale=480:-2",
        "-q:v", "4", "-y", first_path,
    ], capture_output=True, timeout=60)
    files = sorted(glob.glob(os.path.join(out_dir, "scene-*.jpg")))
    items: list[dict] = []
    for index, path in enumerate(files[:40]):
        time_value = 0.0 if Path(path).name == "scene-000.jpg" else (
            times[min(max(index - 1, 0), len(times) - 1)] if times else 0.0)
        raw = Path(path).read_bytes()
        items.append({
            "shot": index + 1,
            "start": round(time_value, 2),
            "end": round(duration, 2),
            "image": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
        })
    items.sort(key=lambda item: item["start"])
    # FFmpeg 的首帧和第一个切点偶尔同为 0 秒，去重。
    deduped: list[dict] = []
    for item in items:
        if deduped and abs(item["start"] - deduped[-1]["start"]) < 0.2:
            continue
        deduped.append(item)
    for index, item in enumerate(deduped):
        item["shot"] = index + 1
        item["end"] = round(deduped[index + 1]["start"] if index + 1 < len(deduped) else duration, 2)
    return deduped


def _creative_asset_prompt(prompt: str, images: list[str], cfg: dict,
                           max_tokens: int = 6144, attempts: int = 2,
                           timeout: int = 120) -> dict:
    """让视觉模型理解人物/产品参考图，并输出受约束 JSON。"""
    valid_images = [image for image in images[:12]
                    if isinstance(image, str) and image.startswith("data:image/")
                    and len(image) <= 1_500_000]
    last_error: Exception | None = None
    attempt_count = max(1, min(3, int(attempts)))
    for attempt in range(attempt_count):
        try:
            image_limit = len(valid_images) if attempt == 0 else (4 if attempt == 1 else 2)
            content = [{"type": "text", "text": prompt}]
            content.extend({"type": "image_url", "image_url": {"url": image}}
                           for image in valid_images[:image_limit])
            response = requests.post(
                API_URL,
                headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
                json={"model": cfg.get("model") or DEFAULT_MODEL,
                      "messages": [{"role": "user", "content": content}],
                      "temperature": 0.2,
                      "max_tokens": max(1024, min(8192, int(max_tokens))),
                      "enable_thinking": False,
                      "response_format": {"type": "json_object"}},
                timeout=timeout,
            )
            if response.status_code != 200:
                raise HTTPException(502, f"视觉模型返回异常（HTTP {response.status_code}）")
            message = response.json()["choices"][0]["message"]
            text = (message.get("content") or message.get("reasoning_content") or "").strip()
            return parse_model_json(re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip())
        except Exception as exc:
            last_error = exc
            logger.warning("creative_json_retry attempt=%s reason=%s", attempt + 1,
                           type(exc).__name__)
            if attempt + 1 < attempt_count:
                time.sleep(0.5 * (attempt + 1))
    if isinstance(last_error, HTTPException):
        raise last_error
    raise HTTPException(502, "内容整理暂未完成")


def _generate_storyboard_grid(board: dict, assets: list[dict], cfg: dict) -> dict:
    """根据新分镜和新资产生成一张无时间标记的二创九宫格。"""
    panels = (board.get("panels") or [])[:9]
    if len(panels) != 9 or any(not isinstance(panel, dict) or
                               not str(panel.get("visual") or "").strip()
                               for panel in panels):
        raise HTTPException(400, "当前分镜组不是完整 9 格，请先重新生成分镜方案")
    wanted = {str(x) for x in (board.get("asset_ids") or [])}
    active = [x for x in assets if isinstance(x, dict) and not x.get("hidden")]
    matched = [x for x in active if str(x.get("id")) in wanted] or active
    refs = [x.get("image") for x in matched if isinstance(x.get("image"), str)
            and x.get("image", "").startswith("data:image/")][:3]
    panel_text = "\n".join(
        f"第{i + 1}格：{p.get('shot_size') or '镜头'}，{p.get('visual') or ''}，"
        f"人物情绪动作：{p.get('emotion_action') or ''}"
        for i, p in enumerate(panels)
    )
    prompt = (
        "生成一张真实影视质感的3×3二创分镜九宫格。严格使用参考图中的人物、产品和场景身份，"
        "保持人物长相、服装、产品外观和场景连续一致；九格按从左到右、从上到下表达连续剧情。"
        "画面内不要出现时间、进度条、序号、字幕、文字、水印或界面控件。"
        f"\n整体分镜要求：{str(board.get('grid_prompt') or '')[:2500]}\n{panel_text[:4500]}"
    )
    response = requests.post(
        IMAGE_API_URL,
        headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
        json={"model": IMAGE_MODEL,
              "input": {"messages": [{"role": "user", "content":
                         [{"image": image} for image in refs] + [{"text": prompt}]}]},
              "parameters": {"size": "1024*1024", "n": 1, "prompt_extend": True,
                             "enable_thinking": False, "watermark": False,
                             "negative_prompt": str(board.get("negative_prompt") or "")[:1000]}},
        timeout=300,
    )
    data = response.json() if response.content else {}
    if response.status_code != 200:
        raise HTTPException(response.status_code, data.get("message") or "二创九宫格生成失败")
    choices = ((data.get("output") or {}).get("choices") or [])
    items = (((choices[0].get("message") or {}).get("content") or []) if choices else [])
    image_url = next((x.get("image") for x in items if isinstance(x, dict) and x.get("image")), "")
    if not image_url:
        raise HTTPException(502, "图片模型没有返回九宫格图片，分镜规划已保留")
    return {"image_url": image_url, "model": IMAGE_MODEL, "reference_count": len(refs),
            "estimated_cost_cny": _grid_model_cost(len(refs))}


ANALYSIS_MODES = {
    "quick": "快速模式：优先速度和成本，只输出最重要、可确认的信息，避免冗长。",
    "standard": "标准模式：兼顾信息覆盖、生成质量、处理速度和成本。",
    "deep": "深度模式：尽量覆盖内容结构、关键细节和可复用的创作素材，但仍不得臆测。",
}
def _analysis_context(mode: str) -> tuple[str, str]:
    safe_mode = mode if mode in ANALYSIS_MODES else "standard"
    return safe_mode, f"\n\n本次分析要求：{ANALYSIS_MODES[safe_mode]}"


RETRYABLE_MODEL_STATUS = {408, 429, 500, 502, 503, 504}


def _model_retry_delay(response, attempt: int) -> float:
    """Honor Retry-After when present, otherwise use bounded exponential backoff."""
    retry_after = (response.headers.get("Retry-After", "") if response is not None else "")
    try:
        return min(8.0, max(0.25, float(retry_after)))
    except (TypeError, ValueError):
        return min(8.0, 0.75 * (2 ** attempt))


SCENARIO_CONTEXT = {
    "course": "本次用于课程与讲座复盘：优先提炼课程结构、知识脉络、教学观点、学习目标与可复习内容。",
    "creative": "本次用于短剧、剧情短视频和电商内容二次创作：优先识别人物关系、冲突、产品植入和分镜结构。长视频先提炼高光主线，再压缩为目标短片，不逐段照搬。",
}


def _scenario_context(scenario: str) -> tuple[str, str]:
    safe = scenario if scenario in SCENARIO_CONTEXT else "course"
    return safe, "\n\n使用场景：" + SCENARIO_CONTEXT[safe]


def _analysis_model(cfg: dict, mode: str) -> str:
    """快速/标准优先低延迟模型，深度模式保留用户选择的高质量模型。"""
    return FAST_VISION_MODEL if mode in {"quick", "standard"} else (
        cfg.get("model") or DEFAULT_MODEL)


def _ensure_overall_summary(result: dict) -> dict:
    """Fill a missing summary strictly from facts already present in the report."""
    if str(result.get("overall_summary") or "").strip():
        return result
    facts = [str(item).strip() for item in (result.get("key_info") or [])
             if str(item).strip()][:4]
    speech = str(result.get("speech_summary") or "").strip()
    if speech:
        facts.append(speech)
    result["overall_summary"] = (
        "本视频的主要内容包括：" + "；".join(facts) + "。"
        if facts else "本次分析未提取到足够信息，建议结合原视频复核内容。"
    )
    return result


def _align_storyboard_to_script(result: dict, script_result: dict,
                                group_seconds: int, total_seconds: int) -> dict:
    """把分镜组固定到脚本时间轴，并让每格台词只引用当前时间段镜头。"""
    boards = [row for row in (result.get("storyboard") or []) if isinstance(row, dict)]
    script = script_result.get("script") if isinstance(script_result, dict) else {}
    shots = [row for row in ((script or {}).get("shots") or []) if isinstance(row, dict)]
    for index, board in enumerate(boards):
        start = index * group_seconds
        end = min(total_seconds, start + group_seconds)
        board["group"] = index + 1
        board["time"] = f"{start}-{end}s"
        board["duration"] = max(1, end - start)
        matching = []
        for shot in shots:
            try:
                shot_start, shot_end = float(shot.get("start")), float(shot.get("end"))
            except (TypeError, ValueError):
                continue
            if shot_start < end and shot_end > start:
                matching.append(shot)
        if not matching and shots:
            midpoint = (start + end) / 2
            matching = [min(shots, key=lambda item: abs(
                (float(item.get("start") or 0) + float(item.get("end") or 0)) / 2 - midpoint))]
        board["source_shots"] = [int(shot.get("shot") or 0) for shot in matching]
        panels = [panel for panel in (board.get("panels") or []) if isinstance(panel, dict)]
        for panel_index, panel in enumerate(panels):
            if not matching:
                continue
            source = matching[min(len(matching) - 1,
                                  panel_index * len(matching) // max(1, len(panels)))]
            panel["source_shot"] = int(source.get("shot") or 0)
            # 台词与说话人属于强连续字段，不能由分镜阶段改写或串到别组。
            panel["dialogue"] = str(source.get("dialogue") or "")
            panel["speaker"] = str(source.get("speaker") or "")
    for index, board in enumerate(boards):
        previous_visual = ""
        next_visual = ""
        if index:
            previous_panels = boards[index - 1].get("panels") or []
            previous_visual = str((previous_panels[-1] if previous_panels else {}).get("visual") or "")
        if index + 1 < len(boards):
            next_panels = boards[index + 1].get("panels") or []
            next_visual = str((next_panels[0] if next_panels else {}).get("visual") or "")
        board["continuity_in"] = ("开场建立人物、场景与冲突" if index == 0 else
                                  "承接上一组末格：" + previous_visual[:80])
        board["continuity_out"] = ("剧情收束并完成行动" if index + 1 == len(boards) else
                                   "下一组从此动作继续：" + next_visual[:80])
    result["storyboard"] = boards
    result["_timeline_aligned"] = True
    return result


def _align_video_prompts(result: dict, storyboard: list[dict]) -> dict:
    """按来源分镜组排序提示词并锁定其时间信息，避免前后组乱穿插。"""
    rows = [row for row in (result.get("video_prompts") or []) if isinstance(row, dict)]
    rows.sort(key=lambda row: min([int(x) for x in (row.get("source_groups") or [])
                                  if str(x).isdigit()] or [10**9]))
    board_by_group = {int(row.get("group") or index + 1): row
                      for index, row in enumerate(storyboard) if isinstance(row, dict)}
    for index, row in enumerate(rows, 1):
        groups = [int(x) for x in (row.get("source_groups") or []) if str(x).isdigit()]
        row["group"] = index
        if len(groups) == 1 and groups[0] in board_by_group:
            board = board_by_group[groups[0]]
            row["time"] = str(board.get("time") or "")
            row["duration"] = int(board.get("duration") or row.get("duration") or 0)
            row["asset_ids"] = list(board.get("asset_ids") or [])
    result["video_prompts"] = rows
    result["_timeline_aligned"] = True
    return result


def call_qwen(frames: list, cfg: dict, duration: float | None = None,
              mode: str = "standard", transcript: str = "",
              scenario: str = "course") -> dict:
    duration_note = ""
    if duration and duration > 0:
        duration_note = (
            f"\n\n视频总时长约 {int(duration)} 秒；下面 {len(frames)} 张关键帧"
            "按时间顺序均匀抽取。章节时间请根据总时长与帧序估算。"
        )
    mode, context_note = _analysis_context(mode)
    scenario, scenario_note = _scenario_context(scenario)
    model = _analysis_model(cfg, mode)
    transcript_note = ""
    if transcript:
        transcript_note = (
            "\n\n以下为自动语音转写（可能有识别误差），请与关键帧相互印证：\n"
            + transcript
        )
    content = [{"type": "text", "text": PROMPT + duration_note + context_note + scenario_note + transcript_note}]
    for f in frames:
        content.append({"type": "image_url", "image_url": {"url": f}})
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
        "max_tokens": 2048 if mode in {"quick", "standard"} else 3072,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    last_err: HTTPException | None = None
    for attempt in range(3):  # 模型偶发返回非 JSON，自动重试
        try:
            r = requests.post(
                API_URL,
                headers={
                    "Authorization": "Bearer " + cfg["api_key"],
                    "Content-Type": "application/json",
                },
                json=body, timeout=120 if mode in {"quick", "standard"} else 240,
            )
        except Exception as exc:
            logger.warning("model_request_failed attempt=%s type=%s", attempt + 1,
                           type(exc).__name__)
            if attempt < 2:
                time.sleep(_model_retry_delay(None, attempt))
                continue
            raise HTTPException(502, "大模型服务暂时不可用，请稍后重试")
        if r.status_code != 200:
            # Never reflect a provider response body: it may contain internal
            # request metadata and is not suitable for public clients or logs.
            logger.warning("model_response_failed attempt=%s status=%s",
                           attempt + 1, r.status_code)
            if r.status_code in RETRYABLE_MODEL_STATUS and attempt < 2:
                time.sleep(_model_retry_delay(r, attempt))
                continue
            raise HTTPException(502, f"大模型服务返回异常（HTTP {r.status_code}），请稍后重试")
        msg = r.json()["choices"][0]["message"]
        # content 为空时兜底取 reasoning_content；并剥离思考标签
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        try:
            # 模型偶尔会漏掉末尾总结；仅用同轮已提取事实补齐，不补造内容。
            return _ensure_overall_summary(parse_model_json(text))
        except HTTPException as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    raise last_err


def call_qwen_text_json(prompt: str, cfg: dict, temperature: float = 0.2,
                        max_tokens: int = 4096) -> dict:
    """调用文本模型并要求返回 JSON，供受控 Agent 的规划与工具执行使用。"""
    body = {
        "model": AGENT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max(1024, min(8192, int(max_tokens))),
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    last_err: HTTPException | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                API_URL,
                headers={
                    "Authorization": "Bearer " + cfg["api_key"],
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=120,
            )
        except Exception:
            logger.warning("agent_model_request_failed attempt=%s", attempt + 1)
            if attempt < 2:
                time.sleep(_model_retry_delay(None, attempt))
                continue
            raise HTTPException(502, "Agent 模型暂时不可用，请稍后重试")
        if response.status_code != 200:
            logger.warning("agent_model_response_failed attempt=%s status=%s",
                           attempt + 1, response.status_code)
            if response.status_code in RETRYABLE_MODEL_STATUS and attempt < 2:
                time.sleep(_model_retry_delay(response, attempt))
                continue
            raise HTTPException(502, f"Agent 模型返回异常（HTTP {response.status_code}）")
        message = response.json()["choices"][0]["message"]
        text = (message.get("content") or message.get("reasoning_content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        try:
            return parse_model_json(text)
        except HTTPException as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    raise last_err


AGENT_TOOLS = ("creative_pack", "course_pack")


def _analysis_for_agent(analysis: dict) -> dict:
    """只向 Agent 传递报告内容，不传密钥、原始帧或内部请求信息。"""
    allowed = (
        "title", "category", "tags", "key_info", "chapters", "teaching",
        "speech_summary", "overall_summary", "remix",
    )
    return {key: analysis.get(key) for key in allowed if key in analysis}


def _normalize_agent_plan(plan: dict, analysis: dict, requested: list[str]) -> dict:
    """把模型计划限制在白名单和最多两步内，并为异常计划提供可靠降级。"""
    raw_steps = plan.get("steps") if isinstance(plan, dict) else []
    steps: list[dict] = []
    for item in raw_steps or []:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")
        if tool not in AGENT_TOOLS or any(step["tool"] == tool for step in steps):
            continue
        steps.append({"tool": tool, "reason": str(item.get("reason") or "")[:160]})
        if len(steps) == 2:
            break

    requested_set = {str(x).lower() for x in requested}
    teaching = bool((analysis.get("teaching") or {}).get("is_teaching"))
    if "all" in requested_set:
        requested_set.update({"creative", "course"})
    required_tools = []
    if "creative" in requested_set:
        required_tools.append(("creative_pack", "用户明确选择了二次创作路线"))
    if "course" in requested_set:
        required_tools.append(("course_pack", "用户明确选择了课程整理路线"))
    if required_tools:
        # 显式目标优先于模型自由规划，避免用户点了课程整理却只得到二创素材。
        steps = [
            {"tool": tool, "reason": reason}
            for tool, reason in required_tools
        ][:2]
    if not steps:
        if "creative" in requested_set or "auto" in requested_set or not requested_set:
            steps.append({"tool": "creative_pack", "reason": "生成多平台二创素材"})
        if teaching and ("course" in requested_set or "auto" in requested_set or not requested_set):
            steps.append({"tool": "course_pack", "reason": "教学内容适合整理为课程资料"})
    return {
        "intent": str(plan.get("intent") or "深化视频内容价值")[:120],
        "route": str(plan.get("route") or ("课程整理" if teaching else "内容二创"))[:80],
        "steps": steps,
    }


def _agent_plan(analysis: dict, requested: list[str], cfg: dict) -> dict:
    snapshot = json.dumps(_analysis_for_agent(analysis), ensure_ascii=False)[:45000]
    prompt = f"""你是 VideoInsight 的任务规划 Agent。根据已有视频分析和用户目标，选择最有价值的工具。
只能选择以下工具，最多两步，不得发明工具：
- creative_pack：生成多时长脚本、精彩片段建议、分镜和短视频口播稿。
- course_pack：生成课程大纲、学习目标、课件页和逐页讲师备注；只适合课程、讲座、知识教学内容。
用户目标：{json.dumps(requested or ['auto'], ensure_ascii=False)}。如果用户明确指定 creative、course 或 all，必须选择对应工具；只有 auto 才可自由取舍。
已有分析：{snapshot}
只输出 JSON：{{"intent":"用户意图","route":"选择的创作路线","steps":[{{"tool":"creative_pack","reason":"选择理由"}}]}}
不要因为工具存在就全部选择；只选择与内容真正匹配的工具。"""
    return _normalize_agent_plan(call_qwen_text_json(prompt, cfg), analysis, requested)


def _run_agent_tool(tool: str, analysis: dict, cfg: dict) -> dict:
    snapshot = json.dumps(_analysis_for_agent(analysis), ensure_ascii=False)[:45000]
    grounding = "所有时间点、事实和金句必须能由已有分析支持；不能确认时明确标注建议复核原视频。"
    if tool == "creative_pack":
        prompt = f"""你是视频二次创作 Agent。基于下方已有分析生成可直接编辑使用的二创素材。{grounding}
已有分析：{snapshot}
只输出 JSON，结构必须为：
{{"creative":{{
"scripts":{{"15s":"15秒脚本","30s":"30秒脚本","60s":"60秒脚本","90s":"90秒脚本"}},
"highlights":[{{"start":"MM:SS","end":"MM:SS","title":"片段标题","reason":"入选原因","hook":"开场钩子"}}],
"storyboard":[{{"shot":1,"time":"时间范围","visual":"画面建议","narration":"旁白","caption":"屏幕字幕"}}],
"post_copy":{{"titles":["标题1","标题2","标题3"],"body":"发布视频时使用的简洁配文","tags":["标签"]}},
"voiceover":{{"title":"口播标题","script":"自然、可直接朗读的口播稿"}}
}}}}
精彩片段给 3-6 个，分镜给 5-10 个；禁止虚构原视频不存在的数据、原话或具体画面。"""
        return call_qwen_text_json(prompt, cfg, 0.35)
    if tool == "course_pack":
        prompt = f"""你是课程内容整理 Agent。基于下方已有分析生成可直接编辑并导出课件的结构。{grounding}
已有分析：{snapshot}
只输出 JSON，结构必须为：
{{"course":{{
"learning_objectives":["学习目标"],
"outline":[{{"title":"章节","points":["知识点"]}}],
"slides":[{{"title":"PPT页标题","bullets":["页面要点"],"speaker_notes":"讲师备注与讲解建议"}}],
"exercises":[{{"question":"复习题或练习","answer":"参考答案"}}]
}}}}
课件建议 6-12 页，每页信息简洁；如果证据不足，不得补造知识。"""
        return call_qwen_text_json(prompt, cfg, 0.25)
    raise HTTPException(400, "不支持的 Agent 工具")


def _agent_quality(result: dict, plan: dict) -> dict:
    """零额外模型费用的结构质检；阻止空壳结果被标记为成功。"""
    checks: list[dict] = []
    selected = [step["tool"] for step in plan.get("steps", [])]
    if "creative_pack" in selected:
        creative = result.get("creative") or {}
        passed = bool(creative.get("scripts") and creative.get("storyboard"))
        checks.append({"name": "二创素材完整性", "passed": passed})
    if "course_pack" in selected:
        course = result.get("course") or {}
        passed = bool(course.get("outline") and course.get("slides"))
        checks.append({"name": "课程资料完整性", "passed": passed})
    ok = bool(checks) and all(item["passed"] for item in checks)
    return {"passed": ok, "checks": checks}


@app.post("/api/agent/enrich")
async def agent_enrich(
    payload: dict = Body(...),
    _code: None = Depends(require_code),
):
    """受控 Agent：规划 -> 选择白名单工具 -> 执行 -> 结构质检。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict) or not analysis.get("title"):
        raise HTTPException(400, "请先完成视频分析")
    if len(json.dumps(analysis, ensure_ascii=False)) > 200_000:
        raise HTTPException(413, "分析报告数据过大")
    requested = payload.get("goals") or ["auto"]
    if not isinstance(requested, list) or len(requested) > 4:
        raise HTTPException(400, "Agent 目标格式不正确")
    if any(not isinstance(goal, str) or len(goal) > 40 for goal in requested):
        raise HTTPException(400, "Agent 目标格式不正确")
    agent_slot = await acquire_analysis_slot("agent")
    try:
        check_daily_usage()
        explicit = {str(item).lower() for item in requested} & {"creative", "course", "all"}
        if explicit:
            plan = _normalize_agent_plan({}, analysis, requested)
            plan["route"] = "课程与讲座复盘" if "course" in explicit else "内容整理与二次创作"
        else:
            plan = await asyncio.to_thread(_agent_plan, analysis, requested, cfg)
        if not plan["steps"]:
            raise HTTPException(422, "当前内容没有匹配到适合的 Agent 创作工具")
        merged: dict = {}
        trace: list[dict] = []
        for step in plan["steps"]:
            output = await asyncio.to_thread(_run_agent_tool, step["tool"], analysis, cfg)
            merged.update(output)
            trace.append({"tool": step["tool"], "reason": step["reason"], "status": "completed"})
        quality = _agent_quality(merged, plan)
        if not quality["passed"]:
            raise HTTPException(502, "Agent 生成结果不完整，请稍后重试")
        return {**merged, "agent": {"model": AGENT_MODEL, "plan": plan,
                                      "trace": trace, "quality": quality}}
    finally:
        agent_slot.release()


def _workbench_audit(workbench: dict) -> dict:
    """本地检查当前工程离可生成成片还差什么，不调用模型。"""
    deconstruction = workbench.get("deconstruction") or {}
    assets = [x for x in (workbench.get("assets") or [])
              if isinstance(x, dict) and not x.get("hidden")]
    script = ((workbench.get("script") or {}).get("script") or {})
    shots = [x for x in (script.get("shots") or []) if isinstance(x, dict)]
    boards = [x for x in (workbench.get("storyboard") or []) if isinstance(x, dict)]
    prompts = [x for x in (workbench.get("prompts") or []) if isinstance(x, dict)]
    checks = []

    def add(name: str, passed: bool, detail: str, action: str = ""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail,
                       "action": action})

    has_deconstruction = bool(deconstruction.get("shots") or
                              deconstruction.get("understanding"))
    understanding_data = deconstruction.get("understanding") or {}
    has_understanding = bool(understanding_data.get("story_units") or
                             understanding_data.get("shot_analysis"))
    if has_understanding:
        add("原片理解", True, "已具备原片拆解与剧情理解")
    elif has_deconstruction:
        add("原片理解", False,
            "已完成镜头拆解；深层剧情理解尚未完成，人物关系与剧情单元暂缺",
            "回到第 01 步重新理解原片，补充剧情结构")
    else:
        add("原片理解", False, "尚未完成原片拆解", "先完成原片理解")
    requirements = str(workbench.get("requirements") or "")
    needs_assets = bool(re.search(r"换人物|人物替换|换产品|产品替换|换场景|场景替换",
                                  requirements))
    assets_ready = bool(assets) or not needs_assets
    asset_detail = (f"已启用 {len(assets)} 项素材" if assets else
                    ("改编要求包含素材替换，但尚未上传对应参考图" if needs_assets else
                     "本次没有要求替换人物、产品或场景，可沿用原片设定"))
    add("参考素材", assets_ready, asset_detail, "上传并启用要替换的人物、产品或场景图")
    usable_shots = [x for x in shots if str(x.get("visual") or "").strip()]
    spoken_shots = [x for x in shots if str(x.get("dialogue") or "").strip()]
    add("新脚本", bool(shots) and len(usable_shots) == len(shots),
        (f"{len(usable_shots)}/{len(shots)} 个镜头具备可拍画面" if shots else
         "尚未生成新脚本"), "完成第 03 步新脚本，或补齐空画面")
    add("人物台词", bool(shots) and len(spoken_shots) >= max(1, len(shots) // 2),
        (f"{len(spoken_shots)}/{len(shots)} 个镜头有台词" if shots else
         "生成新脚本后才能检查人物与台词关系"), "补齐需要说话镜头的台词")
    complete_boards = [x for x in boards if len(x.get("panels") or []) == 9 and
                       all(str(p.get("visual") or "").strip() for p in (x.get("panels") or []))]
    add("九宫格", bool(boards) and len(complete_boards) == len(boards),
        (f"{len(complete_boards)}/{len(boards)} 组为完整 9 格" if boards else
         "尚未生成九宫格分镜"), "完成第 04 步分镜方案，或重新生成不完整分镜组")
    board_ids = {int(x.get("group") or i + 1) for i, x in enumerate(boards)}
    prompt_ids = {int(g) for x in prompts for g in (x.get("source_groups") or [])
                  if str(g).isdigit()}
    add("视频提示词", bool(prompts) and board_ids.issubset(prompt_ids),
        (f"已覆盖 {len(prompt_ids & board_ids)}/{len(board_ids)} 个分镜组" if prompts else
         "尚未生成视频提示词"),
        "为全部分镜组重新生成提示词")
    passed = sum(1 for x in checks if x["passed"])
    score = round(passed / len(checks) * 100) if checks else 0
    blockers = [x for x in checks if not x["passed"]]
    started = any((has_deconstruction, assets, shots, boards, prompts))
    status = "ready" if started and not blockers else ("in_progress" if started else
                                                        "not_started")
    if status == "not_started":
        summary = "尚未进入制作流程。请先完成原片理解，再按步骤生成脚本、分镜和视频提示词。"
    elif status == "ready":
        summary = "必要材料均已具备，可以进入后续成片。"
    else:
        summary = f"当前已完成 {passed}/{len(checks)} 项；按下方建议补齐后再进入成片。"
    return {"score": score, "status": status, "ready": status == "ready",
            "checks": checks, "blockers": blockers, "summary": summary}


def _sanitize_agent_patches(raw: dict, workbench: dict) -> list[dict]:
    """只允许 Agent 修改明确白名单字段，避免模型越权改工程结构。"""
    allowed = {
        "script_shot": {"visual", "dialogue", "action", "emotion", "shot_type"},
        "storyboard_group": {"continuity_in", "continuity_out", "grid_prompt"},
    }
    script_count = len((((workbench.get("script") or {}).get("script") or {}).get("shots") or []))
    board_count = len(workbench.get("storyboard") or [])
    clean = []
    for item in (raw.get("patches") or [])[:8]:
        if not isinstance(item, dict):
            continue
        target, field = str(item.get("target") or ""), str(item.get("field") or "")
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        limit = script_count if target == "script_shot" else board_count
        value = str(item.get("value") or "").strip()
        if target not in allowed or field not in allowed[target] or not 0 <= index < limit or not value:
            continue
        clean.append({"target": target, "index": index, "field": field,
                      "value": value[:1200], "reason": str(item.get("reason") or "")[:240]})
    return clean


@app.post("/api/agent/workbench")
async def agent_workbench(
    payload: dict = Body(...),
    _code: None = Depends(require_code),
):
    """二创工作台 Agent 工具：质检、连续性修复和裂变方向。"""
    action = str(payload.get("action") or "")
    workbench = payload.get("workbench") or {}
    if not isinstance(workbench, dict):
        raise HTTPException(400, "工作台数据格式不正确")
    if len(json.dumps(workbench, ensure_ascii=False)) > 600_000:
        raise HTTPException(413, "工作台数据过大")
    audit = _workbench_audit(workbench)
    if action == "audit":
        return {"action": action, "audit": audit, "model_used": False}
    if action not in {"repair_continuity", "variants"}:
        raise HTTPException(400, "不支持的 Agent 动作")
    if action == "repair_continuity" and not (
        (((workbench.get("script") or {}).get("script") or {}).get("shots") or [])
    ):
        raise HTTPException(422, "请先完成第 03 步新脚本，再检查并修复连贯性")
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    charge_daily_cost(0.05)  # 质检 / 连贯修复 / 裂变：单次文本调用
    snapshot = {
        "analysis": _analysis_for_agent(payload.get("analysis") or {}),
        "requirements": str(workbench.get("requirements") or "")[:3000],
        "assets": [{k: x.get(k) for k in ("id", "name", "type")}
                   for x in (workbench.get("assets") or []) if isinstance(x, dict) and not x.get("hidden")],
        "script": workbench.get("script") or {},
        "storyboard": workbench.get("storyboard") or [],
        "audit": audit,
    }
    agent_slot = await acquire_analysis_slot("agent")
    try:
        if action == "variants":
            prompt = """你是短视频裂变策划 Agent。只基于提供的视频分析和真实素材，给出恰好3个差异明显、能继续生成脚本的方向。不得虚构未上传的产品卖点、人物履历或原片数据。每个方向要说明钩子、冲突和需要改变的内容。只输出 JSON：{\"variants\":[{\"title\":\"\",\"angle\":\"\",\"hook\":\"\",\"conflict\":\"\",\"changes\":[\"\"],\"requirement\":\"可直接写入创作要求的完整指令\"}]}。\n工作区：""" + json.dumps(snapshot, ensure_ascii=False)[:55000]
            result = await asyncio.to_thread(call_qwen_text_json, prompt, cfg, 0.45, 2200)
            variants = [x for x in (result.get("variants") or []) if isinstance(x, dict)][:3]
            if len(variants) != 3 or any(not str(x.get("requirement") or "").strip() for x in variants):
                raise HTTPException(502, "Agent 没有生成完整的三个裂变方向")
            return {"action": action, "variants": variants, "audit": audit, "model_used": True}
        prompt = """你是影视连续性修复 Agent。你的目标只有一个：让脚本和分镜达到「可以顺利生成成片」的实用标准，而不是打磨完美剧本。

【只修以下 5 类硬伤，其余一律不动】
1. 人物状态矛盾：同一人物的位置、持有物、服装、伤损在前后镜头/分镜组直接冲突。
2. 场景跳变：相邻分镜组之间地点或时间突变，且没有任何承接交代。
3. 台词因果断裂：对话答非所问、说话人与台词对不上、后一镜头的反应缺少前一镜头的诱因。
4. 承接字段冲突：上一组 continuity_out 与下一组 continuity_in 描述的状态互相矛盾。
5. 时间线错误：时间倒流、跳段，或与既定时长明显不符。

【以下一律不算问题，禁止修改】
- 措辞风格、表达喜好、镜头美学倾向；
- 可以有多种合理解读、不构成硬冲突的描述；
- 只有引入工作区中不存在的新人物、新产品、新事实才能"修复"的问题——宁可保留原样，不得编造。

【收敛标准（到这就停，不要继续找问题）】
相邻分镜组在「人物、场景、动作状态」三要素上能对上、时间线连续、台词因果成立，即视为通过。通过时 patches 返回空数组且 passed=true。不要为了让报告显得有价值而制造修改。单次最多提出 8 条补丁，只修必要的。

通过受控补丁返回修改，不要返回整份脚本。target 只能是 script_shot 或 storyboard_group；index 从0开始；script_shot field 只能是 visual/dialogue/action/emotion/shot_type，storyboard_group field 只能是 continuity_in/continuity_out/grid_prompt。只输出 JSON：{\"passed\":false,\"summary\":\"\",\"patches\":[{\"target\":\"script_shot\",\"index\":0,\"field\":\"visual\",\"value\":\"修复后的内容\",\"reason\":\"修复原因\"}]}。\n工作区：""" + json.dumps(snapshot, ensure_ascii=False)[:60000]
        result = await asyncio.to_thread(call_qwen_text_json, prompt, cfg, 0.2, 3200)
        patches = _sanitize_agent_patches(result, workbench)
        passed = bool(result.get("passed")) and not patches
        return {"action": action, "passed": passed,
                "summary": str(result.get("summary") or "连续性检查完成")[:500],
                "patches": patches, "audit": audit, "model_used": True}
    finally:
        agent_slot.release()


@app.get("/api/asr-media/{token}")
def asr_media(token: str):
    """供百炼在短时间内拉取拆解音轨；随机令牌过期后立即失效。"""
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise HTTPException(404, "Not found")
    with _asr_media_lock:
        entry = _asr_media.get(token)
    if not entry or entry[1] < time.time() or not os.path.isfile(entry[0]):
        raise HTTPException(404, "Not found")
    return FileResponse(entry[0], media_type="audio/mpeg", filename="track.mp3")


@app.post("/api/creative/deconstruct")
async def creative_deconstruct(
    request: Request,
    file: UploadFile | None = File(None),
    subtitle: UploadFile | None = File(None),
    url: str = Form(""),
    subtitle_text: str = Form(""),
    analysis: str = Form(""),
    _code: None = Depends(require_code),
):
    """电商二创第一步：FFmpeg 临时拆镜头，百炼返回带时间轴台词。

    支持两种来源：上传本地视频文件（file），或粘贴视频链接（url）。
    链接来源时服务器直接从链接读取原片并拆解，全程无需用户上传本地视频。
    """
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    charge_daily_cost(0.60)  # 拆镜理解含 ASR + 多次模型调用，最重的一步
    tmpdir = tempfile.mkdtemp(prefix="vinsight_creative_")
    audio_path = os.path.join(tmpdir, "track.mp3")
    token = uuid.uuid4().hex
    creative_slot = None
    try:
        total = 0
        video_digest = hashlib.md5()
        if url and url.strip() and file is None:
            # 链接来源：服务器直接从链接读取原片，全程无需上传本地视频。
            if len(url) > 4096:
                raise HTTPException(400, "视频链接过长，请粘贴原始分享链接")
            try:
                _, title, vpath = await asyncio.to_thread(
                    resolver.download_video, url.strip(), tmpdir, FFMPEG,
                    cfg.get("xhs_cookie", ""),
                )
            except resolver.ResolveError as exc:
                raise HTTPException(400, str(exc))
            except Exception as exc:
                raise HTTPException(400, f"视频下载失败：{exc}")
            if not vpath or not os.path.isfile(vpath) or os.path.getsize(vpath) == 0:
                raise HTTPException(400, "视频下载失败：未获取到有效文件，请稍后重试或改用上传")
            if os.path.getsize(vpath) > min(MAX_VIDEO_BYTES, 300 * 1024 * 1024):
                raise HTTPException(413, "深度拆解视频暂限 300MB 以内")
            suffix = Path(vpath).suffix.lower() or ".mp4"
            video_path = vpath
            video_digest.update(("url:" + url.strip()).encode("utf-8"))
        elif file is not None:
            suffix = Path(file.filename or "video.mp4").suffix.lower()
            if suffix not in {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi"}:
                raise HTTPException(400, "请上传 MP4、MOV、WebM、MKV 或 AVI 视频")
            video_path = os.path.join(tmpdir, "source" + suffix)
            with open(video_path, "wb") as output:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > min(MAX_VIDEO_BYTES, 300 * 1024 * 1024):
                        raise HTTPException(413, "深度拆解视频暂限 300MB 以内")
                    video_digest.update(chunk)
                    output.write(chunk)
        else:
            raise HTTPException(400, "请先上传视频文件或粘贴视频链接")
        duration = await asyncio.to_thread(get_duration, video_path)
        if not duration or duration <= 0:
            raise HTTPException(400, "无法读取视频，请转换为 MP4（H.264/AAC）后重试")
        if duration > 30 * 60:
            raise HTTPException(400, "电商二创深度拆解暂支持 30 分钟以内视频")
        srt_raw = await subtitle.read() if subtitle and subtitle.filename else b""
        if not srt_raw and subtitle_text.strip():
            srt_raw = subtitle_text.encode("utf-8")
        srt_segments = _parse_srt(srt_raw) if srt_raw else []
        # 同一视频（含相同字幕）在缓存期内直接复用拆解结果：
        # 省去 FFmpeg 全片拆镜 + 语音转写 + 剧情理解三段最重的耗时。
        video_digest.update(b"|srt|")
        video_digest.update(hashlib.md5(srt_raw).digest())
        cache_key = "cdec:" + video_digest.hexdigest()
        cached = _cache_get(cache_key, _creative_cache)
        if cached is not None:
            return _with_cache_meta(cached)
        # 全片拆镜是 CPU 密集重活，走独立的 creative 并发池防止 OOM
        creative_slot = await acquire_analysis_slot("creative")
        scenes_task = asyncio.to_thread(_creative_scene_frames, video_path, tmpdir, duration)
        audio_ok = False
        if srt_segments:
            scenes = await scenes_task
        else:
            audio_task = asyncio.to_thread(
                subprocess.run,
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", video_path,
                 "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-y", audio_path],
                capture_output=True, timeout=120,
            )
            scenes, audio_run = await asyncio.gather(scenes_task, audio_task)
            audio_ok = bool(audio_run and audio_run.returncode == 0 and
                            os.path.isfile(audio_path) and os.path.getsize(audio_path) > 256)

        transcript = {"text": "", "segments": []}
        asr_warning = ""
        cacheable = True  # 降级产生的残缺结果不允许进缓存，否则重试无法自愈
        segments = srt_segments or []

        # 剧情理解与语音转写并行：理解先用画面+镜头结构跑（字幕留空），
        # 转写完成后用字幕证据覆盖校准说话人，整体省掉一次串行等待。
        base_analysis = json.loads(analysis) if analysis and len(analysis) <= 500_000 else {}
        base_analysis = base_analysis if isinstance(base_analysis, dict) else {}
        understanding_task = asyncio.create_task(asyncio.to_thread(
            _creative_understanding, scenes, segments, cfg, base_analysis))

        if not srt_segments and audio_ok:
            with _asr_media_lock:
                _asr_media[token] = (audio_path, time.time() + 600)
            media_url = str(request.url_for("asr_media", token=token))
            forwarded_proto = request.headers.get("x-forwarded-proto", "")
            if forwarded_proto == "https" and media_url.startswith("http://"):
                media_url = "https://" + media_url[7:]
            try:
                transcript = await asyncio.to_thread(transcribe_remote_audio_details, media_url, cfg)
            except Exception as exc:
                logger.warning("creative_asr_fallback reason=%s", exc)
                asr_warning = "台词识别暂时失败，镜头拆解已保留，可稍后重试。"
                cacheable = False
        elif not srt_segments:
            asr_warning = "视频没有可识别音轨，已完成镜头拆解。"

        segments = srt_segments or transcript.get("segments") or []
        for scene in scenes:
            scene["dialogue"] = " ".join(
                seg["text"] for seg in segments
                if seg.get("end_ms", 0) / 1000 > scene["start"] and
                seg.get("start_ms", 0) / 1000 < scene["end"]
            ).strip()
        understanding = {}
        understanding_warning = ""
        try:
            understanding = await understanding_task
            understanding = _apply_explicit_speaker_evidence(segments, understanding)
            segments = _merge_speaker_calibration(segments, understanding)
        except Exception as exc:
            logger.warning("creative_understanding_fallback reason=%s", exc)
            understanding = _apply_explicit_speaker_evidence(
                segments, _understanding_from_analysis(base_analysis, segments))
            segments = _merge_speaker_calibration(segments, understanding)
            understanding_warning = "深层剧情理解暂未完成，已保留可核验的字幕人物和基础结构。"
            cacheable = False
        result = {
            "duration": round(duration, 2), "shots": scenes,
            "transcript": segments, "transcript_text": " ".join(x.get("text", "") for x in segments),
            "transcript_source": "srt" if srt_segments else "asr",
            "understanding": understanding,
            "warning": " ".join(x for x in (asr_warning if not srt_segments else "", understanding_warning) if x),
            "processing": "优先使用 SRT；否则由语音模型转写。FFmpeg 提取镜头，AI 校准说话人与剧情结构；临时文件完成后删除。",
        }
        # 只有完整成功才写缓存；降级结果（转写/理解失败）不缓存，保证重试可自愈
        if cacheable:
            _cache_put(cache_key, result, _creative_cache)
        return result
    finally:
        if creative_slot is not None:
            creative_slot.release()
        with _asr_media_lock:
            _asr_media.pop(token, None)
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/creative/workbench")
async def creative_workbench(
    payload: dict = Body(...),
    _code: None = Depends(require_code),
):
    """根据当前阶段受控生成新脚本、分镜改造方案或视频提示词。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(payload_json) > 18_000_000:
        raise HTTPException(413, "素材数据过大，请减少图片数量或压缩图片")
    phase = str(payload.get("phase") or "")
    if phase not in {"script", "storyboard", "prompts"}:
        raise HTTPException(400, "不支持的生成阶段")
    charge_daily_cost(0.10 if phase in {"script", "storyboard"} else 0.05)
    cache_key = "workbench:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    cached = _cache_get(cache_key)
    if cached is not None:
        return _with_cache_meta(cached, phase=phase)
    context = {
        "analysis": _analysis_for_agent(payload.get("analysis") or {}),
        "shots": [{k: item.get(k) for k in ("shot", "start", "end", "dialogue")}
                  for item in ((payload.get("deconstruction") or {}).get("shots") or [])[:40]
                  if isinstance(item, dict)],
        "transcript": (payload.get("deconstruction") or {}).get("transcript", [])[:80],
        "assets": [{k: item.get(k) for k in ("id", "name", "type", "hidden")}
                   for item in (payload.get("assets") or [])[:12] if isinstance(item, dict)],
        "requirements": str(payload.get("requirements") or "")[:4000],
        "script": payload.get("script") or {},
        "storyboard": payload.get("storyboard") or [],
        "target_duration": int(payload.get("target_duration") or 15),
        "target_total_seconds": max(60, min(420, int(payload.get("target_total_seconds") or 120))),
        "variation": payload.get("variation") or {},
        "reference_script": str(payload.get("reference_script") or "")[:6000],
        "understanding": (payload.get("deconstruction") or {}).get("understanding") or {},
    }
    # 两分钟视频必须按用户选择的每组时长完整覆盖：10/15/30 秒分别为
    # 12/8/4 组。旧逻辑写死最多 4 组，导致 2 分钟、每组 15 秒时只生成
    # 约 60 秒内容。单次请求最多 12 组，足以完整覆盖当前 2 分钟档。
    expected_storyboard_groups = max(
        1, min(12, (context["target_total_seconds"] + context["target_duration"] - 1)
               // context["target_duration"]))
    asset_images = [item.get("image", "") for item in (payload.get("assets") or [])
                    if isinstance(item, dict) and not item.get("hidden")]
    # 原片关键帧让模型真正看见镜头内容；均匀限量，避免一次请求过大。
    original_images = [item.get("image", "") for item in
                       ((payload.get("deconstruction") or {}).get("shots") or [])[::4]
                       if isinstance(item, dict)]
    if phase == "script":
        images = (original_images[:2] + asset_images[:4])[:6]
        context.pop("storyboard", None)
    elif phase == "storyboard":
        images = asset_images[:6]
        for key in ("analysis", "shots", "transcript", "reference_script", "understanding"):
            context.pop(key, None)
    else:
        images = []
        for key in ("analysis", "shots", "transcript", "reference_script", "understanding", "requirements"):
            context.pop(key, None)
    if phase == "script":
        instruction = """你是短剧情裂变编剧。以目标总时长重新规划剧情，不照抄原片；根据用户选择决定是否换产品、场景、人物或冲突模式，并可参考成熟脚本的结构但不得复制原文。说话人必须继承已校准人物；待确认台词不得擅自归属。完整脚本要写清剧情单元承接、出场人物、人物关系、服装、道具、情绪动作、产品植入方式。另生成一份与新视频匹配的通用发布配文，不指定小红书、抖音等平台。输出 JSON：{\"role_profiles\":[{\"asset_id\":\"\",\"name\":\"\",\"identity\":\"\",\"personality\":\"\",\"appearance\":\"\",\"wardrobe_by_unit\":[{\"unit\":\"\",\"wardrobe\":\"\"}]}],\"product_profiles\":[{\"asset_id\":\"\",\"name\":\"\",\"features\":\"\",\"placement_strategy\":\"\"}],\"script\":{\"title\":\"\",\"creative_angle\":\"\",\"target_seconds\":120,\"story_units\":[{\"unit\":1,\"purpose\":\"\",\"transition\":\"\",\"characters\":[],\"wardrobe\":\"\",\"props\":[],\"product_placement\":\"\"}],\"shots\":[{\"shot\":1,\"unit\":1,\"start\":0,\"end\":10,\"speaker\":\"\",\"emotion\":\"\",\"action\":\"\",\"shot_type\":\"\",\"visual\":\"\",\"dialogue\":\"\",\"asset_ids\":[\"\"]}]},\"post_copy\":{\"titles\":[\"标题1\",\"标题2\",\"标题3\"],\"body\":\"与新视频内容一致的简洁发布配文\",\"tags\":[\"标签\"]}}。"""
    elif phase == "storyboard":
        instruction = f"""你是连续分镜导演。把完整脚本严格拆成 {expected_storyboard_groups} 个连续分镜组，组号必须从 1 连续到 {expected_storyboard_groups}。时间必须从0秒开始连续递增，禁止跳段、倒序或重叠。每组只能使用与本组时间重叠的脚本镜头、人物和台词，台词必须原样引用对应脚本镜头，不得把后面剧情提前或把前面剧情挪到后面。每一组必须恰好规划 9 个连续画面（不是最多9格），九格内部按动作发生顺序推进，末格必须能自然接上下一组首格；不得添加脚本没有的新人物、新产品、新事件或重复凑数。每格都必须有可拍摄的 visual，并包含景别变化、前后连续动作、实际出场人物、服装、场景和产品素材引用。不要声称已经生成图片。每格 visual 控制在 15~25 字，只写可直接拍摄的画面要点。只输出 JSON：{{\"storyboard\":[{{\"group\":1,\"time\":\"0-15s\",\"duration\":15,\"unit\":1,\"source_shots\":[1],\"asset_ids\":[\"\"],\"continuity_in\":\"\",\"continuity_out\":\"\",\"panels\":[{{\"panel\":1,\"source_shot\":1,\"shot_size\":\"全景/中景/近景/特写\",\"visual\":\"可直接拍摄的具体画面\",\"speaker\":\"\",\"dialogue\":\"\",\"emotion_action\":\"\"}}],\"grid_prompt\":\"完整3×3九宫格生图提示词\",\"negative_prompt\":\"\"}}]}}。"""
    else:
        instruction = """你是视频生成提示词编排器。必须按分镜组编号逐组生成，一组提示词只允许引用同编号 source_group 的九宫格、人物、场景、产品和台词，禁止跨组挪用、倒序、提前剧透或改写人物台词。提示词里的动作顺序必须与九宫格1到9格一致，并写清承接上一组的起始状态与交给下一组的结束状态。人物音频没有真实素材时标记 voice_status=missing，这只表示后期需要配音，不影响画面提示词。输出组数必须与输入分镜组数相同且顺序完全一致。输出 JSON：{\"video_prompts\":[{\"group\":1,\"source_groups\":[1],\"time\":\"0-15s\",\"duration\":15,\"asset_ids\":[\"\"],\"speakers\":[\"\"],\"voice_status\":\"ready/missing\",\"prompt\":\"严格按本组九宫格顺序，包含主体、动作、原台词、运镜、场景、产品、节奏、转场和声音的可执行提示词\"}]}。"""
    if phase == "script":
        instruction += """\n真实性硬规则：只能引用 assets 中真实存在的 asset_id；没有 product 类型素材时 product_profiles 和 product_placement 必须为空，台词与画面不得虚构产品、品牌、价格、人物履历或原片未提供的事实；信息不足时使用中性描述并标记待确认。"""
        instruction += """\n镜头字段硬规则：每个 shot_type 必须写成“景别 · 机位/运镜”（如“中景 · 平视跟拍”）；每个 visual 必须描述可直接拍摄的新画面、主体动作及与前后镜头的衔接。禁止填写“待补充”“待确认”或“参考原镜头重新设计画面”。"""
        instruction += """\n台词创作硬规则：每个镜头的 dialogue 必须写出一至两句可直接配音的新台词——台词要与该镜头 visual 的动作严格对应，说话人必须引用已校准人物或 role_profiles 中的真实素材人物，语气贴合人物情绪；只有纯动作、无对白也无旁白的镜头才允许留空，留空镜头不得超过总数的三分之一。禁止填写“待补充”“同原片”或照抄原片台词。"""
        planned_shots = max(6, min(24, (context["target_total_seconds"] + 14) // 15))
        instruction += f"\n控制篇幅：约 {planned_shots} 个镜头；每个字段只写一到两句必要信息，避免重复服装和背景描写。"
    prompt = instruction + "\n用户当前工作区数据：" + json.dumps(context, ensure_ascii=False)[:65000]
    if phase == "script":
        token_limit = 2300 if context["target_total_seconds"] <= 120 else (
            3200 if context["target_total_seconds"] <= 300 else 4096)
    elif phase == "storyboard":
        token_limit = min(8192, max(4200, expected_storyboard_groups * 1200))
    else:
        token_limit = 1800
    # 第 1 步已完成原片视觉理解，后续三步只消费结构化脚本数据。
    # 真正生成九宫格时才把参考图交给视觉模型，避免重复看图造成慢和超时。
    result = await asyncio.to_thread(
        call_qwen_text_json, prompt, cfg, 0.2, token_limit)
    if phase == "script":
        result = _sanitize_creative_script(result, payload.get("assets") or [],
                                           context["target_total_seconds"])
    elif phase == "storyboard":
        result = _align_storyboard_to_script(
            result, context.get("script") or {}, context["target_duration"],
            context["target_total_seconds"])
    elif phase == "prompts":
        result = _align_video_prompts(result, context.get("storyboard") or [])
    try:
        expected = (expected_storyboard_groups if phase == "storyboard" else
                    [int(row.get("group") or index + 1)
                     for index, row in enumerate(context.get("storyboard") or [])
                     if isinstance(row, dict)] if phase == "prompts" else None)
        final_result = _validate_creative_phase(phase, result, expected)
    except HTTPException:
        repair_prompt = prompt + "\n上一次结果字段不完整。请重新输出完整结果：所有目标分镜组都要齐全，每个九宫格必须恰好 9 个非空画面，视频提示词必须覆盖全部来源组；新脚本必须为绝大多数镜头写出与素材人物对应的新台词。只返回 JSON。"
        repaired = await asyncio.to_thread(
            call_qwen_text_json, repair_prompt, cfg, 0.2, token_limit)
        if phase == "script":
            repaired = _sanitize_creative_script(repaired, payload.get("assets") or [],
                                                 context["target_total_seconds"])
        elif phase == "storyboard":
            repaired = _align_storyboard_to_script(
                repaired, context.get("script") or {}, context["target_duration"],
                context["target_total_seconds"])
        elif phase == "prompts":
            repaired = _align_video_prompts(repaired, context.get("storyboard") or [])
        final_result = _validate_creative_phase(phase, repaired, expected)
    _cache_put(cache_key, final_result)
    return final_result


@app.post("/api/creative/storyboard-grid/quote")
async def creative_storyboard_grid_quote(
    request: Request,
    payload: dict = Body(...),
    _code: None = Depends(require_code),
):
    """在调用付费图片模型前返回体验余额与透明平台报价。"""
    refs = min(3, max(0, int(payload.get("reference_count") or 0)))
    with _grid_usage_lock:
        return quota_identity.quote(_grid_usage, quota_identity.visitor_keys(request),
                                    _grid_model_cost(refs), GRID_FREE_LIMIT_CNY,
                                    GRID_DAILY_BUDGET_CNY, GRID_SERVICE_FEE_CNY)


@app.post("/api/creative/storyboard-grid")
async def creative_storyboard_grid(
    request: Request,
    payload: dict = Body(...),
    _code: None = Depends(require_code),
):
    """用户确认费用后，为一个新分镜组生成真正的二创九宫格。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    if len(json.dumps(payload, ensure_ascii=False)) > 6_000_000:
        raise HTTPException(413, "参考图片过大，请减少素材或压缩后重试")
    board = payload.get("storyboard") or {}
    if not isinstance(board, dict):
        raise HTTPException(400, "缺少分镜组")
    # 九宫格生图最慢也最贵：按分镜组+可见素材指纹缓存，重复生成/失败重试秒回。
    grid_digest = hashlib.md5()
    grid_digest.update(json.dumps(board, ensure_ascii=False, sort_keys=True,
                                  default=str)[:120000].encode("utf-8"))
    for item in (payload.get("assets") or []):
        if isinstance(item, dict) and not item.get("hidden"):
            image = item.get("image") or ""
            if isinstance(image, str) and image.startswith("data:image/"):
                grid_digest.update(b"|")
                grid_digest.update(hashlib.md5(image.encode("utf-8")).digest())
    cache_key = "csb:" + grid_digest.hexdigest()
    cached = _cache_get(cache_key, _creative_cache)
    if cached is not None:
        return _with_cache_meta(cached)
    active_refs = [item for item in (payload.get("assets") or [])
                   if isinstance(item, dict) and not item.get("hidden") and
                   isinstance(item.get("image"), str) and
                   item.get("image", "").startswith("data:image/")]
    model_cost = _grid_model_cost(min(3, len(active_refs)))
    client_keys = quota_identity.visitor_keys(request)
    with _grid_usage_lock:
        usage = quota_identity.reserve(_grid_usage, client_keys, model_cost,
                                       GRID_FREE_LIMIT_CNY, GRID_DAILY_BUDGET_CNY,
                                       GRID_SERVICE_FEE_CNY)
        if usage["reservation"] == "denied":
            raise HTTPException(402, detail={"code": "grid_payment_required",
                                            "message": "免费体验额度已用完，完成本次付款核验后即可继续生成。",
                                            **usage})
        _save_grid_usage_locked()
    try:
        result = await asyncio.to_thread(
            _generate_storyboard_grid, board, payload.get("assets") or [], cfg)
    except Exception:
        with _grid_usage_lock:
            quota_identity.release(_grid_usage, client_keys, model_cost,
                                   usage.get("reservation", "free"))
            _save_grid_usage_locked()
        raise
    result["usage"] = usage
    _cache_put(cache_key, result, _creative_cache)
    return result


def _validated_export_report(payload: dict) -> dict:
    report = payload.get("report") if isinstance(payload, dict) else None
    if not isinstance(report, dict) or not str(report.get("title") or "").strip():
        raise HTTPException(400, "没有可导出的报告")
    if len(json.dumps(report, ensure_ascii=False)) > 12_000_000:
        raise HTTPException(413, "报告内容过大")
    return report


def _export_image_bytes(report: dict, limit: int = 6) -> list[bytes]:
    """只接受前端当前工作区传入的受限 data URL，不抓取任何外部图片。"""
    images: list[bytes] = []
    total = 0
    for value in (report.get("_export_images") or [])[:limit]:
        if not isinstance(value, str):
            continue
        match = re.match(r"^data:image/(?:jpeg|jpg|png|webp);base64,([A-Za-z0-9+/=]+)$", value)
        if not match:
            continue
        try:
            raw = base64.b64decode(match.group(1), validate=True)
        except Exception:
            continue
        if not 1024 <= len(raw) <= 2_000_000 or total + len(raw) > 8_000_000:
            continue
        images.append(raw)
        total += len(raw)
    return images


def _workbench_export(report: dict) -> dict:
    value = report.get("creative_workbench") or {}
    return value if isinstance(value, dict) else {}


def _build_docx(report: dict) -> io.BytesIO:
    """生成包含完整分析、创作成果与真实参考图的原生 DOCX。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    styles = doc.styles
    styles["Normal"].font.name = "Microsoft YaHei"
    styles["Normal"].font.size = Pt(10.5)
    title = doc.add_heading(str(report.get("title") or "视频分析报告"), 0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.add_run("VideoInsight · 完整成果报告").font.color.rgb = RGBColor(91, 92, 226)

    def heading(name: str):
        doc.add_heading(name, level=1)

    def bullets(items):
        for item in items or []:
            doc.add_paragraph(str(item), style="List Bullet")

    if report.get("overall_summary"):
        heading("总体总结")
        doc.add_paragraph(str(report["overall_summary"]))
    heading("关键信息")
    bullets(report.get("key_info"))
    if report.get("chapters"):
        heading("章节时间轴")
        table = doc.add_table(rows=1, cols=2)
        table.style = "Light Shading Accent 1"
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "时间", "内容"
        for item in report.get("chapters") or []:
            cells = table.add_row().cells
            cells[0].text = str(item.get("time") or "")
            cells[1].text = str(item.get("label") or item.get("title") or "")
    if report.get("speech_summary"):
        heading("语音内容摘要")
        doc.add_paragraph(str(report["speech_summary"]))

    course = report.get("course") or {}
    if course:
        heading("课程整理")
        doc.add_heading("学习目标", level=2); bullets(course.get("learning_objectives"))
        doc.add_heading("课程大纲", level=2)
        for item in course.get("outline") or []:
            doc.add_paragraph(str(item.get("title") or "课程章节"), style="Heading 3")
            bullets(item.get("points"))
        doc.add_heading("课件与讲师备注", level=2)
        for index, item in enumerate(course.get("slides") or [], 1):
            doc.add_paragraph(f"{index}. {item.get('title') or '课程内容'}", style="Heading 3")
            bullets(item.get("bullets"))
            if item.get("speaker_notes"):
                doc.add_paragraph("讲师备注：" + str(item["speaker_notes"]))

    creative = report.get("creative") or {}
    if creative:
        heading("二创内容包")
        for name, value in (creative.get("scripts") or {}).items():
            doc.add_heading(f"{name} 脚本", level=2); doc.add_paragraph(str(value))
        doc.add_heading("精彩片段建议", level=2)
        for item in creative.get("highlights") or []:
            doc.add_paragraph(f"{item.get('start','')}–{item.get('end','')} {item.get('title','')}：{item.get('reason','')}", style="List Bullet")
        copy = creative.get("post_copy") or creative.get("xiaohongshu") or {}
        if copy:
            doc.add_heading("视频配文案", level=2)
            doc.add_paragraph(str(copy.get("body") or ""))
            doc.add_paragraph(" ".join("#" + str(x) for x in copy.get("tags") or []))

    workbench = _workbench_export(report)
    script = (workbench.get("script") or {}).get("script") or {}
    if script:
        heading("二创工作台 · 新脚本")
        doc.add_heading(str(script.get("title") or "新脚本"), level=2)
        if script.get("creative_angle"): doc.add_paragraph(str(script["creative_angle"]))
        table = doc.add_table(rows=1, cols=5); table.style = "Light Shading Accent 1"
        for cell, value in zip(table.rows[0].cells, ["镜头", "时间", "人物/情绪", "画面", "台词"]): cell.text = value
        for item in (script.get("shots") or [])[:80]:
            cells = table.add_row().cells
            values = [item.get("shot", ""), f"{item.get('start','')}–{item.get('end','')}s",
                      " / ".join(str(item.get(k) or "") for k in ("speaker", "emotion")),
                      item.get("visual", ""), item.get("dialogue", "")]
            for cell, value in zip(cells, values): cell.text = str(value)
    if workbench.get("storyboard"):
        heading("二创工作台 · 分镜方案")
        for item in workbench["storyboard"][:50]:
            doc.add_heading(f"第 {item.get('group') or item.get('shot') or ''} 组 · {item.get('time','')}", level=2)
            for panel in item.get("panels") or []:
                doc.add_paragraph(f"{panel.get('panel','')}. {panel.get('shot_size','')}｜{panel.get('visual','')}｜{panel.get('dialogue','')}", style="List Bullet")
            if item.get("grid_prompt"): doc.add_paragraph("生图提示词：" + str(item["grid_prompt"]))
    if workbench.get("prompts"):
        heading("二创工作台 · 视频提示词")
        for item in workbench["prompts"][:50]:
            doc.add_heading(f"第 {item.get('group','')} 组 · {item.get('duration','')} 秒", level=2)
            doc.add_paragraph(str(item.get("prompt") or ""))

    images = _export_image_bytes(report)
    if images:
        heading("真实参考画面")
        for index, raw in enumerate(images, 1):
            try:
                doc.add_picture(io.BytesIO(raw), width=Inches(5.8))
                caption = doc.add_paragraph(f"参考图 {index}")
                caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
            except Exception:
                continue
    output = io.BytesIO(); doc.save(output); output.seek(0)
    return output


def _build_pptx(report: dict) -> io.BytesIO:
    """生成原生 Office Open XML 演示文稿，而不是伪装成 PPT 的 HTML。"""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.core_properties.title = str(report.get("title") or "视频课程课件")[:200]
    title_slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    title_slide.shapes.title.text = str(report.get("title") or "视频课程课件")
    title_slide.placeholders[1].text = "VideoInsight · 课程与讲座复盘"

    overview = presentation.slides.add_slide(presentation.slide_layouts[1])
    overview.shapes.title.text = "课程概览与核心收获"
    overview_frame = overview.placeholders[1].text_frame
    overview_frame.clear()
    overview_items = ([str(report.get("overall_summary"))] if report.get("overall_summary") else []) + [
        str(item) for item in (report.get("key_info") or [])[:6]
    ]
    for index, value in enumerate(overview_items or ["暂无课程概览"]):
        paragraph = overview_frame.paragraphs[0] if index == 0 else overview_frame.add_paragraph()
        paragraph.text = value[:500]; paragraph.font.size = Pt(22 if index == 0 else 19)

    course = report.get("course") or {}
    slides = course.get("slides") or []
    if not slides:
        slides = [{
            "title": "核心内容",
            "bullets": report.get("key_info") or [report.get("overall_summary") or "暂无内容"],
            "speaker_notes": "",
        }]
    for item in slides[:30]:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = str(item.get("title") or "课程内容")[:180]
        frame = slide.placeholders[1].text_frame
        frame.clear()
        bullets = item.get("bullets") or []
        for index, bullet in enumerate(bullets[:10]):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = str(bullet)[:500]
            paragraph.font.size = Pt(24)
        notes = str(item.get("speaker_notes") or "").strip()
        if notes:
            slide.notes_slide.notes_text_frame.text = "讲师备注：" + notes[:4000]

    images = _export_image_bytes(report)
    for offset in range(0, len(images), 4):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        title_box = slide.shapes.add_textbox(Inches(.55), Inches(.25), Inches(12.2), Inches(.55))
        title_box.text_frame.text = "视频真实参考画面"
        title_box.text_frame.paragraphs[0].font.size = Pt(24)
        for local_index, raw in enumerate(images[offset:offset + 4]):
            col, row = local_index % 2, local_index // 2
            try:
                slide.shapes.add_picture(io.BytesIO(raw), Inches(.65 + col * 6.35),
                                         Inches(1 + row * 3.15), width=Inches(5.8), height=Inches(2.75))
            except Exception:
                continue
    output = io.BytesIO()
    presentation.save(output)
    output.seek(0)
    return output


def _build_pdf(report: dict) -> io.BytesIO:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    output = io.BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("CNTitle", parent=styles["Title"], fontName="STSong-Light",
                                 fontSize=22, leading=30, alignment=TA_CENTER, textColor=colors.HexColor("#0C447C"))
    heading = ParagraphStyle("CNHeading", parent=styles["Heading2"], fontName="STSong-Light",
                             fontSize=15, leading=22, spaceBefore=12, textColor=colors.HexColor("#185FA5"))
    body = ParagraphStyle("CNBody", parent=styles["BodyText"], fontName="STSong-Light",
                          fontSize=10.5, leading=17, spaceAfter=5)
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                            topMargin=18*mm, bottomMargin=18*mm,
                            title=str(report.get("title") or "视频分析报告"))
    story = [Paragraph(html.escape(str(report.get("title") or "视频分析报告")), title_style), Spacer(1, 8)]

    def add_section(name: str, items):
        if not items:
            return
        story.append(Paragraph(html.escape(name), heading))
        if isinstance(items, str):
            story.append(Paragraph(html.escape(items).replace("\n", "<br/>"), body))
        else:
            for index, item in enumerate(items, 1):
                story.append(Paragraph(f"{index}. {html.escape(str(item))}", body))

    add_section("关键信息", report.get("key_info"))
    add_section("章节时间轴", [f"{item.get('time','')}  {item.get('label') or item.get('title') or ''}"
                               for item in (report.get("chapters") or []) if isinstance(item, dict)])
    add_section("语音内容摘要", report.get("speech_summary"))
    add_section("总体总结", report.get("overall_summary"))
    course = report.get("course") or {}
    if course:
        add_section("学习目标", course.get("learning_objectives"))
        story.append(PageBreak())
        story.append(Paragraph("课程课件与讲师备注", heading))
        for index, item in enumerate((course.get("slides") or [])[:30], 1):
            story.append(Paragraph(f"{index}. {html.escape(str(item.get('title') or '课程内容'))}", heading))
            for bullet in (item.get("bullets") or [])[:10]:
                story.append(Paragraph("• " + html.escape(str(bullet)), body))
            notes = str(item.get("speaker_notes") or "").strip()
            if notes:
                story.append(Paragraph("讲师备注：" + html.escape(notes), body))
    creative = report.get("creative") or {}
    if creative:
        add_section("多时长脚本", [f"{key}: {value}" for key, value in (creative.get("scripts") or {}).items()])
        add_section("精彩片段建议", [
            f"{item.get('start','')}–{item.get('end','')} {item.get('title','')}：{item.get('reason','')}"
            for item in (creative.get("highlights") or []) if isinstance(item, dict)])
        add_section("分镜建议", [
            f"镜头 {item.get('shot','')}（{item.get('time','')}）：{item.get('visual','')}；旁白：{item.get('narration','')}"
            for item in (creative.get("storyboard") or []) if isinstance(item, dict)])
        add_section("视频配文案", ((creative.get("post_copy") or creative.get("xiaohongshu") or {}).get("body")))
    workbench = _workbench_export(report)
    script = (workbench.get("script") or {}).get("script") or {}
    if script:
        add_section("二创新脚本", [
            f"镜头 {item.get('shot','')} {item.get('start','')}–{item.get('end','')}s｜{item.get('speaker','')}｜{item.get('visual','')}｜{item.get('dialogue','')}"
            for item in (script.get("shots") or [])[:80] if isinstance(item, dict)])
    add_section("连续分镜方案", [
        f"第 {item.get('group') or item.get('shot') or ''} 组 {item.get('time','')}：" +
        "；".join(f"{panel.get('shot_size','')} {panel.get('visual','')} {panel.get('dialogue','')}"
                 for panel in (item.get("panels") or []) if isinstance(panel, dict))
        for item in (workbench.get("storyboard") or [])[:50] if isinstance(item, dict)])
    add_section("视频生成提示词", [str(item.get("prompt") or "")
                                  for item in (workbench.get("prompts") or [])[:50]
                                  if isinstance(item, dict)])
    images = _export_image_bytes(report)
    if images:
        story.append(PageBreak()); story.append(Paragraph("真实参考画面", heading))
        for index, raw in enumerate(images, 1):
            try:
                picture = RLImage(io.BytesIO(raw)); picture._restrictSize(170 * mm, 92 * mm)
                story.extend([picture, Paragraph(f"参考图 {index}", body), Spacer(1, 5)])
            except Exception:
                continue
    doc.build(story)
    output.seek(0)
    return output


@app.post("/api/export/pptx")
async def export_pptx(payload: dict = Body(...), _code: None = Depends(require_code)):
    report = _validated_export_report(payload)
    output = await asyncio.to_thread(_build_pptx, report)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": 'attachment; filename="video-course.pptx"'},
    )


@app.post("/api/export/docx")
async def export_docx(payload: dict = Body(...), _code: None = Depends(require_code)):
    report = _validated_export_report(payload)
    output = await asyncio.to_thread(_build_docx, report)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="video-report.docx"'},
    )


@app.post("/api/export/pdf")
async def export_pdf(payload: dict = Body(...), _code: None = Depends(require_code)):
    report = _validated_export_report(payload)
    output = await asyncio.to_thread(_build_pdf, report)
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="video-report.pdf"'},
    )


@app.get("/api/health")
def health():
    """Render 健康检查专用，不依赖外部模型或视频平台。"""
    return {"ok": True, "service": "video-insight"}


@app.post("/api/payment/order")
async def payment_order(request: Request, payload: dict = Body(default={}),
                        _code: None = Depends(require_code)):
    """锁定本次生成的应付金额：价格由服务端按参考素材数计算，客户端不可自报金额。"""
    _require_durable_payment_store()
    try:
        refs = max(0, min(3, int(payload.get("reference_count") or 0)))
    except (TypeError, ValueError):
        refs = 0
    amount = round(_grid_model_cost(refs) + GRID_SERVICE_FEE_CNY, 2)
    order_id = "VI" + uuid.uuid4().hex[:8].upper()
    keys = quota_identity.visitor_keys(request)
    with _grid_usage_lock:
        _grid_usage.setdefault("orders", {})[order_id] = {
            "order_id": order_id, "amount_cny": amount, "status": "pending",
            "keys": keys, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_grid_usage_locked()
    return {"order_id": order_id, "amount_cny": amount}


@app.post("/api/payment/proof")
async def payment_proof(
    order_id: str = Form(...),
    screenshot: UploadFile = File(...),
    _code: None = Depends(require_code),
):
    """用户上传微信付款截图；保存并推送提醒，由管理员人工核对金额后开通。"""
    _require_durable_payment_store()
    if not _ORDER_ID_RE.fullmatch(order_id or ""):
        raise HTTPException(400, "订单号格式不正确")
    content_type = (screenshot.content_type or "").lower()
    if content_type not in ("image/png", "image/jpeg", "image/webp"):
        raise HTTPException(400, "请上传 PNG / JPG / WebP 格式的付款截图")
    raw = await screenshot.read()
    if not 1024 <= len(raw) <= 5 * 1024 * 1024:
        raise HTTPException(400, "截图大小需在 1KB~5MB 之间")
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[content_type]
    proof_hash = hashlib.sha256(raw).hexdigest()
    with _grid_usage_lock:
        order = _grid_usage.setdefault("orders", {}).get(order_id)
        if not order:
            raise HTTPException(404, "订单不存在，请返回重新发起生成")
        if order.get("status") == "approved":
            raise HTTPException(400, "该订单已开通，无需重复提交")
        duplicate = next((item for oid, item in _grid_usage.get("orders", {}).items()
                          if oid != order_id and item.get("proof_sha256") == proof_hash), None)
        filename = f"{order_id}.{ext}"
        PAYMENT_STORE.proof_dir = PAYMENT_PROOF_DIR
        PAYMENT_STORE.save_proof(order_id, filename, content_type, proof_hash, raw)
        order["status"] = "reviewing"
        order["proof"] = filename
        order["proof_sha256"] = proof_hash
        order["auto_review"] = {"verified": False, "reason": "相同截图已提交到其他订单"} \
            if duplicate else {"verified": False, "reason": "等待自动识别"}
        _save_grid_usage_locked()
        order_snapshot = dict(order)
    if PAYMENT_AUTO_REVIEW and not duplicate:
        try:
            review = await asyncio.to_thread(_analyze_payment_proof, raw, content_type, order_snapshot)
            with _grid_usage_lock:
                order = _grid_usage.setdefault("orders", {}).get(order_id)
                if order:
                    order["auto_review"] = review
                    if review["verified"]:
                        _grant_payment_credit_locked(order, "payment-agent")
                    _save_grid_usage_locked()
        except Exception as exc:
            logger.warning("payment_auto_review_failed order=%s reason=%s", order_id,
                           type(exc).__name__)
            with _grid_usage_lock:
                order = _grid_usage.setdefault("orders", {}).get(order_id)
                if order:
                    order["auto_review"] = {"verified": False,
                        "reason": "自动识别暂不可用，已转人工审核"}
                    _save_grid_usage_locked()
    _notify_payment_order(order)
    approved = order.get("status") == "approved"
    return {"ok": True, "status": order.get("status"),
            "message": ("截图信息已自动核验通过，本次生成已开通。" if approved else
                        "付款信息已收到，核验完成后即可继续生成；其他功能可以正常使用。")}


@app.get("/api/payment/orders")
def payment_orders(token: str = ""):
    """审核后台：列出全部订单（需管理员口令）。"""
    _require_payment_admin(token)
    _require_durable_payment_store()
    with _grid_usage_lock:
        orders = [dict(v, order_id=k) if "order_id" not in v else dict(v)
                  for k, v in _grid_usage.get("orders", {}).items()]
    orders.sort(key=lambda x: str(x.get("created") or ""), reverse=True)
    return {"orders": orders}


@app.get("/api/payment/proof/{order_id}")
def payment_proof_file(order_id: str, token: str = ""):
    """审核后台：查看订单对应的付款截图（需管理员口令）。"""
    _require_payment_admin(token)
    _require_durable_payment_store()
    if not _ORDER_ID_RE.fullmatch(order_id or ""):
        raise HTTPException(400, "订单号格式不正确")
    PAYMENT_STORE.proof_dir = PAYMENT_PROOF_DIR
    proof = PAYMENT_STORE.get_proof(order_id)
    if not proof:
        raise HTTPException(404, "该订单还没有上传截图")
    raw, media, filename = proof
    return Response(content=raw, media_type=media,
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


@app.post("/api/payment/review")
async def payment_review(payload: dict = Body(...)):
    """管理员审核：批准即按订单绑定身份加 1 次生成额度（换设备/换网络仍有效）。"""
    _require_payment_admin(str(payload.get("token") or ""))
    _require_durable_payment_store()
    order_id = str(payload.get("order_id") or "")
    action = str(payload.get("action") or "")
    if not _ORDER_ID_RE.fullmatch(order_id):
        raise HTTPException(400, "订单号格式不正确")
    with _grid_usage_lock:
        order = _grid_usage.setdefault("orders", {}).get(order_id)
        if not order:
            raise HTTPException(404, "订单不存在")
        if action == "approve":
            _grant_payment_credit_locked(order, "admin")
        elif action == "reject":
            order["status"] = "rejected"
            order["reviewed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            raise HTTPException(400, "action 仅支持 approve / reject")
        _save_grid_usage_locked()
    return {"ok": True, "status": order["status"]}


def readiness_checks() -> dict:
    """Check dependencies required to accept real analysis traffic.

    payment_store 失败只作为信息项展示，不计入 ready 判定：
    数据库未就绪时付款接口自行降级报错，但解析主流程必须照常服务，
    否则健康检查 503 会让整个部署被 Render 回滚。
    """
    payment_store_ready = True
    if PAYMENT_REQUIRE_DURABLE:
        try:
            _require_durable_payment_store()
        except HTTPException:
            payment_store_ready = False
            logger.warning("durable_payment_store_unreachable — "
                           "付款接口将不可用，解析主流程不受影响")
    return {
        "api_key": bool(load_config().get("api_key")),
        "ffmpeg": bool(FFMPEG and os.path.isfile(FFMPEG)),
        "static": STATIC_DIR.joinpath("index.html").is_file(),
        "payment_store": payment_store_ready,
    }


@app.get("/api/ready")
def ready():
    """Deployment readiness: 503 means keep this instance out of traffic.

    payment_store 仅作信息展示，不计入 ready 判定：数据库未就绪时付款
    接口自行降级报错，解析主流程照常服务，避免部署被健康检查回滚。
    """
    checks = readiness_checks()
    core_ok = bool(checks.get("api_key") and checks.get("ffmpeg") and checks.get("static"))
    return JSONResponse(
        status_code=200 if core_ok else 503,
        content={"ok": core_ok, "service": "video-insight", "checks": checks},
    )


@app.get("/api/status")
def status():
    cfg = load_config()
    return {
        "has_key": bool(cfg.get("api_key")),  # 只回传布尔值，永不暴露 Key 明文
        "model": cfg.get("model") or DEFAULT_MODEL,
        "models": MODEL_OPTIONS,
        "deploy_mode": DEPLOY_MODE,
        "need_code": bool(ACCESS_CODE) and not PUBLIC_MODE,
        "public_mode": PUBLIC_MODE,
        "daily_limit": DAILY_LIMIT,
        "max_frames": MAX_FRAMES,
        "frame_concurrency": MAX_FRAME_CONCURRENT,
        "link_concurrency": MAX_LINK_CONCURRENT,
        "version": SERVICE_VERSION,
    }


@app.post("/api/config")
async def update_config(
    api_key: str = Form(""),
    model: str = Form(""),
    xhs_cookie: str = Form(""),
    _code: None = Depends(require_code),
):
    # 公开部署版绝不接收访客的 API Key 或登录 Cookie。
    # 这既避免服务端配置被覆盖，也避免诱导用户上传敏感登录凭证。
    if DEPLOY_MODE:
        raise HTTPException(403, "公开版已统一配置 AI 服务，不接收访客的 API Key 或 Cookie")
    cfg = load_config()
    if not DEPLOY_MODE:
        if api_key:
            cfg["api_key"] = api_key.strip()
        if model:
            cfg["model"] = model.strip()
    if xhs_cookie:
        cfg["xhs_cookie"] = xhs_cookie.strip()
    save_config(cfg)
    return {"ok": True, "has_key": bool(cfg.get("api_key")),
            "model": cfg.get("model"),
            "has_xhs_cookie": bool(cfg.get("xhs_cookie"))}


@app.post("/api/analyze")
async def analyze(
    file: UploadFile | None = File(None),
    url: str = Form(None),
    mode: str = Form("standard"),
    scenario: str = Form("course"),
    _code: None = Depends(require_code),
):
    cfg = load_config()
    if url and len(url) > 4096:
        raise HTTPException(400, "视频链接过长，请粘贴原始分享链接")
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key：请点右上角「设置」填写阿里百炼 API Key，或先点「演示模式」看效果")
    # 提前归一化模式/场景，用于缓存键与命中判断
    mode_norm, _ = _analysis_context(mode)
    scenario_norm, _ = _scenario_context(scenario)
    cache_key: str | None = None
    # 本地上传走独立并发池（4 路），链接解析走下载池（2 路），互不阻塞
    analysis_slots = await acquire_analysis_slot("upload" if file is not None else "link")
    tmpdir = tempfile.mkdtemp(prefix="vinsight_video_")
    try:
        platform, title = "本地文件", ""
        remote_info = None
        if file is not None:
            # Browser supplied filenames are untrusted and must never become paths.
            original_name = Path(file.filename or "video.mp4").name
            suffix = Path(original_name).suffix.lower()
            if suffix not in {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".flv", ".ts"}:
                suffix = ".mp4"
            vpath = os.path.join(tmpdir, f"upload-{uuid.uuid4().hex}{suffix}")
            total = 0
            digest = hashlib.md5()
            with open(vpath, "wb") as f:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise HTTPException(400, "视频超过 500MB，请换一个更小的文件")
                    digest.update(chunk)
                    f.write(chunk)
            cache_key = ("file:" + digest.hexdigest() + ":"
                         + mode_norm + ":" + scenario_norm)
            cached = _cache_get(cache_key)
            if cached is not None:
                # 相同文件在缓存期内直接复用上次解析，省去抽帧 + 大模型推理
                return _with_cache_meta(cached)
        elif url and url.strip():
            cache_key = ("url:" + hashlib.md5(url.strip().encode("utf-8")).hexdigest()
                         + ":" + mode_norm + ":" + scenario_norm)
            cached = _cache_get(cache_key)
            if cached is not None:
                return _with_cache_meta(cached)
            remote_info = None
            try:
                # 长视频优先只解析媒体地址，再从远程稀疏抽帧，避免下载整段。
                remote_info = await asyncio.to_thread(
                    resolver.resolve_stream_info, url.strip(), tmpdir, True)
                platform, title, stream_url, duration, media_headers, platform_subtitle = remote_info
                if duration <= 0:
                    duration = await asyncio.to_thread(
                        get_duration, stream_url, media_headers) or 0
                vpath = ""
            except resolver.ResolveError:
                remote_info = None
            try:
                if remote_info is None:
                # 支持直接粘贴 App 复制的整段分享文案（自动提取链接，通用平台解析）
                    platform, title, vpath = await asyncio.to_thread(
                        resolver.download_video, url.strip(), tmpdir, FFMPEG,
                        cfg.get("xhs_cookie", ""),
                    )
            except resolver.ResolveError as exc:
                raise HTTPException(400, str(exc))
            except Exception as exc:
                raise HTTPException(400, f"视频下载失败：{exc}")
        else:
            raise HTTPException(400, "请先上传视频文件或粘贴视频链接")

        transcript = ""
        speech_status = "仅画面分析"
        if url and url.strip() and remote_info is not None:
            # 平台已有字幕时直接复用，省去 ASR 的等待、费用和失败点；
            # 无字幕才让抽帧与语音转写并行执行。
            frames_task = asyncio.to_thread(
                extract_remote_frames, stream_url, duration, media_headers)
            transcript = str((platform_subtitle or {}).get("text") or "")
            if transcript:
                frames = await frames_task
                speech_status = "画面 + " + str(platform_subtitle.get("source") or "平台字幕")
            else:
                async def _run_asr() -> str:
                    try:
                        return await asyncio.to_thread(
                            transcribe_remote_audio, stream_url, cfg)
                    except Exception as exc:
                        logger.warning("asr_fallback platform=%s reason=%s", platform, exc)
                        return ""

                asr_task = asyncio.create_task(_run_asr())
                frames = await frames_task
                transcript = await asr_task
                if transcript:
                    speech_status = "画面 + 语音转写"
        else:
            frames, duration = await asyncio.to_thread(extract_frames, vpath)
        if not frames:
            raise HTTPException(500, "视频抽帧失败：请确认文件是可播放的视频格式（mp4 / mov / webm 等）")
        check_daily_usage()  # 真正要调用大模型了才计数
        charge_daily_cost(_analysis_cost_estimate(mode, bool(transcript)))
        mode, _ = _analysis_context(mode)
        scenario, _ = _scenario_context(scenario)
        analysis = await asyncio.to_thread(
            call_qwen, frames, cfg, duration, mode, transcript, scenario)
        analysis["_meta"] = {
            "frames": len(frames),
            "duration": int(duration),
            "model": _analysis_model(cfg, mode),
            "platform": platform,
            "title": title,
            "mode": mode,
            "scenario": scenario,
            "speech": speech_status,
        }
        if cache_key:
            _cache_put(cache_key, analysis)
        return analysis
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        analysis_slots.release()


@app.post("/api/analyze-frames")
async def analyze_frames(
    frames: list[UploadFile] = File(...),
    duration: float = Form(...),
    filename: str = Form("course-video"),
    mode: str = Form("standard"),
    scenario: str = Form("course"),
    _code: None = Depends(require_code),
):
    """长课程专用：视频留在浏览器本地，只接收浏览器均匀抽取的 JPEG 关键帧。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    if duration <= 0:
        raise HTTPException(400, "无法读取视频时长，请换成浏览器可播放的 MP4(H.264) 视频")
    if duration > MAX_LINK_DURATION:
        raise HTTPException(400, "视频超过 3 小时，当前只支持 3 小时以内的视频")
    if not 2 <= len(frames) <= 36:
        raise HTTPException(400, "请上传 2–36 张关键帧")

    encoded: list[str] = []
    total = 0
    frame_digest = hashlib.md5()
    for frame in frames:
        raw = await frame.read()
        total += len(raw)
        if len(raw) > 2 * 1024 * 1024 or total > 30 * 1024 * 1024:
            raise HTTPException(400, "关键帧数据过大，请重新选择视频")
        content_type = (frame.content_type or "").lower()
        if content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(400, "关键帧格式不支持")
        frame_digest.update(raw)
        encoded.append(
            f"data:{content_type};base64," + base64.b64encode(raw).decode("ascii")
        )

    mode, _ = _analysis_context(mode)
    scenario, _ = _scenario_context(scenario)
    frame_limits = {"quick": 6, "standard": 18, "deep": 36}
    if scenario == "creative" and duration >= 30 * 60:
        frame_limits.update({"quick": 8, "standard": 24})
    limit = frame_limits[mode]
    if len(encoded) > limit:
        # 浏览器通常已按模式控制帧数；服务端再做一次上限保护，防止异常请求放大成本。
        picks = [round(i * (len(encoded) - 1) / (limit - 1)) for i in range(limit)]
        encoded = [encoded[i] for i in picks]
    cache_key = ("frames:" + frame_digest.hexdigest() + ":"
                 + mode + ":" + scenario)
    cached = _cache_get(cache_key)
    if cached is not None:
        return _with_cache_meta(cached)
    analysis_slots = await acquire_analysis_slot("frames")
    try:
        check_daily_usage()
        charge_daily_cost(_analysis_cost_estimate(mode, False))
        analysis = await asyncio.to_thread(
            call_qwen, encoded, cfg, duration, mode, "", scenario)
        analysis["_meta"] = {
            "frames": len(encoded),
            "duration": int(duration),
            "model": _analysis_model(cfg, mode),
            "platform": "本地文件（浏览器抽帧）",
            "title": Path(filename).name[:120],
            "mode": mode,
            "scenario": scenario,
            "speech": "仅画面分析（原视频未上传）",
        }
        _cache_put(cache_key, analysis)
        return analysis
    finally:
        analysis_slots.release()


@app.post("/api/resolve-test")
async def resolve_test(url: str = Form(...)):
    """调试接口：只做「下载视频」这一步，不调用大模型，用于快速定位链接解析是否成功。
    返回平台 / 标题 / 文件大小 / 视频时长 / 耗时。"""
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(404, "Not found")
    tmpdir = tempfile.mkdtemp(prefix="vinsight_dbg_")
    t0 = time.time()
    try:
        try:
            platform, title, stream_url, dur, headers = resolver.resolve_stream_info(
                url.strip(), tmpdir)
            if dur <= 0:
                dur = get_duration(stream_url, headers) or 0
            sample_frames = extract_remote_frames(stream_url, dur, headers)
            return {
                "ok": True, "platform": platform, "title": title,
                "size_mb": 0, "duration": round(dur, 1),
                "frames": len(sample_frames), "mode": "remote-sparse",
                "secs": round(time.time() - t0, 1),
            }
        except resolver.ResolveError:
            pass
        platform, title, vpath = resolver.download_video(url.strip(), tmpdir, FFMPEG)
        size = os.path.getsize(vpath) if os.path.exists(vpath) else 0
        dur = get_duration(vpath) if os.path.exists(vpath) else None
        return {
            "ok": True, "platform": platform, "title": (title or "")[:80],
            "size_mb": round(size / 1024 / 1024, 2),
            "duration": round(dur, 1) if dur else None,
            "secs": round(time.time() - t0, 1),
        }
    except resolver.ResolveError as exc:
        return {"ok": False, "error": str(exc), "secs": round(time.time() - t0, 1)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                "secs": round(time.time() - t0, 1)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/probe")
async def probe(url: str = Form(...)):
    """诊断接口：回传服务端实际抓到的页面结构，用于定位链接解析失败原因。不下载视频。"""
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(404, "Not found")
    try:
        return {"ok": True, **resolver.probe_url(url.strip())}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


@app.get("/api/analyze-demo")
def analyze_demo(_code: None = Depends(require_code)):
    """演示模式：无需 API Key，返回内置的示例分析结果"""
    return {
        "title": "《高效学习法》第一课：为什么你学得慢？",
        "category": "教育培训",
        "tags": ["学习方法", "教育学", "记忆", "自我提升"],
        "key_info": [
            "视频主题：讲解三个被科学验证的高效学习方法，针对「学了就忘」的普遍痛点",
            "核心痛点：被动阅读（反复看书、划线）只会产生「熟悉感错觉」，并不等于真正记住",
            "方法一「主动回忆」：合上书本默写要点、自测，记忆效果可提升约 50%，心理学上称为「测试效应」",
            "方法二「间隔重复」：按 1 天 / 3 天 / 7 天的节奏回头复习，有效对抗艾宾浩斯遗忘曲线",
            "方法三「费曼技巧」：用自己的话把知识讲给别人听，卡壳的地方就是理解的漏洞",
            "结论：高效学习 = 主动回忆 + 间隔重复 + 输出讲解，三步组成可执行的闭环流程",
            "视频结构：痛点引入 → 破除误区 → 三个方法逐一展开（原理+做法+效果）→ 总结串联",
            "适合人群：备考学生、需要快速掌握新知识的职场人",
        ],
        "chapters": [
            {"time": "00:00", "label": "开场：为什么你学得慢"},
            {"time": "00:03", "label": "误区：被动阅读效率低"},
            {"time": "00:06", "label": "方法一：主动回忆（测试效应）"},
            {"time": "00:09", "label": "方法二：间隔重复"},
            {"time": "00:12", "label": "方法三：费曼技巧与总结"},
        ],
        "teaching": {
            "is_teaching": True,
            "viewpoints": [
                "学习的本质是「提取练习」而不是「反复输入」",
                "记忆要对抗遗忘曲线，复习节奏比复习时长更重要",
                "能讲清楚才算真的学会，输出倒逼理解",
            ],
            "logic": "先抛痛点问题引起共鸣 → 破除常见误区 → 依次讲三个方法（每个方法：原理+做法+效果）→ 最后串联成可执行的学习流程",
            "audience": "备考学生、职场自我提升人群",
        },
        "remix": {
            "cards": [
                {"title": "越熟悉≠越记得住", "desc": "反复阅读只产生熟悉感，主动回忆才是真记忆"},
                {"title": "复习看节奏", "desc": "1 天、3 天、7 天的间隔重复，比一次学 3 小时更有效"},
                {"title": "讲不出来=没学会", "desc": "用费曼技巧把知识讲给别人听，卡壳处就是漏洞"},
                {"title": "学习流程化", "desc": "先回忆、再间隔复习、最后输出，三步组成闭环"},
            ],
            "script": "你有没有这种感觉：书看了三遍，一合上就忘？问题不在你笨，而在方法。今天 60 秒，教你三个被科学验证的学习方法。第一，主动回忆。别反复看了，合上书默写要点，记忆提升可达一半，这叫测试效应。第二，间隔重复。别一次学到底，按 1 天、3 天、7 天的节奏回头复习，专治遗忘曲线。第三，费曼技巧。把学的知识讲给别人听，哪里讲不下去，哪里就是你的理解漏洞。记住：先回忆，再间隔复习，最后讲出来。关注我，下节课拆解做笔记的正确姿势。",
            "quotes": [
                "学习的本质是提取，不是输入",
                "能讲清楚，才算真的学会",
                "复习的节奏，比复习的时长更重要",
            ],
        },
        "speech_summary": "讲者指出，被动阅读容易产生已经掌握的错觉，真正有效的学习需要主动回忆。课程进一步说明了间隔重复与费曼技巧的具体使用方法，并建议把三种方法组合成可持续执行的学习闭环。",
        "overall_summary": "这是一节面向「学了就忘」人群的学习方法教学视频。视频先用「书看三遍一合上就忘」的痛点引起共鸣，指出被动阅读只产生熟悉感错觉；随后依次讲解主动回忆（测试效应，记忆提升约 50%）、间隔重复（1/3/7 天节奏对抗遗忘曲线）、费曼技巧（讲给别人听，卡壳即漏洞）三个方法，每个方法都按「原理 + 做法 + 整体结构清晰、节奏紧凑，结论可落地：先回忆、再间隔复习、最后输出讲解。适合备考学生和需要高效自学新知识的职场人，看完即可直接套用到自己的学习流程中。",
        "_meta": {"frames": 5, "duration": 15, "model": "演示模式（未调用真实大模型）", "speech": "画面 + 语音转写"},
    }


@app.middleware("http")
async def disable_stale_frontend_cache(request, call_next):
    """前端更新后立即生效，避免浏览器继续显示旧的 95MB 限制页面。"""
    response = await call_next(request)
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
