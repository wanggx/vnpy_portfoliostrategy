"""Markdown -> PDF 转换器（Markdown -> 带样式 HTML -> Chrome 无头打印 PDF -> 写入书签）。

为什么不用 pandoc/wkhtmltopdf：本机没有装。可用的现成工具是 Chrome / Edge，
用无头模式的 ``--print-to-pdf`` 即可获得带完整 CSS（含中文字体）的 PDF。

本脚本在此之上补两件 Chrome 打印做不到的事：

1. **左侧层级目录（PDF 书签 / outline）**：Chrome 的 ``--print-to-pdf`` 不生成书签，
   因此打印完成后用 pypdf 把标题层级写进 PDF 的 outline —— 阅读器左侧目录栏就能
   展开/折叠、点击跳转。
2. **可视目录页**：在正文前插入一页层级目录（双栏），并带上真实页码。
   页码需要多遍渲染：第一遍先排版定位，第二遍把页码填进目录，直到页码稳定
   （正常两遍即收敛）。

Markdown 里的 ```mermaid 代码块会交给 mermaid.js 在 Chrome 内渲染成 SVG 后再打印，
因此流程图不会丢失（mermaid.min.js 从 ``script/md2pdf_assets/`` 内联加载，离线可用）。

用法::

    python script/md2pdf.py doc/近均线快速拉升策略说明.md
    python script/md2pdf.py doc/a.md doc/b.md -o doc/pdf/manual.pdf   # 多文件合并为一份
    python script/md2pdf.py doc/x.md --no-toc --keep-html

依赖：``markdown``、``pypdf``。浏览器路径按 Edge / Chrome 常见安装位置自动探测，
也可用 ``--browser`` 显式指定。
"""

from __future__ import annotations

import argparse
import html as html_lib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown
from pypdf import PdfReader, PdfWriter


SCRIPT_DIR: Path = Path(__file__).resolve().parent
ASSETS_DIR: Path = SCRIPT_DIR / "md2pdf_assets"
MERMAID_JS: Path = ASSETS_DIR / "mermaid.min.js"

# 浏览器候选路径（按顺序探测，命中即用）
BROWSER_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

# 目录页末尾的哨兵：白色 1px 文本，视觉不可见但会进 PDF 文本层，
# 用来精确定位"目录页到哪一页结束"（正文从它的下一页开始）。
TOC_SENTINEL: str = "ZZTOCENDZZ"

