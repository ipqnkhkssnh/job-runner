# 示例 B · GUI 交接协议（暂停 / 续跑 / 人闸门）

这个示例**故意不接任何真实系统**，目的只有一个：把「引擎跑到 GUI 步骤就停下来、等 agent 干完活写回、再继续」这套协议跑通并验证。

## 它演示了什么

| 步骤 | 发生了什么 |
|---|---|
| `collect-all`（map + skill） | 每个元素都要 agent 动手 → 引擎落 `pending/*.json` 指令、**退出码 3** |
| `normalize` / `checks` | 引擎自己跑脚本 + 数据层断言，不需要模型参与 |
| `signoff`（approve） | **人闸门**：暂停等用户点头 |
| `deliver`（notify, gate=approve） | **对外投递再放行一次**（两步确认是刻意设计） |

## 跑通它

```bash
JOB=~/.agents/skills/job-runner/examples/b-gui-handoff
CTL="python3 ~/.agents/skills/job-runner/scripts/jobctl.py --root /tmp/jobs"

# 1) 起跑：会在第一个 map 元素上暂停（退出码 3）
$CTL run $JOB --input scope=alpha,beta        # 记下 run-id

# 2) 这里本该是 agent 用 a2desk 干活；示例用假执行方代替
RUN=/tmp/jobs/runs/<run-id>
python3 $JOB/tools/fake_agent_fulfill.py --run-dir $RUN --step 'collect-all#0/collect' --item alpha --gap
$CTL resume <run-id>                           # 继续 → 在 beta 上再暂停

python3 $JOB/tools/fake_agent_fulfill.py --run-dir $RUN --step 'collect-all#1/collect' --item beta
$CTL resume <run-id>                           # 继续 → 停在 signoff（等人）

$CTL resume <run-id> --approve --by 张三       # 放行 → 停在 deliver（对外）
$CTL resume <run-id> --approve --by 张三       # 放行 → 完成

# 3) 看结果
$CTL status <run-id>
$CTL report <run-id>
$CTL gaps <run-id> --brief                     # 给 learn-skill 的补学单
```

## agent 在暂停时该做什么

1. 读 `runs/<run-id>/pending/<step>.json`（指令里有：能力卡全文、selectors、期望产物、
   证据目录、放行命令）；
2. 按 `learn-skill` §3 的通道优先级操作（remote-a2desk → local-a2desk → playwright），
   **每步先截图 → 操作 → 再截图**，截图写进 `evidenceDir`；
3. 把结果写进 `resultFile`（形状见指令里的 `resultSchema`）：
   `ok` / `out` / `observations` / `verify` / `effects` / `gaps`；
4. `jobctl resume <run-id>`；
5. 界面与能力卡不符时，在 `gaps` 里如实写一条 `kind: "knowledge"` —— 它会进 `gaps.json`，
   用 `--brief` 生成给 `learn-skill` 模式 B 的补学单。

> 不要自己改技能包。补学与回写是 `learn-skill` 的职责，这里只负责报告缺口。
