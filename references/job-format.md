# job 格式规范（job.jsonc）与能力卡契约

## 0. 为什么是 JSONC 而不是 YAML

运行时不保证有 PyYAML，而 job-runner 只依赖 Python 3 标准库。所以规范格式是
**JSONC = JSON + `//` 注释 + `/* */` 注释 + 尾逗号容错**（引擎自己剥注释，不是靠第三方库）。
文件名 `job.jsonc`（也接受 `job.json`）；装了 PyYAML 时 `job.yaml` 也能跑，但不推荐。

## 1. 顶层字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `job` | ✅ | 作业名，小写 kebab-case，与目录名一致 |
| `version` | 建议 | job 自己的版本（`0.1.0`） |
| `goal` | ✅ | 一句话目标（人看得懂，不要写实现） |
| `inputs` | | 入参声明，见下 |
| `requires` | | 依赖的技能版本与环境约束 |
| `channels` | | skill 步骤的通道兜底顺序 |
| `steps` | ✅ | 步骤数组（顺序执行） |
| `onFailure` | 建议 | `{"classify": ["env","knowledge","data"]}` |

### inputs

```jsonc
"inputs": [
  { "name": "scope", "type": "list", "required": true,
    "desc": "这次要处理的范围（客户名/单号/分片）" },
  { "name": "period", "type": "string", "required": false, "desc": "账期 YYYY-MM" }
]
```
`type` 取值：`string` / `list` / `file` / `path` / `json` / `number` / `bool`。
运行时传参：`--input scope=a,b,c`（逗号自动变列表，`[...]`/`{...}` 会按 JSON 解析）
或 `--inputs-file params.json`。**必填项没传，引擎直接拒绝跑。**

### requires

```jsonc
"requires": {
  "skills": [ { "name": "some-platform", "version": ">=0.3" } ],
  "env": { "class": "test", "secrets": ["ref:some-account"] }
}
```
- `skills[].version` 不满足 → **validate 报错、拒绝运行**（防止用旧知识跑新流程）。
- `env.class`：`test` / `prod` / `any`。`prod` 会额外警告一次。
- `env.secrets`：**只能写 `ref:xxx`**，写值会被 validate 拒绝。

## 2. 引用表达式 `${...}`

| 表达式 | 得到 |
|---|---|
| `${inputs.scope}` | 入参（可能是列表） |
| `${inputs.scope[0]}` | 列表下标（负下标可用） |
| `${steps.<id>.out}` | 某步骤声明的产物路径列表（**map 步骤给的是汇总**） |
| `${steps.<id>.out[0]}` | 第一个产物 |
| `${steps.<id>.detail}` | 该步骤的说明（脚本最后一行 stdout） |
| `${artifacts}` | 本次 run 的 artifacts 目录（绝对路径） |
| `${run.dir}` / `${run.id}` | run 目录 / run id |
| `${job.name}` / `${job.dir}` / `${job.version}` | 作业名 / 作业目录 / 版本 |
| `${item}` / `${index}` | map 内当前元素与其下标 |
| `${env.HOME}` | 环境变量（只读） |
| `${secrets.<name>}` | 凭据**引用**（`ref:xxx`），不是值 |

规则：
- 整个字符串就是 `${...}` → 返回原始类型（列表/字典）；混在文本里 → 转成字符串（列表用空格连接）。
- **列表里的引用若解析成列表会自动展开**，所以 `"in": ["${steps.a.out}"]` 直接就是多个路径。
- 引用未定义 → 运行前 validate 就会报错（不会跑一半才炸）。

## 3. 步骤通用字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `id` | — | 小写 kebab-case，run 内唯一（map 子步骤路径写成 `map-id/sub-id`） |
| `kind` | — | `tool` / `skill` / `map` / `assert` / `approve` / `notify` |
| `when` | 无 | 条件：`"${inputs.flag}"`、`"effects==write"`、`"env==prod"`；不成立就整步跳过（记 `skipped`） |
| `effects` | `read` | `read` / `write` / `outbound` / `irreversible` |
| `gate` | `auto` | `auto` / `outbound` / `approve`；宽松于 `effects` 要求 → validate 报错 |
| `idempotency` | 无 | 幂等键（可含引用）。`effects != read` 且跨 run 命中账本 → 跳过 |
| `out` | 无 | 产物 glob（**相对 run 目录**，如 `artifacts/x.csv`）。声明了却没有文件 → 失败 |
| `verify` | 无 | 该步骤完成后的数据层判据（同 assert 的不变量） |
| `timeout` | 3600 | tool 超时秒数 |
| `errorClass` | `env` | tool 非零退出时的失效分类 |

**作用域**：`out` / `verify` 在 map 子步骤里可以用 `${item}`。

## 4. 每种 kind 的字段

见 `step-kinds.md`。速查：

