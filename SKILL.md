---
name: job-runner
description: 作业编排与执行——把跨系统、多步骤、要重复做的事写成可校验、可续跑、有判据的 job 并跑起来：确定性加工交给脚本（tool），界面操作交给 learn-skill 学出的能力卡（skill），批量用 map，写/对外/不可逆操作强制人闸门，断言只认数据层不变量；执行中发现的缺口回写 learn-skill 补学。触发词：作业、编排、批量、跑批、流程、流水线、job、pipeline、批量导出、批量处理、对账、汇总、定时任务、断点续跑、失败重跑、幂等、审核闸门、缺口回写、jobctl。
whenToUse: 一件事要跨多个系统或多次重复、步骤多到不该每次靠临场发挥时；要跑批量（N 个客户/N 个单号/N 个分片）时；需要在写库/对外发送/不可逆动作前设人工放行时；长流程需要断点续跑、失败只重跑一部分时；要把一次成功的操作固化成可重复执行的作业时。
---

# 作业编排与执行 · job-runner

> 一句话：**learn-skill 管「这个系统怎么用」，job-runner 管「这件事怎么一步步做完、怎么证明做对了、出错怎么接着做」。**

## 0. 什么时候用 / 什么时候别用

| 用 | 别用 |
|---|---|
| 跨 ≥2 个系统，或同一系统 N 次重复 | 一次性、几步就能做完的临时操作 |
| 批量：N 个客户 / N 个单号 / N 个分片 | 纯查询、看一眼就走 |
| 有写操作、对外发送、不可逆动作 | 没有产物、没有判据的闲聊式任务 |
| 需要断点续跑 / 失败只重跑一部分 | 探索性摸索（该先让 learn-skill 学） |
| 交付物是一个或多个文件/记录（要能验收） | 只输出一段话 |

**先判定这三件事，缺一件就别急着写 job：**
1. **目标可验收吗**？（产物 + 数据层判据；答不出就先想清楚）
2. **业务计算能落到脚本里吗**？（能 → 别让模型心算）
3. **有没有需要人的地方**？（写/对外/验证码 → 闸门）

## 1. 铁律

1. **数据走文件，不走对话**：步骤之间只传路径（`${artifacts}/x.csv`）。几千行表格、长流程、断点续跑，全靠这一条。
2. **确定性优先**：能写成脚本的绝不交给模型临场算；模型只在语义不可约处出手（找界面元素、判歧义、归因）。
3. **判据落数据层**：`kind:"assert"` 要证明「条数守恒 / 键唯一 / 合计相等 / 一行不丢」，不是「看到提示条」。
4. **副作用分级 + 人闸门**：`read < write < outbound < irreversible`；写以上必须 `gate: approve`——**引擎在副作用发生前真正拦人**（不是只在 validate 里声明）；写操作还要 `idempotency`。
5. **凭据不入库**：只写 `ref:xxx` 引用，值由用户现场提供；`validate` 会扫泄漏。
6. **知识要分等级**：能力卡必须写 `evidenceLevel`（`unknown < observed < verified-once < verified-repeat`）。**没实测过的写路径不许自动跑，更不许批量**——批量会把一次错误放大成 N 条错数据。
7. **写了一定要回读**：写路径的卡片必须给 `readback`（重新读一次 + 比对字段值）。没有 API 的系统里，"界面上弹了成功提示"不算判据，那正是假成功的来源。
8. **能走接口就别点界面**：通道优先级 `mcp > api > remote-a2desk > local-a2desk > playwright > human`。界面操作是全链路里精度最低的一环，能少一次就少一次。
9. **不许偷偷改技能包**：执行中发现界面与能力卡不符 → 写 `gaps`，交回 `learn-skill` 模式 B。
10. **缺口分三类**：`env`（环境，现场修） / `knowledge`（没学过，回写 learn-skill） / `data`（数据/判据，查上游）。**只有 knowledge 进补学单。**
11. **先 dry-run 再真跑**：`--dry-run` 推演一遍全链路，确认步骤、入参、闸门位置。
12. **每一步都要能回答「失败怎么办」**：不能续跑的长流程等于没有。
13. **不写一次性脚本**：job 目录里的 `tools/` 是资产，下次同类事直接复用。

## 2. 作业区与文件

