# Run 报告 · {{RUN_ID}}

| | |
|---|---|
| 作业 | `{{JOB}}` |
| 目标 | {{GOAL}} |
| 状态 | **{{STATUS}}** |
| 开始 | {{STARTED}} |
| 更新 | {{UPDATED}} |
| dry-run | {{DRY_RUN}} |

## 入参

```json
{{INPUTS}}
```

## 步骤

| 步骤 | 状态 | 副作用 | 说明 | 产物 |
|---|---|---|---|---|
{{STEPS_TABLE}}

## 判据（数据层断言）

| 步骤 | 判据 | 结果 | 说明 |
|---|---|---|---|
{{ASSERTS_TABLE}}

## 产物

{{ARTIFACTS}}

## 缺口（回写 learn-skill）

| 类型 | 技能/任务 | 缺口 | 建议动作 |
|---|---|---|---|
{{GAPS_TABLE}}

## 失败信息

{{ERROR}}

---

> 由 job-runner 的 jobctl 生成。判据只证明「数据对不对」，界面反馈的证据在 `evidence/`，
> 审计时间线在 `run.jsonl` 与作业区 `ledger.jsonl`。
