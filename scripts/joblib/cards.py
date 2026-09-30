#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力卡（capability card）解析与校验。

能力卡是 learn-skill 与 job-runner 之间的**唯一接口**：
  * learn-skill 侧产出：`<skill>/tasks/<task>.json`（人读的 `tasks/<task>.md` 照旧并存）
  * job-runner 侧消费：`kind: "skill"` 的步骤用 `use: "<skill>/<task>"` 引用它

卡片格式是 job-runner 定义的契约，learn-skill 的模板只是它的写入端。
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import (CHANNELS, EVIDENCE_LEVELS, KINDS, JobError, load_any, read_json,
                   version_satisfies)
from .invariants import unknown_invariants

CARD_REQUIRED = ("card", "skill", "task", "version", "outputs")
CARD_VERSIONS = ("v1", "v2", "inline")

READBACK_HOW = ("skill", "tool")

_USE_SKILL_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$")


class CardError(JobError):
    """能力卡缺失或与实测不符（对应失效分类 knowledge）。"""


def card_path_for(skill: str, task: str, skills_root: Path):
    base = Path(skills_root) / skill
    for cand in (base / "tasks" / f"{task}.json",
                 base / "tasks" / f"{task}.jsonc",
                 base / "tasks" / f"{task}.card.json"):
        if cand.is_file():
            return cand
    return None


def resolve_card(use: str, job_dir: Path, skills_root: Path, inline: dict | None = None):
    """把 `use` 解析成 (card, card_path, skill, task)。

    use 形式：
      * `<skill>/<task>`        → 技能根里的能力卡（正常路径）
      * `local:<相对路径>` / `./x.jsonc` → job 目录内的卡片（示例/自测用）
      * 配合步骤里的 `card: {...}` 内联卡（最后兜底）
    """
    job_dir = Path(job_dir)
    if inline:
        card = dict(inline)
        card.setdefault("card", "inline")
        card.setdefault("skill", card.get("skill", "inline"))
        card.setdefault("task", card.get("task", "inline"))
        card.setdefault("version", card.get("version", "0.0.0"))
        return card, None, card["skill"], card["task"]

    use = (use or "").strip()
    if use.startswith("local:"):
        rel = use[len("local:"):]
    elif use.startswith("./") or use.startswith("../"):
        rel = use
    elif use.startswith("job:"):
        rel = use[len("job:"):]
    else:
        m = _USE_SKILL_RE.match(use)
        if not m:
            raise CardError(
                f"use 写法不对：{use!r}。应为 `<skill>/<task>`（如 erp-orders/query-order）"
                f"，或 `local:<job 内相对路径>`")
        skill, task = m.group(1), m.group(2)
        path = card_path_for(skill, task, skills_root)
        if path is None:
            raise CardError(
                f"技能 {skill} 没有任务 {task} 的能力卡（找过 "
                f"{Path(skills_root) / skill / 'tasks'}/）。"
                f"请按 learn-skill 的模式 B 补学并产卡（tasks/{task}.json）。")
        card = load_any(path)
        return _normalize(card, skill, task, path), path, skill, task

    path = (job_dir / rel).resolve()
    if not path.is_file():
        raise CardError(f"找不到本地能力卡：{path}")
    card = load_any(path)
    return _normalize(card, card.get("skill", "local"), card.get("task", path.stem), path), path, \
        card.get("skill", "local"), card.get("task", path.stem)


def _normalize(card: dict, skill: str, task: str, path) -> dict:
    if not isinstance(card, dict):
        raise CardError(f"能力卡不是对象：{path}")
    card.setdefault("card", "v1")
    card.setdefault("skill", skill)
    card.setdefault("task", task)
    card.setdefault("version", "0.0.0")
    card.setdefault("outputs", [])
    card.setdefault("inputs", [])
    card.setdefault("channel", "auto")
    card.setdefault("envClass", "unknown")
    card.setdefault("effects", "read")
    card.setdefault("secretsRef", [])
    return card


