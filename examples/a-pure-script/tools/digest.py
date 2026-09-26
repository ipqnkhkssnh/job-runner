#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例用：产出交付物 —— 一份机器读的摘要 JSON + 一份人看的对账结果 CSV。"""
import argparse
import csv
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--joined", required=True)
    ap.add_argument("--exceptions", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-csv", required=True)
    a = ap.parse_args()

    with Path(a.joined).open(newline="", encoding="utf-8-sig") as fh:
        joined = [dict(r) for r in csv.DictReader(fh)]
    with Path(a.exceptions).open(newline="", encoding="utf-8-sig") as fh:
        exceptions = [dict(r) for r in csv.DictReader(fh)]

    by_status = {}
    for r in joined:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    digest = {
        "total_rows": len(joined),
        "by_status": by_status,
        "needs_attention": len(exceptions),
        "exceptions": exceptions[:20],
        "conclusion": ("全部对平" if not exceptions
                       else f"{len(exceptions)} 行需要人工确认"),
    }
    p = Path(a.out_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(digest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    q = Path(a.out_csv)
    q.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "amount", "reference_amount", "status"]
    with q.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(joined)
    print(digest["conclusion"] + f"（{len(joined)} 行）")


if __name__ == "__main__":
    main()