# ```mermaid ... ``` 代码块（贪婪禁止跨块匹配）
_MERMAID_BLOCK: re.Pattern[str] = re.compile(
    r"^[ \t]*```mermaid[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

# Markdown 标题行
_HEADING: re.Pattern[str] = re.compile(r"^(#{1,4})[ \t]+(.+?)[ \t]*$", re.MULTILINE)

# 行内标记（用于把标题转成纯文本）
_INLINE_CODE: re.Pattern[str] = re.compile(r"`([^`]*)`")
_BOLD: re.Pattern[str] = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC: re.Pattern[str] = re.compile(r"\*([^*]+)\*")
_LINK: re.Pattern[str] = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WS: re.Pattern[str] = re.compile(r"[\s\u200b\ufeff]+")

CSS: str = """
@page { size: A4; margin: 18mm 15mm 16mm 15mm; }

* { box-sizing: border-box; }

body {
    font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
                 "Source Han Sans SC", "Noto Sans CJK SC", sans-serif;
    font-size: 10.5pt;
    line-height: 1.75;
    color: #1f2328;
    margin: 0;
    padding: 0;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
}

h1, h2, h3, h4 { line-height: 1.35; font-weight: 600; page-break-after: avoid; }
h1 { font-size: 20pt; margin: 0 0 6mm; padding-bottom: 3mm; border-bottom: 2px solid #d0d7de; }
h2 { font-size: 15pt; margin: 9mm 0 3mm; padding: 2mm 0 2mm 3mm; border-left: 4px solid #0969da;
     background: #f6f8fa; }
h3 { font-size: 12.5pt; margin: 6mm 0 2mm; color: #0969da; }
h4 { font-size: 11pt; margin: 4mm 0 1.5mm; color: #57606a; }

p { margin: 2mm 0; }
ul, ol { margin: 2mm 0; padding-left: 6mm; }
li { margin: 0.8mm 0; }

a { color: #0969da; text-decoration: none; }

code {
    font-family: Consolas, "Cascadia Mono", "Courier New", monospace;
    font-size: 0.92em;
    background: #f0f2f5;
    border: 1px solid #e3e6ea;
    border-radius: 3px;
    padding: 0.5px 3px;
    color: #a31515;
}

pre {
    background: #f6f8fa;
    border: 1px solid #d8dee4;
    border-radius: 5px;
    padding: 3mm 3.5mm;
    overflow-wrap: break-word;
    white-space: pre-wrap;
    page-break-inside: avoid;
    margin: 3mm 0;
}
pre code { background: none; border: none; padding: 0; color: #1f2328; font-size: 9pt; line-height: 1.5; }

blockquote {
    margin: 3mm 0;
    padding: 2mm 4mm;
    border-left: 4px solid #d4a72c;
    background: #fffbef;
    color: #4d3800;
    page-break-inside: avoid;
}
blockquote p { margin: 1mm 0; }

table {
    border-collapse: collapse;
    width: 100%;
    margin: 3mm 0;
    font-size: 9.5pt;
    page-break-inside: avoid;
}
th, td { border: 1px solid #d0d7de; padding: 1.6mm 2.4mm; text-align: left; vertical-align: top; }
th { background: #f0f3f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }

hr { border: none; border-top: 1px solid #d8dee4; margin: 6mm 0; }

.mermaid {
    text-align: center;
    margin: 4mm 0;
    page-break-inside: avoid;
}
.mermaid svg { max-width: 100%; height: auto; }

/* ---- 目录页 ---- */
.toc-block { page-break-after: always; break-after: page; }
.toc-heading {
    font-size: 16pt; font-weight: 600; margin: 0 0 5mm;
    padding-bottom: 2mm; border-bottom: 1px solid #d0d7de;
}
.toc-cols { column-count: 2; column-gap: 9mm; }
.toc-item {
    font-size: 8.5pt; line-height: 1.55;
    break-inside: avoid; page-break-inside: avoid;
    overflow-wrap: break-word;
}
.toc-l1 { font-weight: 600; margin-top: 1.8mm; }
.toc-l2 { padding-left: 3mm; }
.toc-l3 { padding-left: 6mm; color: #4a5560; }
.toc-pg { float: right; color: #6a737d; }
.toc-sentinel { font-size: 1px; line-height: 1px; color: #ffffff; margin: 0; }

/* 合并多份文档时的分隔 */
.doc-sep { page-break-before: always; break-before: page; }
"""

HTML_TEMPLATE: str = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
{body}
<script>{mermaid_js}</script>
<script>
mermaid.initialize({{
    startOnLoad: true,
    theme: 'neutral',
    securityLevel: 'loose',
    flowchart: {{ useMaxWidth: true, htmlLabels: true, curve: 'basis' }},
    fontFamily: '"Microsoft YaHei", "PingFang SC", sans-serif'
}});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 标题解析
# ---------------------------------------------------------------------------
def _plain_title(raw: str) -> str:
    """把 Markdown 标题行转成纯文本（去掉行内代码 / 加粗 / 链接等标记）。"""
    text: str = raw.strip()
    text = _INLINE_CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = text.replace("\\", "")
    return text.strip()


def _strip_code_blocks(md_text: str) -> str:
    """把 fenced code block 的内容清空，避免代码块里的 ``#`` 被当成标题。"""
    output: list[str] = []
    in_fence: bool = False
    for line in md_text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            output.append("")
            continue
        output.append("" if in_fence else line)
    return "\n".join(output)


def parse_headings(md_texts: list[str]) -> list[tuple[int, str]]:
    """按文档顺序提取 (层级, 纯文本标题)，层级取 1~4。

    目录页与 PDF 书签共用这一份结果，保证两者完全一致。
    """
    headings: list[tuple[int, str]] = []
    for text in md_texts:
        for match in _HEADING.finditer(_strip_code_blocks(text)):
            title: str = _plain_title(match.group(2))
            if title:
                headings.append((len(match.group(1)), title))
    return headings


def _normalize(text: str) -> str:
    """归一化用于 PDF 文本匹配：去掉所有空白与零宽字符。"""
    return _WS.sub("", text)


# ---------------------------------------------------------------------------
# Markdown -> HTML
# ---------------------------------------------------------------------------
def _extract_mermaid(text: str) -> tuple[str, list[str]]:
    """把 ```mermaid 代码块替换为占位符，返回 (替换后的文本, mermaid 源码列表)。

    先抽出再交给 markdown 解析，避免代码块被包成 <pre><code>（那样 mermaid 不渲染）。
    """
    blocks: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        blocks.append(match.group(1).rstrip())
        return f"\n@@MERMAID_{len(blocks) - 1}@@\n"

    return _MERMAID_BLOCK.sub(_replace, text), blocks


def _restore_mermaid(html_text: str, blocks: list[str]) -> str:
    """把占位符还原为 ``<div class="mermaid">``，源码按 HTML 实体转义。"""
    for index, source in enumerate(blocks):
        escaped = html_lib.escape(source, quote=False)
        html_text = html_text.replace(
            f"@@MERMAID_{index}@@",
            f'<div class="mermaid">\n{escaped}\n</div>',
        )
    return html_text


def md_to_body(md_text: str) -> str:
    """Markdown 文本 -> HTML 片段（含 mermaid 占位还原）。"""
    text, blocks = _extract_mermaid(md_text)
    body: str = markdown.markdown(
        text,
        extensions=[
            "tables",
            "fenced_code",
            "sane_lists",
            "attr_list",
            "md_in_html",
        ],
        output_format="html5",
    )
    return _restore_mermaid(body, blocks)


# ---------------------------------------------------------------------------
# 目录页与 PDF 书签
# ---------------------------------------------------------------------------
def build_toc_html(
    headings: list[tuple[int, str]],
    pages: list[int | None] | None,
) -> str:
    """生成目录页 HTML。``pages`` 为每个标题的页码（1 基）；为 None 时先不带页码。"""
    items: list[str] = []
    for index, (level, title) in enumerate(headings):
        # 文档主标题（第一个标题）不进目录：它已经出现在正文首页
        if index == 0:
            continue
        cls: str = {1: "toc-l1", 2: "toc-l2", 3: "toc-l3"}.get(level, "toc-l3")
        page_html: str = ""
        if pages is not None and pages[index] is not None:
            # 内部页码是 0 基索引，展示时转成读者看到的 1 基页码
            page_html = f'<span class="toc-pg">{pages[index] + 1}</span>'
        items.append(
            f'<div class="toc-item {cls}">{page_html}{html_lib.escape(title)}</div>'
        )
    columns: str = '<div class="toc-cols">' + "".join(items) + "</div>"
    return (
        '<div class="toc-block">'
        '<div class="toc-heading">目录</div>'
        f"{columns}"
        f'<div class="toc-sentinel">{TOC_SENTINEL}</div>'
        "</div>"
    )


def locate_headings(
    pdf_path: Path,
    headings: list[tuple[int, str]],
    has_toc: bool,
) -> list[int | None]:
    """在已生成的 PDF 中定位每个标题所在的页（返回 0 基页索引）。

    标题按文档顺序出现，因此用**单调游标**从前向后找，避免同一个词在更早的
    正文里出现造成误匹配。目录页本身包含所有标题文字，所以有目录页时从哨兵
    所在页的**下一页**开始找。
    """
    reader = PdfReader(str(pdf_path))
    page_texts: list[str] = [
        _normalize(page.extract_text() or "") for page in reader.pages
    ]

    start: int = 0
    if has_toc:
        for index, text in enumerate(page_texts):
            if TOC_SENTINEL in text:
                start = index + 1
                break

    located: list[int | None] = []
    cursor: int = start
    for _level, title in headings:
        needle: str = _normalize(title)
        found: int | None = None
        if needle:
            for index in range(cursor, len(page_texts)):
                if needle in page_texts[index]:
                    found = index
                    break
            if found is None:  # 游标之后找不到，退回全文搜一遍
                for index, text in enumerate(page_texts):
                    if needle in text:
                        found = index
                        break
        located.append(found)
        if found is not None:
            cursor = found
    return located


def write_outline(
    pdf_path: Path,
    headings: list[tuple[int, str]],
    pages: list[int | None],
    title: str = "",
) -> int:
    """把标题层级写入 PDF outline（左侧书签栏），返回写入的条目数。

    ``add_outline_item`` 支持 ``parent`` 参数实现嵌套：层级更深者挂到最近的
    更浅层级条目下 —— 即严格按 Markdown 的 #/##/###/#### 形成树。

    重写页面（PdfWriter 逐页 add_page）会丢掉原 PDF 的元数据，因此这里把
    reader 的元数据原样搬运回来，并在 ``/Title`` 缺失时用文档标题补上 ——
    阅读器的标题栏与新窗口标题都靠它。
    """
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    metadata: dict[str, str] = {}
    if reader.metadata:
        metadata = {str(key): str(value) for key, value in reader.metadata.items()}
    if title and not metadata.get("/Title"):
        metadata["/Title"] = title
    if metadata:
        writer.add_metadata(metadata)

    parent_of: dict[int, object] = {}
    written: int = 0
    for index, (level, title) in enumerate(headings):
        page_index: int | None = pages[index] if index < len(pages) else None
        if page_index is None or page_index >= len(reader.pages):
            continue

        parent = None
        for shallower in range(level - 1, 0, -1):
            if shallower in parent_of:
                parent = parent_of[shallower]
                break

        item = writer.add_outline_item(
            title=title,
            page_number=page_index,
            parent=parent,
        )
        parent_of[level] = item
        # 更深层级遗留的父节点失效，避免后续条目挂错
        for deeper in [key for key in parent_of if key > level]:
            parent_of.pop(deeper, None)
        written += 1

    with open(pdf_path, "wb") as handle:
        writer.write(handle)
    return written


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _find_browser(explicit: str | None) -> str:
    """定位可用的 Chromium 系浏览器；找不到则报错退出。"""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"指定的浏览器不存在：{explicit}")
        return str(path)
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge") or shutil.which("chromium")
    if found:
        return found
    raise SystemExit(
        "未找到 Chrome / Edge。请用 --browser 指定浏览器可执行文件路径。"
    )


