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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import resolver

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
MAX_LINK_CONCURRENT = max(1, int(os.environ.get("VI_MAX_LINK_CONCURRENT", "1")))
MAX_FRAME_CONCURRENT = max(1, int(os.environ.get("VI_MAX_FRAME_CONCURRENT", "4")))
MAX_AGENT_CONCURRENT = max(1, int(os.environ.get("VI_MAX_AGENT_CONCURRENT", "2")))
ANALYSIS_QUEUE_TIMEOUT = max(1, int(os.environ.get("VI_QUEUE_TIMEOUT", "20")))
_link_slots = asyncio.Semaphore(MAX_LINK_CONCURRENT)
_frame_slots = asyncio.Semaphore(MAX_FRAME_CONCURRENT)
_agent_slots = asyncio.Semaphore(MAX_AGENT_CONCURRENT)
_asr_media: dict[str, tuple[str, float]] = {}
_asr_media_lock = threading.Lock()

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
ASR_SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
ASR_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
ASR_MODEL = os.environ.get(
    "VI_ASR_MODEL", "qwen-audio-3.0-asr-flash-filetrans").strip()
DEFAULT_MODEL = "qwen3-vl-plus"
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
2. 关键信息提取（核心任务）：逐条提取视频中的关键信息，共 8-12 条，按重要性从高到低排列。覆盖：主题、人物或主体、事件、关键数据、方法步骤、重要结论等，每条一句话，尽量带画面中的具体细节，让没看过视频的人读完就能掌握全部要点；
3. 章节时间轴：按关键帧先后顺序估算时间点划分章节；
4. 如果属于教学/知识类视频，提炼教学观点：核心教学主张、讲解思路、适合人群；
5. Remix 衍生创作：生成 3-5 张观点卡片（一句话观点+简短说明）、1 个约 60 秒的短视频口播脚本、3 条金句摘录；
6. 语音内容摘要：提供了语音转写时，用 3-6 句话概括口头讲述的重点；未提供时返回空字符串；
7. 总体总结：综合全部内容写一段 150-250 字的总结，概括视频讲了什么、整体结构如何、核心结论与价值、适合什么人看，作为整份报告的收尾。

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
    """本地抽帧与链接解析分池，轻任务多人并行，重任务限制并发防止 OOM。"""
    slots = (_frame_slots if kind == "frames" else
             _agent_slots if kind == "agent" else _link_slots)
    try:
        await asyncio.wait_for(slots.acquire(), timeout=ANALYSIS_QUEUE_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        raise HTTPException(503, "当前有其他视频正在分析，请稍等片刻后重试")
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
    """逐帧抽取（兜底方案）：依赖 -ss 快速 seek，兼容性最好。"""
    files = []
    for i in range(MAX_FRAMES):
        t = duration * (i + 0.5) / MAX_FRAMES
        out = os.path.join(tmpdir, f"f{i}.jpg")
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-ss", f"{t:.2f}", "-i", path,
            "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "5", out,
        ]
        if _run_ffmpeg(cmd, timeout=60) and os.path.exists(out) and os.path.getsize(out) > 1000:
            files.append(out)
    return files


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


def parse_model_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise HTTPException(500, "模型未返回有效的 JSON 结果")
    try:
        return json.loads(text[s:e + 1])
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


def transcribe_remote_audio_details(media_url: str, cfg: dict, timeout: int = 300) -> dict:
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
        time.sleep(2)
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
            segments.append({"start_ms": begin, "end_ms": end, "text": text,
                             "speaker": speaker or "待校准",
                             "speaker_source": "asr" if speaker else "unknown"})
    return {
        "text": _compact_transcript(_transcript_text(payload)),
        "segments": segments[:2000],
    }


def transcribe_remote_audio(media_url: str, cfg: dict, timeout: int = 300) -> str:
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


def _creative_understanding(shots: list[dict], transcript: list[dict], cfg: dict) -> dict:
    compact = [{k: item.get(k) for k in ("start_ms", "end_ms", "speaker", "text")}
               for item in transcript[:1200]]
    prompt = """你是短剧情原片分析导演。结合按时间排列的关键帧和字幕，完成可核验的结构化理解。不得凭声音猜人物身份；说话人证据不足时 speaker 写“待确认”并降低 confidence。只输出 JSON：
{"speaker_calibration":[{"start_ms":0,"end_ms":1000,"speaker":"人物A/旁白/待确认","text":"","confidence":0.0,"evidence":"画面口型/SRT标注/仅上下文推断"}],
"characters":[{"id":"char-1","name":"人物A","identity":"","personality":"","appearance":"","wardrobe_by_unit":[{"unit":"剧情单元1","wardrobe":""}]}],
"relationships":[{"from":"char-1","to":"char-2","relationship":"","changes":""}],
"scenes":[{"name":"","time_range":"","props":[]}],
"story_units":[{"unit":1,"time_range":"","summary":"","characters":[],"emotion_changes":[{"character":"","from":"","to":"","cause":""}],"transition":""}],
"product_placements":[{"time_range":"","product":"","method":"","plot_function":""}],
"appeal_logic":{"first_3_seconds":"","conflict":"","payoffs":[],"pace":"","why_it_works":""},
"review_required":["需要人工确认的说话人或事实"]}。
字幕数据：""" + json.dumps(compact, ensure_ascii=False)[:48000]
    return _creative_asset_prompt(prompt, [item.get("image", "") for item in shots[::3]][:12], cfg)


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


