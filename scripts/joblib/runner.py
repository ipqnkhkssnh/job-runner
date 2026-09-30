#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行引擎：步骤调度 / 暂停-续跑 / 账本 / 报告。

关键机制
--------
* **进度即文件**：每完成一步就写 `run.json`，所以进程可以被杀、会话可以断，`resume` 接着跑。
* **暂停协议**：遇到要 agent 或人接手的步骤（skill / approve / gate 的 notify），
  落一份 `pending/<step>.json` 指令并退出码 3；外部执行完写 `<step>.result.json`，再 `resume`。
* **失效分类**：每个失败都带 `class`（env / knowledge / data / unknown），
  只有 knowledge 进 `gaps.json` 回写 learn-skill。
* **幂等**：`effects != read` 的步骤可声明 `idempotency`，跨 run 命中全局账本就跳过。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .cards import CardError, resolve_card
from .core import (KINDS, MIN_GATE_BY_EFFECT, _GATE_RANK, JobError, append_ledger,
                   apply_input_defaults, expand_globs, ledger_has_key, now_iso,
                   rand_suffix, read_json, resolve, sha256_file, stamp,
                   stricter_effect, update_registry, write_json)
from .invariants import check as check_invariant
from .kinds import HANDLERS, gate_approved, gate_directive


def _gate_paths(runner, runtime_key):
    """人闸门的 pending / result 文件（与执行本身的结果文件分开）。"""
    sp = str(runtime_key).replace("/", "_")
    base = runner.run_dir / "pending"
    return base / f"{sp}.gate.json", base / f"{sp}.gate.result.json"


class _Paused(Exception):
    def __init__(self, directive, what):
        super().__init__("paused")
        self.directive = directive
        self.what = what


class _Failed(Exception):
    def __init__(self, error, cls):
        super().__init__(error)
        self.error = error
        self.cls = cls


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "none", "null")
    return bool(v)