def build_html(body_parts: list[str], title: str, toc_html: str) -> str:
    """拼装完整 HTML 文档（内联 CSS 与 mermaid.js）。"""
    if not MERMAID_JS.is_file():
        raise SystemExit(
            f"缺少 mermaid 渲染库：{MERMAID_JS}\n"
            "请下载 https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js "
            f"放到 {ASSETS_DIR}"
        )
    mermaid_js: str = MERMAID_JS.read_text(encoding="utf-8")
    return HTML_TEMPLATE.format(
        title=html_lib.escape(title),
        css=CSS,
        body=toc_html + "\n".join(body_parts),
        mermaid_js=mermaid_js,
    )


def html_to_pdf(browser: str, html_path: Path, pdf_path: Path, timeout: int) -> None:
    """调用 Chrome/Edge 无头模式把 HTML 打印为 PDF。

    ``--virtual-time-budget`` 给页面内 JS（mermaid 渲染）留出执行时间：
    预算耗尽后浏览器才真正打印，保证流程图已画好。
    """
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        pdf_path.unlink()

    command: list[str] = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--allow-file-access-from-files",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=20000",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        # 显式指定 UTF-8：Windows 下默认用本地编码（GBK）解码子进程输出，
        # 而 Chrome 的输出里会带上中文路径等非 ASCII 字节，会导致解码报错、
        # 进而丢掉 stdout/stderr（出错时就看不到原因了）。
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if not pdf_path.is_file():
        raise SystemExit(
            "PDF 生成失败。\n"
            f"命令：{' '.join(command)}\n"
            f"退出码：{result.returncode}\n"
            f"stdout：{result.stdout}\n"
            f"stderr：{result.stderr}\n"
            "提示：Chrome 的 --print-to-pdf 需要能创建目标文件（检查目录写权限）。"
        )


