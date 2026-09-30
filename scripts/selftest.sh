#!/usr/bin/env bash
# selftest.sh — job-runner 自测（P1 + P2 验收）
#
# 覆盖：
#   1. 纯脚本作业端到端跑通（map / tool / assert / 报告 / 账本）
#   2. 幂等与断点续跑（--dry-run、resume）
#   3. GUI 交接协议（skill 步骤暂停 → 写回 result.json → 续跑）
#   4. 人闸门（approve）与对外投递闸门（notify gate=approve）
#   5. 负例：判据失败被拦住；能力卡缺失 → 归因为 knowledge 并进 gaps
#   6. 校验器的安全 lint：写操作缺闸门、凭据泄漏
#
# 用法：bash selftest.sh [作业区根目录]（默认用临时目录，不碰 ~/.agents/jobs）
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTL="python3 $SKILL_DIR/scripts/jobctl.py"
ROOT="${1:-$(mktemp -d /tmp/jobrunner-selftest-XXXXXX)}"
EX_A="$SKILL_DIR/examples/a-pure-script"
EX_B="$SKILL_DIR/examples/b-gui-handoff"
PASS=0; FAIL=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
head_() { printf '\n== %s\n' "$*"; }
# 断言上一条命令的退出码等于 $1
expect_rc() { # <期望码> <说明>
  local rc=$?  # 注意：需紧跟被测命令
  :
}
run_id_of() { printf '%s\n' "$1" | sed -n 's/^run-id: //p' | head -1; }

printf 'job-runner selftest\n作业区：%s\n' "$ROOT"

# ---------------------------------------------------------------- 1. 纯脚本作业
head_ "1. 纯脚本作业（${EX_A}）"
out="$($CTL --root "$ROOT" validate "$EX_A" 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "validate 通过" || { bad "validate 失败"; printf '%s\n' "$out" | tail -5; }

out="$($CTL --root "$ROOT" run "$EX_A" --input shards=alpha,beta,gamma 2>&1)"; rc=$?
RID="$(run_id_of "$out")"
[ $rc -eq 0 ] && ok "run 成功（${RID}）" || { bad "run 失败"; printf '%s\n' "$out" | tail -8; }
RUN="$ROOT/runs/$RID"
for f in artifacts/normalized.csv artifacts/joined.csv artifacts/exceptions.csv \
         artifacts/result.csv artifacts/digest.json report.md; do
  [ -f "$RUN/$f" ] && ok "产物存在：$f" || bad "缺产物：$f"
done
grep -q '"matched": 8' "$RUN/artifacts/digest.json" 2>/dev/null \
  && ok "对账结果符合预期（8 行对平）" || bad "对账结果不符合预期"
grep -q '^| checks |' "$RUN/report.md" && ok "报告里有判据表" || bad "报告缺判据表"
[ -s "$ROOT/ledger.jsonl" ] && ok "审计账本已写" || bad "审计账本为空"

# 幂等/续跑：已完成再 resume 应当是「不重复执行」的空操作
before="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('finishedAt'))" "$RUN/run.json")"
out="$($CTL --root "$ROOT" resume "$RID" 2>&1)"; rc=$?
after="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('finishedAt'))" "$RUN/run.json")"
if [ "$before" = "$after" ] && printf '%s' "$out" | grep -q '已经完成'; then
  ok "已完成的 run 是空操作，不会重复执行"
else
  bad "已完成的 run 被重复执行了"
fi
# dry-run 不产生副作用
out="$($CTL --root "$ROOT" run "$EX_A" --dry-run --input shards=alpha 2>&1)"; rc=$?
RID2="$(run_id_of "$out")"
[ $rc -eq 0 ] && [ ! -e "$ROOT/runs/$RID2/artifacts/normalized.csv" ] \
  && ok "dry-run 不落地产物" || bad "dry-run 疑似真的执行了"