```
~/.agents/jobs/                        # 作业区（JOBS_ROOT / --root 可改）
├── registry.json                      # 登记表：job、版本、最近状态、依赖技能
├── ledger.jsonl                       # 全局审计账本（含幂等键，跨 run 去重用）
├── <job-name>/
│   ├── job.jsonc                      # 作业定义（唯一入口）
│   ├── tools/                         # kind:"tool" 调的脚本（业务计算都在这）
│   └── cards/                         # 仅本地/示例用的能力卡（正式的卡在技能包里）
└── runs/<run-id>/
    ├── run.json                       # 进度状态（续跑靠它；含批量放行的批准/驳回清单）
    ├── pending/<step>.json            # 给 agent / 人的指令（phase: exec）
    ├── pending/<step>.result.json     # 执行方写回的结果
    ├── pending/<step>.gate.json       # 写闸门放行卡（含影响面与证据等级）
    ├── pending/<step>.readback.json   # 写后回读指令（phase: readback）
    ├── artifacts/                     # 产物（交付物的唯一来源）
    ├── evidence/<step>/               # 每步的截图/证据
    ├── logs/                          # tool 的 stdout/stderr
    ├── report.md                      # 运行报告
    └── gaps.json                      # 缺口（knowledge → 补学单）
```

## 3. 标准流程

### Step 0 · 先看有没有现成的，以及知识够不够
```bash
JOBCTL="python3 ~/.agents/skills/job-runner/scripts/jobctl.py"
$JOBCTL list                     # 有没有做过同类 job（改比写快）
$JOBCTL audit                    # ★ 知识体检：哪些任务的卡齐、证据到几级、能不能自动跑
$JOBCTL card <skill>/<task>      # 要用的能力卡在不在、字段全不全、有没有回读判据
```

`audit` 是回答「这件事现在到底能不能交给 AI 自动做」的入口：它把每个技能的每个任务、
有没有机器读的能力卡、副作用等级、证据等级、有没有回读判据一次列出来。
**有写路径而证据只有 `observed`，就该先去 learn-skill 实测，而不是硬跑。**

### Step 1 · 拆步骤（照这四类分派）

| 步骤性质 | 用什么 | 谁来干 |
|---|---|---|
| 取数、转换、联接、校验、生成文件 | `kind: "tool"` | 脚本，模型不介入 |
| 只能点界面的系统 | `kind: "skill"`（+ 能力卡） | agent 用 a2desk / playwright |
| N 个对象重复 | `kind: "map"` + `for_each` | 引擎 |
| 判据 | `kind: "assert"` | 脚本 |
| 写/对外/不可逆前 | `kind: "approve"` / `gate: approve` | 人 |
| N 个对象的放行 | `kind: "approve"` + `batch` / `items` / `impact` | 人（一次决定一批，别让他点 N 次） |

### Step 2 · 写 job
```bash
$JOBCTL new <job-name>           # 从模板起骨架
$EDITOR ~/.agents/jobs/<job-name>/job.jsonc
```
格式全文见 `references/job-format.md`；每种步骤的字段与坑见 `references/step-kinds.md`。

写路径（`effects != read`）的卡片必须给三件套：`evidenceLevel`（多可信）、`impact`（错多大）、
`readback`（拿什么回读核对）。缺任何一个 validate 直接拒绝——这是 v2 契约。

### Step 3 · 校验 + 推演
```bash
$JOBCTL validate <job>                       # 结构/引用/闸门/证据等级/凭据/能力卡
$JOBCTL run <job> --dry-run --input k=v      # 推演：不执行任何副作用
```
**校验不通过就不要跑。** 报错分两类：job 写错了（改 job）、知识没学过（去补学）。
"证据等级不够"属于第二类：去 learn-skill 模式 B 实测，回来把 `evidenceLevel` 升上去。

### Step 4 · 跑，并在暂停点接手
```bash
$JOBCTL run <job> --input scope=a,b,c        # 退出码 3 = 暂停
$JOBCTL status <run-id>                      # 看停在哪、为什么停、什么阶段
```
暂停有四种（看 `phase` 字段）：
- `needs_agent` + `phase: exec`（skill 步骤）：读 `pending/<step>.json`，按里面的
  `capability.selectors` 用 **mcp → api → a2desk → playwright** 的顺序操作，
  **每步截图存进 `evidenceDir`**，然后写 `resultFile`，再 `resume`。
