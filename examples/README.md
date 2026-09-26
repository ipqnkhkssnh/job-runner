# 示例

两个都能直接跑，互不依赖真实系统。

| 目录 | 演示什么 | 需要什么 |
|---|---|---|
| [`a-pure-script/`](a-pure-script/) | 纯脚本作业：`map` 批量 → `tool` 加工 → `assert` 数据层判据 → 产出对账结果 | 只要 python3（无第三方依赖） |
| [`b-gui-handoff/`](b-gui-handoff/) | GUI 交接协议：`skill` 暂停 → 写回 `result.json` → 续跑；人闸门与对外投递闸门；缺口回写 learn-skill | 只要 python3；用的是**假能力卡** |

```bash
JOBCTL="python3 $(cd .. && pwd)/scripts/jobctl.py --root /tmp/jobs-demo"

# A：一次跑完
$JOBCTL run "$(pwd)/a-pure-script" --input shards=alpha,beta,gamma
$JOBCTL report <run-id>

# B：按 README 里的步骤走一遍暂停/续跑
$JOBCTL run "$(pwd)/b-gui-handoff" --input scope=alpha,beta
```

跑完想看全部行为（含负例），直接跑自测：

```bash
bash ../scripts/selftest.sh
```

## 写自己的 job 时从哪抄

| 想做的事 | 抄哪里 |
|---|---|
| 批量处理 N 个对象 | `a-pure-script` 的 `fetch-all`（map） |
| 两个来源核对、一行不丢 | `a-pure-script` 的 `reconcile` + `checks` |
| 界面操作后拿回产物 | `b-gui-handoff` 的 `collect-all`（map + skill） |
| 交付前让人点头 | `b-gui-handoff` 的 `signoff`（approve） |
| 对外投递 | `b-gui-handoff` 的 `deliver`（notify + gate） |