# 可选入参没传 → 解析成"没有值"（不是引用解析失败），default 生效
OPTT="$(mktemp -d)"
mkdir -p "$OPTT/job"
cat > "$OPTT/job/job.jsonc" <<'JSON'
{
  "job": "optional-inputs", "goal": "可选入参没传时不该在引用解析上失败",
  "inputs": [ { "name": "days", "type": "string", "required": false },
              { "name": "tag", "type": "string", "required": false, "default": "dft" } ],
  "steps": [ { "id": "echo", "kind": "tool", "effects": "read",
               "run": ["python3", "-c", "import sys;open(sys.argv[1],'w').write('|'.join(sys.argv[2:]))",
                       "${artifacts}/args.txt", "${inputs.days}", "${inputs.tag}"],
               "out": "artifacts/args.txt" } ]
}
JSON
out="$($CTL --root "$OPTT/jobs" run "$OPTT/job" 2>&1)"; rc=$?
if [ $rc -eq 0 ] && [ "$(cat "$OPTT/jobs/runs/$(run_id_of "$out")/artifacts/args.txt" 2>/dev/null)" = "|dft" ]; then
  ok "可选入参没传 → 空值（不再引用解析失败），default 生效"
else
  bad "可选入参处理不对，rc=${rc}；$(printf '%s' "$out" | tail -2)"
fi
rm -rf "$OPTT"

# ---------------------------------------------------------------- 2. GUI 交接协议
head_ "2. GUI 交接协议（${EX_B}）"
out="$($CTL --root "$ROOT" run "$EX_B" --input scope=alpha,beta 2>&1)"; rc=$?
RIDB="$(run_id_of "$out")"; RUNB="$ROOT/runs/$RIDB"
[ $rc -eq 3 ] && ok "在 skill 步骤暂停（退出码 3）" || { bad "期望暂停，实际 rc=$rc"; printf '%s\n' "$out" | tail -6; }
grep -q 'needs_agent' "$RUNB/pending/collect-all#0_collect.json" 2>/dev/null \
  && ok "落下 needs_agent 指令" || bad "缺少 pending 指令"
grep -q 'selectors' "$RUNB/pending/collect-all#0_collect.json" 2>/dev/null \
  && ok "指令里带了能力卡 selectors" || bad "指令缺 selectors"

python3 "$EX_B/tools/fake_agent_fulfill.py" --run-dir "$RUNB" \
  --step 'collect-all#0/collect' --item alpha --gap >/dev/null 2>&1
[ -f "$RUNB/pending/collect-all#0_collect.result.json" ] && ok "写回 result.json" || bad "写回失败"

out="$($CTL --root "$ROOT" resume "$RIDB" 2>&1)"; rc=$?
[ $rc -eq 3 ] && ok "续跑后在第二个元素上再暂停" || { bad "期望再次暂停，rc=$rc"; printf '%s\n' "$out" | tail -6; }
python3 "$EX_B/tools/fake_agent_fulfill.py" --run-dir "$RUNB" \
  --step 'collect-all#1/collect' --item beta >/dev/null 2>&1
out="$($CTL --root "$ROOT" resume "$RIDB" 2>&1)"; rc=$?
[ $rc -eq 3 ] && grep -q 'signoff' "$RUNB/run.json" && ok "采集完成后停在人闸门 signoff" \
  || { bad "期望停在 signoff，rc=$rc"; printf '%s\n' "$out" | tail -6; }

out="$($CTL --root "$ROOT" resume "$RIDB" --approve --by selftest 2>&1)"; rc=$?
[ $rc -eq 3 ] && ok "放行后停在对外投递闸门 deliver" || bad "期望停在 deliver，rc=$rc"
out="$($CTL --root "$ROOT" resume "$RIDB" --approve --by selftest 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "再次放行后完成" || { bad "期望完成，rc=$rc"; printf '%s\n' "$out" | tail -6; }
[ -f "$RUNB/artifacts/outbox/delivery.json" ] && ok "对外投递产物已生成" || bad "投递产物缺失"

# 缺口回写链路
out="$($CTL --root "$ROOT" gaps "$RIDB" --brief 2>&1)"
printf '%s' "$out" | grep -q 'learn-skill' && ok "生成了给 learn-skill 的补学单" || bad "补学单没生成"
[ -f "$RUNB/gaps-brief.md" ] && ok "补学单落盘 gaps-brief.md" || bad "补学单未落盘"
python3 - "$RUNB/gaps.json" <<'PY' && ok "缺口被归类为 knowledge" || bad "缺口分类不对"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["counts"]["knowledge"] >= 1, d
PY

