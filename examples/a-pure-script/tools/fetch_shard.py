#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例用：生成一份**确定性**的分片数据（替代真实系统的导出）。

确定性很重要：同一次 job 重跑必须得到同样的数据，否则对账结果不可复现、也无法断言。
不要用 hash()（Python 的字符串 hash 每次进程都不同），这里用 ord 求和。
"""
import argparse
import csv
from pathlib import Path


def amount_for(shard: str, i: int) -> int:
    return 100 + (sum(map(ord, shard)) % 50) + i


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", required=True)
    ap.add_argument("--rows", type=int, default=4)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    p = Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "shard", "amount", "currency"])
        for i in range(1, a.rows + 1):
            w.writerow([f"SH-{a.shard}-{i:03d}", a.shard, amount_for(a.shard, i), "CNY"])
    print(f"{a.shard}: {a.rows} 行 → {p}")


if __name__ == "__main__":
    main()
