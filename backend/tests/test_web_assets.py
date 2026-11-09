"""shikar Web 前端静态资源测试（不依赖浏览器，仅做静态检查）。

本测试针对仓库根 ``web/`` 下三个前端文件：

* ``web/index.html`` / ``web/styles.css`` / ``web/app.js`` 必须存在；
* 三者均**不得**出现 ``http://`` 或 ``https://`` 外链（离线可用）；
* ``app.js`` 必须引用同源 API 路径 ``/api/overview`` 与 ``/api/xiaoqu/``。

路径解析相对于仓库根（``backend/tests`` 的上两级），因此不依赖 pytest 的
工作目录，也不需要第三方依赖。
"""

from __future__ import annotations

import os

#: 仓库根目录：本文件位于 ``<repo>/backend/tests/test_web_assets.py``。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: web 目录与三个前端文件。
_WEB_DIR = os.path.join(_REPO_ROOT, "web")
_WEB_FILES = ("index.html", "styles.css", "app.js")


def _web_path(name: str) -> str:
    """返回 ``web/`` 下某文件的绝对路径。"""
    return os.path.join(_WEB_DIR, name)


def _read(name: str) -> str:
    """以 UTF-8 读取 ``web/`` 下某文件的文本内容。"""
    with open(_web_path(name), "r", encoding="utf-8") as fh:
        return fh.read()


def test_web_files_exist() -> None:
    """三个前端文件都必须存在且非空。"""
    for name in _WEB_FILES:
        path = _web_path(name)
        assert os.path.isfile(path), f"缺少前端文件: {path}"
        assert os.path.getsize(path) > 0, f"前端文件为空: {path}"


def test_no_external_links() -> None:
    """三个文件都不得包含 ``http://`` 或 ``https://`` 外链资源。"""
    for name in _WEB_FILES:
        text = _read(name)
        assert "http://" not in text, f"{name} 含 http:// 外链"
        assert "https://" not in text, f"{name} 含 https:// 外链"


def test_app_js_references_api() -> None:
    """``app.js`` 必须引用同源 API：/api/overview 与 /api/xiaoqu/。"""
    js = _read("app.js")
    assert "/api/overview" in js, "app.js 未引用 /api/overview"
    assert "/api/xiaoqu/" in js, "app.js 未引用 /api/xiaoqu/"


def test_index_html_links_assets() -> None:
    """``index.html`` 应通过相对路径引用本站样式与脚本。"""
    html = _read("index.html")
    assert "styles.css" in html, "index.html 未引用 styles.css"
    assert "app.js" in html, "index.html 未引用 app.js"
    assert "shikar" in html, "index.html 缺少品牌标题"
