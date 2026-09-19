# -*- coding: utf-8 -*-
"""小红书笔记解析（移动端 Web API + x-s 签名，无需登录态）。

小红书网页版（www.xiaohongshu.com/explore/xxx）现在对无登录态的请求
不返回笔记数据（__INITIAL_STATE__ 里的 noteData.data 恒为空）。
但移动端 Web API（edith.xiaohongshu.com/api/sns/web/v1/feed）只要带上
正确的 x-s / x-t / x-s-common 签名和匿名设备 cookie（a1）就能取到笔记。

签名算法是公开逆向的（参考 ReaJason/xhs 开源项目），本模块做了精简自包含实现。
"""
import binascii
import ctypes
import hashlib
import json
import random
import string
import time
import urllib.parse

import requests

API_HOST = "https://edith.xiaohongshu.com"
PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_LOOKUP = [
    "Z", "m", "s", "e", "r", "b", "B", "o", "H", "Q", "t", "N", "P", "+", "w",
    "O", "c", "z", "a", "/", "L", "p", "n", "g", "G", "8", "y", "J", "q", "4",
    "2", "K", "W", "Y", "j", "0", "D", "S", "f", "d", "i", "k", "x", "3", "V",
    "T", "1", "6", "I", "l", "U", "A", "F", "M", "9", "7", "h", "E", "C", "v",
    "u", "R", "X", "5",
]


def _mrc(e):
    ie = [
        0, 1996959894, 3993919788, 2567524794, 124634137, 1886057615, 3915621685,
        2657392035, 249268274, 2044508324, 3772115230, 2547177864, 162941995,
        2125561021, 3887607047, 2428444049, 498536548, 1789927666, 4089016648,
        2227061214, 450548861, 1843258603, 4107580753, 2211677639, 325883990,
        1684777152, 4251122042, 2321926636, 335633487, 1661365465, 4195302755,
        2366115317, 997073096, 1281953886, 3579855332, 2724688242, 1006888145,
        1258607687, 3524101629, 2768942443, 901097722, 1119000684, 3686517206,
        2898065728, 853044451, 1172266101, 3705015759, 2882616665, 651767980,
        1373503546, 3369554304, 3218104598, 565507253, 1454621731, 3485111705,
        3099436303, 671266974, 1594198024, 3322730930, 2970347812, 795835527,
        1483230225, 3244367275, 3060149565, 1994146192, 31158534, 2563907772,
        4023717930, 1907459465, 112637215, 2680153253, 3904427059, 2013776290,
        251722036, 2517215374, 3775830040, 2137656763, 141376813, 2439277719,
        3865271297, 1802195444, 476864866, 2238001368, 4066508878, 1812370925,
        453092731, 2181625025, 4111451223, 1706088902, 314042704, 2344532202,
        4240017532, 1658658271, 366619977, 2362670323, 4224994405, 1303535960,
        984961486, 2747007092, 3569037538, 1256170817, 1037604311, 2765210733,
        3554079995, 1131014506, 879679996, 2909243462, 3663771856, 1141124467,
        855842277, 2852801631, 3708648649, 1342533948, 654459306, 3188396048,
        3373015174, 1466479909, 544179635, 3110523913, 3462522015, 1591671054,
        702138776, 2966460450, 3352799412, 1504918807, 783551873, 3082640443,
        3233442989, 3988292384, 2596254646, 62317068, 1957810842, 3939845945,
        2647816111, 81470997, 1943803523, 3814918930, 2489596804, 225274430,
        2053790376, 3826175755, 2466906013, 167816743, 2097651377, 4027552580,
        2265490386, 503444072, 1762050814, 4150417245, 2154129355, 426522225,
        1852507879, 4275313526, 2312317920, 282753626, 1742555852, 4189708143,
        2394877945, 397917763, 1622183637, 3604390888, 2714866558, 953729732,
        1340076626, 3518719985, 2797360999, 1068828381, 1219638859, 3624741850,
        2936675148, 906185462, 1090812512, 3747672003, 2825379669, 829329135,
        1181335161, 3412177804, 3160834842, 628085408, 1382605366, 3423369109,
        3138078467, 570562233, 1426400815, 3317316542, 2998733608, 733239954,
        1555261956, 3268935591, 3050360625, 752459403, 1541320221, 2607071920,
        3965973030, 1969922972, 40735498, 2617837225, 3943577151, 1913087877,
        83908371, 2512341634, 3803740692, 2075208622, 213261112, 2463272603,
        3855990285, 2094854071, 198958881, 2262029012, 4057260610, 1759359992,
        534414190, 2176718541, 4139329115, 1873836001, 414664567, 2282248934,
        4279200368, 1711684554, 285281116, 2405801727, 4167216745, 1634467795,
        376229701, 2685067896, 3608007406, 1308918612, 956543938, 2808555105,
        3495958263, 1231636301, 1047427035, 2932959818, 3654703836, 1088359270,
        936918000, 2847714899, 3736837829, 1202900863, 817233897, 3183342108,
        3401237130, 1404277552, 615818150, 3134207493, 3453421203, 1423857449,
        601450431, 3009837614, 3294710456, 1567103746, 711928724, 3020668471,
        3272380065, 1510334235, 755167117,
    ]
    o = -1

    def right_without_sign(num, bit=0):
        val = ctypes.c_uint32(num).value >> bit
        return (val + 4294967296) % 8589934592 - 4294967296

    for n in range(57):
        o = ie[(o & 255) ^ ord(e[n])] ^ right_without_sign(o, 8)
    return o ^ -1 ^ 3988292384


