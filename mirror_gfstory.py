#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27",
#   "tqdm>=4.66",
# ]
# ///
"""
完整镜像 gfstory.pages.dev：
  - 剧情模拟器 (/simulator.html)
  - 剧情编辑器 (/index.html)
  - 阅读器 (/viewer.html)
  - 全部剧情脚本、立绘、背景、音频
  - pagefind 搜索索引

用法（推荐 uv）:
    uv run mirror_gfstory.py                  # 增量抓取到 ./gfstory-mirror
    uv run mirror_gfstory.py -o my-out        # 自定义输出目录
    uv run mirror_gfstory.py --force          # 强制重抓
    uv run mirror_gfstory.py --skip-audio     # 跳过音频（占体积最大）
    uv run mirror_gfstory.py --no-search      # 跳过 pagefind 搜索索引

抓完之后:
    cd gfstory-mirror
    python -m http.server 8000
    # http://localhost:8000/             编辑器
    # http://localhost:8000/simulator.html 模拟器
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from tqdm import tqdm

BASE = "https://gfstory.pages.dev"
DEFAULT_OUT = Path("gfstory-mirror")

ENTRY_PATHS = ["/", "/simulator.html", "/viewer.html"]
EXTRA_PATHS = ["/favicon.ico", "/_headers", "/sample.lua"]
INDEX_PATHS = [
    "/stories/stories.json",
    "/stories/chapters.json",
    "/audio/audio.json",
    "/images/characters.json",
    "/images/backgrounds.json",
]

# 从 HTML 里抠出 src/href（同时支持相对与绝对路径，由 urljoin 解析）
HREF_SRC_RE = re.compile(r'''(?:href|src)\s*=\s*["']([^"'#?]+?)["']''')
# 从 JS/CSS 文本里抠出站内绝对路径
JS_PATH_RE = re.compile(
    r'''["'`](/(?:assets|search|stories|audio|images)/[^"'`?\s)<>]+)'''
)

CONCURRENCY = 32
RETRIES = 4
TIMEOUT = httpx.Timeout(30.0, connect=15.0)
TEXT_EXTS = {".html", ".js", ".css", ".json", ".txt", ".lua", ".svg", ".map"}

# 字体本地化相关
FONT_HOSTS = (
    "fonts.googleapis.com", "fonts.gstatic.com",
    "fonts.font.im", "static.font.im",
)
LINK_TAG_RE = re.compile(r'<link\b[^>]*?>', re.IGNORECASE)
HREF_ATTR_RE = re.compile(r'''\bhref\s*=\s*["']([^"']+)["']''', re.IGNORECASE)
CSS_URL_RE = re.compile(r'''url\(\s*(['"]?)([^'")\s]+)\1\s*\)''')
FONT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


_IS_WIN = sys.platform == "win32"


def _windows_safe_segments(parts: list[str]) -> list[str]:
    """Windows 不允许文件/目录名以空格或点结尾，每段都修剪掉。"""
    cleaned = []
    for seg in parts:
        s = seg.rstrip(" .")
        cleaned.append(s if s else "_")
    return cleaned


def url_to_local(url_path: str, root: Path) -> Path:
    if url_path in ("/", ""):
        return root / "index.html"
    parts = url_path.lstrip("/").split("/")
    if _IS_WIN:
        parts = _windows_safe_segments(parts)
    return root.joinpath(*parts)


def windows_safe_url_path(url_path: str) -> str:
    """把 URL 路径段也按 Windows 规则修剪，用于改写本地 JSON 索引。"""
    if not _IS_WIN or not url_path:
        return url_path
    leading = "/" if url_path.startswith("/") else ""
    parts = url_path.lstrip("/").split("/")
    return leading + "/".join(_windows_safe_segments(parts))


def rewrite_json_paths(obj):
    """递归把 JSON 里看起来像路径的字符串做 Windows 安全化。"""
    if not _IS_WIN:
        return obj
    if isinstance(obj, str):
        if "/" in obj or obj.endswith(
            (".png", ".jpg", ".jpeg", ".webp", ".gif", ".m4a", ".mp3", ".ogg", ".txt")
        ):
            return windows_safe_url_path(obj)
        return obj
    if isinstance(obj, dict):
        return {k: rewrite_json_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite_json_paths(x) for x in obj]
    return obj


def ext_of(p: str) -> str:
    name = p.rsplit("/", 1)[-1]
    if "." not in name:
        return ".html" if not name else ""
    return "." + name.rsplit(".", 1)[-1].lower()


async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            return r.content
        except Exception as e:
            last = e
            await asyncio.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"{url} -> {last}")


async def download_paths(
    client: httpx.AsyncClient,
    paths: list[str],
    root: Path,
    force: bool,
    desc: str,
    *,
    keep_content: bool = False,
) -> dict[str, bytes]:
    """并发下载一批路径。keep_content=True 时把字节流也存进结果，便于解析。"""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, bytes] = {}
    bar = tqdm(total=len(paths), desc=desc, unit="f", ncols=80)

    async def one(p: str) -> None:
        out = url_to_local(p, root)
        try:
            if out.exists() and not force:
                if keep_content:
                    try:
                        results[p] = out.read_bytes()
                    except Exception:
                        results[p] = b""
                else:
                    results[p] = b""
                return
            async with sem:
                try:
                    data = await fetch(client, BASE + p)
                except Exception as e:
                    bar.write(f"  [失败] {p}  {e}")
                    return
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
            except Exception as e:
                bar.write(f"  [写入失败] {p} -> {out}  {e}")
                return
            results[p] = data if keep_content else b""
        finally:
            bar.update(1)

    await asyncio.gather(*[one(p) for p in paths])
    bar.close()
    return results


def scan_text_for_paths(data: bytes, ext: str, base_path: str = "/") -> set[str]:
    """从 HTML/JS/CSS/JSON 文本中挖出站内资源路径，统一返回站内绝对路径。"""
    if not data:
        return set()
    text = data.decode("utf-8", errors="replace")
    paths: set[str] = set(JS_PATH_RE.findall(text))
    if ext == ".html":
        # HTML 里的 href/src 既可能是 ./assets/... 也可能是 /assets/...
        # 用一个虚拟域名让 urljoin 把相对路径补全成绝对
        FAKE = "http://_local_"
        for m in HREF_SRC_RE.finditer(text):
            href = m.group(1).strip()
            if not href:
                continue
            if href.startswith(
                ("http://", "https://", "data:", "javascript:", "mailto:", "//", "#")
            ):
                continue
            try:
                full = urljoin(FAKE + base_path, href)
                parsed = urlparse(full)
                if parsed.netloc == "_local_" and parsed.path:
                    paths.add(parsed.path)
            except Exception:
                pass
    return paths


def collect_resources_from_indexes(
    blobs: dict[str, bytes],
    skip_audio: bool,
) -> set[str]:
    """从五个 JSON 索引里抽出全部资源 URL。"""
    found: set[str] = set()

    def add(prefix: str, value: object) -> None:
        if not isinstance(value, str):
            return
        v = value.strip()
        if not v or v.startswith("http://") or v.startswith("https://"):
            return
        if v.startswith("/"):
            found.add(v)
        else:
            found.add(prefix + v.lstrip("/"))

    def safe_load(path: str):
        raw = blobs.get(path)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as e:
            print(f"  [警告] 解析 {path} 失败: {e}")
            return None

    # stories.json: { "1-1-1.txt": "1-1-1.txt", ... }
    d = safe_load("/stories/stories.json")
    if isinstance(d, dict):
        for v in d.values():
            add("/stories/", v)

    # chapters.json: { type: [ {stories: [{files: [...] | [[...]]}]} ] }
    d = safe_load("/stories/chapters.json")
    if isinstance(d, dict):
        for chap_list in d.values():
            if not isinstance(chap_list, list):
                continue
            for chap in chap_list:
                if not isinstance(chap, dict):
                    continue
                for st in chap.get("stories", []) or []:
                    if not isinstance(st, dict):
                        continue
                    for f in st.get("files", []) or []:
                        if isinstance(f, list):
                            for ff in f:
                                add("/stories/", ff)
                        else:
                            add("/stories/", f)

    # audio.json: { alias: "bgm/xxx.m4a" | "se/xxx.m4a" }
    if not skip_audio:
        d = safe_load("/audio/audio.json")
        if isinstance(d, dict):
            for v in d.values():
                add("/audio/", v)

    # characters.json: { id: { variant: { path, scale, offset } } }
    d = safe_load("/images/characters.json")
    if isinstance(d, dict):
        for variants in d.values():
            if isinstance(variants, dict):
                for entry in variants.values():
                    if isinstance(entry, dict):
                        add("/images/", entry.get("path", ""))

    # backgrounds.json: 结构未知，递归扫描所有图片字符串
    d = safe_load("/images/backgrounds.json")
    if d is not None:
        IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

        def walk(x):
            if isinstance(x, str):
                if x.lower().endswith(IMG_EXTS):
                    add("/images/", x)
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)

        walk(d)

    return found


async def localize_fonts(out: Path, force: bool) -> None:
    """扫描镜像目录里的 HTML，把外链字体 CSS + 字体文件抓到本地并改写引用。"""
    fonts_dir = out / "fonts"
    files_dir = fonts_dir / "files"

    # 1. 扫所有 HTML，找外链字体 CSS
    html_files = sorted(out.glob("*.html"))
    css_url_to_html: dict[str, list[Path]] = {}
    for hf in html_files:
        text = hf.read_text(encoding="utf-8", errors="replace")
        for m in HREF_ATTR_RE.finditer(text):
            href = m.group(1).strip()
            if href.startswith("//"):
                href = "https:" + href
            if not href.startswith(("http://", "https://")):
                continue
            if any(host in href for host in FONT_HOSTS):
                css_url_to_html.setdefault(href, []).append(hf)

    if not css_url_to_html:
        print("  未发现外链字体 CSS")
        return

    fonts_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": FONT_UA,
        "Accept": "text/css,*/*;q=0.1",
    }
    css_url_to_local: dict[str, str] = {}

    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as client:
        for css_url in css_url_to_html:
            digest = hashlib.sha1(css_url.encode()).hexdigest()[:8]
            css_local_name = f"font-{digest}.css"
            css_local_path = fonts_dir / css_local_name
            css_url_to_local[css_url] = f"/fonts/{css_local_name}"

            if css_local_path.exists() and not force:
                print(f"  已存在 (跳过): {css_local_name}")
                continue

            print(f"  下载字体 CSS: {css_url}")
            try:
                r = await client.get(css_url, follow_redirects=True)
                r.raise_for_status()
                css_text = r.text
            except Exception as e:
                print(f"  [失败] {css_url}: {e}")
                continue

            font_urls: set[str] = set()
            for m in CSS_URL_RE.finditer(css_text):
                u = m.group(2).strip()
                if u.startswith("data:"):
                    continue
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith(("http://", "https://")):
                    font_urls.add(u)

            sem = asyncio.Semaphore(8)
            url_to_local_font: dict[str, str] = {}
            taken_names: set[str] = set()

            async def dl_font(url: str) -> None:
                base_name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "font"
                fname = base_name
                i = 1
                while fname in taken_names:
                    stem, _, ext = base_name.rpartition(".")
                    fname = f"{stem}-{i}.{ext}" if stem else f"{base_name}-{i}"
                    i += 1
                taken_names.add(fname)
                local = files_dir / fname
                if not local.exists() or force:
                    async with sem:
                        try:
                            rr = await client.get(url, follow_redirects=True)
                            rr.raise_for_status()
                            local.write_bytes(rr.content)
                        except Exception as e:
                            print(f"    [字体失败] {url}: {e}")
                            return
                url_to_local_font[url] = f"./files/{fname}"

            await asyncio.gather(*[dl_font(u) for u in sorted(font_urls)])
            print(f"    {css_local_name}: 下载 {len(url_to_local_font)}/{len(font_urls)} 个字体文件")

            def replace_url(m: re.Match) -> str:
                quote = m.group(1)
                u = m.group(2).strip()
                if u.startswith("//"):
                    u = "https:" + u
                if u in url_to_local_font:
                    return f"url({quote}{url_to_local_font[u]}{quote})"
                return m.group(0)

            css_local_path.write_text(CSS_URL_RE.sub(replace_url, css_text), encoding="utf-8")

    # 2. 改写 HTML：仅替换 href，不动 rel/as 等其他属性
    for hf in html_files:
        text = hf.read_text(encoding="utf-8", errors="replace")
        original = text

        def patch_link(m: re.Match) -> str:
            tag = m.group(0)
            href_m = HREF_ATTR_RE.search(tag)
            if not href_m:
                return tag
            href = href_m.group(1).strip()
            full = "https:" + href if href.startswith("//") else href
            if not full.startswith(("http://", "https://")):
                return tag
            if not any(host in full for host in FONT_HOSTS):
                return tag
            local = css_url_to_local.get(full)
            if not local:
                return tag
            return tag[:href_m.start(1)] + local + tag[href_m.end(1):]

        text = LINK_TAG_RE.sub(patch_link, text)
        if text != original:
            hf.write_text(text, encoding="utf-8")
            print(f"  已改写 HTML: {hf.name}")


async def run(out: Path, force: bool, no_search: bool, skip_audio: bool, no_fonts: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {out.resolve()}")
    print(f"基准站点: {BASE}\n")

    headers = {"User-Agent": "gfstory-mirror/1.0"}
    async with httpx.AsyncClient(headers=headers, timeout=TIMEOUT) as client:

        # ───── 阶段 1：入口 HTML + JSON 索引 + 杂项静态文件 ─────
        print("[1/4] 抓取入口 HTML 与 JSON 索引")
        first = await download_paths(
            client,
            ENTRY_PATHS + EXTRA_PATHS + INDEX_PATHS,
            out, force, "入口",
            keep_content=True,
        )

        # ───── 阶段 2：递归挖前端 bundle / pagefind 索引 ─────
        print("[2/4] 解析前端 bundle 并递归挖路径")
        seen: set[str] = set(first.keys())
        frontier: set[str] = set()
        for p, data in first.items():
            frontier |= scan_text_for_paths(data, ext_of(p), p)
        frontier -= seen
        if no_search:
            frontier = {x for x in frontier if not x.startswith("/search/")}

        while frontier:
            new = await download_paths(
                client, sorted(frontier), out, force, "前端",
                keep_content=True,
            )
            seen |= set(new.keys())
            next_frontier: set[str] = set()
            for p, data in new.items():
                if ext_of(p) in TEXT_EXTS:
                    next_frontier |= scan_text_for_paths(data, ext_of(p), p)
            next_frontier -= seen
            if no_search:
                next_frontier = {x for x in next_frontier if not x.startswith("/search/")}
            frontier = next_frontier

        # ───── 阶段 3：从 5 个索引 JSON 收集全部剧情/图片/音频 ─────
        print("[3/4] 收集剧情/立绘/背景/音频清单")
        resources = collect_resources_from_indexes(first, skip_audio)
        resources -= seen
        print(f"  待下载资源数: {len(resources)}")

        # ───── 阶段 4：下载所有资源（不保留内容，省内存） ─────
        print("[4/4] 下载全部资源")
        await download_paths(client, sorted(resources), out, force, "资源")

        # ───── Windows 后处理：把 5 个 JSON 索引里的路径同步做安全化 ─────
        if _IS_WIN:
            print("[5/5] Windows 后处理: 修正 JSON 索引中的路径")
            for p in INDEX_PATHS:
                local = url_to_local(p, out)
                if not local.exists():
                    continue
                try:
                    raw = local.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    fixed = rewrite_json_paths(data)
                    if fixed != data:
                        local.write_text(
                            json.dumps(fixed, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        print(f"  已修正: {p}")
                except Exception as e:
                    print(f"  [警告] 修正 {p} 失败: {e}")

    if not no_fonts:
        print("[字体] 本地化外链字体")
        await localize_fonts(out, force)

    print()
    print(f"镜像完成: {out.resolve()}")
    print()
    print("启动方式:")
    print(f"    cd {out}")
    print("    python -m http.server 8000")
    print()
    print("浏览器访问:")
    print("    http://localhost:8000/                编辑器")
    print("    http://localhost:8000/simulator.html  剧情模拟器")
    print("    http://localhost:8000/viewer.html     单剧情阅读器")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="完整镜像 gfstory.pages.dev (剧情模拟器 + 编辑器 + 资源)"
    )
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT,
                    help=f"输出目录 (默认 {DEFAULT_OUT})")
    ap.add_argument("-f", "--force", action="store_true",
                    help="强制重新下载已存在的文件")
    ap.add_argument("--no-search", action="store_true",
                    help="跳过 /search 下的 pagefind 搜索索引")
    ap.add_argument("--skip-audio", action="store_true",
                    help="跳过 /audio 下的音频文件 (体积最大的部分)")
    ap.add_argument("--no-fonts", action="store_true",
                    help="跳过外链字体本地化 (默认会做)")
    ap.add_argument("--fonts-only", action="store_true",
                    help="只对已镜像目录做字体本地化, 不重抓站点")
    args = ap.parse_args()

    try:
        if args.fonts_only:
            if not args.out.exists():
                sys.exit(f"目录不存在: {args.out}")
            print(f"输出目录: {args.out.resolve()}")
            print("[字体] 本地化外链字体")
            asyncio.run(localize_fonts(args.out, args.force))
            print("\n字体本地化完成")
            return
        asyncio.run(run(args.out, args.force, args.no_search, args.skip_audio, args.no_fonts))
    except KeyboardInterrupt:
        sys.exit("\n用户中断")


if __name__ == "__main__":
    main()