def validate_card(card: dict, path=None) -> list:
    """返回错误清单（空 = 合格）。学习侧写完卡后应当也能跑这一条自检。

    v2 起，"写路径"还必须是**可审计的**：这张卡是被看到还是被跑过（evidenceLevel）、
    出错会波及多大（impact）、写完之后拿什么回读核对（readback）。
    这三样缺了，编排侧就只能靠猜——那正是"AI 可能不准"的来源。
    """
    errs = []
    where = f"（{path}）" if path else ""
    for k in CARD_REQUIRED:
        if k not in card:
            errs.append(f"能力卡缺字段 `{k}`{where}")
    if card.get("card") not in CARD_VERSIONS:
        errs.append(f"能力卡 card 版本不认识：{card.get('card')!r}（当前认 {'/'.join(CARD_VERSIONS)}）")
    effects = card.get("effects")
    if effects not in ("read", "write", "outbound", "irreversible"):
        errs.append(f"能力卡 effects 非法：{effects!r}")
    outs = card.get("outputs") or []
    if not isinstance(outs, list):
        errs.append("能力卡 outputs 必须是数组")
    else:
        for i, o in enumerate(outs):
            if not isinstance(o, dict) or not o.get("path"):
                errs.append(f"能力卡 outputs[{i}] 缺 path")
                continue
            p = str(o["path"])
            if p.startswith("/") or ".." in Path(p).parts:
                errs.append(f"能力卡 outputs[{i}].path 必须是 run 目录内的相对路径：{p}")
    for s in card.get("secretsRef") or []:
        if not str(s).startswith("ref:"):
            errs.append(f"能力卡 secretsRef 只能写引用（ref:xxx），不能写凭据值：{s!r}")

    # ---- 通道（能走接口就别点界面）----
    channel = card.get("channel", "auto")
    if channel not in CHANNELS:
        errs.append(f"能力卡 channel 不认识：{channel!r}（可用：{', '.join(CHANNELS)}）")
    if isinstance(channel, str) and channel.startswith("mcp"):
        mcp = card.get("mcp")
        tools = []
        if isinstance(mcp, dict):
            tools = [mcp.get("tool")] if mcp.get("tool") else []
            extra = mcp.get("tools")
            if isinstance(extra, list):
                tools += [t for t in extra if t]
        if not isinstance(mcp, dict) or not mcp.get("server") or not tools:
            errs.append("能力卡 channel=mcp 时必须写 `mcp: {\"server\": ..., \"tool\": ...}`"
                        "（一个任务要用多个工具时用 `tools: [...]`，`tool` 写主工具）"
                        "——否则 agent 不知道调哪个工具，只能退回点界面")
    # GUI 通道才需要 selectors；接口通道由 mcp/api 契约代替（见 job-format.md §5）
    if channel in ("remote-a2desk", "local-a2desk", "playwright", "human") \
            and not card.get("selectors") and not card.get("locators"):
        errs.append(f"能力卡是界面通道（{channel}）却没有 `selectors`"
                    f"——agent 只能靠猜坐标，正是 learn-skill 红线禁止的事")

    # ---- 写路径三件套：证据等级 / 影响面 / 回读 ----
    if effects in ("write", "outbound", "irreversible"):
        ev = card.get("evidenceLevel")
        if ev is None:
            errs.append(
                f"能力卡是 {effects} 却没写 `evidenceLevel`——编排侧无法区分"
                f"「录屏里看到过」和「实测跑通过」。取值：{', '.join(EVIDENCE_LEVELS)}")
        elif ev not in EVIDENCE_LEVELS:
            errs.append(f"能力卡 evidenceLevel 非法：{ev!r}（可用：{', '.join(EVIDENCE_LEVELS)}）")
        impact = card.get("impact")
        if impact is None:
            errs.append(f"能力卡是 {effects} 却没写 `impact`（出错会波及什么、能不能回退）")
        elif not isinstance(impact, dict):
            errs.append("能力卡 impact 必须是对象："
                        "{blastRadius, count, reversible, note}")
        else:
            if not impact.get("blastRadius"):
                errs.append("能力卡 impact 缺 `blastRadius`（波及范围，如「单台设备」）")
            if "count" in impact and not isinstance(impact["count"], (int, float)):
                errs.append(f"能力卡 impact.count 必须是数字：{impact['count']!r}")
            if impact.get("reversible") is False and card.get("effects") == "write":
                errs.append("能力卡 impact.reversible=false 时 effects 应写 irreversible（不要低报）")
        if not card.get("readback"):
            errs.append(
                "能力卡是写操作却没写 `readback`——无 API 的系统里「数据层判据」只能靠"
                "写后回读（重新查一次，比对字段值）。写法见 job-format.md §5.1")

    # ---- readback 结构 ----
    rb = card.get("readback")
    if rb is not None:
        if not isinstance(rb, dict):
            errs.append("能力卡 readback 必须是对象")
        else:
            how = rb.get("how")
            if how not in READBACK_HOW:
                errs.append(f"能力卡 readback.how 非法：{how!r}（可用：{', '.join(READBACK_HOW)}）")
            if how == "skill" and not rb.get("use"):
                errs.append("能力卡 readback.how=skill 时缺 `use`（用哪个只读任务回读）")
            if how == "tool" and not rb.get("run"):
                errs.append("能力卡 readback.how=tool 时缺 `run`（回读脚本）")
            if rb.get("out"):
                p = str(rb["out"])
                if p.startswith("/") or ".." in Path(p).parts:
                    errs.append(f"能力卡 readback.out 必须是 run 目录内的相对路径：{p}")
            expect = rb.get("expect") or []
            if not isinstance(expect, list):
                errs.append("能力卡 readback.expect 必须是数组（不变量清单）")
            else:
                bad = unknown_invariants(expect)
                if bad:
                    errs.append(f"能力卡 readback.expect 里有未知不变量：{', '.join(bad)}")
    return errs


def card_evidence(card: dict) -> str:
    """取卡片证据等级；没写就按最保守的 unknown 算。"""
    return str(card.get("evidenceLevel") or "unknown")


def card_impact(card: dict) -> dict:
    return card.get("impact") if isinstance(card.get("impact"), dict) else {}


def check_requires(job: dict, skills_root: Path) -> tuple:
    """检查 job 的 requires.skills 与技能实际版本是否匹配。返回 (errors, warnings, resolved)。"""
    errors, warnings, resolved = [], [], {}
    req = ((job.get("requires") or {}).get("skills")) or []
    for item in req:
        if isinstance(item, str):
            name, spec = item, "*"
        else:
            name, spec = item.get("name"), item.get("version", "*")
        if not name:
            errors.append("requires.skills 有一项缺 name")
            continue
        meta = read_json(Path(skills_root) / name / "state" / "meta.json", default=None)
        if meta is None:
            warnings.append(f"requires.skills 里的技能 {name} 在 {skills_root} 下不存在（本机未安装？）")
            resolved[name] = None
            continue
        actual = meta.get("version", "0.0.0")
        resolved[name] = actual
        if not version_satisfies(actual, spec):
            errors.append(
                f"技能 {name} 版本不满足：需要 {spec}，实际 {actual}。"
                f"请先补学/升级该技能（learn-skill 模式 B），或放宽 job 的 requires。")
    secrets = (((job.get("requires") or {}).get("env")) or {}).get("secrets") or []
    for s in secrets:
        if not str(s).startswith("ref:"):
            errors.append(f"requires.env.secrets 只能写引用（ref:xxx），不能写凭据值：{s!r}")
    return errors, warnings, resolved