class Runner:
    def __init__(self, job, job_dir, job_file, inputs, root, skills_root,
                 run_id=None, dry_run=False, ignore_done=False, template_dir=None,
                 jobctl_path=None):
        self.job = job
        self.job_dir = Path(job_dir).expanduser().resolve()
        self.job_file = Path(job_file).expanduser().resolve()
        self.inputs = apply_input_defaults(job, inputs)
        # 必须绝对化：run 目录会被交给 tool 当工作参数，相对路径会随 cwd 漂移
        self.root = Path(root).expanduser().resolve()
        self.skills_root = Path(skills_root).expanduser().resolve()
        self.dry_run = dry_run
        self.ignore_done = ignore_done
        self.template_dir = Path(template_dir) if template_dir else None
        self.jobctl_path = jobctl_path or "jobctl.py"
        self.job_name = str(job.get("job") or self.job_dir.name)
        self.run_id = run_id or f"{self.job_name}-{stamp()}-{rand_suffix()}"
        self.run_dir = self.root / "runs" / self.run_id
        self.artifacts_dir = self.run_dir / "artifacts"
        self.state_path = self.run_dir / "run.json"
        self.state = None
        self.gaps: list = []
        self.asserts: dict = {}

    # ------------------------------------------------------------ 状态

    def _new_state(self):
        return {
            "runId": self.run_id, "job": self.job_name, "jobDir": str(self.job_dir),
            "jobFile": str(self.job_file), "jobHash": sha256_file(self.job_file),
            "jobVersion": self.job.get("version", "0.1.0"),
            "goal": self.job.get("goal", ""),
            "status": "running", "startedAt": now_iso(), "updatedAt": now_iso(),
            "dryRun": self.dry_run, "inputs": self.inputs,
            "steps": {}, "maps": {}, "pause": None, "gaps": [], "asserts": {},
            "artifacts": [], "skills": {},
        }

    def load_state(self):
        st = read_json(self.state_path, default=None)
        if st is None:
            raise JobError(f"找不到 run：{self.run_id}（{self.state_path}）")
        self.state = st
        self.gaps = st.setdefault("gaps", [])
        self.asserts = st.setdefault("asserts", {})
        return st

    def save(self):
        self.state["updatedAt"] = now_iso()
        self.state["gaps"] = self.gaps
        self.state["asserts"] = self.asserts
        self.state["artifacts"] = self._all_artifacts()
        write_json(self.state_path, self.state)

    def _all_artifacts(self) -> list:
        out: list = []
        for st in (self.state.get("steps") or {}).values():
            for o in st.get("out") or []:
                if o not in out:
                    out.append(o)
        return out

    def ledger(self, entry):
        try:
            append_ledger(self.root, entry)
        except OSError:
            pass

    # ------------------------------------------------------------ 引用上下文

    def _refs(self, step=None, item=None, index=None):
        steps = {}
        for path, st in (self.state.get("steps") or {}).items():
            entry = {"out": st.get("out") or [],
                     "detail": st.get("detail", ""),
                     "status": st.get("status")}
            # 步骤产出的额外结构化结果（如批量放行的 approvedItems）直接可引用：
            #   ${steps.<id>.approvedItems}
            for k, v in (st.get("extra") or {}).items():
                entry.setdefault(k, v)
            steps[path.replace("#", "/")] = entry
            steps[path] = entry
        ctx = {
            "inputs": self.inputs,
            "steps": steps,
            "job": {"name": self.job_name, "dir": str(self.job_dir),
                    "version": self.job.get("version", "0.1.0")},
            "run": {"id": self.run_id, "dir": str(self.run_dir),
                    "artifacts": str(self.artifacts_dir)},
            "artifacts": str(self.artifacts_dir),
            "env": dict(os_environ()),
            "secrets": {str(s).split(":", 1)[-1]: f"ref:{str(s).split(':', 1)[-1]}"
                        for s in (((self.job.get("requires") or {}).get("env") or {})
                                  .get("secrets") or [])},
            "loop": {},
        }
        if item is not None:
            ctx["item"] = item
            ctx["index"] = index
        return ctx

    # ------------------------------------------------------------ 主循环

    def execute(self, resume=False):
        if resume:
            self.load_state()
            st = self.state
            if st.get("status") == "done":
                return {"status": "done", "message": "这次 run 已经完成了，不重复执行"}
            st["status"] = "running"
            pause = st.get("pause")
            if pause is None:
                raise JobError("这次 run 没有待处理的暂停点；直接用 run --run-id 重跑即可")
        else:
            if self.run_dir.exists():
                raise JobError(f"run 目录已存在：{self.run_dir}（换 --run-id，或用 resume）")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.state = self._new_state()
            self.gaps = self.state["gaps"]
            self.asserts = self.state["asserts"]

        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "pending").mkdir(exist_ok=True)
        (self.run_dir / "evidence").mkdir(exist_ok=True)
        if not resume:
            snap = self.run_dir / "job.snapshot.jsonc"
            shutil.copyfile(self.job_file, snap)
            update_registry(self.root, self.job_name, path=str(self.job_dir),
                            version=self.state["jobVersion"],
                            lastRunId=self.run_id, lastStatus="running",
                            lastRunAt=now_iso(),
                            requiresSkills=[s.get("name") if isinstance(s, dict) else s
                                            for s in (((self.job.get("requires") or {})
                                                       .get("skills")) or [])])
            self.ledger({"job": self.job_name, "runId": self.run_id, "event": "run-start",
                         "dryRun": self.dry_run, "inputs": self.inputs})
        self.save()

        try:
            self._exec_steps(self.job["steps"], prefix="")
            self.state["status"] = "done"
            self.state["finishedAt"] = now_iso()
            self.save()
            self._write_report()
            self.write_gaps()
            update_registry(self.root, self.job_name, lastStatus="done",
                            lastFinishedAt=now_iso())
            self.ledger({"job": self.job_name, "runId": self.run_id, "event": "run-done",
                         "artifacts": len(self.state["artifacts"]), "gaps": len(self.gaps)})
            return {"status": "done", "message": "完成"}
        except _Paused as p:
            self.state["status"] = "paused"
            self.state["pause"] = p.directive
            self.save()
            self._write_report()
            self.write_gaps()
            update_registry(self.root, self.job_name, lastStatus="paused")
            self.ledger({"job": self.job_name, "runId": self.run_id, "event": "pause",
                         "step": p.directive.get("step"), "what": p.what})
            return {"status": "paused", "what": p.what, "directive": p.directive,
                    "message": f"停在步骤 {p.directive.get('step')}，等 {p.what} 接手"}
        except _Failed as f:
            self.state["status"] = "failed"
            self.state["error"] = {"message": f.error, "class": f.cls, "at": now_iso()}
            self.state["pause"] = None
            self.save()
            self._write_report()
            update_registry(self.root, self.job_name, lastStatus="failed",
                            lastErrorClass=f.cls)
            self.ledger({"job": self.job_name, "runId": self.run_id, "event": "run-failed",
                         "class": f.cls, "error": f.error[:500]})
            self.write_gaps()
            return {"status": "failed", "class": f.cls, "message": f.error}

    # ------------------------------------------------------------ 步骤调度

    def _exec_steps(self, steps, prefix="", ancestors=(), item=None, index=None):
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            sid = str(step.get("id"))
            runtime_key = f"{prefix}{sid}"
            if self.state["steps"].get(runtime_key, {}).get("status") == "done":
                continue
            if not self._when_ok(step, runtime_key):
                self.state["steps"][runtime_key] = {
                    "status": "skipped", "at": now_iso(), "detail": "when 条件不成立"}
                self.save()
                continue
            kind = step.get("kind")
            if kind == "map":
                self._handle_map(step, runtime_key, ancestors)
                continue
            if kind not in HANDLERS:
                raise _Failed(f"步骤 {runtime_key} 的 kind 不认识：{kind!r}", "data")
            self._exec_leaf(step, runtime_key, runtime_key, item=item, index=index)

    def _step_state(self, runtime_key) -> dict:
        return self.state["steps"].get(runtime_key) or {}

    def _card_hint(self, step, ctx):
        """给放行卡找卡片信息（证据等级/影响面）。拿不到就退回 None——闸门照样拦人。"""
        if step.get("kind") != "skill" or not step.get("use"):
            return None, None
        try:
            card, cpath, _s, _t = resolve_card(
                step["use"], self.job_dir, self.skills_root, inline=step.get("card"))
            return card, cpath
        except CardError:
            return None, None

    def _result_exists(self, runtime_key, result_file) -> bool:
        if not result_file:
            return False
        return (self.run_dir / result_file).is_file()

    def _ensure_gate(self, step, runtime_key, ctx, card=None, card_path=None):
        """写 / 对外 / 不可逆操作：**执行之前**必须已经有人放行。

        validate 只保证"你声明了闸门"；真正拦住它的是这里。
        放行结果落在 `pending/<step>.gate.result.json`，与执行本身的结果文件分开，
        这样"人批了"和"活干完了"是两件可分别审计的事。
        """
        effects = step.get("effects", "read")
        if card is not None:
            # 卡片说是写、步骤说是读 → 以更严的为准（不许靠低报副作用绕过闸门）
            effects = stricter_effect(effects, card.get("effects"))
        gate = step.get("gate", "auto")
        if effects == "read" and gate == "auto":
            return
        need = MIN_GATE_BY_EFFECT.get(effects, "approve")
        if _GATE_RANK.get(gate, 0) < _GATE_RANK.get(need, 2):
            # 闸门声明得不够严：validate 本该拒绝这次运行；真跑到这里说明被绕过了，直接失败
            raise _Failed(
                f"步骤 {runtime_key} 的闸门 `{gate}` 覆盖不了 {effects} 操作（至少要 {need}）",
                "data")
        if self.dry_run:
            return
        _, gate_result = _gate_paths(self, runtime_key)
        if gate_result.is_file():
            if gate_approved(ctx, step):
                ctx["gate_approved"] = True
                self.ledger({"job": self.job_name, "runId": self.run_id, "step": runtime_key,
                             "event": "gate-approved", "effects": effects})
                return
            raise _Failed(
                f"人闸门驳回：{effects} 操作未获放行"
                f"（{gate_result.read_text(encoding='utf-8', errors='ignore')[:200]}）", "unknown")

        directive = gate_directive(step, ctx, card=card, card_path=card_path)
        gate_pending, _ = _gate_paths(self, runtime_key)
        gate_pending.parent.mkdir(parents=True, exist_ok=True)
        write_json(gate_pending, directive)
        self.state["steps"][runtime_key] = {"status": "paused", "at": now_iso()}
        self.state["pause"] = directive
        self.save()
        self.ledger({"job": self.job_name, "runId": self.run_id, "step": runtime_key,
                     "event": "gate-pause", "effects": effects})
        raise _Paused(directive, "user")

    def _exec_leaf(self, step, runtime_key, ref_key, item=None, index=None):
        # 幂等：跨 run 已经做过的写操作直接跳过。
        # ⚠️ 但 **dry-run 绝不许写账本、也绝不许在这里被判为"做过"**：
        #    否则一次推演就会毒化幂等键，之后真跑时写操作被静默跳过——
        #    连带人闸门也不会触发（本 bug 就是真跑时被下游缺产物才暴露的）。
        effects = step.get("effects", "read")
        idem = resolve(step.get("idempotency"), self._refs(step, item, index)) \
            if step.get("idempotency") else None
        if idem and effects != "read" and not self.ignore_done and not self.dry_run:
            hit = ledger_has_key(self.root, str(idem))
            if hit:
                self.state["steps"][runtime_key] = {
                    "status": "done", "at": now_iso(), "out": [],
                    "detail": f"幂等跳过：key={idem} 已于 {hit.get('at')} 完成"
                              f"（run {hit.get('runId')}）", "idempotentSkip": True}
                self.save()
                self.ledger({"job": self.job_name, "runId": self.run_id, "step": runtime_key,
                             "event": "idempotent-skip", "key": idem})
                return

        # 结果文件还没出现（暂停后回来）→ 用**存下来的那份指令**重新暂停。
        # 注意用 pause 自己记的 resultFile：同一步有 exec / gate / readback 三种阶段，
        # 各自有独立的结果文件，不能拿死名字去猜。
        pause = self.state.get("pause") or {}
        if pause and pause.get("step") == runtime_key \
                and not self._result_exists(runtime_key, pause.get("resultFile")):
            raise _Paused(pause, self.what_of(pause))

        ctx = self._ctx(step, runtime_key, ref_key, item, index)

        # 写闸门：人工放行在**副作用发生之前**
        if step.get("kind") != "approve":
            self._ensure_gate(step, runtime_key, ctx, *self._card_hint(step, ctx))

        handler = HANDLERS[step["kind"]]
        try:
            result = handler(step, ctx)
        except JobError as exc:
            result = {"status": "fail", "error": str(exc), "class": "data"}

        if result["status"] == "complete":
            verrs = self._post_verify(step, runtime_key, ctx, item, index)
            if verrs:
                result = {"status": "fail", "error": verrs, "class": "data"}
        if result["status"] == "complete":
            self.state["steps"][runtime_key] = {
                "status": "done", "at": now_iso(), "out": result.get("out") or [],
                "detail": result.get("detail", ""), "effects": effects,
                "idempotency": idem, "refKey": ref_key,
                "extra": result.get("extra") or {}}
            self.state["pause"] = None
            self.save()
            # 只有**真跑了**才记副作用与幂等键：dry-run 不许留下任何"我做过"的痕迹
            if effects != "read" and not self.dry_run:
                self.ledger({"job": self.job_name, "runId": self.run_id, "step": runtime_key,
                             "event": "side-effect", "effects": effects, "key": idem,
                             "out": result.get("out") or []})
            elif effects != "read":
                self.ledger({"job": self.job_name, "runId": self.run_id, "step": runtime_key,
                             "event": "dry-run-skip", "effects": effects,
                             "note": "推演：未执行、未记幂等键"})
            return

        if result["status"] == "pause":
            self.state["steps"][runtime_key] = {"status": "paused", "at": now_iso()}
            self.state["pause"] = result["directive"]
            self.save()
            raise _Paused(result["directive"], result["what"])

        # fail
        cls = result.get("class", "unknown")
        msg = result.get("error", "未知失败")
        self.state["steps"][runtime_key] = {
            "status": "failed", "at": now_iso(), "error": msg, "class": cls}
        self.state["pause"] = None
        self.save()
        self.ledger({"job": self.job_name, "runId": self.run_id, "step": runtime_key,
                     "event": "step-failed", "class": cls, "error": str(msg)[:500]})
        if cls == "knowledge":
            self.gaps.append({
                "kind": "knowledge", "step": runtime_key,
                "reason": str(msg)[:500], "at": now_iso(),
                "action": "用 learn-skill 模式 B 补学后回写能力卡/页面知识",
            })
        raise _Failed(str(msg), cls)

    def _handle_map(self, step, runtime_key, ancestors):
        ctx = self._ctx(step, runtime_key, runtime_key, None, None)
        try:
            items = resolve(step.get("for_each"), ctx["refs"])
        except JobError as exc:
            raise _Failed(f"map {runtime_key} 的 for_each 解析失败：{exc}", "data")
        if not isinstance(items, list):
            items = [items] if items is not None else []
        mstate = self.state["maps"].setdefault(runtime_key, {"done": [], "sub": {}})
        for i, item in enumerate(items):
            if i in mstate["done"]:
                continue
            sub_done = mstate["sub"].setdefault(str(i), [])
            for sub in step.get("steps") or []:
                sub_id = str(sub.get("id"))
                if sub_id in sub_done:
                    continue
                sub_runtime = f"{runtime_key}#{i}/{sub_id}"
                if sub.get("kind") == "map":
                    raise _Failed(f"map 暂不支持嵌套 map（{sub_runtime}）", "data")
                self._exec_leaf(sub, sub_runtime, f"{runtime_key}/{sub_id}", item=item, index=i)
                sub_done.append(sub_id)
                self.save()
            mstate["done"].append(i)
            self.save()

        # 汇总产物
        outs: list = []
        for key, st in self.state["steps"].items():
            if key.startswith(f"{runtime_key}#") and st.get("status") == "done":
                outs.extend(st.get("out") or [])
        if step.get("out"):
            outs.extend(expand_globs(resolve(step["out"], self._refs()), self.run_dir))
        outs = list(dict.fromkeys(outs))
        self.state["steps"][runtime_key] = {
            "status": "done", "at": now_iso(), "out": outs,
            "detail": f"{len(mstate['done'])}/{len(items)} 个元素完成",
            "effects": step.get("effects", "read"), "refKey": runtime_key}
        self.save()

    # ------------------------------------------------------------ 辅助

    def _ctx(self, step, runtime_key, ref_key, item, index):
        refs = self._refs(step, item, index)
        if item is not None:
            refs["item"] = item
            refs["index"] = index
        # 这一步**自己的**入参也挂进 refs：`${step.inputs.x}`
        # 为什么必须有（真跑才发现）：能力卡的 readback.expect 要断言的"期望值"往往就是
        # 这一步自己的入参（如 expect_material），而 `${inputs.x}` 指的是 **job 级**入参——
        # 两者混用会让**回读阶段**解析失败，而那时副作用已经发生了，重试代价很高。
        # 注意要在 refs 建好之后再挂：步骤入参自己也可能引用 ${inputs.*}。
        try:
            step_inputs = resolve(step.get("inputs") or step.get("in") or {}, refs)
        except JobError:
            step_inputs = {}
        refs["step"] = {"id": runtime_key, "inputs": step_inputs}
        pause = self.state.get("pause") or {}
        if pause.get("step") != runtime_key:
            pause = None
        return {
            "run_id": self.run_id, "run_dir": self.run_dir,
            "artifacts_dir": self.artifacts_dir, "job_dir": self.job_dir,
            "job": self.job, "job_name": self.job_name, "inputs": self.inputs,
            "step_path": runtime_key, "ref_key": ref_key, "refs": refs,
            "item": item, "index": index, "dry_run": self.dry_run,
            "skills_root": self.skills_root, "gaps": self.gaps, "asserts": self.asserts,
            "ledger": self.ledger, "pause_what": None,
            "pause": pause, "step_state": self._step_state(runtime_key),
            "gate_approved": False,
            "resume_cmd": lambda extra="": self._resume_cmd(extra),
        }

    def _resume_cmd(self, extra=""):
        parts = ["python3", str(self.jobctl_path), "resume", self.run_id,
                 "--root", str(self.root)]
        if extra:
            parts.append(extra)
        return " ".join(parts)

    @staticmethod
    def what_of(directive) -> str:
        return "user" if directive.get("status") == "needs_user" else "agent"

    def _when_ok(self, step, runtime_key) -> bool:
        when = step.get("when")
        if when is None:
            return True
        if isinstance(when, str):
            if "${" in when:
                return _truthy(resolve(when, self._refs(step)))
            m = re.match(r"^(effects|env)\s*==\s*(\S+)$", when.strip())
            if m:
                left, right = m.group(1), m.group(2)
                if left == "effects":
                    return step.get("effects", "read") == right
                return ((self.job.get("requires") or {}).get("env") or {}).get("class") == right
            return _truthy(when)
        return _truthy(when)

    def _post_verify(self, step, runtime_key, ctx, item, index):
        problems = []
        if self.dry_run:
            return ""
        for inv in step.get("verify") or []:
            passed, detail = check_invariant(inv, self.run_dir, ctx["refs"])
            name = inv if isinstance(inv, str) else (inv.get("name") or inv.get("invariant"))
            self.asserts.setdefault(runtime_key, []).append(
                {"name": name, "ok": passed, "detail": detail, "phase": "verify"})
            if not passed:
                problems.append(f"{name}: {detail}")
        return "；".join(problems)

    # ------------------------------------------------------------ 报告 / 缺口

    def _write_report(self, path=None):
        st = self.state
        rows = []
        for key, s in st.get("steps", {}).items():
            flag = {"done": "✅", "failed": "❌", "paused": "⏸", "skipped": "⏭"}.get(
                s.get("status"), "?")
            rows.append(f"| {key} | {s.get('status')} {flag} | {s.get('effects', '')} | "
                        f"{(s.get('detail') or s.get('error') or '')[:160]} | "
                        f"{', '.join(s.get('out') or [])[:160]} |")
        asserts = []
        for key, items in (st.get("asserts") or {}).items():
            for a in items:
                asserts.append(f"| {key} | {a.get('name')} | "
                               f"{'✅' if a.get('ok') else '❌'} | {a.get('detail', '')[:160]} |")
        gaps = [f"| {g.get('kind', '')} | {g.get('skill', '')}/{g.get('task', '')} | "
                f"{g.get('reason', '')[:160]} | {g.get('action', '')} |" for g in self.gaps]
        tpl = None
        if self.template_dir and (self.template_dir / "report.template.md").is_file():
            tpl = (self.template_dir / "report.template.md").read_text(encoding="utf-8")
        body = {
            "{{GOAL}}": str(self.job.get("goal", "")),
            "{{JOB}}": self.job_name,
            "{{RUN_ID}}": self.run_id,
            "{{STATUS}}": str(st.get("status")),
            "{{STARTED}}": str(st.get("startedAt", "")),
            "{{UPDATED}}": str(st.get("updatedAt", "")),
            "{{DRY_RUN}}": "是" if self.dry_run else "否",
            "{{INPUTS}}": json.dumps(self.inputs, ensure_ascii=False, indent=2),
            "{{STEPS_TABLE}}": "\n".join(rows) or "| — | — | — | — | — |",
            "{{ASSERTS_TABLE}}": "\n".join(asserts) or "| — | — | — | — |",
            "{{ARTIFACTS}}": "\n".join(f"- `{a}`" for a in self._all_artifacts()) or "- （无）",
            "{{GAPS_TABLE}}": "\n".join(gaps) or "| — | — | — | — |",
            "{{ERROR}}": json.dumps(st.get("error"), ensure_ascii=False) if st.get("error") else "无",
        }
        if tpl is None:
            tpl = ("# Run {{RUN_ID}}\n\n状态：{{STATUS}}\n\n{{STEPS_TABLE}}\n\n{{ARTIFACTS}}\n")
        text = tpl
        for k, v in body.items():
            text = text.replace(k, v)
        Path(path or (self.run_dir / "report.md")).write_text(text, encoding="utf-8")

    def write_gaps(self, path=None):
        data = {
            "runId": self.run_id, "job": self.job_name, "generatedAt": now_iso(),
            "status": self.state.get("status"),
            "class": (self.state.get("error") or {}).get("class"),
            "gaps": self.gaps,
            "counts": {"knowledge": sum(1 for g in self.gaps if g.get("kind") == "knowledge"),
                       "env": sum(1 for g in self.gaps if g.get("kind") == "env"),
                       "data": sum(1 for g in self.gaps if g.get("kind") == "data")},
            "handoff": "learn-skill",
            "handoffNote": "按 learn-skill 模式 B：读现状 → 确认安全边界 → 边操作边补学 → 回写能力卡/页面知识 → 升版本",
        }
        p = Path(path or (self.run_dir / "gaps.json"))
        write_json(p, data)
        return p

    def gaps_brief(self) -> str:
        job_name = self.state.get("job") or getattr(self, "job_name", "?")
        lines = [f"# 给 learn-skill 的补学清单（run {self.run_id}）", "",
                 f"- job：`{job_name}`状态：`{self.state.get('status')}`",
                 f"- 生成时间：{now_iso()}", ""]
        if not self.gaps:
            lines.append("没有缺口（本次执行没有产生 knowledge 类缺口）。")
            return "\n".join(lines) + "\n"
        lines += ["| 类型 | 技能/任务 | 缺什么 | 证据 | 建议动作 |", "|---|---|---|---|---|"]
        for g in self.gaps:
            lines.append("| {} | {}/{} | {} | {} | {} |".format(
                g.get("kind", ""), g.get("skill", ""), g.get("task", ""),
                str(g.get("reason", ""))[:200], g.get("evidence", ""), g.get("action", "")))
        lines += ["", "## 建议的 learn-skill 模式 B 步骤", "",
                  "1. 读对应技能包的 `SKILL.md` / `pages/` / `tasks/`，把上面的缺口逐条对照；",
                  "2. 涉及写操作先向用户确认环境与授权；",
                  "3. 按 learn-skill §3 通道优先级实测，每步截图留证；",
                  "4. 回写：`tasks/<task>.md` + `tasks/<task>.json`（能力卡）、`pages/`、"
                  "`state/meta.json`（version +0.1、unknowns 移除已验证项）、`CHANGELOG.md`；",
                  "5. 跑 `validate_skill.sh` 与本技能的 `jobctl validate` 复核。"]
        return "\n".join(lines) + "\n"


def os_environ():
    import os
    return dict(os.environ)


def load_inputs(pairs, inputs_file=None) -> dict:
    data: dict = {}
    if inputs_file:
        f = Path(inputs_file)
        if not f.is_file():
            raise JobError(f"--inputs-file 不存在：{f}")
        data.update(json.loads(f.read_text(encoding="utf-8")))
    for p in pairs or []:
        if "=" not in p:
            raise JobError(f"--input 需要 k=v 形式：{p!r}")
        k, v = p.split("=", 1)
        k = k.strip()
        if v.startswith("[") or v.startswith("{"):
            try:
                data[k] = json.loads(v)
                continue
            except json.JSONDecodeError:
                pass
        if "," in v and not v.strip().startswith('"'):
            data[k] = [x.strip() for x in v.split(",") if x.strip()]
        else:
            data[k] = v
    return data
