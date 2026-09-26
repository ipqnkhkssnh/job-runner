# job-runner

**作业编排与执行技能**——把「跨系统、多步骤、要重复做的事」写成可校验、可续跑、有判据的 job，
交给 agent 跑起来；执行中缺什么知识，回流给 [learn-skill](https://github.com/ipqnkhkssnh/learn-skill) 补学。

它是一个 [Agent Skills](https://agentskills.io/specification) 技能：`SKILL.md` 教 agent 怎么编排，
`scripts/jobctl.py` 是真正干活的运行时（**只用 Python 3 标准库**）。

```bash
JOBCTL="python3 ~/.agents/skills/job-runner/scripts/jobctl.py"

$JOBCTL new recon-monthly                     # 起骨架
$JOBCTL validate recon-monthly                # 校验：结构/引用/闸门/凭据/能力卡
$JOBCTL run recon-monthly --dry-run --input period=2026-09   # 推演，不产生副作用
$JOBCTL run recon-monthly --input period=2026-09             # 真跑；退出码 3 = 停在等人/等 agent
$JOBCTL resume <run-id> --approve --by 张三    # 人闸门放行后续跑
$JOBCTL report <run-id>                       # 报告：步骤/判据/产物
```

## 它解决什么问题

让 agent 做一件稍微复杂的事，现在通常会散成一堆临场操作：模型一边看界面一边算数，
中间结果留在对话里，出了错从头再来，做完了也说不清「到底对不对」。

job-runner 把这套东西拆成四件确定的事：

| 问题 | 做法 |
|---|---|
| 谁来算 | **脚本**。业务计算一律写进 `tools/`，模型不算数、不搬运 |
| 界面怎么办 | **能力卡**。只能点界面的系统，由 learn-skill 学出 `tasks/<task>.json`，引擎暂停、agent 照卡执行、写回结果再续跑 |
| 怎么算做对了 | **数据层判据**。条数守恒 / 键唯一 / 合计相等 / 一行不丢，而不是「看到提示条」 |
| 出事怎么办 | **续跑 + 归因**。进度每一步落盘；失败分 `env` / `knowledge` / `data`，只有 knowledge 回写技能 |

## 六个概念

| 概念 | 说明 |
|---|---|
| **job** | 一个作业定义（`job.jsonc`：JSONC = JSON + 注释 + 尾逗号容错），放在作业区 `~/.agents/jobs/<job-name>/` |
| **run** | 一次执行实例，产物/证据/报告全在 `runs/<run-id>/`，断点续跑靠 `run.json` |
| **step** | 六种：`tool`（脚本）、`skill`（能力卡/界面）、`map`（批量）、`assert`（判据）、`approve`（人闸门）、`notify`（对外投递） |
| **artifact** | 步骤之间只传**路径**不传内容，所以几千行表格、长流程、断点续跑都成立 |
| **gate** | 副作用分四级 `read < write < outbound < irreversible`；写以上必须人工放行，写操作还要幂等键 |
| **gap** | 缺口。`env` 现场修 / `knowledge` 交 learn-skill / `data` 查上游 |

## 安装

装在用户技能根（DSH / Cursor / Claude 等共用同一目录约定）：

```bash
mkdir -p "$HOME/.agents/skills"
cp -R . "$HOME/.agents/skills/job-runner"
```

Windows：

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
Copy-Item -Recurse -Force . "$HOME\.agents\skills\job-runner"
```

**依赖**：`python3`（标准库即可）。读 `.xlsx` 判据时才需要可选的 `openpyxl`，缺失会明确报错而不是静默跳过。

## 60 秒验证

```bash
bash scripts/selftest.sh
```

33 项自测，覆盖：纯脚本端到端跑通、dry-run、断点续跑、幂等跳过、GUI 交接协议（逐元素暂停与写回）、
两次人闸门、对外投递闸门、判据拦坏数据、能力卡缺失归因、写操作缺闸门被拒、凭据泄漏被拒。
自测自己开临时作业区，不碰真实数据。

两个可跑的示例：

- [`examples/a-pure-script/`](examples/a-pure-script/) — 批量取数 → 加工 → 对账 → 数据层判据 → 产出结果（一条命令跑完）
- [`examples/b-gui-handoff/`](examples/b-gui-handoff/) — 演示暂停/续跑协议与人闸门（用假能力卡，不依赖真实系统）

## 与 learn-skill 的分工

```
人录屏 ──▶ learn-skill 模式A ──▶ 能力卡 tasks/<task>.json + pages/selectors.json
                                        │
                                        ▼
                            job-runner 读卡执行（skill 步骤：暂停 → agent 操作 → 写回 → 续跑）
                                        │
                    缺口 gaps.json ─────┘──▶ learn-skill 模式B 补学回写（版本 +0.1）
```

| 方向 | 交接物 |
|---|---|
| learn-skill → job-runner | `<技能包>/tasks/<task>.json`（能力卡）、`<技能包>/pages/selectors.json`（语义定位器集） |
| job-runner → learn-skill | `runs/<run-id>/gaps.json`、`gaps-brief.md`（补学单） |

**能力卡缺失不算失败**：引擎就地暂停并记一条 `knowledge` 缺口，agent 现场补学、产出卡，
`resume` 即继续——不用重跑整个作业。

## 目录

```
SKILL.md                  技能入口：何时用、10 条铁律、标准流程、分工
references/
  job-format.md           job.jsonc 规范 + 能力卡契约 + validate 自检清单
  step-kinds.md           6 种步骤的字段、坑、如何加新 kind
  safety.md               副作用分级、闸门、幂等、凭据、审计
  recovery.md             失败归因、续跑、只重跑一部分、缺口回写
  authoring-guide.md      从零写一个 job（含「把一次成功操作固化成 job」）
templates/                job / 能力卡 / 报告 / 缺口 / job README 模板
examples/                 两个可跑示例
scripts/
  jobctl.py               唯一入口：validate / run / resume / status / log / report / gaps / list / new / card
  joblib/                 引擎（core / cards / validate / invariants / kinds / runner）
  selftest.sh             33 项验收
```

## 状态

已实现：六种 kind、JSONC job 定义、能力卡解析与版本校验、map 批量与逐元素断点、
人闸门与对外投递闸门、幂等账本、跨 run 审计、数据层不变量（11 条）、
缺口分类与补学单、dry-run、安全 lint（凭据泄漏 / 越界路径 / 闸门严密度）。

计划中：`map` 并行、定时调度、从审计日志自动合成 job 草稿、更多投递通道、replay。

## License

MIT
