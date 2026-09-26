# 失败归因 · 续跑 · 缺口回写

长流程的价值全在这里：**出错时知道是谁的错，并且只重做该重做的那一小段。**

## 1. 先归因，再动手

```
失败 → 看 report.md 的「失败信息」+ jobctl log <run-id>
     ├── class = env         → 现场修（装依赖/开权限/网络/超时），改完 resume
     ├── class = knowledge   → 交 learn-skill 模式 B 补学，补完 resume
     ├── class = data        → 查上游步骤与数据源，别改技能也别改闸门
     └── class = unknown     → 看指令/结果文件里执行方写的原话（可能被人驳回）
```

**最常见的错误动作**：把数据问题当环境问题重跑（白跑）、把知识缺口当 job 问题改 job
（把界面差异「绕过去」，下次换个人跑还是错）。

| 现象 | 分类 | 正确处理 |
|---|---|---|
| `命令不存在` / 超时 / 权限拒绝 | env | 装依赖、调超时、申请权限 |
| 能力卡不存在 | knowledge | learn-skill 模式 B 学出 `tasks/<task>.json` |
| 界面与卡片不符（按钮改名/位置变了/多新按钮） | knowledge | 在 result.json 的 `gaps` 里如实记一条 |
| `判据不通过`（键重复/合计不符/行数不守恒） | data | 回到产出这一步查上游；**不要**放宽判据 |
| `声明了 out 但没找到产物` | data | 脚本没按约定落盘，或传参路径不对 |
| 产出被人驳回（`--reject`） | unknown | 问用户要改什么，改完重跑该步骤 |

## 2. 续跑（断点就是 `run.json`）

```bash
jobctl status <run-id>          # 停在哪一步、为什么停
jobctl resume <run-id>          # 从暂停点继续（已完成步骤不会重跑）
```

- 进度每完成一步就落盘，所以**进程被杀、会话断了都不会丢进度**；
- `resume` 遇到 `skill`/`approve`/`notify` 步骤会先找 `pending/<step>.result.json`：
  有就消费并继续，没有就再次暂停；
- 已完成（`status: done`）的 run 再 `resume` 是**空操作**（不会重复执行副作用）；
- job 定义在 run 之后被改过 → 会警告 hash 不一致：**改结构请新开 run，改文案可续跑**。

## 3. 只重跑一部分

| 需求 | 做法 |
|---|---|
| 从某个失败步骤重来 | 直接 `resume`（失败步骤本来就没标记完成） |
| 整个 run 重跑 | `run <job> --run-id <新 id>`（不要覆盖旧的，留证据） |
| 忽略幂等账本强制重做写操作 | `run/resume ... --ignore-done`（会记进账本，事后可查） |
| 只验行为不落副作用 | `--dry-run`（skill 步骤只给指令不执行；assert 跳过） |
| 用同一批入参复现 | run 目录里有 `job.snapshot.jsonc`（当时用的 job 定义）+ `run.json` 的 inputs |

## 4. 缺口回写（与 learn-skill 的闭环）

```bash
jobctl gaps <run-id>            # 机器读的 JSON
jobctl gaps <run-id> --brief    # 给人/agent 的补学单（同时写 runs/<id>/gaps-brief.md）
```

`gaps.json` 里只有 `kind: knowledge` 的条目需要进技能包，其余属于现场处理。

**回写流程（由 learn-skill 执行，job-runner 不碰技能包）**：

1. 读补学单，定位到具体技能与任务；
2. 按 learn-skill §7 模式 B：读现状 → 确认安全边界 → 边操作边补学（每步截图）→
   回写 `tasks/<task>.md` + `tasks/<task>.json`（能力卡）、`pages/`、`state/meta.json`（version +0.1、
   unknowns 移除已验证项）、`CHANGELOG.md`；
3. 跑 `validate_skill.sh` 与 `jobctl card <skill>/<task>` 复核；
4. 回到 job：`jobctl resume <run-id>`（引擎会重新解析卡片，无需改 job）。

如果补学发现是 **job 本身拆错了**（比如该拆成两个步骤），那就改 job —— 但要**留下证据链**：
报告里记下「为什么改」，必要时在 job 的 `README.md` 里写清楚。

## 5. 沉淀

- 同类事第二次做：先 `jobctl list` 找现成 job，**改**不要重写；
- `tools/` 里的脚本是资产，抽掉业务细节就能复用（例如「按列联接两个 csv」）；
- 一个 job 反复出现同一类 `gaps` → 说明该在 learn-skill 那边把知识补全，或在
  `tools/` 里补一个更稳的处理方式，而不是每次靠 agent 现场发挥。
