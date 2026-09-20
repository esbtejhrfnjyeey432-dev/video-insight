# -*- coding: utf-8 -*-
"""
VideoInsight 视频解析工具 · 后端服务
流程：视频上传/链接下载 -> ffmpeg 抽关键帧 -> 阿里百炼 Qwen-VL 内容理解 -> 结构化分析 + Remix
"""
import base64
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests
import imageio_ffmpeg
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import resolver

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"


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

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# 公开模式：默认开启（分享链接给任何人都能直接用）。
# 需要「仅授权人可用」时才在部署平台把 VI_PUBLIC 设为 false 并配置 VI_ACCESS_CODE。
PUBLIC_MODE = _env_flag("VI_PUBLIC", True)

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen3-vl-plus"
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

PROMPT = """你是专业的视频内容分析师。我会给你一段视频中按时间顺序抽取的关键帧画面，请完成：
1. 内容理解：判断视频主题、类型（教育培训/知识科普/新闻资讯/娱乐/产品演示/VLOG/其他）与标签；
2. 关键信息提取（核心任务）：逐条提取视频中的关键信息，共 8-12 条，按重要性从高到低排列。覆盖：主题、人物或主体、事件、关键数据、方法步骤、重要结论等，每条一句话，尽量带画面中的具体细节，让没看过视频的人读完就能掌握全部要点；
3. 章节时间轴：按关键帧先后顺序估算时间点划分章节；
4. 如果属于教学/知识类视频，提炼教学观点：核心教学主张、讲解思路、适合人群；
5. Remix 衍生创作：生成 3-5 张观点卡片（一句话观点+简短说明）、1 个约 60 秒的短视频口播脚本、3 个精华剪辑点（时间+理由）、3 条金句摘录；
6. 总体总结：综合全部内容写一段 150-250 字的总结，概括视频讲了什么、整体结构如何、核心结论与价值、适合什么人看，作为整份报告的收尾。

只输出严格的 JSON，不要输出任何其他文字，不要用 markdown 代码块包裹。JSON 结构：
{"title":"视频标题","category":"视频类型","tags":["标签"],
"key_info":["关键信息1","关键信息2"],
"chapters":[{"time":"MM:SS","label":"章节名称"}],
"teaching":{"is_teaching":true,"viewpoints":["教学观点"],"logic":"讲解思路","audience":"适合人群"},
"remix":{"cards":[{"title":"一句话观点","desc":"简短说明"}],"script":"60秒口播脚本","clips":[{"time":"MM:SS","reason":"剪辑理由"}],"quotes":["金句"]},
"overall_summary":"150-250字的总体总结"}
若不是教学类视频，teaching.is_teaching 填 false，其余教学字段填空数组或空字符串。"""

app = FastAPI(title="VideoInsight")

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
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_duration(path: str):
    """用 ffmpeg 读取视频时长（imageio-ffmpeg 自带 ffmpeg，无需单独安装）"""
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path],
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


