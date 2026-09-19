# -*- coding: utf-8 -*-
"""小红书新版 X-S 签名（XYS / mnsv2 算法，2025 起）。

小红书 2025 年把 x-s 签名从老的「md5 + 自定义表」换成了 mnsv2（XYS_ 前缀）。
老算法现在一律 406。本模块是从 Go 开源实现（smalls0098/xs）移植的纯 Python 版本。

注意：该签名依赖登录态 cookie 里的 a1 字段（匿名生成的 a1 过不了风控），
所以小红书解析需要用户提供登录后的 Cookie。
"""
import base64
import hashlib
import json
import random
import time
import urllib.parse

_ALPHABET = "NOPQRStuvwxWXYZabcyz012DEFTKLMdefghijkl4563GHIJBC7mnop89+/"
_BASE = 58

_STD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUSTOM_B64 = "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5"

U32 = 0xFFFFFFFF


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _compute_value(seed: int) -> int:
    seed &= U32
    s15 = seed >> 15
    s13 = seed >> 13
    s12 = seed >> 12
    s10 = seed >> 10
    xor_part = (s15 & (~s13)) | (s13 & (~s15))
    xor_part &= U32
    return ((xor_part ^ s12 ^ s10) << 31) & U32


def _xor(arr: bytes, seed: int) -> bytes:
    seed &= U32
    res = bytearray()
    for b in arr:
        res.append(b ^ (seed & 0xFF))
        seed = (_compute_value(seed) | (seed >> 1)) & U32
    return bytes(res)


def _base58_encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    if n == 0:
        return _ALPHABET[0]
    res = []
    while n > 0:
        n, r = divmod(n, _BASE)
        res.append(_ALPHABET[r])
    return "".join(reversed(res))


def _custom_b64(data: bytes) -> str:
    std = base64.b64encode(data).decode()
    std = std.rstrip("=")
    return "".join(_CUSTOM_B64[_STD_B64.index(c)] for c in std)


def _encrypt_encode_utf8(s: str) -> bytes:
    """对应 Go 的 url.PathEscape + 逐字节转十六进制。"""
    encoded = urllib.parse.quote(s, safe="")
    out = bytearray()
    i = 0
    while i < len(encoded):
        if encoded[i] == "%":
            out.append(int(encoded[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(ord(encoded[i]))
            i += 1
    return bytes(out)


def _encode_timestamp(ts: int, randomize_first: bool) -> bytes:
    key = [41] * 8
    arr = ts.to_bytes(8, "little")
    encoded = bytearray(arr[i] ^ key[i] for i in range(8))
    if randomize_first:
        encoded[0] = random.randint(0, 255)
    return bytes(encoded)


def _hash_xor(hash_hex: str, xor_key: int) -> bytes:
    data = bytes.fromhex(hash_hex)
    return bytes(b ^ xor_key for b in data[:8])


def _bytes_prefix_len(s: str) -> bytes:
    data = s.encode("utf-8")
    return bytes([len(data)]) + data


def _build_x3(rand_num: int, ts: int, startup_ts: int, hash_hex: str,
              a1: str, platform: str, params: str) -> bytes:
    arr = bytearray([119, 104, 96, 41])
    rand_data = rand_num.to_bytes(4, "little")
    arr += rand_data
    arr += _encode_timestamp(ts, True)
    arr += startup_ts.to_bytes(8, "little")
    arr += (4).to_bytes(4, "little")
    arr += (1269).to_bytes(4, "little")
    arr += len(params).to_bytes(4, "little")
    arr += _hash_xor(hash_hex, rand_data[0])
    arr += _bytes_prefix_len(a1)
    arr += _bytes_prefix_len(platform)
    arr += bytes([1, random.randint(0, 255),
                  249, 83, 102, 103, 201, 181, 128, 99, 94, 7, 68, 250, 132, 21])
    return bytes(arr)


def _mns0101(hash_hex: str, a1: str, platform: str, params: str,
              rand_num: int, ts: int, startup_ts: int) -> str:
    x3 = _build_x3(rand_num, ts, startup_ts, hash_hex, a1, platform, params)
    data = _xor(x3, 858975407)
    return "mns0101_" + _base58_encode(data)


def xys(params: str, a1: str):
    """生成小红书新版 x-s 签名（XYS_ 前缀）。返回 (x_s, x_t)。

    x-t 必须与签名内部的时间戳一致（服务端会校验），所以这里一并返回。
    """
    platform = "xhs-pc-web"
    hash_hex = _md5_hex(params)
    x4 = "object" if "{" in params else ""
    rand_num = random.randint(0, 0xFFFFFFFF)
    ts = int(time.time() * 1000)
    startup_ts = ts - random.randint(1000, 5000)
    x3 = _mns0101(hash_hex, a1, platform, params, rand_num, ts, startup_ts)
    obj = {"x0": "4.2.1", "x1": platform, "x2": "Mac OS", "x3": x3, "x4": x4}
    js = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    ret = _encrypt_encode_utf8(js)
    return "XYS_" + _custom_b64(ret), str(ts)
