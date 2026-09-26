#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""叶子步骤的执行器：tool / skill / assert / approve / notify。

`map` 是结构型步骤，由 runner 处理（要递归执行子步骤）。

每个 handler 返回 dict：
  {"status": "complete", "out": [...], "detail": "...", "effects": "...", "idempotency": "..."}
  {"status": "pause", "what": "agent|user", "directive": {...}}
  {"status": "fail", "error": "...", "class": "env|knowledge|data|unknown"}
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from .cards import CardError, resolve_card
from .core import (JobError, expand_globs, now_iso, resolve, write_json)
from .invariants import EnvError, check as check_invariant

RUN_LOGS = "logs"


def _ok(out=None, detail="", effects="read", idempotency=None, extra=None):
    r = {"status": "complete", "out": out or [], "detail": detail, "effects": effects}
    if idempotency:
        r["idempotency"] = idempotency
    if extra:
        r["extra"] = extra
    return r


def _fail(error, cls="unknown"):
    return {"status": "fail", "error": str(error), "class": cls}


def _pause(what, directive):
    return {"status": "pause", "what": what, "directive": directive}


def _write_log(ctx, name, text):
    d = Path(ctx["run_dir"]) / RUN_LOGS
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.log").write_text(text or "", encoding="utf-8")


# ---------------------------------------------------------------- tool

def handle_tool(step, ctx):
    refs = ctx["refs"]
    if ctx["dry_run"]:
        return _ok(detail="dry-run：未执行 " + " ".join(map(str, [step.get("run")])))
    raw = step["run"]
    argv = shlex.split(raw) if isinstance(raw, str) else [str(x) for x in raw]
    try:
        argv = [str(resolve(a, refs)) for a in argv]
    except JobError as exc:
        return _fail(exc, "data")

    cwd = Path(ctx["job_dir"])
    if step.get("cwd"):
        cwd = cwd / str(step["cwd"])
    env = os.environ.copy()
    env.update({
        "JOB_RUN_DIR": str(ctx["run_dir"]),
        "JOB_ARTIFACTS": str(ctx["artifacts_dir"]),
        "JOB_JOB_DIR": str(ctx["job_dir"]),
        "JOB_RUN_ID": ctx["run_id"],
        "JOB_STEP_ID": str(step.get("id")),
        "JOB_INPUTS": json.dumps(ctx["inputs"], ensure_ascii=False),
    })
    timeout = int(step.get("timeout", 3600))
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return _fail(f"命令不存在：{argv[0]}（{exc}）", "env")
    except subprocess.TimeoutExpired:
        return _fail(f"超时（{timeout}s）：{' '.join(argv)}", "env")

    _write_log(ctx, f"{ctx['step_path'].replace('/', '_')}",
               f"$ {' '.join(argv)}\nexit={proc.returncode}\n--- stdout ---\n{proc.stdout}\n"
               f"--- stderr ---\n{proc.stderr}")
    ctx["ledger"]({"job": ctx["job_name"], "runId": ctx["run_id"], "step": ctx["step_path"],
                   "event": "tool", "argv": argv, "rc": proc.returncode})

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        cls = step.get("errorClass") or "env"
        return _fail(f"命令退出码 {proc.returncode}（{' '.join(argv)}）\n" + "\n".join(tail), cls)

    outs = []
    if step.get("out"):
        try:
            outs = expand_globs(resolve(step["out"], refs), Path(ctx["run_dir"]))
        except JobError as exc:
            return _fail(exc, "data")
        if not outs:
            return _fail(f"声明了 out 但没找到产物：{step['out']}", "data")
    detail = (proc.stdout or "").strip().splitlines()
    return _ok(outs, detail[-1] if detail else "完成", step.get("effects", "read"),
               resolve(step.get("idempotency"), refs) if step.get("idempotency") else None)


# ---------------------------------------------------------------- skill（GUI / 人工能力）

RESULT_SCHEMA = {
    "ok": "boolean，是否成功",
    "out": ["run 目录内的产物相对路径（必须真实存在）"],
    "observations": "string，你实际看到的（界面反馈原话 / 报错原文）",
    "verify": [{"name": "能力卡里 verify 的条目", "ok": True, "detail": "证据"}],
    "effects": ["实际产生的副作用"],
    "gaps": [{"skill": "..", "task": "..", "item": "..", "reason": "..", "evidence": "..",
              "kind": "knowledge|env|data"}],
    "note": "string，给下一次执行的提示",
}


