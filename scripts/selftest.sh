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
