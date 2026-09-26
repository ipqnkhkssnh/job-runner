#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jobctl — job-runner 的唯一入口（校验 / 运行 / 续跑 / 查看 / 报告 / 缺口 / 脚手架）。

用法速览（详见 ../SKILL.md）：
  jobctl.py validate <job>                  # 校验 job 定义 + 能力卡可用性 + 凭据泄漏
  jobctl.py run      <job> [--input k=v]    # 跑到暂停点或结束（--dry-run 只推演）
  jobctl.py resume   <run-id> [--approve]   # 续跑；--approve/--reject 用于人闸门
  jobctl.py status   <run-id>               # 看这次 run 到哪了
  jobctl.py log      <run-id> [-n 30]       # 看审计账本
  jobctl.py report   <run-id>               # 打印报告
  jobctl.py gaps     <run-id> [--brief]     # 缺口清单（--brief 给 learn-skill 的补学单）
  jobctl.py list                            # 作业登记表
  jobctl.py new      <name>                 # 从模板建一个 job 骨架
  jobctl.py card     <skill>/<task>         # 解析并校验一张能力卡
  jobctl.py schema                          # 打印规范文档路径与 kind 列表

退出码：0 成功 / 1 失败或校验不通过 / 2 用法错误 / 3 暂停（等人或 agent 接手）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from joblib import cards as cards_mod            # noqa: E402
from joblib import core                          # noqa: E402
from joblib import validate as validate_mod      # noqa: E402
from joblib.core import (FAIL_EXIT, KINDS, OK_EXIT, PAUSE_EXIT, USAGE_EXIT,  # noqa: E402
                         JobError, jobs_root, now_iso, read_json, skills_root,
                         write_json)
from joblib.runner import Runner, load_inputs    # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = SKILL_DIR / "templates"
JOBCTL = Path(__file__).resolve()

C_OK, C_WARN, C_ERR, C_DIM, C_OFF = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def say(msg=""):
    print(msg)


def head(title):
    say(f"\n{title}")


def _color(s, c):
    return f"{c}{s}{C_OFF}" if sys.stdout.isatty() else s


# ---------------------------------------------------------------- validate

def cmd_validate(args):
    root = jobs_root(args.root)
    sroot = skills_root()
    jdir = core.find_job_dir(args.job, root)
    try:
        job, jf, errors, warnings, info = validate_mod.load_and_validate(jdir, sroot)
    except JobError as exc:
        say(_color(f"✗ {exc}", C_ERR))
        return FAIL_EXIT
    say(f"job: {job.get('job')}　定义: {jf.relative_to(jdir)}")
    say(f"目录: {jdir}")
    say(f"技能根: {sroot}")
    if info.get("steps"):
        head("步骤（按声明顺序）")
        for s in info["steps"]:
            say(f"  {s['path']:<28} {s['kind']:<8} effects={s['effects']:<12} gate={s['gate']}")
    if info.get("cards"):
        head("能力卡")
        for c in info["cards"]:
            say(f"  {c['step']}: {c['skill']}/{c['task']} v{c['version']}  ({c['path']})")
    if info.get("skills"):
        head("依赖技能")
        for k, v in info["skills"].items():
            say(f"  {k}: {v or '（未安装）'}")
    if warnings:
        head("警告")
        for w in warnings:
            say("  " + _color("! " + w, C_WARN))
    if errors:
        head("错误")
        for e in errors:
            say("  " + _color("✗ " + e, C_ERR))
        say(_color(f"\n不通过：{len(errors)} 个错误，{len(warnings)} 个警告", C_ERR))
        return FAIL_EXIT
    say(_color(f"\n通过：0 个错误，{len(warnings)} 个警告", C_OK))
    return OK_EXIT


# ---------------------------------------------------------------- run / resume

