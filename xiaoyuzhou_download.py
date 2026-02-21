#!/usr/bin/env python3
"""小宇宙播客下载、转录与总结工具

完整流程: 下载播客音频 → 通义听悟转录 → Claude 脱水总结 → 保存 Markdown

用法:
  # 完整流程（下载 + 转录 + 总结）
  python xiaoyuzhou_download.py <episode_url>

  # 仅下载音频
  python xiaoyuzhou_download.py --download-only <episode_url>

  # 对已有的音频 URL 直接转录 + 总结（跳过下载）
  python xiaoyuzhou_download.py --audio-url <公开音频URL> --title <标题>

示例:
  python xiaoyuzhou_download.py https://www.xiaoyuzhoufm.com/episode/67b0f5e45e77040a10ead57d

环境变量（转录和总结功能需要）:
  ALIBABA_CLOUD_ACCESS_KEY_ID      阿里云 AccessKey ID
  ALIBABA_CLOUD_ACCESS_KEY_SECRET  阿里云 AccessKey Secret
  TINGWU_APP_KEY                   通义听悟 AppKey（在听悟管控台创建）
  ANTHROPIC_API_KEY                Anthropic API Key
"""

import argparse
import datetime
import json
import os
import re
import sys
import textwrap
import time
from urllib.parse import urlparse

import anthropic
import requests
from aliyunsdkcore.auth.credentials import AccessKeyCredential
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from bs4 import BeautifulSoup

# ── 目录配置 ──────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads", "xiaoyuzhou")
SUMMARY_DIR = os.path.join(BASE_DIR, "summaries")

# ── HTTP 请求头 ───────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ── 通义听悟 API 配置 ────────────────────────────────────────────────────────
TINGWU_DOMAIN = "tingwu.cn-beijing.aliyuncs.com"
TINGWU_API_VERSION = "2023-09-30"

# ── Claude 脱水总结 System Prompt ─────────────────────────────────────────────
SUMMARY_SYSTEM_PROMPT = """\
# 角色
你是一位"高密度信息萃取专家"。你的任务是把一份冗长的播客逐字稿，压缩为一份面向"认知效率极高的读者"的脱水笔记。

# 处理规则
1. **噪音过滤**：静默删除以下内容——
   - 开场寒暄、节目赞助/广告口播、嘉宾互捧、"对对对/嗯嗯/哈哈"等语气填充
   - 重复表达同一观点的不同说法（只保留最精炼的一版）
   - 纯粹的情感共鸣段落（"我当时特别感动"），除非它直接承载一个关键论据

2. **信息分级**（按优先级从高到低提取）：
   - S级：反直觉观点、有数据/案例支撑的独到方法论、嘉宾亲历的"决策转折点"
   - A级：行业趋势判断、可复用的框架/模型/工具
   - B级：书籍/人物/资源推荐
   - 忽略：众所周知的常识性铺垫

3. **结构化输出**（严格按以下格式）：

---

## 元信息
- 节目/期数：
- 嘉宾：（一句话说明此人为什么值得听）
- 核心议题：（一句话概括）

## 🧠 核心洞察（3-7条，每条不超过3句话）
> 格式：【洞察标题】+ 论据/案例 + 与常规认知的差异点
> 如果嘉宾给出了具体数字或时间节点，务必保留。

## ✅ 行动清单（可直接执行的建议，附门槛说明）
> 格式：动作 + 预期效果 + 最低启动成本
> 过滤掉"多读书""保持好奇心"这类正确但无法操作的建议。

## 💬 金句摘要（3-5句，保留原话）
> 只收录满足以下任一条件的句子：
> - 高信息密度（一句话浓缩了一整段逻辑）
> - 有强烈的"可引用性"（适合发朋友圈/做笔记标注）

## 📚 延伸资源
> 播客中提到的书、论文、工具、人物，附简要说明。

## ⚡ 一句话总结
> 用一句不超过30字的话，回答"听完这期，我最该记住什么？"

---

# 约束
- 输出语言与逐字稿一致。
- 不要编造逐字稿中没有的信息。
- 如果某个模块在本期内容中确实无法提取（比如没有可执行的行动建议），标注"本期未涉及"而非硬凑。"""