def _pending_paths(ctx):
    step_path = ctx["step_path"].replace("/", "_")
    pending = Path(ctx["run_dir"]) / "pending" / f"{step_path}.json"
    result = Path(ctx["run_dir"]) / "pending" / f"{step_path}.result.json"
    return pending, result


def _has_result(ctx) -> bool:
    return _pending_paths(ctx)[1].is_file()


def _consume_result(ctx, step, require_field=None):
    _, result_file = _pending_paths(ctx)
    if not result_file.is_file():
        return None
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fail(f"{result_file.name} 不是合法 JSON：{exc}", "data")
    if require_field and not data.get(require_field):
        return _fail(f"未获放行（{require_field} 不为真）：{data.get('note', '')}", "unknown")
    if require_field is None and not data.get("ok", True):
        return _fail(f"执行方报告失败：{data.get('observations', '')[:300]}", "unknown")

    out = list(data.get("out") or [])
    declared = resolve(step.get("out"), ctx["refs"]) if step.get("out") else []
    if declared:
        found = expand_globs(declared, Path(ctx["run_dir"]))
        out = sorted(set(out) | set(found))
        if not found and not data.get("out"):
            return _fail(
                f"声明了 out 但没有产物落地：{declared}（执行方也没在 result.json 里给 out）", "data")
    # 只保留真实存在的产物
    missing = [o for o in out if not (Path(ctx["run_dir"]) / o).exists()]
    if missing:
        return _fail(f"result.json 里声明的产物不存在：{', '.join(missing[:5])}", "data")

    gaps = data.get("gaps") or []
    for g in gaps:
        _add_gap(ctx, g)
    ctx["ledger"]({"job": ctx["job_name"], "runId": ctx["run_id"], "step": ctx["step_path"],
                   "event": "fulfilled", "what": ctx.get("pause_what"),
                   "out": out, "gaps": len(gaps)})
    return _ok(out, (data.get("observations") or "").strip()[:500],
               data.get("effects") or step.get("effects", "read"))


def _add_gap(ctx, gap):
    """去重后登记缺口（同一个缺口在多次 resume 里只会出现一次）。"""
    key = (gap.get("kind"), gap.get("skill"), gap.get("task"), gap.get("reason"))
    for g in ctx["gaps"]:
        if (g.get("kind"), g.get("skill"), g.get("task"), g.get("reason")) == key:
            return
    gap.setdefault("step", ctx["step_path"])
    gap.setdefault("at", now_iso())
    ctx["gaps"].append(gap)


def _missing_card_directive(step, ctx, exc):
    """能力卡不存在 = 这一步的知识还没学过。

    不直接失败：落一份指令、记一条 knowledge 缺口、暂停等 agent 现场补学
    （learn-skill 模式 B）。补完卡片直接 resume，引擎会重新解析。
    """
    use = str(step.get("use", ""))
    skill, _, task = use.partition("/")
    searched = Path(ctx["skills_root"]) / skill / "tasks"
    _add_gap(ctx, {
        "kind": "knowledge", "skill": skill, "task": task,
        "reason": f"能力卡不存在：{exc}",
        "evidence": f"searched: {searched}",
        "action": "用 learn-skill 模式 B 学出该任务并产出能力卡 "
                  f"{skill}/tasks/{task}.json，然后 jobctl resume 续跑",
    })
    pending, result_file = _pending_paths(ctx)
    pending.parent.mkdir(parents=True, exist_ok=True)
    ev = Path(ctx["run_dir"]) / "evidence" / ctx["step_path"].replace("/", "_")
    ev.mkdir(parents=True, exist_ok=True)
    return {
        "runId": ctx["run_id"], "job": ctx["job_name"], "step": ctx["step_path"],
        "kind": "skill", "status": "needs_agent",
        "instruction": (
            "这一步的知识还没学过：能力卡不存在。两条路——"
            "① 让用户给出该系统的录屏或现场演示，加载 learn-skill 走模式 B（执行中补学回写），"
            f"产出能力卡 `{skill}/tasks/{task}.json`；"
            "② 若这一步其实不必要，写 result.json 说明并给出替代产物。"
            "补学完成后直接 resume，引擎会重新解析卡片，不需要重跑整个 job。"),
        "missingCard": {"use": use, "skill": skill, "task": task,
                        "searchedFor": str(searched)},
        "learnSkill": {"skill": "learn-skill", "mode": "B",
                       "why": "执行中发现知识缺口 → 边操作边补学 → 回写能力卡"},
        "inputs": resolve(step.get("inputs") or step.get("in") or {}, ctx["refs"]),
        "expect": {"out": resolve(step.get("out"), ctx["refs"]) if step.get("out") else [],
                   "verify": step.get("verify") or []},
        "evidenceDir": str(ev.relative_to(Path(ctx["run_dir"]))),
        "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
        "resultSchema": RESULT_SCHEMA,
        "resumeCmd": ctx["resume_cmd"](),
    }


