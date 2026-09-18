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
        home = re.sub(r"^https?://", "", final_url).strip("/").lower()
        if home in ("www.douyin.com", "douyin.com", "v.douyin.com"):
            raise ResolveError(
                "这条抖音短链已失效（跳回了首页）。抖音分享短链有时效，请让对方重新复制一次最新链接")
    # 分享链接可能指向「用户主页」或「直播间」，两者都不是可下载的视频作品。
    # 不区分的话会统一报成「解析失败」，用户根本不知道该怎么改。
    if "/share/user/" in final_url or re.search(r"/user/(MS4w|[\w-]{20,})", final_url):
        raise ResolveError(
            "这是抖音「用户主页」链接，不是一个视频。请打开具体那个视频，再点分享 → 复制链接")
    if "webcast.amemv.com" in final_url or "/live/" in final_url:
        raise ResolveError("这是抖音直播间链接，暂不支持解析直播。请分享具体的视频作品")
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
        # 短链有时效，失效后会跳回小红书首页而不是报错。这里提前识别，
        # 避免把「链接过期」误报成「代码解析失败」
        home = re.sub(r"^https?://", "", final_url).strip("/").lower()
        if home in ("www.xiaohongshu.com", "xiaohongshu.com", "www.xhslink.com", "xhslink.com"):
            raise ResolveError(
                "这条小红书短链已失效（跳回了首页）。小红书分享短链有时效，请让对方重新复制一次最新链接")
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