- `needs_user` + `phase: gate`（写闸门）：**副作用发生之前**的放行。把 `impact` / `evidence` /
  `willDo` 原样念给用户，等他明确答复。**没放行之前什么都不会发生。**
- `needs_agent` + `phase: readback`（写后回读）：写操作已经执行，现在去**重新读一次**刚改的对象，
  把真实值落成产物，写 `<step>.readback.result.json`。不一致就照实报告，别硬说成功。
- `needs_user`（approve / 对外投递，可能是一批）：把指令里的内容**原样**念给用户。
  ```bash
  $JOBCTL resume <run-id> --approve --by 张三              # 放行（批量=放行全部）
  $JOBCTL resume <run-id> --reject  --note "金额不对"       # 驳回
  $JOBCTL resume <run-id> --approve --only A,C             # 批量里只放 A、C
  $JOBCTL resume <run-id> --reject-items B                 # 驳回 B，其余放行
  ```
  **agent 不可以自己放行**：`--approve` 必须来自用户的明确答复。

### Step 5 · 收口
```bash
$JOBCTL report <run-id>          # 报告：步骤/判据/产物
$JOBCTL gaps  <run-id> --brief   # 有缺口 → 生成给 learn-skill 的补学单
```
失败时按 `references/recovery.md` 归因：**env 现场修 / knowledge 交 learn-skill / data 查上游**，
不要混着改。

## 4. 与 learn-skill 的分工（接口只有两样东西）

```
人录屏 ──▶ learn-skill 模式A ──▶ 能力卡（tasks/<task>.json）+ pages/selectors.json
                                        │
                                        ▼
                            job-runner 读卡执行（skill 步骤）
                                        │
                    缺口 gaps.json ─────┘──▶ learn-skill 模式B 补学回写（版本 +0.1）
```

| | learn-skill | job-runner |
|---|---|---|
| 产物 | 技能包：系统怎么用 | job：这件事怎么做 |
| 机器可读接口 | `tasks/<task>.json`（能力卡）、`pages/selectors.json` | `job.jsonc` |
| 谁能改谁 | 只有 learn-skill 改技能包 | 只有 job-runner 改 job |
| 交接物 | 能力卡 + 版本号 | `gaps.json` / `gaps-brief.md` |

规则：
- **能力卡缺失不是错误，是知识缺口**：引擎会在此暂停并记 `knowledge` 缺口，让 agent 现场
  用 learn-skill 模式 B 补学；补完直接 `resume`，不用重跑整个 job。
- **job 声明依赖技能版本**（`requires.skills`），版本不满足直接拒绝跑——避免用旧知识跑新流程。
- 执行中发现界面与卡不符 → 只写 `gaps`，不动技能包。
- **补学完要顺手升证据等级**：实测通过一次 → `verified-once`；多次/回归过 → `verified-repeat`。
  等级不升，编排侧就只能把它当"没实测过"处理（写要人放行、禁止批量），
  这正是让闸门不至于退化成点确认键的关键。

## 5. 参考文档

| 文件 | 什么时候读 |
|---|---|
| `references/job-format.md` | 写 job：字段、引用表达式、能力卡契约（含 readback）、校验规则 |
| `references/step-kinds.md` | 6 种步骤怎么用、坑在哪、怎么加新 kind（含写闸门与回读阶段） |
| `references/safety.md` | 副作用分级、证据阶梯、闸门聚合、幂等、凭据、对外投递 |
| `references/recovery.md` | 失败归因、续跑、幂等跳过、replay |
| `references/authoring-guide.md` | 从零写一个 job（含从一次成功操作固化成 job） |
| `examples/` | 两个可跑示例：纯脚本、GUI 交接 |

## 6. 自测

```bash
bash ~/.agents/skills/job-runner/scripts/selftest.sh
```
覆盖：端到端跑通、断点续跑、幂等、GUI 交接、人闸门、判据拦截、能力卡缺失归因、凭据 lint，
以及 v2 新增的：**写闸门在副作用之前真的拦人**、缺证据等级/影响面/回读的写卡被拒、
`observed` 写路径被禁止批量、低报副作用被拒、写后回读两阶段（不一致判失败）、
批量闸门的部分放行、`jobctl audit` 知识体检。
