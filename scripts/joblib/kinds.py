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

from .cards import CardError, card_evidence, card_impact, resolve_card
from .core import (CHANNEL_PRIORITY, EVIDENCE_MEANING, JobError, evidence_at_least,
                   expand_globs, now_iso, resolve, write_json)
from .invariants import EnvError, check as check_invariant

RUN_LOGS = "logs"

#: 同一步的三种"阶段"，各自有独立的 pending/result 文件（互不覆盖）：
#:   exec     正常执行（agent 干活）
#:   gate     人闸门（写/对外/不可逆操作执行**之前**的放行）
#:   readback 写后回读（重新读一次，用数据层判据证明写对了）
PHASE_SUFFIX = {"exec": "", "gate": ".gate", "readback": ".readback"}


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
        # 整串引用解析成 None（可选入参没传）时给空串：命令行里不该出现 "None"
        argv = ["" if (v := resolve(a, refs)) is None else str(v) for a in argv]
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

#: 写后回读阶段要 agent 交回来的东西
READBACK_SCHEMA = {
    "ok": "boolean，回读这一步是否成功",
    "out": ["回读产物的相对路径（重新查出来的那份数据，必须真实存在）"],
    "observations": "string，回读时看到的原文（关键字段的实际值）",
    "matches": [{"field": "字段名", "expected": "期望值", "actual": "实际值",
                 "ok": True}],
    "note": "不一致时说明为什么（没生效 / 写错对象 / 被人改回去）",
}


def _paths(ctx, phase="exec"):
    sp = ctx["step_path"].replace("/", "_")
    base = Path(ctx["run_dir"]) / "pending"
    sfx = PHASE_SUFFIX.get(phase, "")
    return base / f"{sp}{sfx}.json", base / f"{sp}{sfx}.result.json"


def current_phase(ctx) -> str:
    """这一步现在处在哪个阶段（由存下来的暂停指令决定）。"""
    p = ctx.get("pause") or {}
    if p.get("step") == ctx["step_path"]:
        return str(p.get("phase") or "exec")
    return "exec"


def _pending_paths(ctx, phase="exec"):
    return _paths(ctx, phase)


def _has_result(ctx, phase="exec") -> bool:
    return _paths(ctx, phase)[1].is_file()


def _consume_result(ctx, step, require_field=None, phase="exec"):
    _, result_file = _paths(ctx, phase)
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
                   "phase": phase, "out": out, "gaps": len(gaps)})
    extra = {}
    for k in ("approvedItems", "rejectedItems", "approvedBy", "matches"):
        if data.get(k) is not None:
            extra[k] = data[k]
    return _ok(out, (data.get("observations") or "").strip()[:500],
               data.get("effects") or step.get("effects", "read"), extra=extra or None)


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


def _evidence_block(card, path=None):
    """把「这张卡有多可信、出错会怎样」摆到执行方和人面前。"""
    lvl = card_evidence(card) if card else "unknown"
    return {
        "level": lvl,
        "meaning": EVIDENCE_MEANING.get(lvl, ""),
        "basis": (card or {}).get("evidenceBasis"),
        "impact": card_impact(card) if card else {},
        "cardPath": str(path) if path else None,
        "needsAttention": not evidence_at_least(lvl, "verified-once"),
    }


def _mcp_tools(card) -> list:
    """卡片声明的 MCP 工具，解析成 `mcp__<server>__<tool>` 全名。"""
    mcp = (card or {}).get("mcp") or {}
    server = mcp.get("server")
    names = ([mcp.get("tool")] if mcp.get("tool") else []) + list(mcp.get("tools") or [])
    out = []
    for n in names:
        if not n:
            continue
        full = f"mcp__{server}__{n}" if server else str(n)
        if full not in out:
            out.append(full)
    return out


def _channel_block(step, card, job):
    """通道优先级：接口 > 远程桌面 > 本机桌面 > 无头浏览器 > 人。"""
    preferred = str(step.get("channel") or (card or {}).get("channel") or "auto")
    return {
        "preferred": preferred,
        "priority": list(CHANNEL_PRIORITY),
        "mcp": (card or {}).get("mcp"),
        "resolvedTools": _mcp_tools(card) if preferred.startswith("mcp") else [],
        "fallback": (job.get("channels") or {}).get("fallback")
        or ["remote-a2desk", "local-a2desk", "playwright"],
        "rule": "按 priority 从左往右取第一个可用通道；**能调 mcp/api 就不要去点界面**"
                "（界面操作是全链路里精度最低的一环）。只有在没有确定性接口时才降级到 GUI。",
    }


