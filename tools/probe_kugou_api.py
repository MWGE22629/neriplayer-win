"""酷狗 API 带登录态探测工具(M6 调研用,非产品代码)。

读 tools/kugou_cookies.local.json(由 probe_kugou_login.py 导出)构造
Cookie 头,按给定 URL 发请求并打印状态码与响应正文(默认截断)。
用于把「候选接口」逐条实测成「已锚定契约」,不预设任何接口为真。

用法(仓库根):
    # 单条
    uv run python tools/probe_kugou_api.py "https://www.kugou.com/..."
    # 多条:每行一个 URL,# 开头为注释
    uv run python tools/probe_kugou_api.py --file tools/kugou_endpoints.txt
    # 换 UA / Referer / 只看 JSON 某字段
    uv run python tools/probe_kugou_api.py --ua "Mozilla/5.0 ..." URL
    # 不截断
    uv run python tools/probe_kugou_api.py --full URL

注意:输出的响应可能含账号信息,只在本机看,不要把响应原文粘进
提交到版本库的文档里(文档里只写字段路径与脱敏样例)。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

_COOKIE_FILE = Path(__file__).with_name("kugou_cookies.local.json")
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_REFERER = "https://www.kugou.com/"


def load_cookies(path: Path) -> dict[str, str]:
    if not path.exists():
        print(f"缺少 cookie 文件:{path}", file=sys.stderr)
        print("先运行 tools/probe_kugou_login.py 完成登录并导出。", file=sys.stderr)
        raise SystemExit(2)
    payload = json.loads(path.read_text(encoding="utf-8"))
    cookies = payload.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        print(f"{path} 里没有 cookies 字段或为空", file=sys.stderr)
        raise SystemExit(2)
    return {str(k): str(v) for k, v in cookies.items()}


def probe(
    url: str,
    cookies: dict[str, str],
    *,
    ua: str,
    referer: str,
    limit: int,
    timeout: float,
) -> None:
    headers = {"User-Agent": ua, "Referer": referer}
    print("=" * 78)
    print("GET", url)
    try:
        with httpx.Client(
            cookies=cookies, headers=headers, timeout=timeout, follow_redirects=True
        ) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        print(f"  请求失败:{type(exc).__name__}: {exc}")
        return
    print(f"  status={response.status_code} len={len(response.content)} "
          f"ctype={response.headers.get('content-type', '')}")
    text = response.text
    if limit > 0 and len(text) > limit:
        print(text[:limit] + f"\n  …(截断,共 {len(text)} 字符;--full 看全文)")
    else:
        print(text)
    try:
        data = response.json()
    except ValueError:
        return
    print("  顶层键:", list(data)[:20] if isinstance(data, dict) else type(data).__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--file", type=Path, help="每行一个 URL")
    parser.add_argument("--cookies", type=Path, default=_COOKIE_FILE)
    parser.add_argument("--ua", default=_DEFAULT_UA)
    parser.add_argument("--referer", default=_DEFAULT_REFERER)
    parser.add_argument("--limit", type=int, default=2000, help="0 表示不截断")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        for line in args.file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                urls.append(stripped)
    if not urls:
        parser.error("至少给一个 URL 或 --file")

    cookies = load_cookies(args.cookies)
    print(f"已载入 {len(cookies)} 个 cookie:{', '.join(sorted(cookies))}")
    limit = 0 if args.full else args.limit
    for url in urls:
        probe(
            url,
            cookies,
            ua=args.ua,
            referer=args.referer,
            limit=limit,
            timeout=args.timeout,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
