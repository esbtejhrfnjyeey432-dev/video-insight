# -*- coding: utf-8 -*-
"""
resolver.py · 通用视频链接解析下载（粘什么平台的链接都能试）
架构（多级降级，尽量「粘什么都能解析」）：
  1. 抖音 / 小红书：自写解析器走移动端分享页（无需登录）
  2. 通用平台（B站 / 微博 / 西瓜 / YouTube 等 1000+ 站点）：yt-dlp 引擎
  3. 视频直链（以 .mp4 等结尾）：直接下载
  每一级失败自动降级到下一级，全部失败时给出可操作的中文提示。
"""
import glob
import json
import os
import re
from urllib.parse import urlparse

import requests

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_VIDEO_BYTES = 500 * 1024 * 1024
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".flv", ".ts")


def _prepare_cookies(url: str, outdir: str):
    """为云服务器环境准备浏览器 Cookie，显著降低视频平台风控（如 B站 412）概率。

    数据中心 IP 直接请求常被 WAF 拦下；先访问站点首页拿到 buvid3 等前置
    Cookie 后，再带着它去请求详情页通常就能通过。
    若用户通过环境变量 VI_BILI_COOKIE 手动注入 Cookie，则优先使用
    （可解锁会员 / 更高清晰度内容）。
    返回 cookiefile 路径；拿不到 Cookie 时返回 None，yt-dlp 仍会无 Cookie 重试。
    """
    host = urlparse(url).netloc.lower()
    if "bilibili" not in host:
        return None

    jar: dict = {}
    manual = os.environ.get("VI_BILI_COOKIE", "").strip()
    if manual:
        for part in manual.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                jar[k.strip()] = v.strip()

    if not jar:
        try:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": DESKTOP_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
            })
            sess.get("https://www.bilibili.com/", timeout=20)
            for k, v in sess.cookies.get_dict().items():
                jar[k] = v
        except Exception:
            return None

    if not jar:
        return None

    path = os.path.join(outdir, "cookies.txt")
    lines = ["# Netscape HTTP Cookie File"]
    for k, v in jar.items():
        lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


class ResolveError(Exception):
    """面向用户的可读错误（中文提示）"""


def extract_url(text: str):
    """从整段分享文案中提取第一个 http(s) 链接（App 复制的文案含中文和链接混排）"""
    if not text:
        return None
    m = re.search(r"https?://[^\s，,。、；;！!？?\"'（）()<>【】\[\]]+", text.strip())
    if m:
        return m.group(0)
    t = text.strip()
    return t if t.startswith("http") else None


def _save_stream(resp, dest: str):
    total = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(1 << 20):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_VIDEO_BYTES:
                raise ResolveError("视频超过 500MB，请换一个更小的视频")
            f.write(chunk)
    if total < 200 * 1024:
        raise ResolveError("下载到的文件过小，可能不是视频（链接可能已失效或需要登录）")


