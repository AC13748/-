# mirror_gfstory.py 使用指南

把 [gfstory.pages.dev](https://gfstory.pages.dev) 完整镜像到本地，实现少女前线一代剧情**纯离线**播放与编辑。

镜像内容：

- 剧情模拟器 `/simulator.html`
- 剧情编辑器 `/index.html`
- 阅读器 `/viewer.html`（编辑器导出 zip 时会用）
- 全部剧情脚本（`stories/*.txt`）
- 全部立绘、背景、UI 图（`images/`）
- 全部 BGM 与音效（`audio/`）
- pagefind 搜索索引（`search/`）—— 模拟器侧边栏的剧情搜索功能依赖它
- 外链字体本地化（默认）—— Noto Sans SC 等会一并下载，断网后字体也正常

## 环境要求

- **Python ≥ 3.10**（脚本头部用了 PEP 723 内联依赖声明）
- 推荐使用 [uv](https://github.com/astral-sh/uv) 直接运行，无需手动建虚拟环境
- 依赖：`httpx`、`tqdm`（uv 会自动装）

## 快速开始

```bash
cd D:/Apps/py/pysave/sq

# 抓取（首次大约 1–2 GB，30 路并发，时间取决于网速）
uv run mirror_gfstory.py

# 抓完后启动本地静态服务
cd gfstory-mirror
python -m http.server 8000
```

浏览器打开：

| 地址 | 用途 |
|------|------|
| <http://localhost:8000/> | 剧情**编辑器** |
| <http://localhost:8000/simulator.html> | 剧情**模拟器**（最常用） |
| <http://localhost:8000/viewer.html> | 单剧情**阅读器** |

> **必须从镜像根目录起 HTTP 服务**，不能 `file://` 双击 HTML。模拟器内部用绝对路径 `/audio/`、`/images/`、`/stories/` 加载资源，`file://` 协议下浏览器同源策略会全部 404。

## 命令行选项

```text
uv run mirror_gfstory.py [选项]

  -o, --out PATH     输出目录（默认 ./gfstory-mirror）
  -f, --force        强制重新下载已存在的文件
  --no-search        跳过 /search 下的 pagefind 搜索索引
  --skip-audio       跳过 /audio 下的音频（体积最大的部分）
  --no-fonts         跳过外链字体本地化
  --fonts-only       只对已镜像目录做字体本地化，不重抓站点
```

## 常见用法

### 增量补抓 / 重试失败文件

直接重跑同一条命令即可。脚本默认跳过本地已存在的文件，只下载缺失或上次失败的部分：

```bash
uv run mirror_gfstory.py
```

### 强制全量重抓

```bash
uv run mirror_gfstory.py --force
```

### 只要剧情、不要 BGM（省带宽）

```bash
uv run mirror_gfstory.py --skip-audio --no-search
```

体积可降到约 200 MB 量级，但模拟器会失去 BGM 与剧情搜索功能。

### 已抓好整站，只补字体本地化

```bash
uv run mirror_gfstory.py --fonts-only
```

### 自定义输出目录

```bash
uv run mirror_gfstory.py -o D:/my-gfstory
```

## 不用 uv 的传统方式

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell / CMD
# 或：source .venv/bin/activate    # bash/zsh
pip install httpx tqdm
python mirror_gfstory.py
```

## 抓取流程

脚本分阶段执行，跑的时候终端能看到 `[1/4]`–`[5/5]` 的进度：

| 阶段 | 内容 |
|------|------|
| 1/4 | 入口 HTML（`/`、`/simulator.html`、`/viewer.html`）+ 5 个 JSON 索引 + 杂项静态文件 |
| 2/4 | 递归扫描 HTML/JS/CSS，挖出 `/assets/`、`/search/` 下所有引用并下载，直到没有新路径 |
| 3/4 | 解析 5 个 JSON 索引，收集全部剧情 txt、立绘 png、背景 png、音频 m4a 路径 |
| 4/4 | 并发下载所有资源（32 路并发 + 重试 4 次 + 指数退避） |
| 5/5 | （仅 Windows）修正 JSON 索引里的路径，处理上游含尾部空格的目录名 |
| 字体 | 下载外链字体 CSS 与字体文件，改写 CSS 与 HTML 引用 |

## 输出目录结构

```
gfstory-mirror/
├── index.html              编辑器入口
├── simulator.html          模拟器入口
├── viewer.html             阅读器（编辑器导出 zip 时会 fetch）
├── favicon.ico
├── sample.lua
├── assets/                 Vite 打包的 JS / CSS bundle
├── search/                 pagefind 搜索索引
├── stories/
│   ├── stories.json        剧情文件名映射
│   ├── chapters.json       章节目录树（主线 EP0–14、各活动、联动等）
│   └── *.txt               全部剧情脚本（micromark directive 格式）
├── audio/
│   ├── audio.json          别名 → 音频文件路径
│   ├── bgm/*.m4a
│   └── se/*.m4a
├── images/
│   ├── characters.json     立绘清单（id → path/scale/offset）
│   ├── backgrounds.json    背景清单
│   ├── background/*.png    背景图
│   └── <角色目录>/*.png      立绘
└── fonts/                  字体本地化产物
    ├── font-<hash>.css     改写后的字体 CSS
    └── files/
        └── *.woff2         字体文件
```

## Windows 注意事项

上游 `images/characters.json` 里某些角色（如 `gg_elfeldt`）的目录名末尾带空格，Linux/Cloudflare CDN 不在意但 Windows NTFS 不允许文件/目录名以空格或点结尾。脚本会自动：

1. 写本地文件时把每段路径末尾的空格和点剥掉
2. 跑完后重写 5 个 JSON 索引，让里面的资源路径与本地文件名一致
3. 写入失败的单个文件只打印警告，不会中断整体流程

如果你看到 `[写入失败] ...` 的日志，意味着该文件名在 Windows 上完全不可写（罕见），可以手动检查上游路径是否含其他非法字符（`<>:"/\|?*`）。

## 疑难排查

**访问首页一片空白？**
通常是 `assets/` 目录没抓全。打开 DevTools 网络面板看 4xx 请求，再 `--force` 重抓一次。如果是仿造 Vite `base: './'` 输出的相对路径未被识别，请确认你用的是最新版脚本。

**搜索功能用不了？**
检查是否加了 `--no-search` 跳过；或 `search/` 目录是否完整。

**字体仍然指向远端？**
重跑 `uv run mirror_gfstory.py --fonts-only`。脚本只识别 `fonts.googleapis.com / fonts.gstatic.com / fonts.font.im / static.font.im` 这几个常见 CDN，如果上游换了其他 CDN，需要把新域名加到脚本的 `FONT_HOSTS` 常量里。

**怎么知道镜像是不是已经"完整"？**
对照 `stories/stories.json` 数量（约 1 万条剧情）与 `audio/audio.json` 数量（约几百条），跑完进度条到 100% 即视为完成。失败计数会以 `[失败]` / `[写入失败]` 打印在终端。

## 后续更新

上游 `gfstory.pages.dev` 由作者 [gudzpoz](https://github.com/gudzpoz) 维护，每次他 push 会自动重新部署。当你想同步新增剧情时：

```bash
uv run mirror_gfstory.py
```

脚本会比对本地与远端，**只下载新增/变化的文件**。chapters/stories JSON 的差异会让阶段 3 自动发现新条目。

如果作者停止维护、或你想自己添加同人剧情，参考镜像目录里 `stories/*.txt` 的格式（micromark directive：`:sprites[]`、`:narrator[]`、`:background[]`、`:audio[]`、`:se[]` 等），再编辑 `stories.json` 与 `chapters.json` 即可。也可以直接打开本地编辑器 `http://localhost:8000/` 用图形界面创作，导出 zip 自带 viewer.html。