def cmd_run(args):
    root = jobs_root(args.root)
    sroot = skills_root()
    jdir = core.find_job_dir(args.job, root)
    job, jf, errors, warnings, info = validate_mod.load_and_validate(jdir, sroot)
    if errors:
        say(_color(f"✗ 校验不通过（{len(errors)} 个错误），先修再跑：", C_ERR))
        for e in errors:
            say("  ✗ " + e)
        return FAIL_EXIT
    for w in warnings:
        say(_color("! " + w, C_WARN))
    inputs = load_inputs(args.input, args.inputs_file)
    missing = [i["name"] for i in (job.get("inputs") or [])
               if i.get("required") and i["name"] not in inputs]
    if missing:
        say(_color(f"✗ 缺必填入参：{', '.join(missing)}（用 --input name=value 传）", C_ERR))
        return USAGE_EXIT
    r = Runner(job, jdir, jf, inputs, root, sroot, run_id=args.run_id,
               dry_run=args.dry_run, ignore_done=args.ignore_done,
               template_dir=TEMPLATE_DIR, jobctl_path=JOBCTL)
    say(f"run-id: {r.run_id}")
    say(f"run-dir: {r.run_dir}" + ("  (dry-run)" if args.dry_run else ""))
    try:
        res = r.execute()
    except JobError as exc:
        say(_color(f"✗ {exc}", C_ERR))
        return FAIL_EXIT
    return _report_result(res, r)


def cmd_resume(args):
    root = jobs_root(args.root)
    sroot = skills_root()
    # 用 run 的 run.json 还原
    state_path = root / "runs" / args.run_id / "run.json"
    if not state_path.is_file():
        say(_color(f"✗ 找不到 run：{args.run_id}（{state_path}）", C_ERR))
        return USAGE_EXIT
    st = read_json(state_path)
    jdir = Path(st["jobDir"])
    try:
        job, jf, _, _, _ = validate_mod.load_and_validate(jdir, sroot)
    except JobError as exc:
        say(_color(f"✗ 无法重新加载 job 定义：{exc}", C_ERR))
        return FAIL_EXIT
    if st.get("jobHash") and core.sha256_file(jf) != st["jobHash"]:
        say(_color("! job 定义在本次 run 之后被改过（hash 不一致）：续跑会用新定义，"
                   "若改的是结构请新开一次 run。", C_WARN))

    # 人闸门快捷放行
    pause = st.get("pause") or {}
    if (args.approve or args.reject) and pause:
        rfile = Path(st["jobDir"]) if False else (root / "runs" / args.run_id /
                                                  pause.get("resultFile", "pending/x.result.json"))
        if not pause.get("resultFile"):
            say(_color("✗ 这个暂停点没有 resultFile，无法用 --approve 放行", C_ERR))
            return USAGE_EXIT
        write_json(rfile, {"approved": bool(args.approve) and not args.reject,
                           "ok": bool(args.approve) and not args.reject,
                           "by": args.by or "user", "note": args.note or "",
                           "at": now_iso()})
        say(f"已写入放行结果：{rfile}")

    runner = Runner(job, jdir, jf, st.get("inputs") or {}, root, sroot,
                    run_id=args.run_id, dry_run=bool(st.get("dryRun")),
                    ignore_done=args.ignore_done, template_dir=TEMPLATE_DIR,
                    jobctl_path=JOBCTL)
    try:
        res = runner.execute(resume=True)
    except JobError as exc:
        say(_color(f"✗ {exc}", C_ERR))
        return FAIL_EXIT
    return _report_result(res, runner)