def call_qwen(frames: list, cfg: dict, duration: float | None = None) -> dict:
    duration_note = ""
    if duration and duration > 0:
        duration_note = (
            f"\n\n视频总时长约 {int(duration)} 秒；下面 {len(frames)} 张关键帧"
            "按时间顺序均匀抽取。章节时间请根据总时长与帧序估算。"
        )
    content = [{"type": "text", "text": PROMPT + duration_note}]
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
            raise HTTPException(502, f"调用大模型失败：{exc}")
        if r.status_code != 200:
            detail = r.text[:300]
            raise HTTPException(502, f"大模型接口返回 {r.status_code}：{detail}")
        msg = r.json()["choices"][0]["message"]
        # content 为空时兜底取 reasoning_content；并剥离思考标签
        text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        try:
            return parse_model_json(text)
        except HTTPException as exc:
            last_err = exc
    raise last_err


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
    _code: None = Depends(require_code),
):
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key：请点右上角「设置」填写阿里百炼 API Key，或先点「演示模式」看效果")
    tmpdir = tempfile.mkdtemp(prefix="vinsight_video_")
    try:
        platform, title = "本地文件", ""
        if file is not None:
            vpath = os.path.join(tmpdir, file.filename or "video.mp4")
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
            try:
                # 支持直接粘贴 App 复制的整段分享文案（自动提取链接，通用平台解析）
                platform, title, vpath = resolver.download_video(
                    url.strip(), tmpdir, FFMPEG,
                    xhs_cookie=cfg.get("xhs_cookie", ""),
                )
            except resolver.ResolveError as exc:
                raise HTTPException(400, str(exc))
            except Exception as exc:
                raise HTTPException(400, f"视频下载失败：{exc}")
        else:
            raise HTTPException(400, "请先上传视频文件或粘贴视频链接")

        frames, duration = extract_frames(vpath)
        if not frames:
            raise HTTPException(500, "视频抽帧失败：请确认文件是可播放的视频格式（mp4 / mov / webm 等）")
        check_daily_usage()  # 真正要调用大模型了才计数
        analysis = call_qwen(frames, cfg, duration)
        analysis["_meta"] = {
            "frames": len(frames),
            "duration": int(duration),
            "model": cfg.get("model") or DEFAULT_MODEL,
            "platform": platform,
            "title": title,
        }
        return analysis
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/analyze-frames")
async def analyze_frames(
    frames: list[UploadFile] = File(...),
    duration: float = Form(...),
    filename: str = Form("course-video"),
    _code: None = Depends(require_code),
):
    """长课程专用：视频留在浏览器本地，只接收浏览器均匀抽取的 JPEG 关键帧。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        raise HTTPException(400, "尚未配置 API Key")
    if duration <= 0:
        raise HTTPException(400, "无法读取视频时长，请换成浏览器可播放的 MP4(H.264) 视频")
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

    check_daily_usage()
    analysis = call_qwen(encoded, cfg, duration)
    analysis["_meta"] = {
        "frames": len(encoded),
        "duration": int(duration),
        "model": cfg.get("model") or DEFAULT_MODEL,
        "platform": "本地文件（浏览器抽帧）",
        "title": Path(filename).name[:120],
    }
    return analysis


@app.post("/api/resolve-test")
async def resolve_test(url: str = Form(...)):
    """调试接口：只做「下载视频」这一步，不调用大模型，用于快速定位链接解析是否成功。
    返回平台 / 标题 / 文件大小 / 视频时长 / 耗时。"""
    tmpdir = tempfile.mkdtemp(prefix="vinsight_dbg_")
    t0 = time.time()
    try:
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
            "clips": [
                {"time": "00:04", "reason": "「测试效应」核心结论出现，是全片最有传播力的知识点"},
                {"time": "00:09", "reason": "间隔重复的复习节奏表，画面信息密度高，适合做封面截图"},
                {"time": "00:13", "reason": "三步学习流程总结，适合作为短视频结尾金句"},
            ],
            "quotes": [
                "学习的本质是提取，不是输入",
                "能讲清楚，才算真的学会",
                "复习的节奏，比复习的时长更重要",
            ],
        },
        "overall_summary": "这是一节面向「学了就忘」人群的学习方法教学视频。视频先用「书看三遍一合上就忘」的痛点引起共鸣，指出被动阅读只产生熟悉感错觉；随后依次讲解主动回忆（测试效应，记忆提升约 50%）、间隔重复（1/3/7 天节奏对抗遗忘曲线）、费曼技巧（讲给别人听，卡壳即漏洞）三个方法，每个方法都按「原理 + 做法 + 整体结构清晰、节奏紧凑，结论可落地：先回忆、再间隔复习、最后输出讲解。适合备考学生和需要高效自学新知识的职场人，看完即可直接套用到自己的学习流程中。",
        "_meta": {"frames": 5, "duration": 15, "model": "演示模式（未调用真实大模型）"},
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