def handle_skill(step, ctx):
    try:
        card, card_path, skill, task = resolve_card(
            step.get("use", ""), Path(ctx["job_dir"]), ctx["skills_root"],
            inline=step.get("card"))
    except CardError as exc:
        if ctx["dry_run"]:
            return _ok(detail=f"dry-run：能力卡缺失，真跑会在此暂停等补学（{exc}）")
        if _has_result(ctx):
            return _consume_result(ctx, step)   # agent 已经给出了替代交代
        return _pause("agent", _missing_card_directive(step, ctx, exc))

    if ctx["dry_run"]:
        return _ok(detail=f"dry-run：会用能力卡 {skill}/{task}（通道 {card.get('channel')}）")

    done = _consume_result(ctx, step)
    if done is not None:
        return done

    pending, result_file = _pending_paths(ctx)
    ev = Path(ctx["run_dir"]) / "evidence" / ctx["step_path"].replace("/", "_")
    for d in (pending.parent, ev):
        d.mkdir(parents=True, exist_ok=True)
    channel = card.get("channel", "auto")
    if channel == "human":
        what = "user"
    else:
        what = "agent"
    directive = {
        "runId": ctx["run_id"],
        "job": ctx["job_name"],
        "step": ctx["step_path"],
        "kind": "skill",
        "status": "needs_" + what,
        "instruction": (
            "按能力卡执行这个任务：每步『先截图 → 操作 → 再截图』；"
            "用语义定位（菜单名/按钮文字/字段标签/selectors），不要用裸坐标；"
            "看不清就二次截图放大，不要猜。完成后把结果写进 resultFile。"),
        "capability": {
            "skill": skill, "task": task, "title": card.get("title"),
            "version": card.get("version"), "cardPath": str(card_path) if card_path else None,
            "effects": card.get("effects"), "envClass": card.get("envClass"),
            "automationBoundary": card.get("automationBoundary"),
            "steps": card.get("steps"), "card": card,
        },
        "selectors": card.get("selectors") or card.get("locators"),
        "channel": {"preferred": channel,
                    "fallback": (ctx["job"].get("channels") or {}).get("fallback")
                    or ["remote-a2desk", "local-a2desk", "playwright"]},
        "inputs": resolve(step.get("inputs") or step.get("in") or {}, ctx["refs"]),
        "item": ctx.get("item"),
        "expect": {"out": resolve(step.get("out"), ctx["refs"]) if step.get("out") else [],
                   "verify": step.get("verify") or card.get("verify") or []},
        "evidenceDir": str(ev.relative_to(Path(ctx["run_dir"]))),
        "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
        "resultSchema": RESULT_SCHEMA,
        "resumeCmd": ctx["resume_cmd"](),
        "safety": {
            "effects": step.get("effects", card.get("effects", "read")),
            "gate": step.get("gate", "auto"),
            "note": ("写/对外/不可逆操作：执行前必须已有用户明确授权；"
                     "凭据由用户现场提供，不要写进任何文件。"),
        },
    }
    write_json(pending, directive)
    return _pause(what, directive)


# ---------------------------------------------------------------- assert

def handle_assert(step, ctx):
    if ctx["dry_run"]:
        return _ok(detail="dry-run：跳过判据（没有真实产物可断言）")
    results, failed = [], []
    for inv in step.get("invariants") or []:
        passed, detail = check_invariant(inv, Path(ctx["run_dir"]), ctx["refs"])
        name = inv if isinstance(inv, str) else (inv.get("name") or inv.get("invariant"))
        results.append({"name": name, "ok": passed, "detail": detail})
        if not passed:
            failed.append(f"{name}: {detail}")
    ctx["asserts"].setdefault(ctx["step_path"], []).extend(results)
    if failed:
        return _fail("判据不通过 → " + "；".join(failed), "data")
    return _ok(detail="；".join(f"{r['name']}✓" for r in results) or "无判据")


