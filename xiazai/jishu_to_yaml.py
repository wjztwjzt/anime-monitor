"""
从 jishu.txt 按行读链接（一行一个，可带引号、行尾逗号，会自动去掉），
在 dongman.yaml 里用纯文本替换「urls: 到 state: 之间」的整段。
也可 --from xxx.json 使用旧版 JSON（episodes 数组）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from json import JSONDecodeError
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TXT = BASE_DIR / "jishu.txt"
DEFAULT_YAML = BASE_DIR / "dongman.yaml"

_URLS_BLOCK = re.compile(r"(?ms)^urls:\s*\n.*?(?=^state:)")  # 顶格 state:


def _line_to_url(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    s = s.rstrip().rstrip(",").strip()
    s = s.strip('"\u201c\u201d' + "'")
    s = s.rstrip().rstrip(",").strip()
    s = s.strip('"\u201c\u201d' + "'")
    s = s.strip()
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return None


def _load_url_list_from_txt(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    out: list[str] = []
    for line in text.splitlines():
        u = _line_to_url(line)
        if u:
            out.append(u)
    if not out:
        sys.exit(f"{path}: 没有解析出任何以 http 开头的 URL（每行一个链接）。")
    return out


def _load_url_list_from_json(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        sys.exit(f"{path}: 文件为空")
    try:
        data: Any = json.loads(text)
    except JSONDecodeError as e:
        fixed = re.sub(r",(\s*])", r"\1", text)
        fixed = re.sub(r",(\s*})", r"\1", fixed)
        try:
            data = json.loads(fixed)
        except JSONDecodeError:
            sys.exit(
                f"{path} JSON 解析失败: {e.msg}。可改用 jishu.txt 一行一个链接。"
            )
    if isinstance(data, list):
        raw: list[Any] = data
    elif isinstance(data, dict):
        for key in ("episodes", "urls", "links", "m3u8"):
            v = data.get(key)
            if isinstance(v, list):
                raw = v
                break
        else:
            sys.exit(f"{path} 需要根对象里含 episodes 等数组字段。")
    else:
        sys.exit(f"{path} 格式不对。")
    out = [str(s).strip() for s in raw if str(s).strip()]
    if not out:
        sys.exit(f"{path}: episodes 等列表里没有有效项。")
    return out


def _load_url_list(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_url_list_from_json(path)
    return _load_url_list_from_txt(path)


def _format_urls_block(urls: list[str]) -> str:
    lines = [f"  - {u}" for u in urls]
    return "urls:\n" + "\n".join(lines) + "\n"


def _find_file(p: Path) -> Path:
    p = p.expanduser()
    if p.is_file():
        return p.resolve()
    for base in (BASE_DIR, Path.cwd()):
        c = (base / p).resolve()
        if c.is_file():
            return c
    return p.resolve()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="把 jishu.txt（或 .json）里的链接写进 dongman.yaml 的 urls:…state: 段。"
    )
    ap.add_argument(
        "--from",
        dest="src",
        type=Path,
        default=DEFAULT_TXT,
        help=f"源文件（默认: {DEFAULT_TXT.name}；.json 走 JSON 解析）",
    )
    ap.add_argument("--to", dest="yaml_path", type=Path, default=DEFAULT_YAML, help="dongman.yaml 路径")
    args = ap.parse_args()

    src = _find_file(args.src)
    ypath = _find_file(args.yaml_path)
    if not src.is_file():
        sys.exit(
            f"找不到源文件: {src}\n  把 {DEFAULT_TXT.name} 放在与本脚本同目录，或: --from 完整路径"
        )
    if not ypath.is_file():
        sys.exit(f"找不到: {ypath}")

    urls = _load_url_list(src)
    body = ypath.read_text(encoding="utf-8-sig")
    new_block = _format_urls_block(urls)
    if not _URLS_BLOCK.search(body):
        sys.exit(
            f"{ypath} 中未找到顶格的「urls:」与紧后面的「state:」段落，无法替换。"
        )
    new_body, n = _URLS_BLOCK.subn(new_block, body, count=1)
    if n != 1:
        sys.exit("替换失败。")
    ypath.write_text(new_body, encoding="utf-8", newline="\n")
    print(f"已从 {src.name} 写入 {len(urls)} 条链接到 {ypath.name} 的 urls 段。")


if __name__ == "__main__":
    main()