def _exec_instruction(step, card, channel) -> str:
    """按通道给执行方下指令——别让走接口的步骤收到"先截图、别用裸坐标"这种 GUI 指令。"""
    if str(channel).startswith("mcp") or channel == "api":
        tools = _mcp_tools(card)
        head = "这一步走**接口通道**：不要去点界面、也不用截图。"
        if tools:
            head += "直接调用 " + "、".join(f"`{t}`" for t in tools) + "。"
        return (head +
                "严格用接口的返回构造产物（字段名以接口返回为准，不要从别处猜）；"
                "接口返回与能力卡不符（字段改名、少了一段数据、报错）→ 照实写进 result.json 的 "
                "observations 与 gaps，**不要为了让流程走下去而编数据**。"
                "完成后把结果写进 resultFile。")
    if channel == "human":
        return ("这一步需要人来做：把 capability.steps 与 expect 念给用户，"
                "等他做完/给出结果后，把实际结果写进 resultFile。")
    return ("按能力卡执行这个任务：每步『先截图 → 操作 → 再截图』；"
            "用语义定位（菜单名/按钮文字/字段标签/selectors），不要用裸坐标；"
            "看不清就二次截图放大，不要猜。完成后把结果写进 resultFile。")


def _check_expects(expects, ctx, base=None):
    """跑一组不变量，返回失败说明清单（空 = 全过）。顺便记进 asserts 供报告展示。"""
    base = Path(base or ctx["run_dir"])
    bad = []
    for inv in resolve(expects, ctx["refs"]) or []:
        passed, detail = check_invariant(inv, base, ctx["refs"])
        name = inv if isinstance(inv, str) else (inv.get("name") or inv.get("invariant"))
        ctx["asserts"].setdefault(ctx["step_path"], []).append(
            {"name": name, "ok": passed, "detail": detail, "phase": "readback"})
        if not passed:
            bad.append(f"{name}: {detail}")
    return bad


def _readback_spec(card, rb):
    return {
        "how": rb.get("how"),
        "use": rb.get("use"),
        "out": rb.get("out"),
        "expect": rb.get("expect") or [],
        "note": rb.get("note") or card.get("effectsDetail", {}).get("idempotent"),
    }


