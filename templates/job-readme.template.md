# {{JOB_NAME}}

一个 job-runner 作业。定义在 `job.jsonc`，脚本在 `tools/`。

## 跑法

```bash
# 先校验（会检查能力卡是否存在、副作用闸门是否够严、有没有凭据泄漏）
python3 ~/.agents/skills/job-runner/scripts/jobctl.py validate .
# 推演一遍（不执行任何副作用）
python3 ~/.agents/skills/job-runner/scripts/jobctl.py run . --dry-run
# 真跑
python3 ~/.agents/skills/job-runner/scripts/jobctl.py run . --input scope=a,b,c
```

## 目录约定

| 路径 | 放什么 |
|---|---|
| `job.jsonc` | 作业定义（唯一入口） |
| `tools/` | `kind: "tool"` 调的脚本；**所有业务计算都写在这里**，不要靠模型心算 |
| `cards/` | 仅本地/示例用的能力卡（正式的卡由 learn-skill 写在技能包的 `tasks/<task>.json`） |
| `runs/<run-id>/` | 运行期数据（不在这个目录里，在作业区根的 `runs/` 下） |

## 写 job 的三条纪律

1. **数据走文件，不走对话**：步骤之间只传路径（`${artifacts}/x.csv`），几千行也不会爆上下文。
2. **判据要落数据层**：`kind: "assert"` 证明「条数守恒/键唯一/合计相等」，不是「看到提示条」。
3. **副作用降级处理**：`effects` 从 `read` 往上升（write/outbound/irreversible）时，
   `gate` 至少要是 `approve`；写操作还要声明 `idempotency`，防止重跑产生两条数据。