def _encode_utf8(e):
    b = []
    m = urllib.parse.quote(e, safe="~()*!.'")
    w = 0
    while w < len(m):
        t = m[w]
        if t == "%":
            s = int(m[w + 1] + m[w + 2], 16)
            b.append(s)
            w += 2
        else:
            b.append(ord(t[0]))
        w += 1
    return b


def _triplet_to_b64(e):
    return (_LOOKUP[63 & (e >> 18)] + _LOOKUP[63 & (e >> 12)]
            + _LOOKUP[(e >> 6) & 63] + _LOOKUP[e & 63])


def _encode_chunk(e, t, r):
    m = []
    for b in range(t, r, 3):
        n = (16711680 & (e[b] << 16)) + ((e[b + 1] << 8) & 65280) + (e[b + 2] & 255)
        m.append(_triplet_to_b64(n))
    return "".join(m)


def _b64_encode(e):
    p = len(e)
    w = p % 3
    u = []
    z = 16383
    h = 0
    zz = p - w
    while h < zz:
        u.append(_encode_chunk(e, h, zz if h + z > zz else h + z))
        h += z
    if w == 1:
        f = e[p - 1]
        u.append(_LOOKUP[f >> 2] + _LOOKUP[(f << 4) & 63] + "==")
    elif w == 2:
        f = (e[p - 2] << 8) + e[p - 1]
        u.append(_LOOKUP[f >> 10] + _LOOKUP[63 & (f >> 4)] + _LOOKUP[(f << 2) & 63] + "=")
    return "".join(u)


def sign(uri, data=None, a1=""):
    """生成 x-s / x-t / x-s-common 签名。"""
    def h(n):
        m = ""
        d = "A4NjFqYu5wPHsO0XTdDgMa2r1ZQocVte9UJBvk6/7=yRnhISGKblCWi+LpfE8xzm3"
        for i in range(0, 32, 3):
            o = ord(n[i])
            g = ord(n[i + 1]) if i + 1 < 32 else 0
            hh = ord(n[i + 2]) if i + 2 < 32 else 0
            x = ((o & 3) << 4) | (g >> 4)
            p = ((15 & g) << 2) | (hh >> 6)
            v = o >> 2
            b = hh & 63 if hh else 64
            if not g:
                p = b = 64
            m += d[v] + d[x] + d[p] + d[b]
        return m

    v = int(round(time.time() * 1000))
    raw = f"{v}test{uri}{json.dumps(data, separators=(',', ':'), ensure_ascii=False) if isinstance(data, dict) else ''}"
    md5 = hashlib.md5(raw.encode("utf-8")).hexdigest()
    x_s = h(md5)
    x_t = str(v)
    common = {
        "s0": 5, "s1": "", "x0": "1", "x1": "3.2.0", "x2": "Windows",
        "x3": "xhs-pc-web", "x4": "2.3.1", "x5": a1, "x6": x_t,
        "x7": x_s, "x8": "", "x9": _mrc(x_t + x_s), "x10": 1,
    }
    encoded = _encode_utf8(json.dumps(common, separators=(",", ":")))
    x_s_common = _b64_encode(encoded)
    return {"x-s": x_s, "x-t": x_t, "x-s-common": x_s_common}