def render(
    browser: str,
    body_parts: list[str],
    title: str,
    toc_html: str,
    tmp_dir: Path,
    pdf_path: Path,
    timeout: int,
) -> str:
    """渲染一次 HTML 并打印为 PDF，返回 HTML 文本。"""
    html_text: str = build_html(body_parts, title, toc_html)
    html_path: Path = tmp_dir / (pdf_path.stem + ".html")
    html_path.write_text(html_text, encoding="utf-8")
    html_to_pdf(browser, html_path, pdf_path, timeout)
    return html_text


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description=(
            "Markdown -> PDF（经 Chrome/Edge 无头打印；支持 mermaid 流程图、"
            "可视目录页与 PDF 左侧层级书签）"
        )
    )
    parser.add_argument("inputs", nargs="+", help="输入的 Markdown 文件（多个则合并为一份 PDF）")
    parser.add_argument("-o", "--output", help="输出 PDF 路径（默认 doc/pdf/<首个文件名>.pdf）")
    parser.add_argument("--browser", help="浏览器可执行文件路径（默认自动探测 Chrome/Edge）")
    parser.add_argument("--no-toc", action="store_true", help="不生成可视目录页")
    parser.add_argument("--no-outline", action="store_true", help="不写入 PDF 左侧书签")
    parser.add_argument("--keep-html", action="store_true", help="保留中间 HTML（--output 同目录）")
    parser.add_argument("--timeout", type=int, default=180, help="浏览器超时秒数（默认 180）")
    args = parser.parse_args()

    inputs: list[Path] = [Path(item).resolve() for item in args.inputs]
    for path in inputs:
        if not path.is_file():
            raise SystemExit(f"输入文件不存在：{path}")

    if args.output:
        pdf_path: Path = Path(args.output).resolve()
    else:
        first: Path = inputs[0]
        pdf_path = first.parent / "pdf" / f"{first.stem}.pdf"

    md_texts: list[str] = [path.read_text(encoding="utf-8") for path in inputs]
    for path in inputs:
        print(f"已解析：{path}")

    body_parts: list[str] = []
    for index, text in enumerate(md_texts):
        if index > 0:
            body_parts.append('<div class="doc-sep"></div>')
        body_parts.append(md_to_body(text))

    headings: list[tuple[int, str]] = parse_headings(md_texts)
    print(f"标题层级：共 {len(headings)} 个（用于目录页与书签）")

    title: str = inputs[0].stem if len(inputs) == 1 else "文档合集"
    browser: str = _find_browser(args.browser)
    print(f"使用浏览器：{browser}")

    pages: list[int | None] | None = None
    html_text: str = ""
    with tempfile.TemporaryDirectory(prefix="md2pdf_") as tmp:
        tmp_dir: Path = Path(tmp)

        if args.no_toc:
            html_text = render(
                browser, body_parts, title, "", tmp_dir, pdf_path, args.timeout
            )
        else:
            # 多遍渲染：第一遍先排版定位，之后把页码填进目录页。
            # 目录页恒为固定页数（双栏），因此填页码不会改变正文分页。
            for attempt in range(1, 4):
                toc_html: str = build_toc_html(headings, pages)
                html_text = render(
                    browser, body_parts, title, toc_html, tmp_dir, pdf_path, args.timeout
                )
                located: list[int | None] = locate_headings(pdf_path, headings, True)
                if located == pages:
                    print(f"目录页码已收敛（第 {attempt} 遍）")
                    break
                if attempt == 3:
                    print("警告：页码在 3 遍内未收敛，目录页码可能略有偏差")
                pages = located

        if args.keep_html:
            kept: Path = pdf_path.with_suffix(".html")
            kept.write_text(html_text, encoding="utf-8")
            print(f"中间 HTML 已保留：{kept}")

    if not args.no_outline:
        if pages is None:
            pages = locate_headings(pdf_path, headings, not args.no_toc)
        doc_title: str = headings[0][1] if headings else title
        written: int = write_outline(pdf_path, headings, pages, doc_title)
        print(f"已写入 PDF 书签：{written} 条（左侧目录栏可展开层级）")

    total: int = len(PdfReader(str(pdf_path)).pages)
    size_kb: float = pdf_path.stat().st_size / 1024
    print(f"完成：{pdf_path}（{total} 页，{size_kb:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
