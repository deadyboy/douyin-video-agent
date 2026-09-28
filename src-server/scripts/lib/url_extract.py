# -*- coding: utf-8 -*-
"""
url_extract.py — 从抖音分享文本中提取干净短链的纯函数。

把 URL 提取+清洗逻辑从 add_link.py 抽出，并修复两个真实缺陷：

  G1 尾部反斜杠：分享文本常见 ".../xxx/\\" 结尾（手机/电脑复制粘贴带入），
     旧 `\\S+` 会把尾部 \\ 一起吞进 URL，导致后续 redirect/SSR 失败
     （data/failed.jsonl 有真实失败现场，如 " https://v.douyin.com/ljGlZCkNm0E/\\"）。
  G2 中文/全角标点紧贴：URL 后紧贴 "，好视频" 这类文案时，旧 `\\S+` 会把
     中文字符也吞进 URL，且 `rstrip` 无法清除。

修复思路：用"只允许 URL 安全字符"的字符类限定短链主体 —— `\\`、中文、全角标点
根本不会被匹配进 URL（治本而非事后猜删），再保留一层尾标点 rstrip 兜底兼容。

纯函数：输入分享文本 → 输出第一个干净的 v.douyin.com 短链（或 None），无副作用。
"""

import re

# 短链主体只匹配 URL 安全字符：字母数字 + _ . ~ % - / ? # & =
# 注意：\\ 不在字符集内 → 尾部反斜杠天然不被吞入（G1）；
#       中文/全角标点不在字符集内 → 紧贴的 "，好视频" 等不被吞入（G2）。
RE_DOUYIN_URL = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9._~%\-/?#=&]+")

# 兜底尾随清洗：正则匹配可能包含语义上属于文案的 URL 安全字符
# （如省略号 "..."、"…"、右引号/右括号等紧贴 URL 的场景）。
# 覆盖：ASCII 标点 . , ; : ! ? 与省略号、反斜杠 \\（G1 双保险）、
#       全角/中文标点 ，。；：！？、）、中文右括号/书名号（ ）】》）、
#       中英文右引号 " ' ” ’ 与 ASCII 右括号 ) ] }。
_TRAILING_PUNCT = ".,;:!?…\\，。；：！？、）】》\"'”’)]}"


def extract_douyin_url(text: str) -> str | None:
    """提取分享文本中第一个干净的抖音 v.douyin.com 短链；无则返回 None。

    行为约定：
    - 保留对 v.douyin.com 短链的匹配（短码不进行 resolve，原样返回）；
    - 多 URL 时取第一个匹配；
    - G1：尾部反斜杠不进入 URL；
    - G2：紧贴 URL 的中文/全角标点不进入 URL；
    - 纯文本无短链 → None。
    """
    if not text or not isinstance(text, str):
        return None
    m = RE_DOUYIN_URL.search(text)
    if not m:
        return None
    url = m.group(0)
    # 双保险：再剥一层紧贴 URL 的尾随标点/反斜杠（如 ".../xxx/..." 的省略号）
    url = url.rstrip(_TRAILING_PUNCT)
    return url or None
