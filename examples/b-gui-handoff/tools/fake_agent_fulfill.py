#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**仅用于自测暂停/续跑协议**的假执行方。

真实运行时这一步由 agent 完成：用 a2desk / playwright 按能力卡的 selectors 操作界面，
每步截图存进 evidenceDir，最后写 result.json。本脚本把这个「写回」动作脚本化，
好让示例 job 能在没有真实系统的情况下端到端验证引擎。

用法：
  fake_agent_fulfill.py --run-dir <run 目录> --step 'collect-all#0/collect' --item alpha \
                        [--rows 3] [--gap] [--ok/--fail]
"""
import argparse
import csv
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--step", required=True, help="run.json 里那个步骤键，如 collect-all#0/collect")
    ap.add_argument("--item", required=True)
    ap.add_argument("--rows", type=int, default=3)
    ap.add_argument("--gap", action="store_true", help="顺便报一个 knowledge 缺口（演示回写链路）")
    ap.add_argument("--fail", action="store_true", help="假装执行失败")
    a = ap.parse_args()

    run_dir = Path(a.run_dir)
    if not run_dir.is_dir():
        raise SystemExit(f"run 目录不存在：{run_dir}")

    key = a.step.replace("/", "_")
    result_file = run_dir / "pending" / f"{key}.result.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)

    if a.fail:
        result_file.write_text(json.dumps({
            "ok": False,
            "observations": "（假执行方）页面上没找到「导出」按钮，界面与能力卡描述不符",
            "gaps": [{
                "kind": "knowledge", "skill": "demo-crm", "task": "collect-records",
                "item": a.item,
                "reason": "能力卡说「导出」是按钮，实际是右键菜单里的菜单项",
                "evidence": "evidence/shot-fail.png",
            }],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"（假执行方）已写失败结果：{result_file}")
        return

    rel = f"artifacts/collected/{a.item}.csv"
    out = run_dir / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "amount"])
        for i in range(1, a.rows + 1):
            w.writerow([f"REC-{a.item}-{i:03d}", 500 + i])

    # 造一张「证据截图」占位，真实的这里是 agent 存的截图
    ev = run_dir / "evidence" / key
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "shot-01.txt").write_text(
        f"（占位）真实运行时这里应是 {a.item} 的界面截图\n", encoding="utf-8")

    payload = {
        "ok": True,
        "out": [rel],
        "observations": f"（假执行方）按 selectors 查询并导出 {a.item}：列表 {a.rows} 行，"
                        f"导出成功；页面标题「记录管理」符合能力卡",
        "verify": [{"name": "columns_present", "ok": True, "detail": "id, amount 均存在"}],
        "effects": ["read"],
        "note": "界面与能力卡一致",
    }
    if a.gap:
        payload["gaps"] = [{
            "kind": "knowledge", "skill": "demo-crm", "task": "collect-records",
            "item": a.item,
            "reason": "列表右上角多了一个「批量导出」按钮，能力卡未覆盖；本次未点击",
            "evidence": f"evidence/{key}/shot-01.txt",
        }]
    result_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"（假执行方）已写回：{result_file}\n  产物：{rel}")


if __name__ == "__main__":
    main()
