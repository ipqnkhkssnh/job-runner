#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jobctl 核心库：路径解析 / JSONC 读取 / 引用表达式 / 版本比较 / 账本与登记表。

设计约束：**只用 Python 3 标准库**（运行时不保证有 PyYAML / pandas / openpyxl）。
所以 job 文件的规范格式是「JSONC」= JSON + // 注释 + 尾逗号容错；
若环境里恰好有 PyYAML，也接受 job.yaml / job.yml。
"""
from __future__ import annotations

import datetime
import glob as _glob
import hashlib
import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------- 常量

KINDS = ("tool", "skill", "map", "assert", "approve", "notify")
EFFECTS = ("read", "write", "outbound", "irreversible")
GATES = ("auto", "approve", "outbound")
FAIL_CLASSES = ("env", "knowledge", "data", "unknown")
ENV_CLASSES = ("test", "prod", "any", "unknown")

#: 通道优先级：**越靠前越确定**。有确定性接口就别去点界面（界面操作是精度最低的一环）。
CHANNEL_PRIORITY = ("mcp", "api", "remote-a2desk", "local-a2desk", "playwright", "human")
CHANNELS = CHANNEL_PRIORITY + ("auto",)

#: 能力卡证据等级：这张卡是被"看到"的，还是被"跑过"的？
#:   unknown         没见过 / 只有猜想        → 只允许只读探测，写操作一律拒绝
#:   observed        录屏或界面上看到，没实操  → 允许人闸门下单件写，禁止批量
#:   verified-once   现场实测成功过 ≥1 次      → 允许人闸门下写，允许批量但逐批放行
#:   verified-repeat 多次实测 / 回归过         → 允许一次批量放行（写仍必须 approve 闸门）
EVIDENCE_LEVELS = ("unknown", "observed", "verified-once", "verified-repeat")
_EVIDENCE_RANK = {"unknown": 0, "observed": 1, "verified-once": 2, "verified-repeat": 3}

#: 每个证据等级允许走到哪一步（写进放行卡，让人知道自己批的是什么成色的东西）。
#: 注意措辞：这些约束针对**写路径**（effects != read）；只读路径不受证据等级限制，
#: 否则一张"只被看到过"的只读卡的说明会读起来像"必须人工放行"，误导放行人。
EVIDENCE_MEANING = {
    "unknown": "没见过/只有猜想——只允许只读探测；**写路径一律拒绝**",
    "observed": "录屏或界面上看到过，没实操——**写路径**只允许人闸门下单件执行、禁止批量；只读路径不受限",
    "verified-once": "现场实测成功过至少一次——**写路径**人闸门下可单件或逐批执行；只读路径不受限",
    "verified-repeat": "多次实测/回归过——**写路径**允许一次批量放行（仍必须人闸门）；只读路径不受限",
}

#: 每种副作用等级允许的最小闸门（gate 比它更松就是错误）
MIN_GATE_BY_EFFECT = {
    "read": "auto",
    "write": "approve",
    "outbound": "approve",
    "irreversible": "approve",
}
_GATE_RANK = {"auto": 0, "outbound": 1, "approve": 2}


def evidence_rank(level) -> int:
    """证据等级的序数；不认识的一律当 unknown（最保守）。"""
    return _EVIDENCE_RANK.get(str(level or "unknown"), 0)


def stricter_effect(*levels) -> str:
    """取最严的副作用等级。

    步骤声明与能力卡声明不一致时**以更严的为准**——否则"顺手把 effects 写成 read"
    就能绕过写闸门，那闸门等于没有。
    """
    best = "read"
    for lv in levels:
        if lv in EFFECTS and EFFECTS.index(str(lv)) > EFFECTS.index(best):
            best = str(lv)
    return best


def evidence_at_least(level, floor) -> bool:
    return evidence_rank(level) >= evidence_rank(floor)


def gate_for(effects: str, evidence: str = "verified-repeat", batched: bool = False) -> str:
    """算这一步**至少**要什么闸门。

    写以上永远要 `approve`——闸门不因为证据等级高而放松。
    证据等级只影响「能不能批量」：不够 `verified-repeat` 就不允许被批量放行，
    这一条由 validate 的 `batch_allowed()` 单独管，不混进闸门等级里。
    """
    return MIN_GATE_BY_EFFECT.get(effects, "approve")


def batch_allowed(evidence: str) -> bool:
    """证据不足的写路径只允许单件执行（每件各过一次人闸门）。"""
    return evidence_at_least(evidence, "verified-repeat")

PAUSE_EXIT = 3
FAIL_EXIT = 1
OK_EXIT = 0
USAGE_EXIT = 2


class JobError(Exception):
    """job 定义或运行期的可预期错误。"""


class RefError(JobError):
    """${...} 引用表达式解析失败。"""


def now_iso() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.date.today().isoformat()


def stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def rand_suffix(nbytes: int = 2) -> str:
    """run id 后缀：同一秒内连跑两次也不会撞目录。"""
    return os.urandom(nbytes).hex()


# ---------------------------------------------------------------- 路径解析

def skills_root() -> Path:
    """技能根。解析顺序与 learn-skill 的 init_skill.sh / validate_skill.sh 完全一致。"""
    env = os.environ.get("LEARN_SKILLS_ROOT")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("DSH_AGENTS_HOME")
    if home:
        return Path(home).expanduser() / "skills"
    for p in (Path.home() / ".agents" / "skills", Path.home() / ".agent" / "skills"):
        if p.is_dir():
            return p
    return Path.home() / ".agents" / "skills"


def jobs_root(override: str | None = None) -> Path:
    """作业区根：JOBS_ROOT / --root > $DSH_AGENTS_HOME/jobs > 已存在的 ~/.agent[s]/jobs > ~/.agents/jobs。"""
    if override:
        return Path(override).expanduser()
    env = os.environ.get("JOBS_ROOT")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("DSH_AGENTS_HOME")
    if home:
        return Path(home).expanduser() / "jobs"
    for p in (Path.home() / ".agents" / "jobs", Path.home() / ".agent" / "jobs"):
        if p.is_dir():
            return p
    return Path.home() / ".agents" / "jobs"


def find_job_dir(name_or_path: str, root: Path) -> Path:
    p = Path(name_or_path).expanduser()
    if p.is_dir():
        return p.resolve()
    cand = Path(root) / name_or_path
    if cand.is_dir():
        return cand.resolve()
    raise JobError(
        f"找不到 job：{name_or_path}（既不是已存在的目录，也不是 {root} 下的 job）")


JOB_FILENAMES = ("job.jsonc", "job.json", "job.yaml", "job.yml")


def job_file_of(job_dir: Path) -> Path:
    for n in JOB_FILENAMES:
        f = Path(job_dir) / n
        if f.is_file():
            return f
    raise JobError(
        f"{job_dir} 下没有 job 定义文件（期望 {' / '.join(JOB_FILENAMES)}）")


def apply_input_defaults(job: dict, provided: dict) -> dict:
    """把入参声明里的 `default` 落到实际入参上；`required: false` 且没传也没默认 → None。

    为什么要这样：job 作者写 `"${inputs.startDate}"` 是**合法**的（声明过就是已知入参），
    只是这次没传。这时应当解析成「没有值」，而不是让整个作业在引用解析上炸掉——
    可选入参传不传是调用方的自由，不该变成引擎的硬错误。
    """
    out = dict(provided or {})
    for spec in job.get("inputs") or []:
        if not isinstance(spec, dict) or not spec.get("name"):
            continue
        name = str(spec["name"])
        if name in out:
            continue
        if "default" in spec:
            out[name] = spec["default"]
        elif not spec.get("required"):
            out[name] = None
    return out


# ---------------------------------------------------------------- JSONC / YAML 读取

def strip_jsonc(text: str) -> str:
    """去掉 // 与 /* */ 注释（字符串内不处理），再去掉尾逗号。"""
    out = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(s: str) -> str:
    out = []
    i, n = 0, len(s)
    in_str = esc = False
    while i < n:
        c = s[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "}]":
                i += 1          # 丢掉这个逗号
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_any(path: Path):
    """读 job / 能力卡：.json(.jsonc) 走 JSONC；.yaml/.yml 需要 PyYAML。"""
    text = Path(path).read_text(encoding="utf-8")
    suffix = Path(path).suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise JobError(
                f"{path} 是 YAML，但当前 python3 没有 PyYAML。"
                f"请改用 job.jsonc（零依赖），或 pip install pyyaml。") from exc
        return yaml.safe_load(text)
    return json.loads(strip_jsonc(text))


def write_json(path: Path, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def read_json(path: Path, default=None):
    p = Path(path)
    if not p.is_file():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- 引用表达式 ${...}

_REF = re.compile(r"\$\{([^{}]+)\}")
_SEG = re.compile(r"\[[^\]]+\]|\.[A-Za-z0-9_\-]+")


def refs_in(value) -> list:
    """收集一段结构里出现的所有 ${...} 表达式（去重、保持顺序）。"""
    found: list = []

    def walk(v):
        if isinstance(v, str):
            for m in _REF.finditer(v):
                e = m.group(1).strip()
                if e not in found:
                    found.append(e)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(value)
    return found


def head_of(expr: str) -> str:
    return expr.strip().split(".", 1)[0].split("[", 1)[0].strip()


def _lookup(expr: str, ctx: dict):
    expr = expr.strip()
    m = re.match(r"^([A-Za-z0-9_\-]+)((?:\[[^\]]+\]|\.[A-Za-z0-9_\-]+)*)$", expr)
    if not m:
        raise RefError(f"引用表达式不合法：${{{expr}}}")
    head, rest = m.group(1), m.group(2)
    if head not in ctx:
        raise RefError(f"引用未定义：${{{expr}}}（当前可用：{', '.join(sorted(ctx))}）")
    cur = ctx[head]
    for tok in _SEG.findall(rest):
        if tok.startswith("."):
            key = tok[1:]
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                raise RefError(f"引用路径不存在：${{{expr}}}（在 .{key} 处断了）")
        else:
            idx = tok[1:-1].strip()
            if not isinstance(cur, list):
                raise RefError(f"引用路径不是列表：${{{expr}}}（{tok} 用在了非列表上）")
            try:
                i = int(idx)
            except ValueError:
                raise RefError(f"列表下标必须是整数：${{{expr}}} 的 {tok}")
            if not (-len(cur) <= i < len(cur)):
                raise RefError(f"列表下标越界：${{{expr}}} 的 {tok}（长度 {len(cur)}）")
            cur = cur[i]
    return cur


def resolve_str(s: str, ctx: dict):
    m = _REF.fullmatch(s.strip())
    if m:
        return _lookup(m.group(1), ctx)

    def sub(mm):
        v = _lookup(mm.group(1), ctx)
        if v is None:
            # 可选入参没传 → 渲染成空串，而不是把 Python 的 "None" 塞进命令行参数
            return ""
        if isinstance(v, list):
            return " ".join("" if x is None else str(x) for x in v)
        if isinstance(v, dict):
            return json.dumps(v, ensure_ascii=False)
        return str(v)

    return _REF.sub(sub, s)


def resolve(value, ctx: dict):
    """递归解析 ${...}。列表元素若解析成列表则展开（便于 in/out 传路径）。"""
    if isinstance(value, str):
        return resolve_str(value, ctx)
    if isinstance(value, list):
        out: list = []
        for x in value:
            r = resolve(x, ctx)
            if isinstance(r, list):
                out.extend(r)
            else:
                out.append(r)
        return out
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- 版本比较

def parse_version(v) -> tuple:
    m = re.match(r"^\s*[v^~=><\s]*?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v or ""))
    if not m:
        return (0, 0, 0)
    return tuple(int(x or 0) for x in m.groups())


def version_satisfies(actual, spec) -> bool:
    """支持 "0.3"、"0.3.1"、">=0.3"、"^0.3"、"*"（够用即可，不追求完整 semver）。"""
    spec = str(spec or "*").strip()
    if spec in ("*", "", "any"):
        return True
    a = parse_version(actual)
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        op = ">="
        body = part
        for cand in (">=", "<=", "==", ">", "<", "^", "~"):
            if part.startswith(cand):
                op, body = cand, part[len(cand):].strip()
                break
        b = parse_version(body)
        if op == ">=" and not a >= b:
            return False
        if op == ">" and not a > b:
            return False
        if op == "<=" and not a <= b:
            return False
        if op == "<" and not a < b:
            return False
        if op == "==" and a[:len(b)] != b:
            return False
        if op == "^" and not (a >= b and a[0] == b[0]):
            return False
        if op == "~" and not (a >= b and a[:2] == b[:2]):
            return False
    return True


# ---------------------------------------------------------------- 账本 / 登记表

def append_ledger(root: Path, entry: dict) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now_iso(), **entry}, ensure_ascii=False) + "\n")


