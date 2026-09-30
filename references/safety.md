# 安全模型：副作用分级 · 闸门 · 幂等 · 凭据

原则：**机器可以快，但只有人能决定「要不要产生不可逆后果」。**

## 1. 副作用四级

| effects | 含义 | 例子 | 必需闸门 |
|---|---|---|---|
| `read` | 只读，或只写 run 自己的目录 | 查询、导出、规范化、生成报告 | `auto` |
| `write` | 改业务数据 | 保存、提交单据、改配置 | `approve`（+ 建议 `idempotency`） |
| `outbound` | 对外发送 | 邮件、IM、上传给客户、回调第三方 | `approve`（`auto` 直接 validate 报错） |
| `irreversible` | 不可撤销 | 开票、支付、发货、删除、审批通过 | `approve` + 建议双重确认 |

**判断口诀**：出事了能不能一键回滚？不能 → `irreversible`。

validate 会做**闸门严密度**检查：`effects` 要求的闸门若比写的 `gate` 严 → 报错。所以
「顺手把 `gate` 写成 `auto`」这条路是堵死的。

**闸门是引擎真正执行的，不只是声明**：`effects != read` 且 `gate: approve` 的叶子步骤，
引擎会在**派发之前**落一份 `pending/<step>.gate.json` 并退出码 3——
副作用一定发生在人放行**之后**，不是"先干完再问"。放行卡里必须带着三样东西：

| 放行卡字段 | 为什么必须有 |
|---|---|
| `impact` | 人要看到「这一下波及多少对象、能不能回退」才谈得上判断 |
| `evidence` | 这张卡是被**看到**的还是被**跑过**的（见 §2），决定他该多谨慎 |
| `willDo` / `inputs` | 具体要写什么、写到哪个对象上 |

低报 `effects`（卡片是 `write`、步骤写 `read`）不解决问题：validate 会报错，
引擎也会**取更严的那个**。

## 2. 证据阶梯：这张卡有多可信，决定它最多能被自动到什么程度

「AI 操作可能不准」不该靠感觉争论，而该在卡片上写成一个等级：

| `evidenceLevel` | 含义 | 允许做什么 |
|---|---|---|
| `unknown` | 没见过 / 只有猜想 | **只能**只读探测；任何写步骤直接 validate 报错 |
| `observed` | 录屏或界面上看到过，**没实操** | 单件 + 人闸门；**禁止**放进 `map` / 批量放行 |
| `verified-once` | 现场实测成功过 ≥1 次 | 单件或逐批执行；批量时每批都要人确认 |
| `verified-repeat` | 多次实测 / 回归过 | 允许一次批量放行（写**仍然**要 `gate: approve`） |

配套两个声明：

- `impact`：`{ blastRadius, count, reversible, note }`——写错了会怎样；
- `readback`：写完之后**拿什么只读地回读核对**（`how: skill|tool` + `expect` 不变量）。
  没有 API 的系统里，这是唯一能落到数据层的「我做对了」的证明。

引擎会自动执行 `readback`：写阶段过了不算完，还要重新读一次并跑 `expect`，
不一致就判 `data` 失败。**证据等级只影响「能不能批量」，绝不放宽「要不要人放行」**。

## 3. 三种暂停语义（退出码 3）

| 暂停 | 触发 | 谁来接手 |
|---|---|---|
| `needs_agent` | `kind: skill` 且有界面要操作 | agent 用 a2desk/playwright 执行并写回 result.json |
| `needs_agent`（phase=readback） | 写步骤的卡片声明了 `readback` | agent 重新读一次刚改的对象，写回 `<step>.readback.result.json` |
| `needs_user`（phase=gate） | `effects != read` 且 `gate: approve` | **人**：副作用发生前放行；驳回则这一步什么都不做 |
| `needs_user` | `kind: approve`（含批量 `items`） | **人**：agent 必须原样转述并等明确答复 |
| `needs_agent`（缺卡） | 能力卡不存在 | agent 先用 learn-skill 模式 B 补学 |

agent 的权力边界：
- 可以：读指令、选通道、按选择器操作、截图留证、报缺口。
- **不可以**：自己放行人闸门（`--approve` 必须来自用户的明确答复）、替用户决定生产环境写操作、
  把凭据写进任何文件、绕过 `gaps` 直接改技能包。

## 3. 幂等

`effects != read` 的步骤建议声明幂等键：

```jsonc
{ "id": "submit", "kind": "tool", "effects": "write", "gate": "approve",
  "idempotency": "submit:${inputs.order_no}", ... }
```

行为：
- 步骤成功完成后，幂等键写进**全局账本** `~/.agents/jobs/ledger.jsonl`；
- 之后任何 run 再遇到同一个键 → **跳过该步骤**并记 `idempotent-skip`；
- 确实要重做：`--ignore-done`（会记进账本，报告里可见）。

