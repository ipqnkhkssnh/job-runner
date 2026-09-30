# 从零写一个 job

## 0. 先做四个决定（不做完不要动手写）

| 决定 | 怎么答 | 影响 |
|---|---|---|
| 目标与产物是什么 | 一句话目标 + 交付文件名 | `goal` / `out` |
| 怎么证明做对了 | 2–4 条数据层判据 | `assert` 步骤 |
| 哪些是「界面活」 | 只能点、没有 API 的部分 | `skill` 步骤 + 能力卡 |
| 哪些要人点头 | 写库 / 对外发送 / 不可逆 | `gate` / `approve` |

## 1. 五分钟起骨架

```bash
JOBCTL="python3 ~/.agents/skills/job-runner/scripts/jobctl.py"
$JOBCTL new recon-monthly
ls ~/.agents/jobs/recon-monthly      # job.jsonc / tools/ / cards/ / README.md
```

## 2. 写步骤的顺序（照这个顺序写，最省返工）

1. **先写 `tool` 取数 + 加工**（能脱离界面就先脱离），用 `--dry-run` 跑通链路；
2. **再换掉需要界面的那一段**：把 tool 步骤替换成 `kind:"skill"` + 能力卡
   （能力卡不在 → 先用 learn-skill 学）；
3. **加 `assert`**：把「怎么算对」写成不变量；
4. **加闸门**：写操作 `effects:"write" + gate:"approve"`；对外 `notify + gate:"approve"`；
5. **加批量**：把「一个对象」的步骤包进 `map`，用 `${item}` 替换写死的值；
6. **补 `idempotency`**：写操作的键；
7. **最后再 `validate` + `--dry-run`**。

## 3. 一个最小但完整的模板

```jsonc
{
  "job": "recon-monthly",
  "version": "0.1.0",
  "goal": "把本月两个来源的数据核对上，产出差异清单",
  "inputs": [ { "name": "period", "type": "string", "required": true, "desc": "账期 YYYY-MM" } ],
  "requires": {
    "skills": [ { "name": "some-platform", "version": ">=0.2" } ],
    "env": { "class": "test", "secrets": ["ref:some-account"] }
  },
  "steps": [
    { "id": "fetch-internal", "kind": "skill", "use": "some-platform/export-records",
      "inputs": { "period": "${inputs.period}" }, "effects": "read",
      "out": "artifacts/internal/*.csv",
      "verify": [ { "name": "not_empty", "paths": "artifacts/internal/*.csv" } ] },

    { "id": "fetch-external", "kind": "tool", "effects": "read",
      "run": ["python3", "tools/pull_external.py", "--period", "${inputs.period}",
              "--out", "${artifacts}/external.csv"],
      "out": "artifacts/external.csv" },

    { "id": "normalize", "kind": "tool",
      "run": ["python3", "tools/normalize.py",
              "--in", "${artifacts}/internal", "--out", "${artifacts}/internal_normalized.csv"],
      "out": "artifacts/internal_normalized.csv" },

    { "id": "reconcile", "kind": "tool",
      "run": ["python3", "tools/reconcile.py",
              "--left", "${artifacts}/internal_normalized.csv",
              "--right", "${artifacts}/external.csv",
              "--out", "${artifacts}/diff.csv"],
      "out": "artifacts/diff.csv" },

    { "id": "checks", "kind": "assert", "invariants": [
      { "name": "not_empty", "path": "artifacts/diff.csv" },
      { "name": "keys_unique", "path": "artifacts/diff.csv", "key": "id" },
      { "name": "subset_keys", "left": { "path": "artifacts/internal_normalized.csv", "key": "id" },
        "right": { "path": "artifacts/diff.csv", "key": "id" } }
    ] },

    { "id": "publish", "kind": "notify", "effects": "outbound", "gate": "approve",
      "target": { "kind": "file", "path": "artifacts/outbox/diff.json" },
      "payload": { "period": "${inputs.period}", "diff": "${steps.reconcile.out}" } }
  ],
  "onFailure": { "classify": ["env", "knowledge", "data"] }
}
```

## 4. 把一次成功的操作「固化成 job」

没有录屏、没有文档，只有「刚才这次做成了」——按这个顺序榨出 job：

1. **翻这次的审计时间线**：`jobctl log <run-id>`、会话里的工具调用记录、命令历史；
2. **按「确定性 / 界面 / 判断」三堆分**：
   - 命令与数据加工 → 抄进 `tools/*.py`（关键是**参数化**：把具体值换成 `--arg`）；
   - 界面点击 → 记成能力卡（`selectors` 用语义定位：菜单名/按钮文字/字段标签）；
   - 「我看了眼觉得不对」这种判断 → 写成 `assert`（把直觉变成不变量）；
3. **抽掉具体值**：单号/客户名/日期 → `${inputs.*}` 或 `${item}`；
4. **补上失败分支**：这次没出错，不代表下次不出；把「如果 X 不对就怎么办」写进 `errorClass`
   与 `verify`；
5. **冷启动重跑一次**：换一批入参、`--dry-run` 先行，再真跑；能跑通才算固化成功。