# ═══════════════════════════════════════════════════════════════════════════════
# 第一步：下载
# ═══════════════════════════════════════════════════════════════════════════════

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
                bar = "\u2588" * int(pct // 2) + "\u2591" * (50 - int(pct // 2))
                size_mb = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                print(f"\r  [{bar}] {pct:5.1f}%  {size_mb:.1f}/{total_mb:.1f} MB", end="", flush=True)

    print()  # 换行
    print(f"下载完成: {filepath}")
    return filepath


# ═══════════════════════════════════════════════════════════════════════════════
# 第二步：通义听悟转录
# ═══════════════════════════════════════════════════════════════════════════════

def _tingwu_request(client: AcsClient, method: str, uri: str,
                    body: dict = None, query_params: dict = None) -> dict:
    """发送通义听悟 API 请求"""
    request = CommonRequest()
    request.set_accept_format("json")
    request.set_domain(TINGWU_DOMAIN)
    request.set_version(TINGWU_API_VERSION)
    request.set_protocol_type("https")
    request.set_method(method)
    request.set_uri_pattern(uri)
    request.add_header("Content-Type", "application/json")

    if query_params:
        for k, v in query_params.items():
            request.add_query_param(k, v)

    if body:
        request.set_content(json.dumps(body).encode("utf-8"))

    response = client.do_action_with_exception(request)
    return json.loads(response)


def create_tingwu_client() -> AcsClient:
    """创建阿里云 AcsClient（用于通义听悟 API 调用）"""
    ak_id = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
    ak_secret = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not ak_id or not ak_secret:
        raise RuntimeError(
            "请设置环境变量 ALIBABA_CLOUD_ACCESS_KEY_ID 和 "
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET\n"
            "参考: https://help.aliyun.com/zh/tingwu/offline-transcribe-of-audio-and-video-files"
        )
    credentials = AccessKeyCredential(ak_id, ak_secret)
    return AcsClient(region_id="cn-beijing", credential=credentials)


def create_transcription_task(client: AcsClient, audio_url: str, app_key: str) -> str:
    """创建通义听悟离线转录任务

    Args:
        client: 阿里云 AcsClient
        audio_url: 音频文件的公开 HTTP/HTTPS URL
        app_key: 通义听悟 AppKey

    Returns:
        TaskId
    """
    task_key = "podcast_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    body = {
        "AppKey": app_key,
        "Input": {
            "SourceLanguage": "cn",
            "TaskKey": task_key,
            "FileUrl": audio_url,
        },
        "Parameters": {
            "Transcription": {
                "DiarizationEnabled": True,
            },
        },
    }

    print(f"正在创建通义听悟转录任务...")
    result = _tingwu_request(
        client, "PUT", "/openapi/tingwu/v2/tasks",
        body=body,
        query_params={"type": "offline"},
    )

    if str(result.get("Code")) != "0":
        raise RuntimeError(f"创建听悟任务失败: {json.dumps(result, ensure_ascii=False)}")

    task_id = result["Data"]["TaskId"]
    print(f"任务已创建，TaskId: {task_id}")
    return task_id


def wait_for_transcription(client: AcsClient, task_id: str,
                           poll_interval: int = 15, max_wait: int = 10800) -> dict:
    """轮询等待通义听悟转录任务完成

    Args:
        client: 阿里云 AcsClient
        task_id: 任务 ID
        poll_interval: 轮询间隔（秒）
        max_wait: 最大等待时间（秒），默认 3 小时

    Returns:
        Result 字典，包含 Transcription URL 等
    """
    uri = f"/openapi/tingwu/v2/tasks/{task_id}"
    elapsed = 0

    print(f"等待转录完成（每 {poll_interval} 秒检查一次）...")
    while elapsed < max_wait:
        result = _tingwu_request(client, "GET", uri)
        status = result.get("Data", {}).get("TaskStatus", "UNKNOWN")

        if status == "COMPLETED":
            print(f"转录完成！")
            return result["Data"]["Result"]
        elif status in ("FAILED", "ERROR"):
            raise RuntimeError(
                f"转录任务失败: {json.dumps(result, ensure_ascii=False)}"
            )

        print(f"  [{datetime.datetime.now().strftime('%H:%M:%S')}] 状态: {status}")
        time.sleep(poll_interval)
        elapsed += poll_interval

    raise RuntimeError(f"转录任务超时（已等待 {max_wait} 秒）")


