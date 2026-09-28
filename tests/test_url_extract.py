# -*- coding: utf-8 -*-
"""
test_url_extract.py — lib/url_extract.extract_douyin_url 的回归测试。

覆盖：
- 标准短链原样返回
- G1：尾部反斜杠（data/failed.jsonl 真实失败样本回归）
- G2：URL 后紧贴中文/全角标点与汉字
- 尾部各类标点清洗（ASCII 省略号/引号、"%" 等合法字符不被误删）
- 纯文本无 URL → None
- 多个 URL → 取第一个
- 前导空格 / 分享文案夹杂（项目 add_link.py 文档字符串模板）

可用 pytest 或直接运行本文件（handwritten assert，零第三方依赖）。

运行：
    python -m pytest tests/test_url_extract.py -v
    python tests/test_url_extract.py
"""

import os
import sys

# 让测试可从项目根直接导入 src-server 下的模块
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
SRC_SERVER = os.path.join(ROOT, "src-server")
SRC_SCRIPTS = os.path.join(SRC_SERVER, "scripts")
if SRC_SCRIPTS not in sys.path:
    sys.path.insert(0, SRC_SCRIPTS)

from lib.url_extract import extract_douyin_url


def test_standard_short_link_passthrough():
    url = "https://v.douyin.com/ABC123/"
    assert extract_douyin_url(url) == url


def test_leading_space_trailing_backslash_g1_regression():
    # data/failed.jsonl 真实失败现场：前导空格 + 尾部反斜杠
    raw = " https://v.douyin.com/ljGlZCkNm0E/\\"
    assert extract_douyin_url(raw) == "https://v.douyin.com/ljGlZCkNm0E/"


def test_trailing_cjk_punctuation_g2():
    # 中文标点紧贴 URL 后，且标点之后还跟着汉字文案
    raw = "https://v.douyin.com/vTsxwbKlHKA/，好视频"
    assert extract_douyin_url(raw) == "https://v.douyin.com/vTsxwbKlHKA/"


def test_trailing_cjk_punctuation_variants():
    cases = {
        "https://v.douyin.com/ABC123/……，": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/。": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/，": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/）": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/】": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/》": "https://v.douyin.com/ABC123/",
    }
    for raw, expected in cases.items():
        assert extract_douyin_url(raw) == expected, f"case {raw!r}"


def test_fullwidth_and_ascii_mixed_punct():
    cases = {
        "https://v.douyin.com/ABC123/；": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/、": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/？": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/！": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/”": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/ABC123/’": "https://v.douyin.com/ABC123/",
    }
    for raw, expected in cases.items():
        assert extract_douyin_url(raw) == expected, f"case {raw!r}"


def test_legit_url_characters_not_stripped():
    # % / - 等是 URL 合法字符，不应被尾标点清洗误删（"..." 省略号除外）
    cases = {
        "https://v.douyin.com/ABC123/": "https://v.douyin.com/ABC123/",
        "https://v.douyin.com/a-b_c.1%2F/": "https://v.douyin.com/a-b_c.1%2F/",
        "https://v.douyin.com/ABC123/?x=1#frag": "https://v.douyin.com/ABC123/?x=1#frag",
    }
    for raw, expected in cases.items():
        assert extract_douyin_url(raw) == expected, f"case {raw!r}"


def test_no_url_returns_none():
    assert extract_douyin_url("") is None
    assert extract_douyin_url("   ") is None
    assert extract_douyin_url("纯文本，没有链接") is None
    assert extract_douyin_url("www.douyin.com/video/123 不带协议") is None
    # 非 v.douyin.com 域名不匹配
    assert extract_douyin_url("https://example.com/video/123") is None


def test_multiple_urls_takes_first():
    raw = (
        "作者主页 https://v.douyin.com/FIRST1A/ 引用 https://v.douyin.com/SECOND2B/ 末尾"
    )
    assert extract_douyin_url(raw) == "https://v.douyin.com/FIRST1A/"


def test_mid_text_douyin_share_template():
    # add_link.py 文档字符串里的真实分享文案形态
    raw = (
        "1.76 复制打开抖音，看看【Rrrruuuii的作品】这里是地狱吗 "
        "https://v.douyin.com/IwKUHooX8us/ ..."
    )
    assert extract_douyin_url(raw) == "https://v.douyin.com/IwKUHooX8us/"


def test_emoji_adjacent_url():
    raw = "看这个 https://v.douyin.com/DrYp2AA9IyY/ \U0001f44d 赞"
    assert extract_douyin_url(raw) == "https://v.douyin.com/DrYp2AA9IyY/"


def test_extract_is_side_effect_free():
    raw = " https://v.douyin.com/ABC123/\\，xx"
    before = raw
    extract_douyin_url(raw)
    assert raw == before


if __name__ == "__main__":
    # 零 pytest 依赖也可直接运行
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