def _validate_creative_phase(phase: str, result: dict) -> dict:
    """拒绝把空壳模型输出当成成功。"""
    if not isinstance(result, dict):
        raise HTTPException(502, "AI 没有返回结构化结果")
    if phase == "script":
        script = result.get("script") or {}
        if not result.get("role_profiles") or not script.get("story_units") or not script.get("shots"):
            raise HTTPException(502, "新脚本缺少人物、剧情单元或镜头，已阻止残缺结果进入下一步")
    elif phase == "storyboard":
        rows = result.get("storyboard") or []
        if not rows or any(not row.get("panels") or not row.get("time") for row in rows if isinstance(row, dict)):
            raise HTTPException(502, "分镜组缺少时间或画面规划，已阻止残缺结果进入下一步")
    elif phase == "prompts" and not result.get("video_prompts"):
        raise HTTPException(502, "视频提示词为空，已阻止残缺结果进入下一步")
    return result


def _creative_scene_frames(video_path: str, out_dir: str, duration: float) -> list[dict]:
    """用 FFmpeg 场景分数寻找镜头切换点并输出低分辨率关键帧。"""
    pattern = os.path.join(out_dir, "scene-%03d.jpg")
    command = [
        FFMPEG, "-hide_banner", "-i", video_path,
        "-vf", "select='gt(scene,0.28)',scale=480:-2,showinfo",
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


def _creative_asset_prompt(prompt: str, images: list[str], cfg: dict) -> dict:
    """让视觉模型理解人物/产品参考图，并输出受约束 JSON。"""
    content = [{"type": "text", "text": prompt}]
    for image in images[:12]:
        if isinstance(image, str) and image.startswith("data:image/") and len(image) <= 1_500_000:
            content.append({"type": "image_url", "image_url": {"url": image}})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json"},
                json={"model": cfg.get("model") or DEFAULT_MODEL,
                      "messages": [{"role": "user", "content": content}],
                      "temperature": 0.2,
                      "max_tokens": 8192,
                      "enable_thinking": False,
                      "response_format": {"type": "json_object"}},
                timeout=240,
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
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    if isinstance(last_error, HTTPException):
        raise last_error
    raise HTTPException(502, "内容整理暂未完成")


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
        "model": cfg.get("model") or DEFAULT_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
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
                json=body, timeout=300,
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
            return parse_model_json(text)
        except HTTPException as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    raise last_err


def call_qwen_text_json(prompt: str, cfg: dict, temperature: float = 0.2) -> dict:
    """调用文本模型并要求返回 JSON，供受控 Agent 的规划与工具执行使用。"""
    body = {
        "model": AGENT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
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
                timeout=180,
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
    file: UploadFile = File(...),
    subtitle: UploadFile | None = File(None),
    _code: None = Depends(require_code),
):
    """电商二创第一步：FFmpeg 临时拆镜头，百炼返回带时间轴台词。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi"}:
        raise HTTPException(400, "请上传 MP4、MOV、WebM、MKV 或 AVI 视频")
    tmpdir = tempfile.mkdtemp(prefix="vinsight_creative_")
    video_path = os.path.join(tmpdir, "source" + suffix)
    audio_path = os.path.join(tmpdir, "track.mp3")
    token = uuid.uuid4().hex
    try:
        total = 0
        with open(video_path, "wb") as output:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > min(MAX_VIDEO_BYTES, 300 * 1024 * 1024):
                    raise HTTPException(413, "深度拆解视频暂限 300MB 以内")
                output.write(chunk)
        duration = await asyncio.to_thread(get_duration, video_path)
        if not duration or duration <= 0:
            raise HTTPException(400, "无法读取视频，请转换为 MP4（H.264/AAC）后重试")
        if duration > 30 * 60:
            raise HTTPException(400, "电商二创深度拆解暂支持 30 分钟以内视频")
        scenes = await asyncio.to_thread(_creative_scene_frames, video_path, tmpdir, duration)
        audio_run = await asyncio.to_thread(
            subprocess.run,
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", video_path,
             "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", "-y", audio_path],
            capture_output=True, timeout=240,
        )
        transcript = {"text": "", "segments": []}
        srt_segments = _parse_srt(await subtitle.read()) if subtitle and subtitle.filename else []
        asr_warning = ""
        if not srt_segments and audio_run.returncode == 0 and os.path.isfile(audio_path) and os.path.getsize(audio_path) > 256:
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
        else:
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
            understanding = await asyncio.to_thread(_creative_understanding, scenes, segments, cfg)
            segments = _merge_speaker_calibration(segments, understanding)
        except Exception as exc:
            logger.warning("creative_understanding_fallback reason=%s", exc)
            understanding_warning = "深层剧情理解暂未完成，镜头和台词仍已保留。"
        return {
            "duration": round(duration, 2), "shots": scenes,
            "transcript": segments, "transcript_text": " ".join(x.get("text", "") for x in segments),
            "transcript_source": "srt" if srt_segments else "asr",
            "understanding": understanding,
            "warning": " ".join(x for x in (asr_warning if not srt_segments else "", understanding_warning) if x),
            "processing": "优先使用 SRT；否则由语音模型转写。FFmpeg 提取镜头，AI 校准说话人与剧情结构；临时文件完成后删除。",
        }
    finally:
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
    if len(json.dumps(payload, ensure_ascii=False)) > 18_000_000:
        raise HTTPException(413, "素材数据过大，请减少图片数量或压缩图片")
    phase = str(payload.get("phase") or "")
    if phase not in {"script", "storyboard", "prompts"}:
        raise HTTPException(400, "不支持的生成阶段")
    context = {
        "analysis": _analysis_for_agent(payload.get("analysis") or {}),
        "shots": [{k: item.get(k) for k in ("shot", "start", "end", "dialogue")}
                  for item in ((payload.get("deconstruction") or {}).get("shots") or [])[:40]
                  if isinstance(item, dict)],
        "transcript": (payload.get("deconstruction") or {}).get("transcript", [])[:120],
        "assets": [{k: item.get(k) for k in ("id", "name", "type", "hidden")}
                   for item in (payload.get("assets") or [])[:12] if isinstance(item, dict)],
        "requirements": str(payload.get("requirements") or "")[:4000],
        "script": payload.get("script") or {},
        "storyboard": payload.get("storyboard") or [],
        "target_duration": int(payload.get("target_duration") or 15),
        "target_total_seconds": max(60, min(420, int(payload.get("target_total_seconds") or 120))),
        "variation": payload.get("variation") or {},
        "reference_script": str(payload.get("reference_script") or "")[:12000],
        "understanding": (payload.get("deconstruction") or {}).get("understanding") or {},
    }
    asset_images = [item.get("image", "") for item in (payload.get("assets") or [])
                    if isinstance(item, dict) and not item.get("hidden")]
    # 原片关键帧让模型真正看见镜头内容；均匀限量，避免一次请求过大。
    original_images = [item.get("image", "") for item in
                       ((payload.get("deconstruction") or {}).get("shots") or [])[::4]
                       if isinstance(item, dict)]
    if phase == "script":
        images = (original_images[:4] + asset_images[:6])[:8]
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
        instruction = """你是连续分镜导演。按目标单段时长把完整脚本拆成连续分镜组；每组只为脚本中实际存在的镜头提供画面规划，总数不超过9格，包含景别变化、前后连续动作、实际出场人物、人物服装、场景和产品素材引用。不要补写重复镜头，不要声称已经生成图片。只输出 JSON：{\"storyboard\":[{\"group\":1,\"time\":\"0-15s\",\"duration\":15,\"unit\":1,\"asset_ids\":[\"\"],\"continuity_in\":\"\",\"continuity_out\":\"\",\"panels\":[{\"panel\":1,\"shot_size\":\"全景/中景/近景/特写\",\"visual\":\"\",\"speaker\":\"\",\"dialogue\":\"\",\"emotion_action\":\"\"}],\"grid_prompt\":\"九宫格生图提示词\",\"negative_prompt\":\"\"}]}。"""
    else:
        instruction = """你是视频生成提示词编排器。逐个连续分镜组生成提示词；引用该组实际出现的人物/场景/产品 asset_id，写明说话人、对应台词、情绪动作、镜头变化与前后连续性。人物音频没有真实素材时标记 voice_status=missing，不得伪称已生成。10至15秒使用一组九宫格；30秒可组合相邻两组但不能打乱剧情。输出 JSON：{\"video_prompts\":[{\"group\":1,\"source_groups\":[1],\"duration\":15,\"asset_ids\":[\"\"],\"speakers\":[\"\"],\"voice_status\":\"ready/missing\",\"prompt\":\"包含主体、动作、台词、运镜、场景、产品、节奏、转场、声音的可执行提示词\"}]}。"""
    prompt = instruction + "\n用户当前工作区数据：" + json.dumps(context, ensure_ascii=False)[:65000]
    result = await asyncio.to_thread(_creative_asset_prompt, prompt, images, cfg)
    return _validate_creative_phase(phase, result)


def _validated_export_report(payload: dict) -> dict:
    report = payload.get("report") if isinstance(payload, dict) else None
    if not isinstance(report, dict) or not str(report.get("title") or "").strip():
        raise HTTPException(400, "没有可导出的报告")
    if len(json.dumps(report, ensure_ascii=False)) > 500_000:
        raise HTTPException(413, "报告内容过大")
    return report


def _build_pptx(report: dict) -> io.BytesIO:
    """生成原生 Office Open XML 演示文稿，而不是伪装成 PPT 的 HTML。"""
    from pptx import Presentation
    from pptx.util import Pt

    presentation = Presentation()
    presentation.core_properties.title = str(report.get("title") or "视频课程课件")[:200]
    title_slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    title_slide.shapes.title.text = str(report.get("title") or "视频课程课件")
    title_slide.placeholders[1].text = "VideoInsight · 课程与讲座复盘"

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
        add_section("视频配文案", ((creative.get("post_copy") or creative.get("xiaohongshu") or {}).get("body")))
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


def readiness_checks() -> dict:
    """Check dependencies required to accept real analysis traffic."""
    return {
        "api_key": bool(load_config().get("api_key")),
        "ffmpeg": bool(FFMPEG and os.path.isfile(FFMPEG)),
        "static": STATIC_DIR.joinpath("index.html").is_file(),
    }


@app.get("/api/ready")
def ready():
    """Deployment readiness: 503 means keep this instance out of traffic."""
    checks = readiness_checks()
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "service": "video-insight", "checks": checks},
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
    analysis_slots = await acquire_analysis_slot("link")
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
            with open(vpath, "wb") as f:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise HTTPException(400, "视频超过 500MB，请换一个更小的文件")
                    f.write(chunk)
        elif url and url.strip():
            remote_info = None
            try:
                # 长视频优先只解析媒体地址，再从远程稀疏抽帧，避免下载整段。
                remote_info = await asyncio.to_thread(
                    resolver.resolve_stream_info, url.strip(), tmpdir)
                platform, title, stream_url, duration, media_headers = remote_info
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
            frames = await asyncio.to_thread(
                extract_remote_frames, stream_url, duration, media_headers)
            try:
                transcript = await asyncio.to_thread(
                    transcribe_remote_audio, stream_url, cfg)
                if transcript:
                    speech_status = "画面 + 语音转写"
            except Exception as exc:
                logger.warning("asr_fallback platform=%s reason=%s", platform, exc)
        else:
            frames, duration = await asyncio.to_thread(extract_frames, vpath)
        if not frames:
            raise HTTPException(500, "视频抽帧失败：请确认文件是可播放的视频格式（mp4 / mov / webm 等）")
        check_daily_usage()  # 真正要调用大模型了才计数
        mode, _ = _analysis_context(mode)
        scenario, _ = _scenario_context(scenario)
        analysis = await asyncio.to_thread(
            call_qwen, frames, cfg, duration, mode, transcript, scenario)
        analysis["_meta"] = {
            "frames": len(frames),
            "duration": int(duration),
            "model": cfg.get("model") or DEFAULT_MODEL,
            "platform": platform,
            "title": title,
            "mode": mode,
            "scenario": scenario,
            "speech": speech_status,
        }
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
    for frame in frames:
        raw = await frame.read()
        total += len(raw)
        if len(raw) > 2 * 1024 * 1024 or total > 30 * 1024 * 1024:
            raise HTTPException(400, "关键帧数据过大，请重新选择视频")
        content_type = (frame.content_type or "").lower()
        if content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(400, "关键帧格式不支持")
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
    analysis_slots = await acquire_analysis_slot("frames")
    try:
        check_daily_usage()
        analysis = await asyncio.to_thread(
            call_qwen, encoded, cfg, duration, mode, "", scenario)
        analysis["_meta"] = {
            "frames": len(encoded),
            "duration": int(duration),
            "model": cfg.get("model") or DEFAULT_MODEL,
            "platform": "本地文件（浏览器抽帧）",
            "title": Path(filename).name[:120],
            "mode": mode,
            "scenario": scenario,
            "speech": "仅画面分析（原视频未上传）",
        }
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