## 5. 写 tool 脚本的模板

```python
#!/usr/bin/env python3
"""一句话说清它干什么 + 输入输出契约。"""
import argparse, csv, os
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)     # 目录或文件
    ap.add_argument("--out", required=True)                # 产物路径
    a = ap.parse_args()

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 1) 读：自己 glob，不依赖 shell 通配符
    # 2) 算：确定性，不用 hash()/当前时间做键
    # 3) 写：列顺序固定，utf-8，表头写清
    # 4) 打印一行结论（会进报告和 steps.<id>.detail）
    print(f"done: N 行 → {out}")

if __name__ == "__main__":
    main()
```

硬要求：
- 退出码非 0 = 失败（引擎按 `errorClass` 归类，默认 `env`）；
- 产物必须落在 `--out` 指定的位置（引擎按 `out` 回读校验）；
- 脚本要**幂等**：同一入参跑两次得到同样结果（否则续跑/重跑都会出问题）。

## 6. 写完的自检清单

- [ ] `jobctl validate` 通过（0 错误）
- [ ] `--dry-run` 能跑完全链路，且闸门位置符合预期
- [ ] 每个 `skill` 步骤都有能力卡，且卡里有 `selectors`
- [ ] 每个 `write`/`outbound`/`irreversible` 步骤都有 `gate: approve`
- [ ] 每个写操作都有 `idempotency`
- [ ] **写路径的卡片有 `evidenceLevel` / `impact` / `readback` 三件套**
- [ ] **没有任何写路径只到 `observed` 或 `unknown`**（该先去 learn-skill 实测）
- [ ] 批量（`map` / `batch`）里的写路径证据到 `verified-repeat`；不到就别批量
- [ ] `assert` 覆盖了「一行不丢」「键唯一」「合计对得上」里适用的那些
- [ ] 失败分类明确（哪类缺口要回写 learn-skill）
- [ ] 报告里能一眼看出：跑了什么、产出什么、判据过没过、还差什么

## 6.1 写路径的标准形状（照这个抄）

无 API 的系统里，一条写路径的完整形状是**四段**，缺一段就有假成功的空间：

```jsonc
// 1) 规划（只读，可自动）：先弄清"要动哪些对象"
{ "id": "plan", "kind": "tool", "run": ["python3", "tools/plan.py", "--out", "${artifacts}/plan.csv"],
  "out": "artifacts/plan.csv" },

// 2) 一次批量放行（人）：把 N 个对象收成一次决定，附影响面
{ "id": "review", "kind": "approve", "effects": "read", "batch": true,
  "items": "${steps.plan.out}", "impact": { "blastRadius": "N 个对象", "reversible": false },
  "message": "即将对计划内的对象执行变更，确认？" },

// 3) 执行（写，人闸门 + 幂等）：卡片自带 readback，引擎会自动加第 4 段
{ "id": "apply", "kind": "map", "for_each": "${steps.review.approvedItems}",
  "steps": [ { "id": "w", "kind": "skill", "use": "some-platform/set-device-params",
               "effects": "write", "gate": "approve", "idempotency": "set:${item}",
               "inputs": { "sn": "${item}" }, "out": "artifacts/applied/${item}.json" } ] },

// 4) 汇总校验（可选，但强烈建议）：把回读产物再交叉验一次
{ "id": "verify-all", "kind": "assert",
  "invariants": [ { "name": "no_duplicate_side_effect",
                    "path": "artifacts/readback.csv", "key": "sn" } ] }
```

第 3 段里每个元素都会各自过一遍：**写闸门（放行）→ 执行 → 写后回读（判据）**。
所以一次批量 12 台设备，除非卡片证据到 `verified-repeat`，否则会停下来问 12 次——
这不是引擎笨，是"这 12 次谁负责"只能由人回答。想少问，就得先把实测攒够。

## 7. 反模式（见到就改）

| 反模式 | 为什么坏 | 改成 |
|---|---|---|
| 把界面文案/坐标写进 job | 界面一改就碎 | 进能力卡的 `selectors` |
| 让模型在对话里算金额/汇总 | 不可复现、不可验证 | `tool` 脚本 |
| 用「看到提示条」当成功判据 | 只证明点了，不证明对了 | 数据层 `assert` / 卡片 `readback` |
| 写路径不给 `readback` | 假成功没有出口 | 重新读一次 + `field_equals` |
| 把没实测的写路径放进 `map` | 一次错误放大成 N 条 | 先实测到 `verified-repeat` |
| 逐个对象点确认（闸门疲劳） | 人会麻木，闸门失效 | `approve` + `batch`/`items`/`impact` |
| 卡片写 `channel: local-a2desk` 但目标有 MCP | 让精度最低的环节去干最要紧的事 | `channel: mcp` + `mcp.server/tool` |
| 一个大 job 做十件事 | 失败无法定位、无法续跑 | 拆成多个 job / 用 `map` |
| 写操作用 `gate: auto` | 事故来源 | `approve` + `idempotency` |
| 每次改 job 都新开 run 却不留证据 | 事后说不清 | 报告 + README 记「为什么改」 |