```jsonc
{ "id": "x", "kind": "tool",   "run": ["python3","tools/x.py","--out","${artifacts}/x.csv"], "out": "artifacts/x.csv" }
{ "id": "y", "kind": "skill",  "use": "<skill>/<task>", "inputs": {...}, "out": "artifacts/y/*.csv" }
{ "id": "z", "kind": "map",    "for_each": "${inputs.scope}", "steps": [ ... ] }
{ "id": "c", "kind": "assert", "invariants": [ { "name": "keys_unique", "path": "artifacts/x.csv", "key": "id" } ] }
{ "id": "s", "kind": "approve","message": "即将提交 X，确认？", "remember": ["范围"] }
{ "id": "d", "kind": "notify", "target": {"kind":"file","path":"artifacts/outbox/x.json"}, "effects": "outbound", "gate": "approve" }
```

## 5. 能力卡契约（learn-skill ↔ job-runner 的唯一接口）

**位置**：`<技能根>/<skill>/tasks/<task>.json`（人读的 `tasks/<task>.md` 并存）。
**引用**：job 里写 `"use": "<skill>/<task>"`（本地/示例可用 `"use": "local:cards/x.json"`，
或直接内联 `"card": {...}`）。

```jsonc
{
  "card": "v1",                       // 契约版本，当前只认 v1
  "skill": "some-platform",
  "task": "collect-records",
  "title": "人能看懂的任务名",
  "version": "0.3.0",                 // 必须与技能 state/meta.json 的版本一致
  "system": "哪个系统",
  "channel": "auto",                  // auto/remote-a2desk/local-a2desk/playwright/human
  "envClass": "test",
  "effects": "read",                  // 与 job 步骤的 effects 取更严的那个
  "inputs":  [ { "name": "key", "type": "string", "required": true, "desc": "..." } ],
  "outputs": [ { "name": "records", "path": "artifacts/collected/${item}.csv", "type": "csv" } ],
  "secretsRef": ["ref:some-account"],
  "selectors": {                      // ★ 稳定定位的依据，替代裸坐标
    "page": "窗口标题/URL 特征（判断前置条件）",
    "entries": [ { "step": 1, "by": "menu", "text": "记录管理" },
                 { "step": 2, "by": "label", "text": "关键字", "action": "type" },
                 { "step": 3, "by": "button", "text": "查询", "action": "click" } ],
    "waits": [ { "after": 3, "for": "结果表出现且行数 ≥ 1" } ]
  },
  "automationBoundary": { "needsHuman": ["登录验证码"], "notes": "..." },
  "steps":  [ { "n": 1, "do": "语义定位 + 动作", "expect": "可观察反馈" } ],
  "verify": [ { "name": "columns_present", "path": "...", "columns": ["id"] } ],
  "effectsDetail": { "changes": "…", "outbound": "…", "idempotent": "…" },
  "evidence": { "frames": "f018-f024", "assets": [] },
  "unknowns": [ "界面上看到但没操作过的分支" ],
  "updatedAt": "YYYY-MM-DD"
}
```

**硬性要求**（`jobctl card <skill>/<task>` 与 `validate` 都会查）：
1. `card` / `skill` / `task` / `version` / `outputs` 必须存在；
2. `outputs[].path` 必须是 run 目录内的相对路径（不允许绝对路径或 `..`）；
3. `secretsRef` 只能是 `ref:xxx`；
4. `effects` 必须是四级之一。

**卡里必须有 `selectors`**——否则 agent 只能靠猜坐标，正是 learn-skill 红线禁止的事。

## 6. 失效分类（`onFailure.classify`）

| 分类 | 含义 | 处理 |
|---|---|---|
| `env` | 命令不存在、依赖缺失、超时、权限 | 现场修（装依赖/开权限）；不进技能 |
| `knowledge` | 能力卡缺失、界面与卡不符、有未学过的分支 | **交 learn-skill 模式 B**，进 `gaps.json` |
| `data` | 判据不通过、产物没落地、上游数据脏 | 查上游步骤/数据源 |
| `unknown` | 预期外（人驳回、格式非法） | 人工看报告 |

## 7. validate 会查什么（写 job 时的自检清单）

- 结构：`job`/`steps`/id 唯一且 kebab-case/kind 合法/各 kind 必填项齐全；
- 引用：表达式语法、`steps.<id>` 存在、`inputs.<name>` 已声明、头名合法；
- 闸门：`effects` 与 `gate` 是否匹配（写以上必须人工放行）；写操作没声明 `idempotency` 会警告；
- 安全：`out` 路径不得逃出 run 目录；job 目录里扫凭据（私钥/AK/token/`password=` 等）；
- 知识：能力卡存在性（缺失 → **警告**，运行时会暂停等补学）、卡片字段合法性、
  `requires.skills` 版本是否满足、`requires.env.secrets` 是否只写引用；
- 数据：`assert` 的不变量名是否已知；
- 提醒：`env.class` 未写、`onFailure` 未写会警告。
