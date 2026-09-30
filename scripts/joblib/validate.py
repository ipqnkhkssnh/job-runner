#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""job 定义校验：结构 / 引用 / 副作用闸门 / 凭据泄漏 / 能力卡可用性。

validate 必须在 run 之前跑；run 自己也会先跑一遍（fail-fast）。
"""
from __future__ import annotations

import re
from pathlib import Path

from .cards import (CardError, card_evidence, card_impact, check_requires, resolve_card,
                    validate_card)
from .core import (CHANNELS, EFFECTS, GATES, KINDS, MIN_GATE_BY_EFFECT, _GATE_RANK,
                   JobError, evidence_at_least, head_of, refs_in, secret_hits)
from .invariants import unknown_invariants

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: 副作用严密度序数（低报副作用等于绕过闸门，所以要能比大小）
_EFFECT_RANK = {lv: i for i, lv in enumerate(EFFECTS)}
KNOWN_HEADS = {"inputs", "steps", "item", "index", "run", "job", "env", "secrets", "loop",
               "artifacts"}   # artifacts = ${run.artifacts} 的简写，作者天天要用


def _rel_safe(p: str) -> bool:
    p = str(p)
    if p.startswith("/") or p.startswith("~"):
        return False
    return ".." not in Path(p).parts


def _step_ids(steps) -> list:
    return [s.get("id") for s in steps or [] if isinstance(s, dict) and s.get("id")]


def walk_steps(steps, path_prefix="") -> list:
    """把（可能嵌套的）步骤树摊平成 [(path, step, ancestors)]，便于统一校验。"""
    out = []

    def rec(items, prefix, ancestors):
        for s in items or []:
            if not isinstance(s, dict):
                continue
            sid = s.get("id", "?")
            p = f"{prefix}{sid}"
            out.append((p, s, ancestors))
            if s.get("kind") == "map":
                rec(s.get("steps"), f"{p}/", ancestors + [p])

    rec(steps, path_prefix, [])
    return out


def validate_job(job: dict, job_dir: Path, skills_root: Path) -> tuple:
    """返回 (errors, warnings, info)。"""
    errors, warnings, info = [], [], {}
    job_dir = Path(job_dir)

    # ---------- 顶层 ----------
    if not isinstance(job, dict):
        return ["job 定义不是对象"], [], info
    if not job.get("job"):
        errors.append("缺顶层 `job`（作业名，kebab-case）")
    elif not ID_RE.match(str(job["job"])):
        errors.append(f"job 名必须是小写 kebab-case：{job['job']!r}")
    if not isinstance(job.get("steps"), list) or not job["steps"]:
        errors.append("缺顶层 `steps`（至少一个步骤）")
        return errors, warnings, info

    inputs = job.get("inputs") or []
    if not isinstance(inputs, list):
        errors.append("inputs 必须是数组")
        inputs = []
    input_names = set()
    for i, it in enumerate(inputs):
        if not isinstance(it, dict) or not it.get("name"):
            errors.append(f"inputs[{i}] 缺 name")
            continue
        input_names.add(str(it["name"]))
        if it.get("type") not in (None, "string", "list", "file", "path", "json", "number", "bool"):
            warnings.append(f"inputs[{i}].type 不常见：{it.get('type')!r}")

    flat = walk_steps(job["steps"])
    ids = [p for p, _, _ in flat]
    seen = set()
    for p in ids:
        if p in seen:
            errors.append(f"步骤 id 重复：{p}")
        seen.add(p)
        leaf = p.rsplit("/", 1)[-1]
        if not ID_RE.match(leaf):
            errors.append(f"步骤 id 必须是小写 kebab-case：{leaf!r}（在 {p}）")

    # ---------- 逐步骤 ----------
    for path, step, ancestors in flat:
        kind = step.get("kind")
        tag = f"[{path}]"
        if kind not in KINDS:
            errors.append(f"{tag} kind 非法或缺失：{kind!r}（可用：{', '.join(KINDS)}）")
            continue
        if not step.get("id"):
            errors.append(f"{tag} 缺 id")

        # effects / gate
        effects = step.get("effects", "read")
        gate = step.get("gate", "auto")
        if effects not in EFFECTS:
            errors.append(f"{tag} effects 非法：{effects!r}（可用：{', '.join(EFFECTS)}）")
        if gate not in GATES:
            errors.append(f"{tag} gate 非法：{gate!r}（可用：{', '.join(GATES)}）")
        if effects in EFFECTS and gate in GATES:
            need = MIN_GATE_BY_EFFECT[effects]
            if _GATE_RANK[gate] < _GATE_RANK[need]:
                errors.append(
                    f"{tag} 副作用是 {effects}，闸门却只有 {gate}；至少要 {need}"
                    f"（写/对外/不可逆操作必须人工放行）")
            elif effects == "write" and gate == "approve" and not step.get("idempotency"):
                warnings.append(f"{tag} 是写操作但没有声明 idempotency（重复执行可能产生两条数据）")
            if effects == "irreversible" and not step.get("idempotency"):
                errors.append(
                    f"{tag} 是不可逆操作（{effects}）却没声明 idempotency——"
                    f"按 safety.md 红线，不可逆动作不做幂等保护不允许重跑")
        if step.get("channel") and step["channel"] not in CHANNELS:
            errors.append(f"{tag} channel 不认识：{step['channel']!r}"
                          f"（可用：{', '.join(CHANNELS)}）")

        # 批量执行：只被"看到"过的写路径不许批量跑
        in_batch = bool(ancestors) or bool(step.get("for_each"))
        if in_batch and effects in ("write", "outbound", "irreversible"):
            warnings.append(
                f"{tag} 在批量（map/for_each）里执行 {effects} 操作："
                f"如果它的能力卡证据等级不到 verified-repeat，validate 会拒绝（批量=放大错误）")

        # out 路径安全
        for o in (step.get("out") or []) if isinstance(step.get("out"), list) else ([step["out"]] if step.get("out") else []):
            if not _rel_safe(o):
                errors.append(f"{tag} out 必须是 run 目录内的相对路径：{o}")

        # 引用表达式
        for e in refs_in(step):
            h = head_of(e)
            if h not in KNOWN_HEADS:
                errors.append(f"{tag} 引用 ${{{e}}} 的头 `{h}` 不认识（可用：{', '.join(sorted(KNOWN_HEADS))}）")
                continue
            if h == "steps":
                target = e.split(".", 1)[1].split(".", 1)[0] if "." in e else ""
                target = target.split("[", 1)[0]
                if target and target not in ids:
                    errors.append(f"{tag} 引用了不存在的步骤：${{{e}}}")
            if h == "inputs":
                nm = e.split(".", 1)[1].split(".", 1)[0] if "." in e else ""
                nm = nm.split("[", 1)[0]
                if nm and nm not in input_names:
                    errors.append(f"{tag} 引用了未声明的入参：${{{e}}}")
            if h in ("item", "index") and not any(a.endswith("map") or True for a in ancestors) and not step.get("for_each"):
                warnings.append(f"{tag} 用了 ${{{e}}}，但它不在 map / for_each 里（可能取不到值）")

        # 分 kind 必填
        if kind == "tool":
            if not step.get("run"):
                errors.append(f"{tag} tool 步骤缺 `run`")
            else:
                argv = step["run"] if isinstance(step["run"], list) else [step["run"]]
                for a in argv:
                    a = str(a)
                    if a.startswith("-") or "${" in a:
                        continue
                    cand = job_dir / a
                    if a.endswith((".py", ".sh", ".js")) and not cand.is_file() and not Path(a).is_absolute():
                        errors.append(f"{tag} tool 脚本不存在：{cand}")
        elif kind == "skill":
            if not step.get("use") and not step.get("card"):
                errors.append(f"{tag} skill 步骤缺 `use`（`<skill>/<task>`）或内联 `card`")
            if step.get("use"):
                try:
                    card, cpath, skill, task = resolve_card(
                        step["use"], job_dir, skills_root, inline=step.get("card"))
                    cerrs = validate_card(card, cpath)
                    errors.extend(f"{tag} {e}" for e in cerrs)
                    ev = card_evidence(card)
                    ceffects = step.get("effects", card.get("effects", "read"))
                    info.setdefault("cards", []).append(
                        {"step": path, "skill": skill, "task": task,
                         "version": card.get("version"), "effects": ceffects,
                         "channel": step.get("channel") or card.get("channel"),
                         "evidenceLevel": ev,
                         "impact": card_impact(card),
                         "readback": bool(card.get("readback")),
                         "path": str(cpath) if cpath else "inline"})

                    # ---- 证据阶梯：这张卡是被"看到"的，还是被"跑过"的 ----
                    if ceffects in ("write", "outbound", "irreversible"):
                        if ev == "unknown":
                            errors.append(
                                f"{tag} 能力卡 {skill}/{task} 的证据等级是 unknown（没学过/没实测）"
                                f"，却要做 {ceffects} 操作——先用 learn-skill 模式 B 实测一遍，"
                                f"把 evidenceLevel 升到 observed 以上")
                        elif ev == "observed":
                            if in_batch:
                                errors.append(
                                    f"{tag} 能力卡 {skill}/{task} 只到 observed（录屏里看到，没实操）"
                                    f"，却被放在批量（map/for_each）里执行 {ceffects}——"
                                    f"批量会放大错误：先实测到 verified-repeat，或拆成单件逐步放行")
                            else:
                                warnings.append(
                                    f"{tag} 能力卡 {skill}/{task} 的写路径只到 observed"
                                    f"（未实测）——运行时会强制人闸门，放行卡会标注该等级，"
                                    f"请如实告知用户")
                        elif ev == "verified-once" and in_batch:
                            warnings.append(
                                f"{tag} 能力卡 {skill}/{task} 是 verified-once 却在批量里："
                                f"只允许逐批放行（每批都要人确认），不能一次放行全部")
                    if ceffects in ("write", "outbound", "irreversible") \
                            and not card.get("readback"):
                        errors.append(
                            f"{tag} 能力卡 {skill}/{task} 是写操作但没有 `readback`——"
                            f"没有回读就只剩『界面上看到成功提示』这一种判据，"
                            f"那正是假成功的来源")
                    card_eff = card.get("effects", "read")
                    if card_eff in EFFECTS and step.get("effects", "read") in EFFECTS \
                            and _EFFECT_RANK.get(step.get("effects", "read"), 0) \
                            < _EFFECT_RANK.get(card_eff, 0):
                        errors.append(
                            f"{tag} 步骤把副作用低报成 {step.get('effects')}，"
                            f"而能力卡 {skill}/{task} 是 {card_eff}——"
                            f"以更严的为准（低报副作用 = 绕过闸门）")
                except CardError as exc:
                    # 卡片缺失 ≠ 配置错误，而是**知识还没学过**：
                    # validate 只警告；run 跑到这里会暂停并记一条 knowledge 缺口，
                    # 让 agent 现场用 learn-skill 模式 B 补学（补完直接 resume 即可）。
                    warnings.append(f"{tag} {exc} → 运行时会在此暂停并记为 knowledge 缺口")
            if not step.get("out") and not step.get("expect"):
                warnings.append(f"{tag} skill 步骤没声明 `out`（拿不回证据/产物，后续步骤无法消费）")
        elif kind == "map":
            if not step.get("for_each"):
                errors.append(f"{tag} map 步骤缺 `for_each`")
            if not step.get("steps"):
                errors.append(f"{tag} map 步骤缺 `steps`（要迭代的子步骤）")
        elif kind == "assert":
            inv = step.get("invariants")
            if not inv:
                errors.append(f"{tag} assert 步骤缺 `invariants`")
            else:
                bad = unknown_invariants(inv)
                if bad:
                    errors.append(f"{tag} 未知不变量：{', '.join(bad)}")
        elif kind == "approve":
            if not step.get("message"):
                warnings.append(f"{tag} approve 步骤建议写 `message`（好让放行的人知道在批什么）")
            batch = bool(step.get("batch")) or bool(step.get("items"))
            if batch:
                if not step.get("items"):
                    warnings.append(
                        f"{tag} 声明了 batch 却没写 `items`（放行卡里看不出这一批覆盖哪些对象）")
                if not step.get("impact"):
                    warnings.append(
                        f"{tag} 批量放行建议写 `impact`（影响面：波及多少对象、能不能回退）——"
                        f"一次点确认覆盖 N 个对象的闸门，不写影响面就等于让人盲批")
                if step.get("effects", "read") == "read":
                    warnings.append(
                        f"{tag} 批量放行的 effects 是 read：下游写步骤要各自带够闸门，"
                        f"这个 approve 只是「批量意愿」而不是「批量授权」")
        elif kind == "notify":
            if not step.get("target"):
                errors.append(f"{tag} notify 步骤缺 `target`（{kind: file|http|command}）")
            if step.get("effects", "outbound") == "outbound" and step.get("gate") == "auto":
                errors.append(f"{tag} notify 对外投递不允许 gate: auto（必须有人放行）")

    # ---------- requires ----------
    rerrs, rwarns, resolved = check_requires(job, skills_root)
    errors.extend(rerrs)
    warnings.extend(rwarns)
    info["skills"] = resolved

    env = ((job.get("requires") or {}).get("env")) or {}
    if not env.get("class"):
        warnings.append("requires.env.class 没写（test/prod/any）——写操作前无法判断环境")
    if env.get("class") == "prod":
        warnings.append("requires.env.class = prod：这是生产环境，执行前请确认用户已授权")

    # ---------- 凭据泄漏 ----------
    for f in sorted(job_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".json", ".jsonc", ".yaml", ".yml", ".md", ".sh", ".py", ".txt"):
            continue
        if any(part.startswith("runs") for part in f.parts):
            continue
        hits = secret_hits(f.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            errors.extend(f"疑似凭据泄漏 {f.relative_to(job_dir)}: {h}" for h in hits[:3])

    info["steps"] = [{"path": p, "kind": s.get("kind"), "effects": s.get("effects", "read"),
                      "gate": s.get("gate", "auto")} for p, s, _ in flat]
    if not job.get("onFailure"):
        warnings.append("没写 onFailure.classify（建议保留默认 env/knowledge/data 三分类）")
    return errors, warnings, info


def load_and_validate(job_dir: Path, skills_root: Path):
    from .core import job_file_of, load_any
    jf = job_file_of(Path(job_dir))
    job = load_any(jf)
    errors, warnings, info = validate_job(job, Path(job_dir), skills_root)
    return job, jf, errors, warnings, info