def download_transcript(result: dict) -> str:
    """从转录结果中下载并提取纯文本

    通义听悟返回的 Result 中包含一个 Transcription URL，
    下载后解析 JSON，拼接所有段落的文字。

    Args:
        result: wait_for_transcription() 返回的 Result 字典

    Returns:
        完整的转录文本
    """
    transcript_url = result.get("Transcription")
    if not transcript_url:
        raise RuntimeError(
            f"转录结果中未找到 Transcription URL: "
            f"{json.dumps(result, ensure_ascii=False)}"
        )

    print(f"正在下载转录结果...")
    resp = requests.get(transcript_url, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    paragraphs = data.get("Transcription", {}).get("Paragraphs", [])
    if not paragraphs:
        raise RuntimeError("转录结果中没有找到任何段落文本")

    lines = []
    for para in paragraphs:
        speaker = para.get("SpeakerId", "")
        words = para.get("Words", [])
        text = "".join(w.get("Text", "") for w in words)
        if text.strip():
            if speaker:
                lines.append(f"[说话人{speaker}] {text}")
            else:
                lines.append(text)

    transcript = "\n\n".join(lines)
    char_count = len(transcript)
    print(f"转录文本提取完成: {len(lines)} 段，{char_count} 字")
    return transcript


def save_transcript(transcript: str, title: str) -> str:
    """将原始转录文本保存到文件"""
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    filename = sanitize_filename(title) + "_transcript.txt"
    filepath = os.path.join(SUMMARY_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(transcript)
    print(f"原始转录已保存: {filepath}")
    return filepath


# ═══════════════════════════════════════════════════════════════════════════════
# 第三步：Claude 脱水总结
# ═══════════════════════════════════════════════════════════════════════════════

def summarize_with_claude(transcript: str) -> str:
    """使用 Claude API 对转录文本进行脱水总结

    Args:
        transcript: 完整的转录文本

    Returns:
        Markdown 格式的脱水笔记
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("请设置环境变量 ANTHROPIC_API_KEY")

    client = anthropic.Anthropic()

    user_message = f"<transcript>\n{transcript}\n</transcript>"

    print("正在使用 Claude 生成脱水笔记（可能需要一些时间）...")

    # 使用 streaming 避免长输入/输出超时
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        system=SUMMARY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        response = stream.get_final_message()

    # 从响应中提取文本（跳过 thinking blocks）
    parts = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)

    summary = "\n".join(parts)
    print(f"脱水笔记生成完成: {len(summary)} 字")
    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# 第四步：保存 Markdown
# ═══════════════════════════════════════════════════════════════════════════════

def save_summary(summary: str, title: str) -> str:
    """将脱水笔记保存为 Markdown 文件

    Args:
        summary: Markdown 格式的脱水笔记
        title: 播客标题（用作文件名）

    Returns:
        保存的文件路径
    """
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    filename = sanitize_filename(title) + ".md"
    filepath = os.path.join(SUMMARY_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(summary)

    print(f"脱水笔记已保存: {filepath}")
    return filepath


# ═══════════════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="小宇宙播客下载、转录与总结工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            环境变量:
              ALIBABA_CLOUD_ACCESS_KEY_ID      阿里云 AccessKey ID
              ALIBABA_CLOUD_ACCESS_KEY_SECRET  阿里云 AccessKey Secret
              TINGWU_APP_KEY                   通义听悟 AppKey
              ANTHROPIC_API_KEY                Anthropic API Key
        """),
    )

    parser.add_argument(
        "episode_url", nargs="?", default=None,
        help="小宇宙节目链接（完整流程：下载 + 转录 + 总结）",
    )
    parser.add_argument(
        "--download-only", action="store_true",
        help="仅下载音频，不进行转录和总结",
    )
    parser.add_argument(
        "--audio-url",
        help="直接提供音频 URL 进行转录 + 总结（跳过下载步骤）",
    )
    parser.add_argument(
        "--title", default=None,
        help="播客标题（与 --audio-url 搭配使用）",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # ── 模式一：通过音频 URL 直接转录 + 总结 ──
    if args.audio_url:
        audio_url = args.audio_url
        title = args.title or "untitled_podcast"

        app_key = os.environ.get("TINGWU_APP_KEY")
        if not app_key:
            print("错误: 请设置环境变量 TINGWU_APP_KEY")
            sys.exit(1)

        tingwu_client = create_tingwu_client()
        task_id = create_transcription_task(tingwu_client, audio_url, app_key)
        result = wait_for_transcription(tingwu_client, task_id)
        transcript = download_transcript(result)
        save_transcript(transcript, title)

        summary = summarize_with_claude(transcript)
        save_summary(summary, title)
        return

    # ── 模式二 / 三：从小宇宙链接开始 ──
    if not args.episode_url:
        print(__doc__)
        sys.exit(1)

    url = args.episode_url.strip()
    if "xiaoyuzhoufm.com" not in url:
        print("错误: 请输入小宇宙（xiaoyuzhoufm.com）的播客链接")
        sys.exit(1)

    # Step 1: 提取信息并下载
    audio_url, title = extract_audio_info(url)
    download_audio(audio_url, title)

    if args.download_only:
        return

    # Step 2: 通义听悟转录
    app_key = os.environ.get("TINGWU_APP_KEY")
    if not app_key:
        print("错误: 请设置环境变量 TINGWU_APP_KEY")
        sys.exit(1)

    tingwu_client = create_tingwu_client()
    task_id = create_transcription_task(tingwu_client, audio_url, app_key)
    result = wait_for_transcription(tingwu_client, task_id)
    transcript = download_transcript(result)
    save_transcript(transcript, title)

    # Step 3: Claude 总结
    summary = summarize_with_claude(transcript)

    # Step 4: 保存 Markdown
    save_summary(summary, title)


if __name__ == "__main__":
    main()
