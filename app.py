# -*- coding: utf-8 -*-
"""
VideoInsight 视频解析工具 · 后端服务
流程：视频上传/链接下载 -> ffmpeg 抽关键帧 -> 阿里百炼 Qwen-VL 内容理解 -> 结构化分析 + Remix
"""
import base64
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
from fastapi.staticfiles import StaticFiles

import resolver

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# ------------------------------------------------------------------
# 隐私与访问控制（公网部署用环境变量注入，代码仓库中不含任何密钥）
#   VI_API_KEY     服务端持有的百炼 API Key（部署模式：前端永远看不到、也改不了）
#   VI_ACCESS_CODE 访问码；设置后所有解析接口都需携带正确的码（发给招聘者的是「链接+访问码」）
# ------------------------------------------------------------------
ENV_API_KEY = os.environ.get("VI_API_KEY", "").strip()
ACCESS_CODE = os.environ.get("VI_ACCESS_CODE", "").strip()
DEPLOY_MODE = bool(ENV_API_KEY)  # 服务端注入 Key 即视为公开部署模式
DAILY_LIMIT = int(os.environ.get("VI_DAILY_LIMIT", "20"))  # 部署模式：每天最多解析次数
_usage: dict = {}  # {访问码: [日期, 已用次数]} · 防止访问码外泄后被刷爆额度

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen3-vl-plus"
MODEL_OPTIONS = ["qwen3-vl-plus", "qwen-vl-plus", "qwen-vl-max", "qwen-vl-max-latest"]
MAX_FRAMES = 12
FRAME_WIDTH = 768
MAX_VIDEO_BYTES = 500 * 1024 * 1024  # 500MB

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
    return cfg


async def require_code(x_access_code: str = Header(default="")):
    """访问码校验：公网部署时防止链接被陌生人滥用额度"""
    if ACCESS_CODE and not hmac.compare_digest(str(x_access_code), ACCESS_CODE):
        raise HTTPException(401, "访问码不正确，请输入正确的访问码")


def check_daily_usage():
    """部署模式下限制每日解析次数（本地自用不限），防止访问码泄露后被刷额度"""
    if not (DEPLOY_MODE and ACCESS_CODE):
        return
    today = time.strftime("%Y-%m-%d")
    rec = _usage.setdefault(ACCESS_CODE, [today, 0])
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


def extract_frames(path: str):
    """均匀抽取关键帧并转为 base64（静态画面自动去重，控制 token 消耗）"""
    duration = get_duration(path) or 0.0
    if duration <= 0:
        duration = 60.0
    tmpdir = tempfile.mkdtemp(prefix="vinsight_frames_")
    frames, seen = [], set()
    try:
        for i in range(MAX_FRAMES):
            t = duration * (i + 0.5) / MAX_FRAMES
            out = os.path.join(tmpdir, f"f{i}.jpg")
            cmd = [
                FFMPEG, "-y", "-loglevel", "error",
                "-ss", f"{t:.2f}", "-i", path,
                "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "5", out,
            ]
            try:
                subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=60)
            except Exception:
                continue
            if os.path.exists(out) and os.path.getsize(out) > 1000:
                raw = Path(out).read_bytes()
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


def call_qwen(frames: list, cfg: dict) -> dict:
    content = [{"type": "text", "text": PROMPT}]
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
        "need_code": bool(ACCESS_CODE),
    }


@app.post("/api/config")
async def update_config(
    api_key: str = Form(""),
    model: str = Form(""),
    _code: None = Depends(require_code),
):
    if DEPLOY_MODE:
        # 公开部署版：API Key 由服务端统一持有，不提供任何修改入口
        raise HTTPException(403, "线上版本已由作者统一配置 AI 服务，无需设置 API Key")
    cfg = load_config()
    if api_key:
        cfg["api_key"] = api_key.strip()
    if model:
        cfg["model"] = model.strip()
    save_config(cfg)
    return {"ok": True, "has_key": bool(cfg.get("api_key")), "model": cfg.get("model")}


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
        analysis = call_qwen(frames, cfg)
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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