def _start_readback(step, ctx, card, rb, done):
    """写操作已执行 → 强制走一遍写后回读（没有 API 的系统里，这是唯一的数据层判据）。"""
    effects = step.get("effects", card.get("effects", "read"))
    expects = rb.get("expect") or []
    out_spec = rb.get("out") or step.get("out")

    if rb.get("how") == "tool":
        if ctx["dry_run"]:
            return done
        sub = {"id": step.get("id"), "kind": "tool", "run": rb.get("run"),
               "out": out_spec, "effects": "read", "timeout": rb.get("timeout", 600)}
        res = handle_tool(sub, ctx)
        if res.get("status") != "complete":
            return _fail(f"写后回读脚本失败：{res.get('error')}", res.get("class", "env"))
        bad = _check_expects(expects, ctx)
        if bad:
            return _fail("写后回读不一致 → " + "；".join(bad), "data")
        return _ok(list(done.get("out") or []) + list(res.get("out") or []),
                   "写成功，回读一致：" + str(res.get("detail", ""))[:200],
                   effects, extra={"readback": "verified"})

    # how == skill：再暂停一次，让执行方重新读一遍
    pending, result_file = _paths(ctx, "readback")
    pending.parent.mkdir(parents=True, exist_ok=True)
    ev = Path(ctx["run_dir"]) / "evidence" / (ctx["step_path"].replace("/", "_") + ".readback")
    ev.mkdir(parents=True, exist_ok=True)
    rcard, rpath, rskill, rtask = None, None, "", ""
    use = str(rb.get("use") or "")
    try:
        rcard, rpath, rskill, rtask = resolve_card(use, Path(ctx["job_dir"]), ctx["skills_root"])
    except CardError:
        pass
    if not rskill:
        rskill, _, rtask = use.partition("/")
    directive = {
        "runId": ctx["run_id"], "job": ctx["job_name"], "step": ctx["step_path"],
        "kind": "skill", "phase": "readback", "status": "needs_agent",
        "instruction": (
            "写操作已经执行。现在做**写后回读**：用只读方式把刚改的对象重新查一次，"
            "把真实值落成产物，再逐项比对期望值。"
            "回读不一致就照实报告——不要为了让流程走下去而说成功。"),
        "readback": {
            "how": "skill", "use": use,
            "expect": resolve(expects, ctx["refs"]),
            "out": resolve(out_spec, ctx["refs"]) if out_spec else [],
            "why": "无 API 的系统拿不到数据层，只能用「重新读一次 + 比对字段值」当判据",
        },
        "capability": {
            "skill": rskill, "task": rtask, "cardPath": str(rpath) if rpath else None,
            "title": (rcard or {}).get("title"), "card": rcard,
            "steps": (rcard or {}).get("steps"),
        },
        "selectors": (rcard or {}).get("selectors") or (rcard or {}).get("locators"),
        "channel": _channel_block(step, rcard or card, ctx["job"]),
        "evidence": _evidence_block(card, ctx.get("card_path")),
        "inputs": resolve(step.get("inputs") or step.get("in") or {}, ctx["refs"]),
        "item": ctx.get("item"),
        "expect": {"out": resolve(out_spec, ctx["refs"]) if out_spec else [],
                   "verify": resolve(expects, ctx["refs"])},
        "evidenceDir": str(ev.relative_to(Path(ctx["run_dir"]))),
        "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
        "resultSchema": READBACK_SCHEMA,
        "resumeCmd": ctx["resume_cmd"](),
    }
    write_json(pending, directive)
    return _pause("agent", directive)


def _finish_readback(step, ctx, card, rb):
    done = _consume_result(ctx, step, phase="readback")
    if done is None:
        return _fail("回读阶段没有找到 result 文件（instruction 见 pending/*.readback.json）", "data")
    if done.get("status") != "complete":
        return done
    pending_expect = ((ctx.get("pause") or {}).get("readback") or {}).get("expect")
    expects = pending_expect if pending_expect is not None else (rb or {}).get("expect") or []
    bad = _check_expects(expects, ctx)
    if bad:
        return _fail("写后回读不一致 → " + "；".join(bad), "data")
    return _ok(done.get("out") or [],
               "写后回读一致：" + str(done.get("detail", ""))[:200],
               step.get("effects", card.get("effects", "read")),
               extra={"readback": "verified"})