# ---------------------------------------------------------------- 抖音
def resolve_douyin(url: str, dest: str) -> str:
    """走移动端分享页解析（无需登录）。返回视频标题（可能为空）。"""
    s = requests.Session()
    s.headers.update({"User-Agent": MOBILE_UA})

    final_url = url
    if "v.douyin.com" in url:
        r = s.get(url, allow_redirects=True, timeout=25)
        final_url = r.url
    m = (re.search(r"/(?:video|note)/(\d+)", final_url)
         or re.search(r"modal_id=(\d+)", final_url)
         or re.search(r"/(\d{15,})", final_url))
    if not m:
        raise ResolveError("无法从抖音链接中识别视频：请确认复制的是视频的分享链接")
    vid = m.group(1)

    # 关键：带 Referer 才能拿到内嵌数据（抖音按请求头决定 SSR 内容）
    r2 = s.get(
        f"https://www.iesdouyin.com/share/video/{vid}/",
        headers={
            "Referer": "https://www.iesdouyin.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        timeout=25,
    )
    m2 = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", r2.text, re.S)
    item = None
    if m2:
        try:
            raw = m2.group(1).replace("undefined", "null")
            data = json.loads(raw)
            for v in (data.get("loaderData") or {}).values():
                if isinstance(v, dict) and isinstance(v.get("videoInfoRes"), dict):
                    items = v["videoInfoRes"].get("item_list") or []
                    if items:
                        item = items[0]
                        break
        except Exception:
            item = None
    if not item:
        raise ResolveError("抖音页面解析失败：该视频可能已删除或平台接口更新")

    title = (item.get("desc") or "")[:80]
    play_addr = item.get("video", {}).get("play_addr", {}) or {}
    candidates = []
    # 无水印直链（社区验证的构造方式）
    uri = play_addr.get("uri")
    if uri:
        candidates.append(f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=720p&line=0")
    candidates.extend(play_addr.get("url_list") or [])

    last_status = None
    for play_url in candidates:
        try:
            with s.get(play_url, stream=True, timeout=180,
                       headers={"Referer": "https://www.douyin.com/"}) as r3:
                if r3.status_code != 200:
                    last_status = r3.status_code
                    continue
                _save_stream(r3, dest)
                return title
        except ResolveError:
            raise
        except Exception:
            continue
    raise ResolveError(f"抖音视频下载失败（HTTP {last_status or '未知'}）")


# ---------------------------------------------------------------- 小红书
def resolve_xhs(url: str, dest: str) -> str:
    """移动端 UA 访问笔记页解析内嵌数据（桌面 UA 会被要求登录）。返回标题。"""
    s = requests.Session()
    s.headers.update({"User-Agent": MOBILE_UA})

    final_url = url
    if "xhslink.com" in url:
        r = s.get(url, allow_redirects=True, timeout=25)
        final_url = r.url
    m = (re.search(r"/explore/([0-9a-zA-Z]+)", final_url)
         or re.search(r"/discovery/item/([0-9a-zA-Z]+)", final_url))
    if not m:
        raise ResolveError("无法从小红书链接中识别笔记：请确认复制的是笔记的分享链接")
    note_id = m.group(1)

    r2 = s.get(
        f"https://www.xiaohongshu.com/explore/{note_id}",
        headers={"Accept-Language": "zh-CN,zh;q=0.9"},
        timeout=25,
    )
    m2 = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", r2.text, re.S)
    play_url, title = None, ""
    if m2:
        try:
            raw = m2.group(1).replace("undefined", "null")
            data = json.loads(raw)
            note = (data.get("note", {}).get("noteDetailMap", {}).get(note_id, {}) or {}).get("note", {})
            if note and not note.get("video"):
                raise ResolveError("这条小红书是图文笔记不是视频，无法解析")
            title = (note.get("title") or note.get("desc") or "")[:80]
            streams = (note.get("video", {}).get("media", {}).get("stream") or {})
            for key in ("h264", "h265", "byteh264"):
                for c in (streams.get(key) or []):
                    if c.get("masterUrl"):
                        play_url = c["masterUrl"]
                        break
                if play_url:
                    break
        except ResolveError:
            raise
        except Exception:
            play_url = None
    if not play_url:
        raise ResolveError("小红书页面解析失败：该笔记可能已删除或平台要求登录")

    with s.get(play_url, stream=True, timeout=180,
               headers={"Referer": "https://www.xiaohongshu.com/"}) as r3:
        if r3.status_code != 200:
            raise ResolveError(f"小红书视频下载失败（HTTP {r3.status_code}）")
        _save_stream(r3, dest)
    return title


# ---------------------------------------------------------------- yt-dlp 通用引擎
def resolve_ytdlp(url: str, outdir: str, ffmpeg_path: str = None):
    """yt-dlp 支持 1000+ 平台（B站/微博/西瓜/YouTube 等）。返回 (标题, 文件路径)。"""
    try:
        import yt_dlp
    except ImportError:
        raise ResolveError("服务器未安装 yt-dlp，无法解析该平台链接")

    prefix = os.path.join(outdir, "dl")
    host = urlparse(url).netloc.lower()
    # 反爬风控：云服务器 IP 常被视频平台标记，补齐浏览器请求头能显著提高成功率
    anti_headers = {
        "User-Agent": DESKTOP_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"https://{host}/",
    }
    if "bilibili" in host:
        # B站会校验来源，缺 Origin 时数据中心 IP 更容易被判定为爬虫
        anti_headers["Origin"] = "https://www.bilibili.com"
    opts = {
        "outtmpl": prefix + ".%(ext)s",
        # 先取 720p 以下的单文件格式，取不到再合并音视频（需要 ffmpeg）
        "format": "b[height<=720]/bv*[height<=720]+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "max_filesize": MAX_VIDEO_BYTES,
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "playlist_items": "1",
        "http_headers": anti_headers,
    }
    # 数据中心 IP 常被 YouTube 用 web 客户端限流（HTTP 429）。改用 android/ios/tv
    # 等移动端客户端取流，风控明显更松，能绕开绝大多数 429。
    if "youtube" in host or "youtu.be" in host:
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android", "ios", "tv_embedded", "web_embedded"]}
        }
    if ffmpeg_path:
        # 注意：必须传 ffmpeg 可执行文件的完整路径，imageio-ffmpeg 的文件名
        # 不叫 ffmpeg.exe，传目录会导致 yt-dlp 找不到而合并失败
        opts["ffmpeg_location"] = ffmpeg_path

    # 先拿站点 Cookie 再请求，能绕开大部分云服务器 IP 的风控拦截（B站 412 等）
    cookiefile = _prepare_cookies(url, outdir)
    if cookiefile:
        opts["cookiefile"] = cookiefile

    title = ""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                title = (info.get("title") or "")[:80]
    except Exception as exc:
        raise ResolveError(_translate_ytdlp_error(str(exc)))

    # 合并后扩展名可能与模板不同，用 glob 找实际产物
    for f in sorted(glob.glob(prefix + ".*")):
        if f.endswith((".part", ".ytdl", ".json")):
            continue
        if os.path.getsize(f) >= 200 * 1024:
            return title, f
    raise ResolveError("未成功下载视频：链接可能已失效、需要登录或被平台限制")


def _translate_ytdlp_error(msg: str) -> str:
    m = msg.lower()
    if "unsupported url" in m:
        return "暂不支持这个网站：请确认链接是视频页面，或下载视频后用「上传文件」"
    if "login" in m or "cookie" in m or "sign in" in m:
        return "该链接需要登录才能查看（私密/会员内容），请下载视频后用「上传文件」"
    if "private" in m:
        return "这是私密内容，无法解析：请下载视频后用「上传文件」"
    if "geo" in m or "not available in your country" in m or "region" in m:
        return "该视频有地区限制，当前网络无法访问"
    if "404" in m or "not found" in m or "removed" in m or "unavailable" in m:
        return "视频不存在或已被删除：请确认链接是否有效"
    if "http error 429" in m or "too many requests" in m:
        return "平台限流了，请稍后再试"
    if "timed out" in m or "timeout" in m:
        return "下载超时：网络较慢或视频过大，请稍后再试或用「上传文件」"
    detail = re.sub(r"\s+", " ", msg)[:160]
    return f"该链接解析失败：{detail}。也可以下载视频后用「上传文件」"


# ---------------------------------------------------------------- 直链
def resolve_direct(url: str, dest: str) -> str:
    with requests.get(url, stream=True, timeout=180,
                      headers={"User-Agent": DESKTOP_UA}) as r:
        if r.status_code != 200:
            raise ResolveError(f"直链下载失败（HTTP {r.status_code}）")
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype and not any(t in ctype for t in ("video", "octet-stream", "application")):
            raise ResolveError("该链接不是视频直链（可能是普通网页）")
        _save_stream(r, dest)
    return ""


# ---------------------------------------------------------------- 主入口
def download_video(text: str, outdir: str, ffmpeg_path: str = None):
    """
    从用户粘贴的分享文案/链接下载视频。
    返回 (平台名称, 视频标题, 视频文件路径)。
    """
    url = extract_url(text)
    if not url:
        raise ResolveError("没有在输入内容中找到链接：请粘贴视频链接或 App 里的分享文案")

    host = urlparse(url).netloc.lower()
    is_douyin = "douyin.com" in host or "iesdouyin.com" in host
    is_xhs = "xiaohongshu.com" in host or "xhslink.com" in host

    # 1. 抖音：自写解析器（快路径）→ yt-dlp 兜底
    if is_douyin:
        dest = os.path.join(outdir, "douyin.mp4")
        try:
            title = resolve_douyin(url, dest)
            return "抖音", title, dest
        except ResolveError as exc:
            if "超过" in str(exc) or "图文" in str(exc):
                raise
            try:
                title, path = resolve_ytdlp(url, outdir, ffmpeg_path)
                return "抖音", title, path
            except ResolveError:
                raise ResolveError(
                    f"{exc}。抖音近期风控较严，最稳妥的方式：在抖音 App 里下载视频，再用「上传文件」解析"
                )

    # 2. 小红书：自写解析器 → yt-dlp 兜底
    if is_xhs:
        dest = os.path.join(outdir, "xhs.mp4")
        try:
            title = resolve_xhs(url, dest)
            return "小红书", title, dest
        except ResolveError as exc:
            if "超过" in str(exc) or "图文" in str(exc):
                raise
            try:
                title, path = resolve_ytdlp(url, outdir, ffmpeg_path)
                return "小红书", title, path
            except ResolveError:
                raise ResolveError(
                    f"{exc}。小红书近期风控较严，最稳妥的方式：保存视频到本地，再用「上传文件」解析"
                )

    # 3. 视频文件直链（.mp4 等结尾）→ 直接下载更快
    path_part = urlparse(url).path.lower()
    if path_part.endswith(VIDEO_EXTS):
        dest = os.path.join(outdir, "direct.mp4")
        try:
            title = resolve_direct(url, dest)
            return "视频直链", title, dest
        except ResolveError:
            pass  # 降级到 yt-dlp（generic extractor 能处理一些奇怪的直链）

    # 4. 通用平台：yt-dlp 引擎（1000+ 站点）
    title, path = resolve_ytdlp(url, outdir, ffmpeg_path)
    return "在线视频", title, path
