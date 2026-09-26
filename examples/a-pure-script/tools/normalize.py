#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例用：把 inbox 里的分片合并、统一列、按 id 去重（保留首见）。

约定：tool 脚本从 argv 拿路径，自己负责 glob/落盘；产物写到传进来的 --out。
"""
import argparse
import csv
from pathlib import Path

COLUMNS = ["id", "shard", "amount", "currency"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True, help="分片目录")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    inbox = Path(a.inbox)
    if not inbox.is_dir():
        raise SystemExit(f"inbox 目录不存在：{inbox}")

    rows, seen = [], set()
    for f in sorted(inbox.glob("*.csv")):
        with f.open(newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r.get("id") in seen:
                    continue
                seen.add(r["id"])
                rows.append({c: (r.get(c) or "").strip() for c in COLUMNS})

    rows.sort(key=lambda r: r["id"])
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"normalize: {len(rows)} 行 → {out}")


if __name__ == "__main__":
    main()