键怎么设计：**能唯一标识「这一次业务动作」的最小信息**——
`submit:${inputs.order_no}`、`invoice:${item}:${period}`。
键里不要放时间戳（那样每次都不同，等于没有幂等）。

## 3.1 幂等的一条硬规矩：**推演（`--dry-run`）绝不写账本**

- `--dry-run` **不执行**副作用、**不记幂等键**，只在账本留一条 `dry-run-skip`；
- 为什么这是硬规矩：如果推演把写步骤的幂等键记进账本，之后**真跑时该步骤会被判为
  「幂等跳过」而静默不执行**——更糟的是，幂等检查发生在写闸门**之前**，
  于是**人闸门也不会触发**，作业看起来还"正常往下走"（只在后续步骤缺产物时才暴露）。
  也就是说：一次随手推演，能让日后一次真实写入**既没执行、也没问人**。
- 两道防线：① `runner._exec_leaf` 在 dry-run 时既不记 key 也不记 `side-effect`；
  ② `core.ledger_has_key` 跳过任何带 `dryRun` 标记的条目。
- 自查：`grep '"event": "side-effect"' ~/.agents/jobs/ledger.jsonl` 里**不该**出现 dry-run 的 runId。

## 4. 凭据

- job 与能力卡里**只允许出现引用**：`"secrets": ["ref:erp-account"]`；
- 值由用户在运行时提供（环境变量 / 现场输入 / 密码管理器），引擎只做「已声明」校验；
- validate 会扫 job 目录（`.json/.jsonc/.yaml/.md/.sh/.py/.txt`）里的私钥、AK、`token=`、
  `password=` 等模式并**直接判为错误**；
- 证据截图里如果出现凭据/客户隐私，`evidence/` 属于 run 目录，交付前要清。

## 5. 环境判定

- `requires.env.class`：`test` / `prod` / `any`，必写；
- `prod` 时 validate 会额外警告；写操作在 prod 必须**当场**再和用户确认一次范围与影响面；
- 拿不准就问，不要猜。报告里会记录 `env.class`，事后能查。

## 6. 审计

| 位置 | 内容 |
|---|---|
| `runs/<id>/run.json` | 每步状态、产物、判据结果、缺口、批量放行的批准/驳回清单 |
| `~/.agents/jobs/ledger.jsonl` | 全局时间线：run-start/tool/fulfilled/**gate-pause**/**gate-approved**/side-effect/pause/idempotent-skip/run-done/run-failed |
| `runs/<id>/pending/<step>.gate.json` | 放行卡（含影响面与证据等级）——事后可回答「当时人批的到底是什么」 |
| `runs/<id>/logs/*.log` | tool 的 argv、退出码、stdout/stderr |
| `runs/<id>/evidence/` | 界面证据（agent 每步截图；回读阶段的证据在同名 `.readback/` 下） |
| `runs/<id>/report.md` | 人看的汇总（含回读判据的通过/失败） |

`jobctl log <run-id>` 看时间线，`jobctl report <run-id>` 看报告。

## 7. 批量闸门：聚合，但要防止"授权漂移"

逐条 approve 在批量作业里会退化成闭眼点确认键。所以批量用 `kind: approve` + `batch: true`
把 N 个对象收成**一次决定**，一次放行全部（`--approve`）、只放一部分（`--only A,C`）
或驳回一部分（`--reject-items B`），结果落进 `${steps.<id>.approvedItems}` /
`.rejectedItems` 供下游直接消费。

两条边界：

1. **一次批量意愿 ≠ 对每一条的授权**：批量放行之后，下游每个写步骤仍然各自要人放行，
   除非那些步骤的卡片证据等级到了 `verified-repeat`；
2. **证据不足不许批量**：`observed` 及以下的写路径出现在 `map` 里会被 validate 拒绝——
   批量会放大错误，一次点错就是 N 条错数据。

## 8. 红线（违反就是事故）

1. 把 `gate` 从 `approve` 改成 `auto` 以「跑得更顺」——**禁止**；
2. 低报 `effects`（卡片是 `write`、步骤写 `read`）来绕过闸门——**禁止**；
3. 把只到 `observed` / `unknown` 的写路径放进批量——**禁止**；
4. 在 job/卡片/脚本里写死账号密码——**禁止**；
5. 生产环境的 `write`/`irreversible` 步骤不经用户当场确认——**禁止**；
6. 执行失败后「自己改技能包让它过」——**禁止**（写 gaps，交 learn-skill）；
7. 对不可逆操作不做幂等保护就重跑——**禁止**；
8. 写路径不给 `readback`，只靠"界面上看到成功提示"判定成功——**禁止**。
