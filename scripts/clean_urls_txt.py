#!/usr/bin/env python3
"""
扫描目录下 .txt，用正则提取 https:// 链接，去掉行首多余符号/空格及 URL 尾部标点，写回原文件。

用法:
  python scripts/clean_urls_txt.py
  python scripts/clean_urls_txt.py --root data/urls/anime
  python scripts/clean_urls_txt.py --dry-run
  python scripts/clean_urls_txt.py --backup
  python scripts/clean_urls_txt.py --unique
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 从 https 起匹配到空白或常见分隔符为止（避免吞掉中文后的无关字符）
URL_RE = re.compile(r"https://[^\s\]\)\"'<>，,、]+", re.IGNORECASE)

# URL 末尾常误带的标点/括号（循环剥离，避免正则里括号转义问题）
_TRAIL_SET = frozenset(
    """.,;:!?)]}>'\"`。，、；？）》」…\\"""
)


def _strip_trailing_junk(url: str) -> str:
    u = url.strip()
    while u and u[-1] in _TRAIL_SET:
        u = u[:-1].rstrip()
    return u


def extract_urls(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        for m in URL_RE.finditer(raw):
            u = _strip_trailing_junk(m.group(0))
            if u.lower().startswith("https://"):
                out.append(u)
    return out


def _dedupe_preserve_order(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    r: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            r.append(u)
    return r


def process_file(path: Path, *, dry_run: bool, backup: bool, unique: bool) -> tuple[bool, int]:
    """
    返回 (是否改写了文件, 提取到的 URL 条数)。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    urls = extract_urls(text)
    if unique:
        urls = _dedupe_preserve_order(urls)
    new_body = "\n".join(urls) + ("\n" if urls else "")
    if not urls:
        return False, 0
    if new_body == text:
        return False, len(urls)
    if dry_run:
        print(f"[dry-run] 将改写 {path} ({len(urls)} 行)")
        return False, len(urls)
    if backup:
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(text, encoding="utf-8")
    path.write_text(new_body, encoding="utf-8")
    return True, len(urls)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="清理 data/urls 下 txt 中的 https 链接并保存")
    ap.add_argument(
        "--root",
        type=Path,
        default=None,
        help="扫描根目录（默认: 项目根下 data/urls）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印将改写的文件，不写盘")
    ap.add_argument("--backup", action="store_true", help="改写前同目录写 .txt.bak")
    ap.add_argument("--unique", action="store_true", help="按顺序去重 URL")
    args = ap.parse_args()

    base = args.root if args.root is not None else root / "data" / "urls"
    base = base.resolve()
    if not base.is_dir():
        print(f"目录不存在: {base}", file=sys.stderr)
        return 1

    files = sorted(base.rglob("*.txt"))
    if not files:
        print(f"未找到 txt: {base}")
        return 0

    changed = 0
    total_urls = 0
    for p in files:
        try:
            did, n = process_file(
                p,
                dry_run=args.dry_run,
                backup=args.backup,
                unique=args.unique,
            )
        except OSError as e:
            print(f"跳过 {p}: {e}", file=sys.stderr)
            continue
        total_urls += n
        if did:
            changed += 1
            print(f"已保存 {p} ({n} 条 URL)")
        elif n and not args.dry_run:
            print(f"跳过（已是干净格式）{p} ({n} 条)")

    mode = "dry-run " if args.dry_run else ""
    print(f"\n{mode}处理 {len(files)} 个文件，改写 {changed} 个，共提取 URL 行 {total_urls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
