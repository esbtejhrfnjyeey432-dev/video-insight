# -*- coding: utf-8 -*-
"""通用解析引擎测试：B站真实链接 / 直链 / 无效链接错误提示"""
import os
import shutil
import tempfile

import imageio_ffmpeg
import resolver

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
tmpdir = tempfile.mkdtemp(prefix="vi_test_")

def case(name, text):
    print(f"\n===== {name} =====")
    try:
        platform, title, path = resolver.download_video(text, tmpdir, FFMPEG)
        size = os.path.getsize(path) / 1024 / 1024
        print(f"OK  平台={platform}  标题={title[:40]!r}  文件={os.path.basename(path)}  {size:.1f}MB")
        return True
    except resolver.ResolveError as e:
        print(f"RESOLVE_ERROR（预期内的友好提示）: {e}")
        return False
    except Exception as e:
        print(f"UNEXPECTED_FAIL: {type(e).__name__}: {str(e)[:200]}")
        return False

# 1. B 站（yt-dlp 通用引擎主战场）
ok_bili = case("B站视频", "https://www.bilibili.com/video/BV1GJ411x7h7")

# 2. 带文案混排的 B 站链接（模拟用户粘贴整段分享文案）
ok_bili_text = case("B站分享文案混排", "【哔哩哔哩】你正在看的是经典名场面 https://www.bilibili.com/video/BV1GJ411x7h7?share_source=copy_web 快来看看吧！")

# 3. 无效链接（验证错误提示是否友好）
case("无效网站", "https://example-not-a-video-site.com/abc")

# 4. 非视频内容链接
case("普通网页", "https://www.baidu.com")

print("\n===== 汇总 =====")
print("B站直链:", "PASS" if ok_bili else "FAIL")
print("B站文案:", "PASS" if ok_bili_text else "FAIL")
shutil.rmtree(tmpdir, ignore_errors=True)
