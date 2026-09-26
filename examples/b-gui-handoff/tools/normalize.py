#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 inbox 里的 CSV 合并、统一列、按 id 去重（示例 B 用，与示例 A 同款）。"""
import argparse
import csv
from pathlib import Path

COLUMNS = ["id", "amount"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    inbox = Path(a.inbox)
    if not inbox.is_dir():
        raise SystemExit(f"inbox 目录不存在：{inbox}（上游 skill 步骤应该已经产出文件）")

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