def _get_a1_and_web_id():
    alphabet = string.ascii_letters + string.digits
    d = hex(int(time.time() * 1000))[2:] + "".join(random.choice(alphabet) for _ in range(30)) + "5" + "0" + "000"
    g = (d + str(binascii.crc32(str(d).encode("utf-8"))))[:52]
    return g, hashlib.md5(g.encode("utf-8")).hexdigest()


def fetch_note(note_id, xsec_token, xsec_source="pc_feed"):
    """请求笔记详情，返回 note_card dict（含 video 字段）。"""
    data = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1},
        "xsec_source": xsec_source,
        "xsec_token": xsec_token,
    }
    uri = "/api/sns/web/v1/feed"
    a1, web_id = _get_a1_and_web_id()
    signs = sign(uri, data, a1=a1)
    headers = {
        "User-Agent": PC_UA,
        "Content-Type": "application/json",
        "Origin": "https://www.xiaohongshu.com",
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "application/json, text/plain, */*",
        "x-s": signs["x-s"],
        "x-t": signs["x-t"],
        "x-s-common": signs["x-s-common"],
    }
    cookies = {
        "a1": a1,
        "webId": web_id,
        "gid.sign": "PSF1M3U6EBC/Jv6eGddPbmsWzLI=",
        "gid": "yYWfJfi820jSyYWfJfdidiKK0YfuyikEvfISMAM348TEJC28K23TxI888WJK84q8S4WfY2Sy",
    }
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    r = requests.post(API_HOST + uri, data=body.encode("utf-8"),
                      headers=headers, cookies=cookies, timeout=30)
    j = r.json()
    items = j.get("data", {}).get("items") or j.get("items") or []
    if not items:
        return None, j
    return items[0].get("note_card"), j


def extract_video(note_card):
    """从 note_card 提取视频直链和标题。图文笔记返回 (None, title, 'image')。"""
    if not note_card:
        return None, "", "missing"
    title = (note_card.get("title") or note_card.get("desc") or "")[:80]
    video = note_card.get("video") or {}
    if not video:
        return None, title, "image"
    key = (video.get("consumer") or {}).get("origin_video_key")
    if not key:
        return None, title, "no_key"
    cdns = [
        "https://sns-video-qc.xhscdn.com",
        "https://sns-video-hw.xhscdn.com",
        "https://sns-video-bd.xhscdn.com",
        "https://sns-video-qn.xhscdn.com",
    ]
    return f"{random.choice(cdns)}/{key}", title, "video"


# ---------------------------------------------------------------- 新版签名（xys / mns0101）
def _parse_cookie(cookie_str: str) -> dict:
    out = {}
    for block in cookie_str.split(";"):
        if "=" in block:
            k, v = block.strip().split("=", 1)
            out[k] = v
    return out


def fetch_note_v2(note_id: str, xsec_token: str, cookie_str: str,
                  xsec_source: str = "pc_feed"):
    """用新版 xys 签名 + 用户登录 Cookie 请求笔记详情。返回 (note_card, resp_dict)。"""
    import xys as xys_mod
    data = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1},
        "xsec_source": xsec_source,
        "xsec_token": xsec_token,
    }
    uri = "/api/sns/web/v1/feed"
    ck = _parse_cookie(cookie_str)
    a1 = ck.get("a1", "")
    params = uri + json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    x_s, x_t = xys_mod.xys(params, a1)
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://www.xiaohongshu.com",
        "referer": "https://www.xiaohongshu.com/",
        "user-agent": PC_UA,
        "x-s": x_s,
        "x-t": x_t,
    }
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    r = requests.post(API_HOST + uri, data=body.encode("utf-8"),
                      headers=headers, cookies=ck, timeout=30)
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:300]}
    items = j.get("data", {}).get("items") or j.get("items") or []
    if not items:
        return None, j
    return items[0].get("note_card"), j
