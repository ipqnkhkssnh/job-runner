# 步骤类型（kind）规范

引擎只认 6 种 kind，**业务永远不进引擎**：新增能力靠「加一个 tool 脚本」或
「加一个 kind 适配器」，前者是常态，后者很少见。

---

## 1. `tool` —— 确定性加工（用得最多）

```jsonc
{
  "id": "normalize",
  "kind": "tool",
  "run": ["python3", "tools/normalize.py", "--inbox", "${artifacts}/inbox", "--out", "${artifacts}/normalized.csv"],
  "out": "artifacts/normalized.csv",
  "effects": "read",
  "timeout": 600,
  "errorClass": "env"
}
```

约定（照做就少踩坑）：

| 约定 | 原因 |
|---|---|
| `run` 用**数组**形式（argv），不要写 shell 一行流 | 不经过 shell，没有注入/引号/通配符展开的坑；管道逻辑写进脚本 |
| `run` 写字符串也可以，会被 `shlex.split` 后按 argv 执行 | 兼容习惯写法，但**不会有 shell 语义** |
| **cwd = job 目录**，所以 `tools/x.py` 这样写就行 | 换机器/换目录都不用改 |
| 产物路径由 `--out` 传进去，脚本自己 `mkdir -p` | 引擎不猜文件名；`out` 只负责回读校验 |
| 需要多个输入文件时传**目录**，脚本自己 glob | 避免在 argv 里依赖 shell 通配符 |
| 脚本最后 `print()` 一行结论 | 这行会进 `steps.<id>.detail` 与报告 |
| 结果必须**确定性**（别用 `hash()`、别用当前时间做键） | 否则重跑不可复现、判据无意义 |

环境变量（脚本里可直接读）：`JOB_RUN_DIR` / `JOB_ARTIFACTS` / `JOB_JOB_DIR` /
`JOB_RUN_ID` / `JOB_STEP_ID` / `JOB_INPUTS`（入参 JSON）。

**声明的 `out` 没匹配到文件 → 该步骤失败（`data`），不会静默继续。**

---

## 2. `skill` —— 界面操作 / 需要人的能力（与 learn-skill 的接缝）

```jsonc
{
  "id": "collect",
  "kind": "skill",
  "use": "some-platform/collect-records",   // ← 能力卡：<skill>/<task>
  "inputs": { "key": "${item}" },
  "effects": "read",
  "out": "artifacts/collected/${item}.csv",
  "verify": [ { "name": "not_empty", "paths": "artifacts/collected/${item}.csv" } ]
}
```

引擎**不自己驱动界面**（skill 不是新工具类型，MCP 由 agent 调）。流程是：

1. 引擎解析能力卡 → 落 `pending/<step>.json`：
   - `capability`：卡片全文（含 `selectors` / `steps` / `automationBoundary`）；
   - `expect`：要产出的 `out`、要满足的 `verify`；
   - `evidenceDir` / `resultFile` / `resumeCmd`；
   - `safety`：effects/gate 与「写操作需授权」的提醒。
2. **退出码 3**，把控制权交给 agent。
3. agent 按 `learn-skill` §3 的通道优先级操作（remote-a2desk → local-a2desk → playwright），
   **每步先截图 → 操作 → 再截图**，截图写进 `evidenceDir`。
4. agent 写 `resultFile`：

```jsonc
{
  "ok": true,
  "out": ["artifacts/collected/alpha.csv"],     // 必须真实存在
  "observations": "界面反馈原话 / 报错原文",
  "verify": [ { "name": "columns_present", "ok": true, "detail": "..." } ],
  "effects": ["read"],
  "gaps": [ { "kind": "knowledge", "skill": "...", "task": "...",
              "reason": "界面多了「批量导出」按钮，卡里没有",
              "evidence": "evidence/xxx/shot-03.png" } ],
  "note": "给下一次执行的提示"
}
```
5. `jobctl resume <run-id>`：引擎校验产物存在 → 跑该步骤的 `verify` → 继续。

**能力卡不存在时不是失败，是知识缺口**：引擎在同一位置暂停，
指令里告诉 agent「去用 learn-skill 模式 B 补学、产出 `<skill>/tasks/<task>.json`」，
并记一条 `knowledge` 缺口。补完卡片直接 `resume`，引擎重新解析，不用重跑整个 job。

**禁止**：在 job 里写裸坐标、写死界面文案的顺序、把登录凭据写进 job 或卡片。

---

## 3. `map` —— 批量（通用，不需要业务代码）

```jsonc
{
  "id": "fetch-all",
  "kind": "map",
  "for_each": "${inputs.scope}",
  "steps": [
    { "id": "fetch", "kind": "tool",
      "run": ["python3", "tools/fetch_one.py", "--key", "${item}", "--out", "${artifacts}/${item}.json"],
      "out": "artifacts/${item}.json" }
  ]
}
```