# ---------------------------------------------------------------- Vimeo
def resolve_vimeo(url: str, dest: str) -> str:
    """Vimeo 自研解析：走官方播放器 config 接口取 mp4 直链。

    yt-dlp 的 Vimeo 提取器现在强制要求 web 客户端登录，公开视频也会报
    "The web client only works when logged-in"。官方播放器 config 接口对
    公开视频仍然可用，能拿到 progressive mp4 直链。
    """
    m = (re.search(r"vimeo\.com/(\d+)", url)
         or re.search(r"player\.vimeo\.com/video/(\d+)", url))
    if not m:
        raise ResolveError("无法从 Vimeo 链接中识别视频 ID")
    vid = m.group(1)
    # 私密/未列出的视频链接形如 vimeo.com/123456/abcdef123，需要带 h 参数
    hm = re.search(r"vimeo\.com/\d+/([0-9a-f]{6,})", url)
    api = f"https://player.vimeo.com/video/{vid}/config"
    if hm:
        api += f"?h={hm.group(1)}"

    s = requests.Session()
    r = s.get(api, timeout=25, headers={
        "User-Agent": DESKTOP_UA,
        "Referer": "https://vimeo.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if r.status_code != 200:
        raise ResolveError(f"Vimeo 播放器接口返回 HTTP {r.status_code}（视频可能设置了域名白名单或已删除）")
    try:
        data = r.json()
    except Exception:
        raise ResolveError("Vimeo 播放器接口返回了非 JSON 内容（可能被风控拦截）")

    title = ((data.get("video") or {}).get("title") or "")[:80]
    progressive = ((data.get("request") or {}).get("files") or {}).get("progressive") or []
    if not progressive:
        raise ResolveError("该 Vimeo 视频没有可直接下载的 mp4 源（可能受域名白名单限制）")
    # 清晰度从高到低挑一个不超过 720p 的
    cands = [p for p in progressive if (p.get("height") or 0) <= 720] or progressive
    pick = max(cands, key=lambda p: p.get("height") or 0)
    play_url = pick.get("url")
    if not play_url:
        raise ResolveError("Vimeo 直链为空，解析失败")

    with s.get(play_url, stream=True, timeout=180,
               headers={"Referer": "https://vimeo.com/",
                        "User-Agent": DESKTOP_UA}) as r3:
        if r3.status_code != 200:
            raise ResolveError(f"Vimeo 视频下载失败（HTTP {r3.status_code}）")
        _save_stream(r3, dest)
    return title


# ---------------------------------------------------------------- yt-dlp 通用引擎
def _has_curl_cffi() -> bool:
    """curl_cffi 让 yt-dlp 可以完整伪装成真实浏览器（TLS 指纹），
    是绕开数据中心 IP 风控的关键依赖。缺失时 yt-dlp 在部分站点会直接报错。"""
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


# 只有这些站点实测需要 TLS 伪装：Dailymotion 缺伪装会直接报
# "attempting impersonation"，YouTube/Vimeo 在数据中心 IP 上被严格风控。
# 反过来，B站开了伪装反而拿不到 formats（实测 3/3 → 0/3），所以必须按站点开关。
IMPERSONATE_HOSTS = ("dailymotion", "dai.ly", "youtube", "youtu.be", "vimeo")


def _impersonate_target(host: str):
    """构造浏览器伪装目标（仅对需要伪装的站点启用）。

    坑：yt-dlp 的 Python API 只接受 ImpersonateTarget 对象，直接传字符串
    'chrome' 会在内部 assert 失败并抛出「空消息」的 AssertionError，
    导致所有平台一起挂掉——必须先用 from_str() 解析。
    """
    if not any(h in host for h in IMPERSONATE_HOSTS):
        return None
    if not _has_curl_cffi():
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str("chrome")
    except Exception:
        return None


# YouTube 各客户端的风控松紧不同，逐个尝试直到成功（数据中心 IP 常 429）
_YT_CLIENT_SETS = (
    ["tv_embedded"],
    ["web_embedded"],
    ["mweb"],
    ["android_vr"],
    ["android", "ios"],
    ["web_safari"],
)


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

    def base_opts():
        return {
            "outtmpl": prefix + ".%(ext)s",
            # AI 抽帧宽度就是 640px，360p 的画面信息完全够用。
            # 优先取 360p 能显著降低下载+合并耗时（26 分钟视频 154s → 更快），
            # 也避免 Render 免费版 512MB 内存在合并大文件时 OOM。
            "format": ("b[height<=360]/bv*[height<=360]+ba/"
                       "b[height<=480]/bv*[height<=480]+ba/"
                       "b[height<=720]/bv*[height<=720]+ba/b"),
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

    # 浏览器 TLS 伪装：Dailymotion / Vimeo / YouTube 都在校验 TLS 指纹，
    # 缺 curl_cffi 时 yt-dlp 会抛 "attempting impersonation but none ..."
    impersonate = _impersonate_target(host)

    # 先拿站点 Cookie 再请求，能绕开大部分云服务器 IP 的风控拦截（B站 412 等）
    cookiefile = _prepare_cookies(url, outdir)

    is_yt = "youtube" in host or "youtu.be" in host
    attempts = []
    if is_yt:
        for clients in _YT_CLIENT_SETS:
            attempts.append({"youtube": {"player_client": list(clients)}})
    attempts.append(None)  # 最后再试一次默认配置

    last_err = ""
    for arg in attempts:
        opts = base_opts()
        if impersonate:
            opts["impersonate"] = impersonate
        if ffmpeg_path:
            opts["ffmpeg_location"] = ffmpeg_path
        if cookiefile:
            opts["cookiefile"] = cookiefile
        if arg:
            opts["extractor_args"] = arg
        try:
            title = ""
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    title = (info.get("title") or "")[:80]
                    # 有些落地页（如腾讯视频剧集页）能解析但没有可下格式
                    if not (info.get("formats") or info.get("url")):
                        raise yt_dlp.utils.DownloadError("no playable formats")
            # 合并后扩展名可能与模板不同，用 glob 找实际产物
            for f in sorted(glob.glob(prefix + ".*")):
                if f.endswith((".part", ".ytdl", ".json")):
                    continue
                if os.path.getsize(f) >= 200 * 1024:
                    return title, f
            # 下载成功但文件过小，换下一套参数重试
            last_err = "下载到的文件过小，可能不是视频"
        except Exception as exc:
            # AssertionError 等异常的 str() 是空串，必须用 repr 兜底，
            # 否则用户会看到一个完全没有信息的「该链接解析失败：」
            msg = str(exc) or repr(exc) or type(exc).__name__
            last_err = msg
            # 伪装目标不被当前环境支持 → 关掉伪装重试，别让整条链路卡死
            if impersonate and ("Impersonate" in msg or type(exc).__name__ == "AssertionError"):
                impersonate = None
                continue
            # 429/限流 → 换下一套客户端参数重试
            if "429" in msg or "Too Many Requests" in msg:
                continue
            if is_yt:
                continue
            raise ResolveError(_translate_ytdlp_error(msg))

    raise ResolveError(_translate_ytdlp_error(last_err or "未知错误"))


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
    if "no playable formats" in m:
        return ("这个页面没有可直接播放的视频（多为剧集/合集首页）。"
                "请打开具体某一集的播放页再复制链接")
    if "only works when logged-in" in m or "web client" in m:
        return "该站点要求登录才能取流：请下载视频到本地后用「上传文件」解析"
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

    # 2.5 Vimeo：自研播放器解析 → yt-dlp 兜底
    if "vimeo.com" in host:
        dest = os.path.join(outdir, "vimeo.mp4")
        try:
            title = resolve_vimeo(url, dest)
            return "Vimeo", title, dest
        except ResolveError as exc:
            if "超过" in str(exc):
                raise
            try:
                title, path = resolve_ytdlp(url, outdir, ffmpeg_path)
                return "Vimeo", title, path
            except ResolveError:
                raise ResolveError(
                    f"{exc}。也可以下载视频到本地后用「上传文件」解析")

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


# ---------------------------------------------------------------- 远程诊断
def probe_url(url: str) -> dict:
    """诊断用：回传服务端实际抓到的页面结构，用于定位解析失败原因（不下载视频）。

    抖音/小红书在本地网络常被风控、无法复现，只能看服务器视角的真实响应。
    """
    out = {"url": url, "tries": []}

    def _fetch(u, headers=None, timeout=25, impersonate=None):
        t = {"url": u}
        try:
            if impersonate:
                from curl_cffi import requests as cr
                r = cr.get(u, headers=headers, timeout=timeout, impersonate=impersonate)
            else:
                r = requests.get(u, headers=headers, timeout=timeout)
            t.update(status=r.status_code, len=len(r.text))
            t["head"] = re.sub(r"\s+", " ", r.text[:200])
            return t, r.text
        except Exception as e:
            t.update(error=f"{type(e).__name__}: {str(e)[:140]}")
            return t, ""

    def _state_keys(text):
        m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", text, re.S)
        if not m:
            return None
        try:
            return list(json.loads(m.group(1).replace("undefined", "null")).keys())
        except Exception as e:
            return f"json-err:{type(e).__name__}"

    host = urlparse(url).netloc.lower()

    # 短链先跟随重定向，把落地 URL 回传 —— 「无法识别视频」多半是落地 URL 变了
    if "v.douyin.com" in host or "xhslink.com" in host:
        try:
            rr = requests.get(url, allow_redirects=True, timeout=25,
                              headers={"User-Agent": MOBILE_UA})
            out["short_redirect"] = rr.url
            out["redirect_status"] = rr.status_code
            out["redirect_len"] = len(rr.text)
        except Exception as e:
            out["short_redirect"] = f"ERR {type(e).__name__}: {str(e)[:120]}"
    elif "douyin" in host or "iesdouyin" in host:
        pass

    if "xiaohongshu" in host or "xhslink" in host:
        m = (re.search(r"/explore/([0-9a-zA-Z]+)", url)
             or re.search(r"/discovery/item/([0-9a-zA-Z]+)", url))
        nid = m.group(1) if m else ""
        out["note_id"] = nid
        page = f"https://www.xiaohongshu.com/explore/{nid}" if nid else url
        for label, kw in (("requests", {}), ("curl_cffi", {"impersonate": "chrome"})):
            t, body = _fetch(page, headers={"User-Agent": MOBILE_UA,
                                            "Accept-Language": "zh-CN,zh;q=0.9"}, **kw)
            t["via"] = label
            t["state_keys"] = _state_keys(body)
            if body and "__INITIAL_STATE__" in body:
                try:
                    m2 = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", body, re.S)
                    d = json.loads(m2.group(1).replace("undefined", "null"))
                    nd = d.get("noteData") or {}
                    t["noteData_keys"] = list(nd.keys())[:12]
                    t["noteData_data_empty"] = not (nd.get("data") or {})
                    t["old_noteDetailMap"] = list(
                        (d.get("note", {}).get("noteDetailMap") or {}).keys())[:3]
                except Exception as e:
                    t["parse_err"] = f"{type(e).__name__}: {str(e)[:100]}"
            out["tries"].append(t)

    elif "douyin" in host or "iesdouyin" in host:
        src = out.get("short_redirect") or url
        m = (re.search(r"/(?:video|note)/(\d+)", src)
             or re.search(r"modal_id=(\d+)", src)
             or re.search(r"/(\d{15,})", src))
        vid = m.group(1) if m else ""
        out["video_id"] = vid
        out["video_id_src"] = "redirect" if out.get("short_redirect") else "url"
        for u in (f"https://www.iesdouyin.com/share/video/{vid}/",
                  f"https://www.douyin.com/video/{vid}"):
            for label, kw in (("requests", {}), ("curl_cffi", {"impersonate": "chrome"})):
                t, body = _fetch(u, headers={"User-Agent": MOBILE_UA,
                                             "Referer": "https://www.douyin.com/"}, **kw)
                t["via"] = label
                t["has_ROUTER_DATA"] = "_ROUTER_DATA" in body
                t["has_RENDER_DATA"] = "RENDER_DATA" in body
                t["has_videoInfo"] = "videoInfo" in body
                out["tries"].append(t)
    else:
        t, body = _fetch(url, headers={"User-Agent": DESKTOP_UA})
        t["via"] = "requests"
        out["tries"].append(t)

    out["has_curl_cffi"] = _has_curl_cffi()
    return out