def _report_result(res, runner: Runner):
    status = res.get("status")
    if status == "done":
        say(_color(f"✓ 完成：{res.get('message', '')}", C_OK))
        if runner.state.get("artifacts"):
            head("产物")
            for a in runner.state["artifacts"]:
                say(f"  {a}")
        if runner.gaps:
            head("缺口（要回写 learn-skill）")
            for g in runner.gaps:
                say(f"  [{g.get('kind')}] {g.get('skill', '')}/{g.get('task', '')}：{g.get('reason', '')[:120]}")
            say(f"  补学单：python3 {JOBCTL} gaps {runner.run_id} --brief")
        say(f"\n报告：{runner.run_dir / 'report.md'}")
        return OK_EXIT
    if status == "paused":
        d = res.get("directive") or {}
        say(_color(f"⏸ 暂停（等 {res.get('what')}）：{res.get('message')}", C_WARN))
        head("待办指令")
        for k in ("instruction", "message", "capability", "expect", "evidenceDir",
                  "resultFile", "howToApprove", "howToReject", "resumeCmd"):
            if d.get(k):
                v = d[k]
                say(f"  {k}: {json.dumps(v, ensure_ascii=False)[:400] if not isinstance(v, str) else v}")
        if runner.gaps:
            head("缺口（要回写 learn-skill）")
            for g in runner.gaps:
                say(f"  [{g.get('kind')}] {g.get('skill', '')}/{g.get('task', '')}："
                    f"{str(g.get('reason', ''))[:120]}")
            say(f"  补学单：python3 {JOBCTL} gaps {runner.run_id} --brief")
        say(f"\n指令文件：{runner.run_dir / 'pending'}")
        say(f"报告：{runner.run_dir / 'report.md'}")
        return PAUSE_EXIT
    say(_color(f"✗ 失败（{res.get('class')}）：{res.get('message')}", C_ERR))
    say(f"  报告：{runner.run_dir / 'report.md'}")
    if runner.gaps:
        say(f"  缺口：python3 {JOBCTL} gaps {runner.run_id} --brief")
    say("  修好后可 `resume` 继续，或 `run --run-id <新id>` 重跑。")
    return FAIL_EXIT


# ---------------------------------------------------------------- 查看类

def _load_state(args):
    root = jobs_root(args.root)
    p = root / "runs" / args.run_id / "run.json"
    if not p.is_file():
        raise JobError(f"找不到 run：{args.run_id}（{p}）")
    return root, read_json(p)


def cmd_status(args):
    root, st = _load_state(args)
    say(f"run: {st.get('runId')}　job: {st.get('job')}　状态: {st.get('status')}")
    say(f"开始: {st.get('startedAt')}　更新: {st.get('updatedAt')}"
        + ("　(dry-run)" if st.get("dryRun") else ""))
    head("步骤")
    for k, s in (st.get("steps") or {}).items():
        flag = {"done": "✅", "failed": "❌", "paused": "⏸", "skipped": "⏭"}.get(s.get("status"), "?")
        line = f"  {flag} {k:<30} {s.get('status'):<8}"
        if s.get("out"):
            line += f" out={len(s['out'])}"
        if s.get("detail"):
            line += f"  {str(s['detail'])[:100]}"
        if s.get("error"):
            line += f"  {str(s['error'])[:160]}"
        say(line)
    if st.get("pause"):
        d = st["pause"]
        head(f"停在：{d.get('step')}（等 {d.get('status')}）")
        say(f"  {d.get('instruction') or d.get('message') or ''}")
        say(f"  结果文件：{(root / 'runs' / st['runId'] / d.get('resultFile', '')).__str__()}")
        say(f"  续跑：python3 {JOBCTL} resume {st['runId']} --root {root}")
    if st.get("artifacts"):
        head("产物")
        for a in st["artifacts"]:
            say("  " + a)
    if st.get("error"):
        head("失败")
        say(f"  [{st['error'].get('class')}] {st['error'].get('message')}")
    return OK_EXIT


def cmd_log(args):
    root, st = _load_state(args)
    f = root / "runs" / args.run_id / "run.json"
    led = root / "ledger.jsonl"
    lines = []
    if led.is_file():
        for line in led.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("runId") == args.run_id:
                lines.append(e)
    for e in lines[-args.n:]:
        ev = e.get("event", "?")
        extra = {k: v for k, v in e.items()
                 if k not in ("event", "job", "runId", "at")}
        say(f"{e.get('at', '')}  {ev:<16} {json.dumps(extra, ensure_ascii=False)[:300]}")
    if not lines:
        say("（这次 run 还没有账本记录）")
    return OK_EXIT


def cmd_report(args):
    root, st = _load_state(args)
    p = root / "runs" / args.run_id / "report.md"
    if not p.is_file():
        say(_color("✗ 还没有报告（run 还没跑过）", C_ERR))
        return FAIL_EXIT
    say(p.read_text(encoding="utf-8"))
    return OK_EXIT