# ---------------------------------------------------------------- approve（人闸门）

def handle_approve(step, ctx):
    done = _consume_result(ctx, step, require_field="approved")
    if done is not None:
        return done
    if ctx["dry_run"]:
        return _ok(detail="dry-run：会在此等人放行")
    pending, result_file = _pending_paths(ctx)
    pending.parent.mkdir(parents=True, exist_ok=True)
    directive = {
        "runId": ctx["run_id"], "step": ctx["step_path"], "kind": "approve",
        "status": "needs_user",
        "instruction": "把下面这件事原样说给用户，等他明确同意后才继续；不要替他决定。",
        "message": resolve(step.get("message", "请确认是否继续"), ctx["refs"]),
        "effects": step.get("effects", "read"),
        "inputs": resolve(step.get("inputs") or {}, ctx["refs"]),
        "remember": resolve(step.get("remember") or [], ctx["refs"]),
        "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
        "resultSchema": {"approved": "boolean，用户是否同意", "by": "谁批准的",
                         "note": "用户的附加要求"},
        "howToApprove": ctx["resume_cmd"](extra="--approve"),
        "howToReject": ctx["resume_cmd"](extra="--reject"),
    }
    write_json(pending, directive)
    return _pause("user", directive)


# ---------------------------------------------------------------- notify（对外投递）

def handle_notify(step, ctx):
    effects = step.get("effects", "outbound")
    gate = step.get("gate", "auto")
    if gate in ("approve", "outbound") or effects in ("outbound", "irreversible"):
        done = _consume_result(ctx, step, require_field="approved")
        if done is None and ctx["dry_run"]:
            return _ok(detail="dry-run：对外投递会先要人放行，未投递")
        if done is None:
            pending, result_file = _pending_paths(ctx)
            pending.parent.mkdir(parents=True, exist_ok=True)
            directive = {
                "runId": ctx["run_id"], "step": ctx["step_path"], "kind": "notify",
                "status": "needs_user",
                "instruction": "对外投递需要人工放行：把收件人、内容摘要、附件清单念给用户，等确认。",
                "target": resolve(step.get("target"), ctx["refs"]),
                "payload": resolve(step.get("payload") or {}, ctx["refs"]),
                "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
                "resultSchema": {"approved": "boolean", "by": "谁批准的",
                                 "note": "实际投递结果/回执"},
                "howToApprove": ctx["resume_cmd"](extra="--approve"),
                "howToReject": ctx["resume_cmd"](extra="--reject"),
            }
            write_json(pending, directive)
            return _pause("user", directive)
        if done["status"] == "fail":
            return done

    if ctx["dry_run"]:
        return _ok(detail="dry-run：未投递")
    target = resolve(step.get("target"), ctx["refs"])
    if not isinstance(target, dict) or not target.get("kind"):
        return _fail("notify.target 缺 kind（file / http / command）", "data")
    kind = target["kind"]
    payload = resolve(step.get("payload") or {}, ctx["refs"])
    if kind == "file":
        p = Path(str(target.get("path", "artifacts/outbox/payload.json")))
        if not p.is_absolute():
            p = Path(ctx["run_dir"]) / p
        write_json(p, payload if payload else {"note": step.get("message", "")})
        return _ok([str(p.relative_to(Path(ctx["run_dir"]))) if
                    str(p).startswith(str(ctx["run_dir"])) else str(p)],
                   f"已写入 {p}", "outbound")
    if kind == "http":
        import urllib.error
        import urllib.request
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(str(target["url"]), data=body, method=target.get("method", "POST"))
        req.add_header("Content-Type", "application/json; charset=utf-8")
        for k, v in (target.get("headers") or {}).items():
            req.add_header(k, str(v))
        try:
            with urllib.request.urlopen(req, timeout=int(target.get("timeout", 30))) as resp:
                txt = resp.read().decode("utf-8", "replace")[:300]
            return _ok(detail=f"HTTP {resp.status}: {txt}", effects="outbound")
        except urllib.error.URLError as exc:
            return _fail(f"投递失败：{exc}", "env")
    if kind == "command":
        sub = dict(step)
        sub["run"] = target["run"]
        return handle_tool(sub, ctx)
    return _fail(f"notify.target.kind 不认识：{kind}", "data")


HANDLERS = {
    "tool": handle_tool,
    "skill": handle_skill,
    "assert": handle_assert,
    "approve": handle_approve,
    "notify": handle_notify,
}