def handle_skill(step, ctx):
    phase = current_phase(ctx)
    try:
        card, card_path, skill, task = resolve_card(
            step.get("use", ""), Path(ctx["job_dir"]), ctx["skills_root"],
            inline=step.get("card"))
    except CardError as exc:
        if ctx["dry_run"]:
            return _ok(detail=f"dry-run：能力卡缺失，真跑会在此暂停等补学（{exc}）")
        if _has_result(ctx, phase):
            return _consume_result(ctx, step, phase=phase)   # agent 已经给出了替代交代
        return _pause("agent", _missing_card_directive(step, ctx, exc))

    effects = step.get("effects", card.get("effects", "read"))
    rb = card.get("readback") if effects != "read" else None
    ctx["card_path"] = card_path

    # ---- 回读阶段：拿回读结果 + 核对 ----
    if phase == "readback":
        return _finish_readback(step, ctx, card, rb)

    if ctx["dry_run"]:
        extra = "，之后还会做一次写后回读" if rb else ""
        return _ok(detail=f"dry-run：会用能力卡 {skill}/{task}"
                          f"（通道 {step.get('channel') or card.get('channel')}，"
                          f"证据 {card_evidence(card)}）{extra}")

    done = _consume_result(ctx, step, phase="exec")
    if done is not None:
        if done.get("status") != "complete":
            return done
        if rb:
            return _start_readback(step, ctx, card, rb, done)
        return done

    pending, result_file = _pending_paths(ctx, "exec")
    ev = Path(ctx["run_dir"]) / "evidence" / ctx["step_path"].replace("/", "_")
    for d in (pending.parent, ev):
        d.mkdir(parents=True, exist_ok=True)
    channel = str(step.get("channel") or card.get("channel", "auto"))
    what = "user" if channel == "human" else "agent"
    directive = {
        "runId": ctx["run_id"],
        "job": ctx["job_name"],
        "step": ctx["step_path"],
        "kind": "skill",
        "phase": "exec",
        "status": "needs_" + what,
        "instruction": _exec_instruction(step, card, channel),
        "capability": {
            "skill": skill, "task": task, "title": card.get("title"),
            "version": card.get("version"), "cardPath": str(card_path) if card_path else None,
            "effects": card.get("effects"), "envClass": card.get("envClass"),
            "automationBoundary": card.get("automationBoundary"),
            "steps": card.get("steps"), "card": card,
        },
        "selectors": card.get("selectors") or card.get("locators"),
        "channel": _channel_block(step, card, ctx["job"]),
        "evidence": _evidence_block(card, card_path),
        "gateApproved": bool(ctx.get("gate_approved")),
        "inputs": resolve(step.get("inputs") or step.get("in") or {}, ctx["refs"]),
        "item": ctx.get("item"),
        "expect": {"out": resolve(step.get("out"), ctx["refs"]) if step.get("out") else [],
                   "verify": step.get("verify") or card.get("verify") or [],
                   "readback": (_readback_spec(card, rb) if rb else None)},
        "evidenceDir": str(ev.relative_to(Path(ctx["run_dir"]))),
        "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
        "resultSchema": RESULT_SCHEMA,
        "resumeCmd": ctx["resume_cmd"](),
        "safety": {
            "effects": effects,
            "gate": step.get("gate", "auto"),
            "gateApproved": bool(ctx.get("gate_approved")),
            "evidenceLevel": card_evidence(card),
            "impact": card_impact(card),
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

def _impact_summary(step, ctx, items):
    """把「这次要批的东西会影响什么」算出来，供人一眼判断。"""
    detail = resolve(step.get("impact"), ctx["refs"]) if step.get("impact") else None
    if isinstance(detail, str):
        detail = {"note": detail}
    detail = dict(detail or {})
    if items is not None:
        detail.setdefault("itemCount", len(items))
        detail.setdefault("items", items[:50])
        if len(items) > 50:
            detail["itemsTruncated"] = len(items) - 50
    effects = step.get("effects", "read")
    if effects in ("write", "outbound", "irreversible"):
        detail.setdefault("effects", effects)
        detail.setdefault(
            "reversible", False if effects == "irreversible" else None)
    return detail


def handle_approve(step, ctx):
    items = None
    if step.get("items"):
        raw = resolve(step.get("items"), ctx["refs"])
        items = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
    done = _consume_result(ctx, step, require_field="approved")
    if done is not None:
        # 部分放行：把批准/驳回的清单交给下游步骤消费（${steps.<id>.approvedItems}）
        if items is not None:
            approved = done.get("extra", {}).get("approvedItems")
            rejected = done.get("extra", {}).get("rejectedItems")
            done.setdefault("extra", {})
            done["extra"].setdefault("items", items)
            if approved is None:
                done["extra"]["approvedItems"] = items if done.get("status") == "complete" else []
            if rejected is None:
                done["extra"]["rejectedItems"] = []
        return done
    if ctx["dry_run"]:
        return _ok(detail="dry-run：会在此等人放行"
                          + (f"（{len(items)} 项一批）" if items is not None else ""))
    pending, result_file = _pending_paths(ctx)
    pending.parent.mkdir(parents=True, exist_ok=True)
    batch = bool(step.get("batch")) or (items is not None and len(items) > 1)
    directive = {
        "runId": ctx["run_id"], "step": ctx["step_path"], "kind": "approve",
        "status": "needs_user",
        "instruction": "把下面这件事原样说给用户，等他明确同意后才继续；不要替他决定。"
                       + ("这是**批量放行**：一次决定覆盖 items 里的全部条目，"
                          "请把 itemCount 与影响面一起念出来。"
                          if batch else ""),
        "message": resolve(step.get("message", "请确认是否继续"), ctx["refs"]),
        "effects": step.get("effects", "read"),
        "batch": batch,
        "items": items,
        "itemCount": (len(items) if items is not None else None),
        "impact": _impact_summary(step, ctx, items),
        "inputs": resolve(step.get("inputs") or {}, ctx["refs"]),
        "remember": resolve(step.get("remember") or [], ctx["refs"]),
        "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
        "resultSchema": {"approved": "boolean，用户是否同意",
                         "approvedItems": "放行哪些条目（部分放行时写）",
                         "rejectedItems": "驳回哪些条目",
                         "by": "谁批准的", "note": "用户的附加要求"},
        "howToApprove": ctx["resume_cmd"](extra="--approve"),
        "howToReject": ctx["resume_cmd"](extra="--reject"),
    }
    if items is not None:
        directive["howToApprovePartially"] = ctx["resume_cmd"](
            extra="--approve --only <条目1,条目2>")
        directive["howToRejectSome"] = ctx["resume_cmd"](
            extra="--reject-items <条目1,条目2>")
    write_json(pending, directive)
    return _pause("user", directive)


# ---------------------------------------------------------------- gate（引擎级写闸门）

def gate_directive(step, ctx, card=None, card_path=None):
    """写/对外/不可逆操作**执行之前**的放行卡。

    这是 safety.md 「写以上必须人工放行」的执行端：validate 只保证你声明了闸门，
    真正拦住它的是这里。放行卡必须把「证据等级 + 影响面 + 要写什么」一起摆出来——
    否则人不清楚自己在批什么，闸门就退化成点确认键。
    """
    effects = step.get("effects", "read")
    impact = {}
    if isinstance(step.get("impact"), dict):
        impact.update(step["impact"])
    if card is not None:
        for k, v in (card_impact(card) or {}).items():
            impact.setdefault(k, v)
    pending, result_file = _paths(ctx, "gate")
    pending.parent.mkdir(parents=True, exist_ok=True)
    return {
        "runId": ctx["run_id"], "job": ctx["job_name"], "step": ctx["step_path"],
        "kind": "gate", "phase": "gate", "status": "needs_user", "effects": effects,
        "instruction": (
            f"这一步的副作用是 **{effects}**，执行前必须由人放行。"
            "把 message、影响面（impact）、证据等级（evidence）和将要写入的内容原样念给用户，"
            "等他明确同意；不要替他决定，也不要用『应该没问题』代替他的答复。"),
        "message": resolve(
            step.get("message")
            or (f"即将执行 {effects} 操作：{card.get('title') or step.get('id')}"
                if card is not None else f"即将执行 {effects} 操作：{step.get('id')}"),
            ctx["refs"]),
        "impact": impact,
        "evidence": _evidence_block(card, card_path) if card is not None else {
            "level": "unknown", "meaning": EVIDENCE_MEANING["unknown"],
            "needsAttention": True},
        "willDo": {"use": step.get("use"), "run": step.get("run"),
                   "target": step.get("target")},
        "inputs": resolve(step.get("inputs") or step.get("in") or {}, ctx["refs"]),
        "idempotency": resolve(step.get("idempotency"), ctx["refs"])
        if step.get("idempotency") else None,
        "resultFile": str(result_file.relative_to(Path(ctx["run_dir"]))),
        "resultSchema": {"approved": "boolean，用户是否同意",
                         "by": "谁批准的", "note": "用户附加要求"},
        "howToApprove": ctx["resume_cmd"](extra="--approve"),
        "howToReject": ctx["resume_cmd"](extra="--reject"),
    }


def gate_approved(ctx, step) -> bool:
    """检查这一步的闸门放行结果。返回 True=已放行，False=还没放行或被驳回。"""
    _, result_file = _paths(ctx, "gate")
    if not result_file.is_file():
        return False
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return bool(data.get("approved"))


# ---------------------------------------------------------------- notify（对外投递）

def handle_notify(step, ctx):
    effects = step.get("effects", "outbound")
    gate = step.get("gate", "auto")
    # 引擎级闸门（gate_directive）已经在派发前放行过一次时，这里不要二次拦人
    if (gate in ("approve", "outbound") or effects in ("outbound", "irreversible")) \
            and not ctx.get("gate_approved"):
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