def cmd_gaps(args):
    root, st = _load_state(args)
    run_dir = root / "runs" / args.run_id
    gaps = st.get("gaps") or []
    if not gaps:
        say("没有缺口。")
        return OK_EXIT
    if args.brief:
        r = Runner.__new__(Runner)
        r.state, r.gaps, r.run_id = st, gaps, args.run_id
        r.job_name = st.get("job", "?")
        text = r.gaps_brief()
        out = run_dir / "gaps-brief.md"
        out.write_text(text, encoding="utf-8")
        say(text)
        say(f"（已写入 {out}）")
        return OK_EXIT
    say(json.dumps({"runId": args.run_id, "gaps": gaps}, ensure_ascii=False, indent=2))
    return OK_EXIT


def cmd_list(args):
    root = jobs_root(args.root)
    reg = read_json(root / "registry.json", default=None)
    head(f"作业区：{root}")
    if not reg or not reg.get("jobs"):
        say("  （还没跑过任何 job。`jobctl.py run <job>` 会自动登记）")
    else:
        say(f"  {'job':<28} {'ver':<8} {'状态':<10} {'最近运行':<22} 依赖技能")
        for e in reg["jobs"]:
            say(f"  {e.get('job', ''):<28} {str(e.get('version', '')):<8} "
                f"{str(e.get('lastStatus', '')):<10} {str(e.get('lastRunAt', '')):<22} "
                f"{', '.join(e.get('requiresSkills') or [])}")
    jd = root
    found = sorted(p.name for p in jd.iterdir() if p.is_dir() and p.name != "runs") if jd.is_dir() else []
    if found:
        head("本地 job 目录")
        for n in found:
            say("  " + n)
    return OK_EXIT


# ---------------------------------------------------------------- new / card / schema

def cmd_new(args):
    root = jobs_root(args.root)
    dest = root / args.name
    if dest.exists() and not args.force:
        say(_color(f"✗ 已存在：{dest}（要覆盖加 --force）", C_ERR))
        return FAIL_EXIT
    (dest / "tools").mkdir(parents=True, exist_ok=True)
    (dest / "cards").mkdir(parents=True, exist_ok=True)
    tpl = TEMPLATE_DIR / "job.template.jsonc"
    text = tpl.read_text(encoding="utf-8").replace("{{JOB_NAME}}", args.name)
    (dest / "job.jsonc").write_text(text, encoding="utf-8")
    readme = TEMPLATE_DIR / "job-readme.template.md"
    if readme.is_file():
        (dest / "README.md").write_text(
            readme.read_text(encoding="utf-8").replace("{{JOB_NAME}}", args.name),
            encoding="utf-8")
    say(f"已创建：{dest}")
    head("下一步")
    say("  1. 改 job.jsonc：goal / inputs / steps（对照 references/job-format.md）")
    say("  2. 有 GUI 操作就写 `kind:\"skill\"` + `use:\"<skill>/<task>\"`（需要 learn-skill 产的能力卡）")
    say("  3. 有数据加工就在 tools/ 写脚本，用 `kind:\"tool\"` 调")
    say(f"  4. 校验：python3 {JOBCTL} validate {args.name} --root {root}")
    say(f"  5. 推演：python3 {JOBCTL} run {args.name} --root {root} --dry-run")
    return OK_EXIT


def cmd_card(args):
    sroot = skills_root()
    try:
        card, path, skill, task = cards_mod.resolve_card(args.use, Path.cwd(), sroot)
    except JobError as exc:
        say(_color(f"✗ {exc}", C_ERR))
        return FAIL_EXIT
    errs = cards_mod.validate_card(card, path)
    say(f"能力卡: {skill}/{task}  v{card.get('version')}  来源: {path or 'inline'}")
    say(f"通道: {card.get('channel')}　环境: {card.get('envClass')}　副作用: {card.get('effects')}")
    head("输入")
    for i in card.get("inputs") or []:
        say(f"  {i.get('name')} ({i.get('type', 'string')}){' 必填' if i.get('required') else ''} - {i.get('desc', '')}")
    head("输出")
    for o in card.get("outputs") or []:
        say(f"  {o.get('name')}: {o.get('path')} ({o.get('type', '?')})")
    if card.get("automationBoundary"):
        head("自动化边界")
        say("  " + json.dumps(card["automationBoundary"], ensure_ascii=False))
    if errs:
        head("错误")
        for e in errs:
            say("  " + _color("✗ " + e, C_ERR))
        return FAIL_EXIT
    say(_color("\n能力卡合格", C_OK))
    return OK_EXIT


