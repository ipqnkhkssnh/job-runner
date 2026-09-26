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

from .core import KINDS, JobError, load_any, read_json, version_satisfies

CARD_REQUIRED = ("card", "skill", "task", "version", "outputs")

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
    """返回错误清单（空 = 合格）。学习侧写完卡后应当也能跑这一条自检。"""
    errs = []
    where = f"（{path}）" if path else ""
    for k in CARD_REQUIRED:
        if k not in card:
            errs.append(f"能力卡缺字段 `{k}`{where}")
    if card.get("card") not in ("v1", "inline"):
        errs.append(f"能力卡 card 版本不认识：{card.get('card')!r}（当前只认 v1）")
    if card.get("effects") not in ("read", "write", "outbound", "irreversible"):
        errs.append(f"能力卡 effects 非法：{card.get('effects')!r}")
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
    return errs


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