| 特性 | 说明 |
|---|---|
| 元素来源 | 任何解析成列表的引用（入参、上游产物里的列表、`${steps.x.out}` 多个文件） |
| 上下文 | 子步骤里 `${item}` / `${index}` 可用 |
| 产物 | 子步骤的 `out` 汇总到 map 步骤自身的 `out`，后续用 `${steps.<map-id>.out}` 取 |
| 断点 | 每个元素的完成情况记在 `run.json` 的 `maps`，续跑不会从头再来 |
| 暂停 | 子步骤（尤其 `skill`）暂停后，续跑从**那个元素的那个子步骤**继续 |
| 引用限制 | 子步骤的 `steps.<map-id>/<sub-id>` 不可被外部引用（map 内用局部变量，外部用 map 的汇总） |
| 暂不支持 | 嵌套 map；并行（`max_parallel`）留到 P3 |

**批量必须防漏防重**：在 map 后用 `assert` 校验「每个元素都产出了文件」——
例如 `row_count_between` 或 `subset_keys`（元素清单 ⊆ 结果键集）。

---

## 4. `assert` —— 数据层判据

```jsonc
{
  "id": "checks",
  "kind": "assert",
  "invariants": [
    { "name": "not_empty", "path": "artifacts/x.csv" },
    { "name": "columns_present", "path": "artifacts/x.csv", "columns": ["id", "amount"] },
    { "name": "keys_unique", "path": "artifacts/x.csv", "key": "id" },
    { "name": "subset_keys", "left": { "path": "artifacts/a.csv", "key": "id" },
      "right": { "path": "artifacts/b.csv", "key": "id" } }
  ]
}
```

内置不变量（支持 `csv` / `json` / `jsonl`；`xlsx` 需 openpyxl）：

| 名称 | 参数 | 证明什么 |
|---|---|---|
| `file_exists` | `path`/`paths` | 产物在 |
| `not_empty` | `path`/`paths` | 非空 |
| `row_count_between` | `path`,`min`,`max` | 行数在区间（防漏/防爆） |
| `columns_present` | `path`,`columns` | 列齐全（防上游改表头） |
| `keys_unique` | `path`,`key` | 键唯一（防重复） |
| `no_null` | `path`,`columns` | 必填无空 |
| `sum_equals` | `path`,`column`,`equals`,`tolerance` | 合计对得上 |
| `sums_equal` | `left{path,column}`,`right{...}` | 两侧合计相等 |
| `equal_counts` | `left`,`right` | 两侧行数相等 |
| `subset_keys` | `left{path,key}`,`right{...}` | 一行不丢 |
| `rows_preserved` | `before`,`after`（glob） | 搬数据前后行数守恒 |

**判据写在「交付前」和「每一步之后」两个位置**：前者拦住错结果（最重要），
后者定位是哪一步弄坏的。快而脏的检查用 `verify`，关键结论用独立 `assert` 步骤。

---

## 5. `approve` —— 人闸门

```jsonc
{ "id": "signoff", "kind": "approve", "effects": "read",
  "message": "即将提交 ${steps.normalize.detail}，确认继续？",
  "remember": ["交付对象", "本次范围"] }
```

暂停后 agent 应把 `message` **原样**念给用户，拿到明确答复再
`jobctl resume <run-id> --approve --by <谁>`（或 `--reject`）。

**写操作不要用 `approve` 代替闸门配置**：`kind: notify` / `effects: write` 的步骤自己就要
`gate: approve`；`approve` 步骤是额外的检查点（例如「提交前让人核对金额」）。
驳回会以 `unknown` 分类失败并终止本次 run（不会继续往下做）。

---

## 6. `notify` —— 对外投递（默认被闸门挡住）

```jsonc
{ "id": "deliver", "kind": "notify", "effects": "outbound", "gate": "approve",
  "target": { "kind": "file", "path": "artifacts/outbox/delivery.json" },
  "payload": { "job": "${job.name}", "artifact": "${steps.normalize.out}" } }
```

`target.kind` 三种：

| kind | 字段 | 说明 |
|---|---|---|
| `file` | `path` | 写到 run 目录内（相对）或绝对路径；用于投递到共享目录/投递箱 |
| `http` | `url`,`method`,`headers`,`timeout` | JSON POST；**凭据只能来自 `secrets` 引用** |
| `command` | `run` | 交给别的 CLI（如企业 IM 的发送命令）；同样受闸门约束 |

`gate: auto` + `effects: outbound` 会被 validate 拒绝——对外发送**必须**有人放行。

---

## 7. 加一个新 kind（很少需要）

判据：**如果某个 job 里出现大量同类专用写法，说明它该降级成 `tool`**。只有当某类步骤需要
引擎级别的特殊语义（如「等待外部回调」「开子 job」「并行扇出」）时才加 kind：

1. 在 `scripts/joblib/kinds.py` 写 `handle_xxx(step, ctx)`，返回
   `complete` / `pause` / `fail` 三种结果之一；
2. 注册进 `HANDLERS`；
3. 在 `core.KINDS` 加名字，在 `validate.py` 加该 kind 的必填字段校验；
4. 在本文件加一节，写清字段、暂停语义、失败分类；
5. 在 `scripts/selftest.sh` 加一条用例（没有用例的 kind 不算支持）。

`ctx` 里可用的东西：`run_dir` / `artifacts_dir` / `job_dir` / `job` / `inputs` /
`refs` / `skills_root` / `gaps` / `asserts` / `ledger` / `dry_run` / `resume_cmd()`。