# ---------------------------------------------------------------- 3. 负例
head_ "3. 负例"
TMP="$(mktemp -d)"
# 3.1 判据失败必须拦住（把 reference 弄坏 → keys_unique 仍应通过，故改测 subset_keys）
mkdir -p "$TMP/bad-assert/tools"
cat > "$TMP/bad-assert/job.jsonc" <<'JSON'
{
  "job": "bad-assert",
  "goal": "负例：断言必须拦住坏数据",
  "inputs": [{"name": "n", "type": "string", "required": true}],
  "steps": [
    { "id": "make", "kind": "tool",
      "run": ["python3", "tools/make.py", "--out", "${artifacts}/x.csv"],
      "out": "artifacts/x.csv" },
    { "id": "checks", "kind": "assert",
      "invariants": [
        { "name": "keys_unique", "path": "artifacts/x.csv", "key": "id" },
        { "name": "sum_equals", "path": "artifacts/x.csv", "column": "amount", "equals": 999 }
      ] }
  ]
}
JSON
cat > "$TMP/bad-assert/tools/make.py" <<'PY'
import argparse, csv
from pathlib import Path
a = argparse.ArgumentParser(); a.add_argument("--out"); a = a.parse_args()
p = Path(a.out); p.parent.mkdir(parents=True, exist_ok=True)
with p.open("w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh); w.writerow(["id", "amount"])
    w.writerow(["A", 1]); w.writerow(["A", 2])       # 重复键 + 合计≠999
PY
out="$($CTL --root "$TMP/jobs" run "$TMP/bad-assert" --input n=1 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "坏数据被断言拦住（rc=1）" || bad "断言没拦住，rc=$rc"
printf '%s' "$out" | grep -q 'keys_unique' && ok "错误信息指出了是哪条判据" || bad "错误信息不明确"

# 3.2 写操作缺闸门 → validate 必须报错
mkdir -p "$TMP/bad-gate"
cat > "$TMP/bad-gate/job.jsonc" <<'JSON'
{
  "job": "bad-gate",
  "goal": "负例：写操作必须有人闸门",
  "inputs": [],
  "steps": [ { "id": "boom", "kind": "tool", "run": ["python3", "-c", "print(1)"],
               "effects": "irreversible", "gate": "auto" } ]
}
JSON
out="$($CTL --root "$TMP/jobs" validate "$TMP/bad-gate" 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "不可逆操作 + gate:auto 被拒绝" || bad "闸门检查没生效，rc=$rc"

# 3.3 能力卡缺失 → knowledge 归因 + gaps
mkdir -p "$TMP/missing-card"
cat > "$TMP/missing-card/job.jsonc" <<'JSON'
{
  "job": "missing-card",
  "goal": "负例：引用了不存在的能力卡",
  "inputs": [],
  "steps": [ { "id": "collect", "kind": "skill", "use": "no-such-skill/no-such-task",
               "effects": "read", "out": "artifacts/*.csv" } ]
}
JSON
out="$($CTL --root "$TMP/jobs" run "$TMP/missing-card" 2>&1)"; rc=$?
[ $rc -eq 3 ] && ok "能力卡缺失时暂停（而不是直接失败），留出补学机会" \
  || bad "期望暂停等补学，实际 rc=$rc"
printf '%s' "$out" | grep -q 'knowledge' && ok "缺口被归因为 knowledge" || bad "归因不对：$out"
RIDM="$(run_id_of "$out")"
[ -s "$TMP/jobs/runs/$RIDM/gaps.json" ] && ok "缺口写进了 gaps.json" || bad "gaps.json 没写"
out="$($CTL --root "$TMP/jobs" gaps "$RIDM" --brief 2>&1)"
printf '%s' "$out" | grep -q 'learn-skill 模式 B' && ok "补学单给出了模式 B 的具体步骤" \
  || bad "补学单内容不完整"

# 3.4 凭据泄漏 → validate 必须报错
mkdir -p "$TMP/leak"
cat > "$TMP/leak/job.jsonc" <<'JSON'
{
  "job": "leak",
  "goal": "负例：凭据不能写进 job",
  "inputs": [],
  "steps": [ { "id": "x", "kind": "tool", "run": ["python3", "-c", "print(1)"] } ],
  "requires": { "env": { "secrets": ["password=hunter2"] } }
}
JSON
out="$($CTL --root "$TMP/jobs" validate "$TMP/leak" 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "凭据泄漏被拒绝" || bad "凭据检查没生效，rc=$rc"

# ---------------------------------------------------------------- 4. 写闸门与证据阶梯（v2）
head_ "4. 写闸门真的生效（副作用只能发生在放行之后）"
SKROOT="$TMP/skills"
mkdir -p "$SKROOT/demo-write/tasks" "$SKROOT/demo-write/state"
cat > "$SKROOT/demo-write/SKILL.md" <<'MD'
---
name: demo-write
description: 自测用的假技能，不连任何真实系统。触发词：自测。
---
MD
echo '{"name":"demo-write","version":"0.1.0","origin":"learned-local"}' > "$SKROOT/demo-write/state/meta.json"

# 一张"合格"的写卡：证据等级 + 影响面 + 写后回读
cat > "$SKROOT/demo-write/tasks/write-thing.json" <<'JSON'
{
  "card": "v2", "skill": "demo-write", "task": "write-thing",
  "title": "写一个东西（自测）", "version": "0.1.0",
  "system": "demo", "channel": "local-a2desk", "envClass": "test",
  "effects": "write",
  "evidenceLevel": "verified-once",
  "evidenceBasis": "自测里实测成功过一次",
  "impact": { "blastRadius": "单条记录", "count": 1, "reversible": true },
  "inputs": [ { "name": "key", "type": "string", "required": true } ],
  "outputs": [ { "name": "receipt", "path": "artifacts/written.txt", "type": "txt" } ],
  "readback": { "how": "skill", "use": "demo-write/read-back",
                "out": "artifacts/readback.csv",
                "expect": [ { "name": "field_equals", "path": "artifacts/readback.csv",
                              "key": "key", "value": "${step.inputs.key}",
                              "field": "status", "equals": "已生效" },
                            { "name": "field_equals", "path": "artifacts/readback.csv",
                              "key": "key", "value": "${inputs.key}",
                              "field": "status", "equals": "已生效" } ] },
  "selectors": { "page": "demo 页面", "entries": [ { "step": 1, "by": "button", "text": "保存" } ] }
}
JSON
cat > "$SKROOT/demo-write/tasks/read-back.json" <<'JSON'
{
  "card": "v2", "skill": "demo-write", "task": "read-back",
  "title": "回读（自测）", "version": "0.1.0", "system": "demo",
  "channel": "local-a2desk", "envClass": "test", "effects": "read",
  "outputs": [ { "name": "rb", "path": "artifacts/readback.csv", "type": "csv" } ],
  "selectors": { "page": "demo 页面", "entries": [ { "step": 1, "by": "button", "text": "查询" } ] }
}
JSON
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" card demo-write/write-thing 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "合格写卡通过 jobctl card 校验" \
  || { bad "写卡校验失败，rc=$rc"; printf '%s\n' "$out" | tail -5; }

# 4.1 写 tool 步骤：放行之前不许产生副作用
mkdir -p "$TMP/gate-write"
cat > "$TMP/gate-write/job.jsonc" <<'JSON'
{
  "job": "gate-write", "goal": "写操作必须等人工放行后才执行",
  "inputs": [], "requires": { "env": { "class": "test" } },
  "steps": [ { "id": "mk", "kind": "tool", "effects": "write", "gate": "approve",
               "idempotency": "gate-write:fixed",
               "run": ["python3", "-c", "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text('done')", "${artifacts}/written.txt"],
               "out": "artifacts/written.txt" } ]
}
JSON
out="$($CTL --root "$TMP/jobs" run "$TMP/gate-write" 2>&1)"; rc=$?
RIDG="$(run_id_of "$out")"
[ $rc -eq 3 ] && ok "写操作在执行前暂停等人放行（rc=3）" || bad "期望 rc=3，实际 $rc"
[ ! -f "$TMP/jobs/runs/$RIDG/artifacts/written.txt" ] \
  && ok "★ 放行之前副作用没有发生（产物不存在）" \
  || bad "★ 未放行就产生了副作用——闸门是假的"
printf '%s' "$out" | grep -q 'phase: gate' && ok "暂停指令标明 gate 阶段与影响面" || bad "指令缺少 phase=gate"
out="$($CTL --root "$TMP/jobs" resume "$RIDG" --approve --by selftest 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "放行后才执行，run 正常完成" || { bad "放行后失败 rc=$rc"; printf '%s\n' "$out" | tail -5; }
[ -f "$TMP/jobs/runs/$RIDG/artifacts/written.txt" ] && ok "放行后副作用落地" || bad "放行后没有产物"

# 4.1b 推演不许毒化幂等键：dry-run 过的写作业，真跑时**必须**重新停闸门并真的执行
DRYDIR="$(mktemp -d)"; mkdir -p "$DRYDIR/job"
cat > "$DRYDIR/job/job.jsonc" <<'JSON'
{
  "job": "dry-then-real", "goal": "推演过的写作业真跑时不许被幂等跳过",
  "inputs": [], "requires": { "env": { "class": "test" } },
  "steps": [ { "id": "w", "kind": "tool", "effects": "write", "gate": "approve",
               "idempotency": "dry-then-real:fixed",
               "run": ["python3", "-c", "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text('done')", "${artifacts}/w.txt"],
               "out": "artifacts/w.txt" } ]
}
JSON
$CTL --root "$DRYDIR/jobs" run "$DRYDIR/job" --dry-run >/dev/null 2>&1
out="$($CTL --root "$DRYDIR/jobs" run "$DRYDIR/job" 2>&1)"; rc=$?
RIDD="$(run_id_of "$out")"
if [ $rc -eq 3 ] && [ ! -f "$DRYDIR/jobs/runs/$RIDD/artifacts/w.txt" ]; then
  ok "★ 推演过的写作业，真跑仍会停闸门（未被幂等键毒化）"
else
  bad "★ dry-run 毒化了幂等键：真跑被跳过或未拦闸门（rc=$rc）"
fi
out="$($CTL --root "$DRYDIR/jobs" resume "$RIDD" --approve --by selftest 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ -f "$DRYDIR/jobs/runs/$RIDD/artifacts/w.txt" ] \
  && ok "推演后的真跑在放行后确实执行了" || bad "真跑没执行（rc=$rc）"
grep -q '"event": "dry-run-skip"' "$DRYDIR/jobs/ledger.jsonl" \
  && ok "推演只留 dry-run-skip 痕迹，不写 side-effect" || bad "推演痕迹不对"
rm -rf "$DRYDIR"

# 4.2 写卡缺证据等级/影响面/回读 → validate 必须报错
cat > "$SKROOT/demo-write/tasks/write-noev.json" <<'JSON'
{
  "card": "v2", "skill": "demo-write", "task": "write-noev",
  "title": "没写证据等级的写卡", "version": "0.1.0", "system": "demo",
  "channel": "local-a2desk", "envClass": "test", "effects": "write",
  "outputs": [ { "name": "r", "path": "artifacts/x.txt", "type": "txt" } ]
}
JSON
mkdir -p "$TMP/no-ev"
cat > "$TMP/no-ev/job.jsonc" <<'JSON'
{
  "job": "no-ev", "goal": "负例：写路径必须声明证据等级",
  "inputs": [], "requires": { "env": { "class": "test" } },
  "steps": [ { "id": "w", "kind": "skill", "use": "demo-write/write-noev",
               "effects": "write", "gate": "approve", "idempotency": "no-ev:1",
               "out": "artifacts/x.txt" } ]
}
JSON
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" validate "$TMP/no-ev" 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "写卡缺 evidenceLevel/impact/readback 被拒绝" || bad "期望 rc=1，实际 $rc"
printf '%s' "$out" | grep -q 'evidenceLevel' && ok "错误信息点明了缺口是证据等级" || bad "错误信息不明确"
printf '%s' "$out" | grep -q 'readback' && ok "错误信息点明了缺回读判据" || bad "没提到 readback"

# 4.3 只到 observed 的写卡放进批量 → validate 必须报错
cat > "$SKROOT/demo-write/tasks/write-observed.json" <<'JSON'
{
  "card": "v2", "skill": "demo-write", "task": "write-observed",
  "title": "只在录屏里看到过的写卡", "version": "0.1.0", "system": "demo",
  "channel": "local-a2desk", "envClass": "test", "effects": "write",
  "evidenceLevel": "observed", "evidenceBasis": "录屏 f018，未实操",
  "impact": { "blastRadius": "单条记录", "count": 1, "reversible": true },
  "selectors": { "page": "demo 页面", "entries": [ { "step": 1, "by": "button", "text": "保存" } ] },
  "outputs": [ { "name": "r", "path": "artifacts/o/${item}.txt", "type": "txt" } ],
  "readback": { "how": "tool", "run": ["python3", "-c", "print('rb')"],
                "out": "artifacts/o/${item}.txt",
                "expect": [ { "name": "not_empty", "path": "artifacts/o/${item}.txt" } ] }
}
JSON
mkdir -p "$TMP/batch-observed"
cat > "$TMP/batch-observed/job.jsonc" <<'JSON'
{
  "job": "batch-observed", "goal": "负例：证据不足的写路径禁止批量",
  "inputs": [], "requires": { "env": { "class": "test" } },
  "steps": [ { "id": "many", "kind": "map", "for_each": ["a", "b"],
               "steps": [ { "id": "w", "kind": "skill", "use": "demo-write/write-observed",
                            "effects": "write", "gate": "approve",
                            "idempotency": "obs:${item}",
                            "out": "artifacts/o/${item}.txt" } ] } ]
}
JSON
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" validate "$TMP/batch-observed" 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "★ 只到 observed 的写路径被禁止批量执行" || bad "期望 rc=1，实际 $rc"
printf '%s' "$out" | grep -q '批量' && ok "错误信息说明了批量会放大错误" || bad "没说清原因"

# 4.4 低报副作用（卡片是 write，步骤写 read）→ validate 必须报错
mkdir -p "$TMP/under-report"
cat > "$TMP/under-report/job.jsonc" <<'JSON'
{
  "job": "under-report", "goal": "负例：不许低报副作用绕过闸门",
  "inputs": [], "requires": { "env": { "class": "test" } },
  "steps": [ { "id": "w", "kind": "skill", "use": "demo-write/write-thing",
               "effects": "read", "gate": "auto", "inputs": { "key": "K" },
               "out": "artifacts/written.txt" } ]
}
JSON
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" validate "$TMP/under-report" 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "低报副作用（write 卡写成 read 步骤）被拒绝" || bad "期望 rc=1，实际 $rc"
printf '%s' "$out" | grep -q '低报' && ok "错误信息点明了低报" || bad "错误信息不明确"

# ---------------------------------------------------------------- 5. 写后回读（两阶段）
head_ "5. 写后强制回读（没有 API 的系统里唯一的数据层判据）"
mkdir -p "$TMP/readback"
cat > "$TMP/readback/job.jsonc" <<'JSON'
{
  "job": "readback-flow", "goal": "写完之后必须回读核对，不许只看提示条",
  "inputs": [ { "name": "key", "type": "string", "required": true } ],
  "requires": { "env": { "class": "test" }, "skills": [ { "name": "demo-write" } ] },
  "steps": [ { "id": "w", "kind": "skill", "use": "demo-write/write-thing",
               "effects": "write", "gate": "approve",
               "idempotency": "rb:${inputs.key}",
               "inputs": { "key": "${inputs.key}" },
               "out": "artifacts/written.txt" } ]
}
JSON
RIDR=""
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" run "$TMP/readback" --input key=K1 2>&1)"; rc=$?
RIDR="$(run_id_of "$out")"
[ $rc -eq 3 ] && ok "写步骤先停在人闸门（rc=3）" || bad "期望 rc=3，实际 $rc"
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" resume "$RIDR" --approve --by selftest 2>&1)"; rc=$?
[ $rc -eq 3 ] && ok "放行后停在执行阶段（等 agent 干活）" || bad "期望 rc=3，实际 $rc"
RUND="$TMP/jobs/runs/$RIDR"
printf '{"ok":true,"out":["artifacts/written.txt"],"observations":"界面显示保存成功"}' > "$RUND/pending/w.result.json"
mkdir -p "$RUND/artifacts"; printf 'done' > "$RUND/artifacts/written.txt"
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" resume "$RIDR" 2>&1)"; rc=$?
[ $rc -eq 3 ] && ok "★ 写完不停：自动进入写后回读阶段" || bad "期望进入回读阶段 rc=3，实际 $rc"
[ -f "$RUND/pending/w.readback.json" ] && ok "落下了 readback 指令（含 expect 判据）" || bad "没有 readback 指令"
printf '%s' "$out" | grep -q 'phase: readback' && ok "指令标明 phase=readback" || bad "指令阶段不对"
# 回读说"没生效" → 必须判失败
printf '{"ok":true,"out":["artifacts/readback.csv"],"observations":"status=未生效"}' > "$RUND/pending/w.readback.result.json"
printf 'key,status\nK1,未生效\n' > "$RUND/artifacts/readback.csv"
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" resume "$RIDR" 2>&1)"; rc=$?
[ $rc -eq 1 ] && ok "★ 回读不一致被判失败（rc=1，不是『看到成功提示』就算过）" || bad "期望 rc=1，实际 $rc"
printf '%s' "$out" | grep -q '回读' && ok "失败归因指向回读" || bad "归因不明确"
# 换一个 run：回读一致 → 完成
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" run "$TMP/readback" --input key=K2 2>&1)"
RIDR2="$(run_id_of "$out")"
LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" resume "$RIDR2" --approve >/dev/null 2>&1
RUND2="$TMP/jobs/runs/$RIDR2"
LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" resume "$RIDR2" >/dev/null 2>&1
printf '{"ok":true,"out":["artifacts/written.txt"],"observations":"保存成功"}' > "$RUND2/pending/w.result.json"
printf 'done' > "$RUND2/artifacts/written.txt"
LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" resume "$RIDR2" >/dev/null 2>&1
printf '{"ok":true,"out":["artifacts/readback.csv"],"observations":"status=已生效"}' > "$RUND2/pending/w.readback.result.json"
printf 'key,status\nK2,已生效\n' > "$RUND2/artifacts/readback.csv"
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" resume "$RIDR2" 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "回读一致 → run 完成" || { bad "期望 rc=0，实际 $rc"; printf '%s\n' "$out" | tail -4; }
grep -q 'field_equals' "$RUND2/report.md" 2>/dev/null && ok "回读判据进了报告（可审计）" \
  || bad "报告里没有回读判据"

# ---------------------------------------------------------------- 6. 批量闸门聚合
head_ "6. 闸门聚合（一批一次确认，而不是逼人点 N 次）"
mkdir -p "$TMP/batch"
cat > "$TMP/batch/job.jsonc" <<'JSON'
{
  "job": "batch-gate", "goal": "一次放行一批对象",
  "inputs": [], "requires": { "env": { "class": "test" } },
  "steps": [ { "id": "review", "kind": "approve", "effects": "read", "batch": true,
               "items": ["A", "B", "C"],
               "impact": { "blastRadius": "3 个对象", "reversible": false,
                           "note": "回退要人工改回来" },
               "message": "即将对 A/B/C 执行变更，确认？" } ]
}
JSON
out="$($CTL --root "$TMP/jobs" run "$TMP/batch" 2>&1)"; rc=$?
RIDB2="$(run_id_of "$out")"
[ $rc -eq 3 ] && ok "批量闸门暂停等人（rc=3）" || bad "期望 rc=3，实际 $rc"
printf '%s' "$out" | grep -q 'itemCount' && ok "放行卡里带了 itemCount（人知道自己在批几件）" \
  || bad "放行卡缺影响面"
printf '%s' "$out" | grep -q 'only' && ok "给了部分放行的命令提示" || bad "没有部分放行的用法提示"
out="$($CTL --root "$TMP/jobs" resume "$RIDB2" --approve --only A,C --by selftest 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "部分放行后续跑完成" || { bad "rc=$rc"; printf '%s\n' "$out" | tail -4; }
python3 - "$TMP/jobs/runs/$RIDB2/run.json" <<'PY' && ok "放行/驳回清单落进 run.json 供下游消费" \
  || bad "部分放行的清单没落进 run.json"
import json, sys
st = json.load(open(sys.argv[1], encoding="utf-8"))
ex = st["steps"]["review"].get("extra") or {}
assert ex.get("approvedItems") == ["A", "C"], ex
assert ex.get("rejectedItems") == ["B"], ex
PY

# ---------------------------------------------------------------- 7. 知识体检
head_ "7. 知识体检（把「AI 准不准」变成可测的基线）"
out="$(LEARN_SKILLS_ROOT="$SKROOT" $CTL --root "$TMP/jobs" audit 2>&1)"; rc=$?
[ $rc -eq 0 ] && ok "jobctl audit 能跑" || bad "audit 失败 rc=$rc"
printf '%s' "$out" | grep -q '禁止批量\|单件' && ok "体检结论指出了证据不足的写路径" || bad "体检结论没抓到风险"
printf '%s' "$out" | grep -q '缺卡' && ok "体检能报出「缺能力卡」这种断链" || bad "体检漏报了缺卡"

rm -rf "$TMP"

# ---------------------------------------------------------------- 汇总
printf '\n────────────────────────────\n'
if [ "$FAIL" -gt 0 ]; then
  printf '结果：\033[31m不通过\033[0m —— %d 项通过，%d 项失败\n' "$PASS" "$FAIL"
  exit 1
fi
printf '结果：\033[32m通过\033[0m —— %d 项全部通过\n' "$PASS"
printf '作业区保留在：%s（可 rm -rf 清掉）\n' "$ROOT"
exit 0
