# -*- coding: utf-8 -*-
"""生成一个中文教学示例视频（5 页幻灯片，每页 3 秒，共 15 秒），用于演示和测试解析流程"""
from pathlib import Path
import subprocess
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
FONT = "C:/Windows/Fonts/msyh.ttc"
W, H = 1280, 720

SLIDES = [
    ("《高效学习法》第一课", "为什么你学得慢？", "#E6F1FB", "#0C447C"),
    ("误区：被动阅读效率低", "反复看、反复划线，只产生「熟悉感错觉」\n记忆效果提升：几乎为 0", "#FAECE7", "#993C1D"),
    ("方法一：主动回忆", "合上书本，凭记忆默写要点\n这就是「测试效应」，记忆提升可达 50%", "#E1F5EE", "#085041"),
    ("方法二：间隔重复", "按 1 天 / 3 天 / 7 天的节奏复习\n对抗艾宾浩斯遗忘曲线", "#FBEAF0", "#72243E"),
    ("方法三：费曼技巧", "用自己的话讲给别人听\n哪里卡壳，哪里就是理解漏洞", "#FAEEDA", "#633806"),
    ("本课总结", "先主动回忆 → 再间隔重复 → 最后讲出来\n能讲清楚，才算真的学会", "#E6F1FB", "#0C447C"),
]


def make_slide(title, body, bg, fg):
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    f_title = ImageFont.truetype(FONT, 64)
    f_body = ImageFont.truetype(FONT, 40)
    f_small = ImageFont.truetype(FONT, 26)
    d.rectangle([0, 0, W, 12], fill=fg)
    d.text((100, 200), title, font=f_title, fill=fg)
    y = 340
    for line in body.split("\n"):
        d.text((100, y), line, font=f_body, fill=fg)
        y += 70
    d.text((100, 640), "高效学习法 · 示例教学视频", font=f_small, fill=fg)
    return img


def main():
    slides_dir = BASE / "slides"
    slides_dir.mkdir(exist_ok=True)
    list_file = BASE / "slides.txt"

    lines = []
    for i, (t, b, bg, fg) in enumerate(SLIDES):
        p = slides_dir / f"s{i}.png"
        make_slide(t, b, bg, fg).save(p)
        lines.append(f"file '{p.as_posix()}'\nduration 3")
    lines.append(f"file '{(slides_dir / ('s' + str(len(SLIDES) - 1) + '.png')).as_posix()}'")
    list_file.write_text("\n".join(lines), encoding="ascii")

    out = BASE / "sample_lesson.mp4"
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-vf", "scale=1280:720,format=yuv420p",
        "-c:v", "libx264", "-r", "24", str(out),
    ]
    subprocess.run(cmd, check=True)
    print("OK:", out)


if __name__ == "__main__":
    main()