def cmd_schema(args):
    say(f"job-runner 技能目录: {SKILL_DIR}")
    say(f"规范文档: {SKILL_DIR / 'references' / 'job-format.md'}")
    say(f"步骤类型: {', '.join(KINDS)}")
    say("副作用等级: read / write / outbound / irreversible（写以上必须人工闸门）")
    say("失效分类: env / knowledge / data / unknown（只有 knowledge 回写 learn-skill）")
    say("job 定义文件名: job.jsonc（推荐，支持注释与尾逗号） / job.json / job.yaml（需 PyYAML）")
    return OK_EXIT


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(prog="jobctl.py", description="job-runner 运行时")
    p.add_argument("--root", help="作业区根（默认 JOBS_ROOT 或 ~/.agents/jobs）")
    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("validate", help="校验 job 定义")
    v.add_argument("job")
    v.set_defaults(fn=cmd_validate)

    r = sub.add_parser("run", help="运行 job")
    r.add_argument("job")
    r.add_argument("--input", action="append", help="k=v，可多次；v 支持逗号分隔成列表")
    r.add_argument("--inputs-file", help="JSON 文件提供入参")
    r.add_argument("--run-id")
    r.add_argument("--dry-run", action="store_true", help="只推演不执行")
    r.add_argument("--ignore-done", action="store_true", help="忽略幂等账本，强制重跑")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("resume", help="续跑（消费 pending/*.result.json）")
    s.add_argument("run_id")
    s.add_argument("--approve", action="store_true", help="人闸门：放行")
    s.add_argument("--reject", action="store_true", help="人闸门：驳回")
    s.add_argument("--by", help="放行人")
    s.add_argument("--note", help="备注")
    s.add_argument("--ignore-done", action="store_true")
    s.set_defaults(fn=cmd_resume)

    st = sub.add_parser("status", help="看 run 进度")
    st.add_argument("run_id")
    st.set_defaults(fn=cmd_status)

    lg = sub.add_parser("log", help="看审计账本")
    lg.add_argument("run_id")
    lg.add_argument("-n", type=int, default=30)
    lg.set_defaults(fn=cmd_log)

    rp = sub.add_parser("report", help="打印 run 报告")
    rp.add_argument("run_id")
    rp.set_defaults(fn=cmd_report)

    gp = sub.add_parser("gaps", help="缺口清单")
    gp.add_argument("run_id")
    gp.add_argument("--brief", action="store_true", help="生成给 learn-skill 的补学单")
    gp.set_defaults(fn=cmd_gaps)

    ls = sub.add_parser("list", help="作业登记表")
    ls.set_defaults(fn=cmd_list)

    nw = sub.add_parser("new", help="新建 job 骨架")
    nw.add_argument("name")
    nw.add_argument("--force", action="store_true")
    nw.set_defaults(fn=cmd_new)

    cd = sub.add_parser("card", help="解析/校验能力卡")
    cd.add_argument("use", help="<skill>/<task> 或 local:<路径>")
    cd.set_defaults(fn=cmd_card)

    sc = sub.add_parser("schema", help="打印规范位置")
    sc.set_defaults(fn=cmd_schema)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return USAGE_EXIT
    try:
        return args.fn(args)
    except JobError as exc:
        say(_color(f"✗ {exc}", C_ERR))
        return FAIL_EXIT
    except KeyboardInterrupt:
        say(_color("\n中断：进度已写在 run.json，可用 resume 继续", C_WARN))
        return FAIL_EXIT


if __name__ == "__main__":
    sys.exit(main())
