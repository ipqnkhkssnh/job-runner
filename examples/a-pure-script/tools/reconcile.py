#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例用：把规范化记录与参考表核对，逐行打状态。

设计要点（值得抄进真实作业）：
  * **一行不丢**：joined 是「记录 ∪ 参考表」，任何一侧独有的行都保留并标状态；
    这样后续才能用 subset_keys / equal_counts 这类不变量证明没丢数据。
  * **差异分类**：matched / amount_mismatch / missing_in_reference / missing_in_records
  * 金额比较带容差，不用 == 比浮点。
"""
import argparse
import csv
from pathlib import Path

TOL = 0.01
FIELDS = ["id", "amount", "reference_amount", "status"]
EXC_FIELDS = ["id", "amount", "reference_amount", "status", "diff"]


def load(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def write(path, fields, rows):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--out-joined", required=True)
    ap.add_argument("--out-exceptions", required=True)
    ap.add_argument("--out-orphans", required=True)
    a = ap.parse_args()

    records = load(a.records)
    reference = {str(r["id"]): r for r in load(a.reference)}

    joined, seen = [], set()
    for r in records:
        rid = str(r["id"])
        seen.add(rid)
        amt = num(r.get("amount"))
        ref = reference.get(rid)
        if ref is None:
            status, ref_amt = "missing_in_reference", ""
        else:
            ref_amt = num(ref.get("amount"))
            if amt is None or ref_amt is None:
                status = "unparsable_amount"
            elif abs(amt - ref_amt) > TOL:
                status = "amount_mismatch"
            else:
                status = "matched"
        joined.append({"id": rid, "amount": r.get("amount", ""),
                       "reference_amount": ref_amt if ref_amt is not None else "",
                       "status": status})

    # 参考表独有的行也要保留（否则「一行不丢」无从证明）
    for rid, ref in reference.items():
        if rid in seen:
            continue
        joined.append({"id": rid, "amount": "",
                       "reference_amount": num(ref.get("amount")) or "",
                       "status": "missing_in_records"})

    joined.sort(key=lambda r: r["id"])
    exceptions = []
    for r in joined:
        if r["status"] == "matched":
            continue
        diff = ""
        if r["amount"] not in ("", None) and r["reference_amount"] not in ("", None):
            diff = round(float(r["amount"]) - float(r["reference_amount"]), 2)
        exceptions.append({**r, "diff": diff})
    orphans = [r for r in joined if r["status"] == "missing_in_records"]

    write(a.out_joined, FIELDS, joined)
    write(a.out_exceptions, EXC_FIELDS, exceptions)
    write(a.out_orphans, FIELDS, orphans)
    print(f"reconcile: 记录 {len(records)} 行，全量 {len(joined)} 行，"
          f"差异 {len(exceptions)} 行，参考表孤立 {len(orphans)} 行")


if __name__ == "__main__":
    main()
