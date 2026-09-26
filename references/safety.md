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

## 2. 三种暂停语义（退出码 3）

| 暂停 | 触发 | 谁来接手 |
|---|---|---|
| `needs_agent` | `kind: skill` 且有界面要操作 | agent 用 a2desk/playwright 执行并写回 result.json |
| `needs_user` | `kind: approve`、`gate: approve` 的 notify | **人**：agent 必须原样转述并等明确答复 |
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
| `runs/<id>/run.json` | 每步状态、产物、判据结果、缺口 |
| `~/.agents/jobs/ledger.jsonl` | 全局时间线：run-start/step/side-effect/pause/idempotent-skip/run-done/run-failed |
| `runs/<id>/logs/*.log` | tool 的 argv、退出码、stdout/stderr |
| `runs/<id>/evidence/` | 界面证据（agent 每步截图） |
| `runs/<id>/report.md` | 人看的汇总 |

`jobctl log <run-id>` 看时间线，`jobctl report <run-id>` 看报告。

## 7. 红线（违反就是事故）

1. 把 `gate` 从 `approve` 改成 `auto` 以「跑得更顺」——**禁止**；
2. 在 job/卡片/脚本里写死账号密码——**禁止**；
3. 生产环境的 `write`/`irreversible` 步骤不经用户当场确认——**禁止**；
4. 执行失败后「自己改技能包让它过」——**禁止**（写 gaps，交 learn-skill）；
5. 对不可逆操作不做幂等保护就重跑——**禁止**。