def ledger_has_key(root: Path, key: str) -> dict | None:
    """账本里有没有这个幂等键**真的执行过**。

    两道防线，防止"推演毒化幂等键"（一次 --dry-run 让日后的真跑被静默跳过）：
      1. 运行时 dry-run 已经不写 key（见 runner._exec_leaf）；
      2. 这里再跳过任何标记了 dryRun 的条目——万一有别的写入方留下痕迹。
    """
    f = Path(root) / "ledger.jsonl"
    if not f.is_file():
        return None
    for line in reversed(f.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("dryRun"):
            continue
        if e.get("key") == key and e.get("effects") in ("write", "outbound", "irreversible"):
            return e
    return None


def update_registry(root: Path, job_name: str, **fields) -> None:
    root = Path(root)
    reg_path = root / "registry.json"
    reg = read_json(reg_path, default=None) or {"jobs": []}
    entry = None
    for e in reg.get("jobs", []):
        if e.get("job") == job_name:
            entry = e
            break
    if entry is None:
        entry = {"job": job_name}
        reg.setdefault("jobs", []).append(entry)
    entry.update(fields)
    entry["updatedAt"] = now_iso()
    reg["jobs"] = sorted(reg.get("jobs", []), key=lambda e: e.get("job", ""))
    write_json(reg_path, reg)


# ---------------------------------------------------------------- 杂项

def expand_globs(patterns, base: Path) -> list:
    """把 out / in 的 glob（相对 run 目录）展开成存在的文件清单（相对路径字符串）。"""
    if isinstance(patterns, str):
        patterns = [patterns]
    out: list = []
    for pat in patterns or []:
        p = Path(str(pat))
        if p.is_absolute():
            hits = sorted(_glob.glob(str(p)))
            for h in hits:
                try:
                    out.append(str(Path(h).relative_to(base)))
                except ValueError:
                    out.append(h)
            if not hits and p.exists():
                out.append(str(p))
        else:
            hits = sorted(_glob.glob(str(base / pat)))
            if hits:
                out.extend(str(Path(h).relative_to(base)) for h in hits)
            elif (base / pat).exists():
                out.append(str(pat))
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


SECRET_VALUE_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
    r"\s*[:=]\s*[\"']?([^\s\"',}]{3,})", re.I)
SECRET_LITERAL_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9._-]{20,}")
SECRET_PLACEHOLDER_RE = re.compile(
    r"ref:|TODO|待填|待补|\$\{|xxx+|<[^>]*>|placeholder|example|来源", re.I)


def secret_hits(text: str) -> list:
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        if SECRET_PLACEHOLDER_RE.search(line):
            continue
        if SECRET_LITERAL_RE.search(line) or SECRET_VALUE_RE.search(line):
            hits.append(f"L{i}: {line.strip()[:120]}")
    return hits
