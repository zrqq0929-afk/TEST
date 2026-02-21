#!/usr/bin/env python3
"""小宇宙播客音频下载器

用法: python xiaoyuzhou_download.py <episode_url>
示例: python xiaoyuzhou_download.py https://www.xiaoyuzhoufm.com/episode/67b0f5e45e77040a10ead57d
"""

import os
import re
import sys
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads", "xiaoyuzhou")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def sanitize_filename(name: str) -> str:
    """移除文件名中不合法的字符"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = name.strip(". ")
    return name[:200] if name else "untitled"


def extract_audio_info(url: str) -> tuple[str, str]:
    """从小宇宙节目页面提取音频下载地址和标题

    Returns:
        (audio_url, title)
    """
    print(f"正在获取页面: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # 提取标题: <meta property="og:title" content="...">
    title_tag = soup.find("meta", property="og:title")
    title = title_tag["content"].strip() if title_tag and title_tag.get("content") else "untitled"

    # 方法1: 从 og:audio meta 标签提取
    audio_tag = soup.find("meta", property="og:audio")
    if audio_tag and audio_tag.get("content"):
        audio_url = audio_tag["content"]
        print(f"从 og:audio 标签找到音频地址")
        return audio_url, title

    # 方法2: 用正则在页面中搜索 m4a / mp3 链接
    patterns = [
        r'https?://[^\s"\'<>]+\.m4a[^\s"\'<>]*',
        r'https?://[^\s"\'<>]+\.mp3[^\s"\'<>]*',
        r'https?://media\.xyzcdn\.net/[^\s"\'<>]+',
    ]
    for pattern in patterns:
        match = re.search(pattern, resp.text)
        if match:
            audio_url = match.group(0)
            print(f"通过正则匹配找到音频地址")
            return audio_url, title

    raise RuntimeError("未能从页面中提取到音频地址，请检查 URL 是否正确")


def download_audio(audio_url: str, title: str) -> str:
    """下载音频文件到本地

    Returns:
        保存的文件路径
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # 根据 URL 判断扩展名
    parsed = urlparse(audio_url.split("?")[0])
    ext = os.path.splitext(parsed.path)[1] or ".m4a"

    filename = sanitize_filename(title) + ext
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    # 若文件已存在则跳过
    if os.path.exists(filepath):
        print(f"文件已存在，跳过下载: {filepath}")
        return filepath

    print(f"正在下载: {title}")
    print(f"音频地址: {audio_url}")

    resp = requests.get(audio_url, headers=HEADERS, stream=True, timeout=60)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0
    chunk_size = 8192

    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = downloaded / total * 100
                bar = "█" * int(pct // 2) + "░" * (50 - int(pct // 2))
                size_mb = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                print(f"\r  [{bar}] {pct:5.1f}%  {size_mb:.1f}/{total_mb:.1f} MB", end="", flush=True)

    print()  # 换行
    print(f"下载完成: {filepath}")
    return filepath


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url = sys.argv[1].strip()

    # 简单校验 URL
    if "xiaoyuzhoufm.com" not in url:
        print("错误: 请输入小宇宙（xiaoyuzhoufm.com）的播客链接")
        sys.exit(1)

    audio_url, title = extract_audio_info(url)
    download_audio(audio_url, title)


if __name__ == "__main__":
    main()
