"""数据包读取与校验（TL01 的核心逻辑）。

支持：本地目录（manifest.json + 各文件）或单个 JSON 数据包（键名与文件类型相同）。
导入内容一律按数据处理，任何字段文本都不会被执行或当作指令。
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

from domain.contracts import (
    SCHEMA_VERSION, Code, GenerationMethod, ReviewStatus, SourceStatus, SourceType, dec, sha256_of,
)
from domain.fields import (
    CURTAIN_WALL_TYPES, DESIGN_STAGES, FIELD_DEFS, NUMERIC_KINDS, PREDICATE_METRICS,
    PREDICATE_OPERATORS, PREDICATE_PARAMS,
)

# 演示包只作为测试夹具存在于 tests/fixtures/demo_package，运行路径不引用。
REQUIRED_TYPES = ["provenance", "cases", "schemes", "profiles", "price_bases", "prices", "rule_sets", "rules"]
CODE_MARKERS = ("__", "lambda", "eval(", "exec(", "import ", "os.", "subprocess", "${", "{{")

KNOWN_KEYS = {
    "provenance": {"provenance_id", "source_status", "source_type", "review_status", "generation_method",
                   "source_ref", "location", "source_hash", "captured_at", "reviewer", "reviewed_at",
                   "review_scope", "notes"},
    "cases": {"case_id", "case_name", "curtain_wall_type", "design_stage", "required_rule_set_id",
              "baseline_seed_id", "description"},
    "schemes": {"seed_id", "scheme_id", "case_id", "parent_seed_id", "label", "change_reason", "parameters"},
    "profiles": {"profile_id", "component_type", "profile_name", "linear_mass", "unit", "provenance_id",
                 "applicability_notes"},
    "price_bases": {"price_basis_id", "price_basis_version", "name", "currency", "price_unit", "tax_basis",
                    "cost_scope", "quote_date", "valid_until", "provenance_id", "notes"},
    "prices": {"price_basis_id", "profile_id", "unit_price", "currency", "unit", "tax_basis",
               "price_basis_version", "provenance_id"},
    "rule_sets": {"rule_set_id", "rule_set_version", "name", "scope", "required_count", "members",
                  "provenance_id", "coverage_notes"},
    "rules": {"rule_id", "rule_version", "evidence_type", "title", "exact_text", "source_ref", "location",
              "content_hash", "standard_name", "standard_number", "edition", "clause_number",
              "publication_date", "effective_from", "effective_to", "effect_status", "status_checked_at",
              "replaced_by", "applicability", "required_parameters", "machine_checkable", "predicate",
              "required_tools", "attention_text", "provenance_id", "effect_review", "mapping_review"},
}
EFFECT_STATUSES = {"current", "repealed", "replaced", "partially_replaced", "unknown", "not_applicable"}


@dataclass
class Finding:
    code: str
    message: str
    location: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "location": self.location}


@dataclass
class ValidationReport:
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    content_hash: str | None = None
    file_hashes: dict = field(default_factory=dict)
    package: dict | None = None
    source_summary: dict = field(default_factory=dict)

    @property
    def can_activate(self) -> bool:
        return not self.errors and self.package is not None

    def err(self, code: str, msg: str, loc: str) -> None:
        self.errors.append(Finding(code, msg, loc))

    def warn(self, code: str, msg: str, loc: str) -> None:
        self.warnings.append(Finding(code, msg, loc))

    def to_dict(self) -> dict:
        return {
            "errors": [e.to_dict() for e in self.errors], "warnings": [w.to_dict() for w in self.warnings],
            "counts": self.counts, "content_hash": self.content_hash, "file_hashes": self.file_hashes,
            "source_summary": self.source_summary, "can_activate": self.can_activate,
        }


# ------------------------------------------------------------------ 读取
def _read_csv(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    return [{k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items() if k} for r in rows]


def read_package_source(source: str | Path | bytes | dict) -> tuple[dict, dict, list[Finding]]:
    """读成原始结构 raw[type] 与文件哈希；读取错误作为 Finding 返回，不抛出。"""
    problems: list[Finding] = []
    file_hashes: dict = {}
    raw: dict = {}
    if isinstance(source, dict):
        bundle = source
    elif isinstance(source, (bytes, bytearray)):
        try:
            bundle = json.loads(source.decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            return {}, {}, [Finding(Code.DATASET_INVALID, f"JSON 解析失败：{exc}", "<upload>")]
        file_hashes["<bundle>"] = sha256_of(bytes(source))
    else:
        p = Path(source)
        if p.is_file():
            data = p.read_bytes()
            file_hashes[p.name] = sha256_of(data)
            try:
                bundle = json.loads(data.decode("utf-8-sig"))
            except Exception as exc:  # noqa: BLE001
                return {}, file_hashes, [Finding(Code.DATASET_INVALID, f"JSON 解析失败：{exc}", p.name)]
        elif p.is_dir():
            mpath = p / "manifest.json"
            if not mpath.exists():
                return {}, {}, [Finding(Code.DATASET_INVALID, "缺少 manifest.json", str(p))]
            mbytes = mpath.read_bytes()
            file_hashes["manifest.json"] = sha256_of(mbytes)
            try:
                manifest = json.loads(mbytes.decode("utf-8-sig"))
            except Exception as exc:  # noqa: BLE001
                return {}, file_hashes, [Finding(Code.DATASET_INVALID, f"manifest 解析失败：{exc}", "manifest.json")]
            bundle = {"manifest": manifest}
            for item in manifest.get("files", []):
                fpath = p / item.get("path", "")
                ftype = item.get("type")
                if not fpath.exists():
                    problems.append(Finding(Code.DATASET_INVALID, "清单列出的文件不存在", item.get("path", "?")))
                    continue
                data = fpath.read_bytes()
                h = sha256_of(data)
                file_hashes[item["path"]] = h
                if item.get("sha256") and item["sha256"] != h:
                    problems.append(Finding(Code.DATASET_INVALID, "文件哈希与清单不一致", item["path"]))
                if ftype == "acceptance":
                    continue  # 验收数据只登记哈希，运行时不读取
                text = data.decode("utf-8-sig")
                try:
                    bundle[ftype] = _read_csv(text) if fpath.suffix.lower() == ".csv" else json.loads(text)
                except Exception as exc:  # noqa: BLE001
                    problems.append(Finding(Code.DATASET_INVALID, f"文件解析失败：{exc}", item["path"]))
        else:
            return {}, {}, [Finding(Code.DATASET_INVALID, "数据包路径不存在", str(p))]
    if not isinstance(bundle, dict):
        return {}, file_hashes, [Finding(Code.DATASET_INVALID, "数据包顶层必须是对象", "<root>")]
    raw = dict(bundle)
    raw.pop("expected_results", None)
    return raw, file_hashes, problems


# ------------------------------------------------------------------ 校验
def _num(value, loc: str, rep: ValidationReport, *, allow_zero: bool, allow_null: bool, what: str):
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if allow_null:
            rep.warn(Code.FIELD_MISSING, f"{what}为空（null 表示缺失，不等于零），相应模块将被阻断", loc)
            return None
        rep.err(Code.FIELD_MISSING, f"{what}缺失", loc)
        return None
    if isinstance(value, float):
        value = repr(value)
    try:
        d = dec(value)
    except ValueError as exc:
        rep.err(Code.VALUE_INVALID, f"{what}非法：{exc}", loc)
        return None
    if d < 0 or (d == 0 and not allow_zero):
        rep.err(Code.VALUE_INVALID, f"{what}必须{'≥0' if allow_zero else '>0'}，当前为 {value}", loc)
        return None
    return d


def _check_unknown_keys(rec: dict, ftype: str, loc: str, rep: ValidationReport) -> None:
    extra = set(rec) - KNOWN_KEYS[ftype]
    for k in sorted(extra):
        rep.warn("UNKNOWN_FIELD", f"未知字段“{k}”已忽略，其内容只作为数据，不改变来源、审核或权限", f"{loc}.{k}")


def _unique(records: list, key: str, ftype: str, rep: ValidationReport) -> dict:
    out: dict = {}
    for i, r in enumerate(records):
        loc = f"{ftype}[{i}]"
        if not isinstance(r, dict):
            rep.err(Code.DATASET_INVALID, "记录必须是对象", loc)
            continue
        k = r.get(key)
        if not k:
            rep.err(Code.FIELD_MISSING, f"缺少主键 {key}", loc)
            continue
        if k in out:
            rep.err(Code.DATASET_INVALID, f"主键重复：{k}", loc)
            continue
        _check_unknown_keys(r, ftype, f"{ftype}[{k}]", rep)
        out[k] = {kk: vv for kk, vv in r.items() if kk in KNOWN_KEYS[ftype]}
    return out


def validate_package(source, expected_schema_version: str = SCHEMA_VERSION) -> ValidationReport:
    rep = ValidationReport()
    raw, file_hashes, problems = read_package_source(source)
    rep.file_hashes = file_hashes
    rep.errors.extend(problems)
    if not raw:
        return rep
    manifest = raw.get("manifest") or {}
    sv = str(manifest.get("schema_version", ""))
    if sv != expected_schema_version:
        rep.err(Code.SCHEMA_UNSUPPORTED, f"不支持的 schema_version：{sv or '缺失'}（需要 {expected_schema_version}）",
                "manifest.schema_version")
        return rep
    for k in ("dataset_id", "dataset_version", "default_case_id"):
        if not manifest.get(k):
            rep.err(Code.FIELD_MISSING, f"manifest 缺少 {k}", f"manifest.{k}")
    for t in REQUIRED_TYPES:
        if t not in raw:
            rep.err(Code.DATASET_INVALID, f"缺少 {t} 数据", t)
    if rep.errors:
        return rep

    # ---------- 来源
    prov = _unique(raw["provenance"], "provenance_id", "provenance", rep)
    for pid, r in prov.items():
        loc = f"provenance[{pid}]"
        if not r.get("source_status"):
            r["source_status"] = SourceStatus.PENDING.value
            rep.warn(Code.SOURCE_UNRESOLVED, "缺少 source_status，已归入 pending", loc)
        for fld, enum in (("source_status", SourceStatus), ("source_type", SourceType),
                          ("review_status", ReviewStatus), ("generation_method", GenerationMethod)):
            r.setdefault(fld, {"source_type": "unknown", "review_status": "unreviewed",
                               "generation_method": "provided"}.get(fld))
            if r[fld] not in {e.value for e in enum}:
                rep.err(Code.VALUE_INVALID, f"{fld} 取值非法：{r[fld]}", f"{loc}.{fld}")
        if r["generation_method"] in ("llm_extracted",) and r["source_status"] == "verified":
            rep.err(Code.VALUE_INVALID, "模型提取内容不能标为 verified", loc)
        if r["source_status"] in ("public", "verified") and not r.get("source_ref"):
            rep.err(Code.FIELD_MISSING, "public/verified 来源必须有来源定位", f"{loc}.source_ref")
        if r["source_status"] == "verified" or r["review_status"] == "technically_reviewed":
            if not (r.get("reviewer") and r.get("reviewed_at") and r.get("review_scope")):
                rep.err(Code.FIELD_MISSING, "verified／technically_reviewed 必须记录审核人、时间与范围", loc)

    def check_prov(pids, loc):
        ids = pids if isinstance(pids, list) else [pids]
        if not ids or any(not p for p in ids):
            rep.err(Code.SOURCE_UNRESOLVED, "缺少来源引用", loc)
        for p in ids:
            if p and p not in prov:
                rep.err(Code.SOURCE_UNRESOLVED, f"来源 ID 不存在：{p}", loc)

    # ---------- 型材
    profiles = _unique(raw["profiles"], "profile_id", "profiles", rep)
    for pid, r in profiles.items():
        loc = f"profiles[{pid}]"
        if r.get("component_type") not in ("column", "beam"):
            rep.err(Code.VALUE_INVALID, "component_type 只能是 column 或 beam", f"{loc}.component_type")
        if (r.get("unit") or "kg/m") != "kg/m":
            rep.err(Code.UNIT_UNSUPPORTED, "线重单位必须为 kg/m", f"{loc}.unit")
        d = _num(r.get("linear_mass"), f"{loc}.linear_mass", rep, allow_zero=False, allow_null=True, what="线重")
        r["linear_mass"] = None if d is None else str(d)
        check_prov(r.get("provenance_id"), f"{loc}.provenance_id")

    # ---------- 价格
    bases = _unique(raw["price_bases"], "price_basis_id", "price_bases", rep)
    for bid, r in bases.items():
        loc = f"price_bases[{bid}]"
        expect = {"currency": "CNY", "price_unit": "CNY/kg", "tax_basis": "exclusive",
                  "cost_scope": "aluminum_material_only"}
        for k, v in expect.items():
            if r.get(k) != v:
                rep.err(Code.PRICE_BASIS_MISMATCH, f"{k} 必须为 {v}（P0 不换算其他口径），当前 {r.get(k)}", f"{loc}.{k}")
        if not r.get("price_basis_version"):
            rep.err(Code.FIELD_MISSING, "缺少 price_basis_version", loc)
        check_prov(r.get("provenance_id"), f"{loc}.provenance_id")
    prices: dict = {}
    for i, r in enumerate(raw["prices"]):
        if not isinstance(r, dict):
            rep.err(Code.DATASET_INVALID, "记录必须是对象", f"prices[{i}]")
            continue
        key = f"{r.get('price_basis_id')}::{r.get('profile_id')}"
        loc = f"prices[{i}:{key}]"
        _check_unknown_keys(r, "prices", loc, rep)
        if key in prices:
            rep.err(Code.PRICE_DUPLICATE, f"同一价格基准内报价重复：{key}", loc)
            continue
        b = bases.get(r.get("price_basis_id"))
        if b is None:
            rep.err(Code.DATASET_INVALID, f"价格基准不存在：{r.get('price_basis_id')}", loc)
            continue
        if r.get("profile_id") not in profiles:
            rep.err(Code.PROFILE_MISSING, f"型材不存在：{r.get('profile_id')}", loc)
            continue
        if str(r.get("price_basis_version")) != str(b.get("price_basis_version")):
            rep.err(Code.PRICE_BASIS_MISMATCH, "报价版本与价格基准版本不一致", loc)
        for k, bk in (("currency", "currency"), ("unit", "price_unit"), ("tax_basis", "tax_basis")):
            if r.get(k) != b.get(bk):
                rep.err(Code.PRICE_BASIS_MISMATCH, f"{k}={r.get(k)} 与价格基准 {b.get(bk)} 不一致", f"{loc}.{k}")
        d = _num(r.get("unit_price"), f"{loc}.unit_price", rep, allow_zero=True, allow_null=True, what="单价")
        check_prov(r.get("provenance_id"), f"{loc}.provenance_id")
        rec = {k: v for k, v in r.items() if k in KNOWN_KEYS["prices"]}
        rec["unit_price"] = None if d is None else str(d)
        prices[key] = rec

    # ---------- 规则
    rules_raw = raw["rules"]
    if not isinstance(rules_raw, dict) or "demo_rules" not in rules_raw:
        rep.err(Code.DATASET_INVALID, "rules 需包含 demo_rules 与 formal_clauses 两个集合", "rules")
        rules_raw = {"demo_rules": [], "formal_clauses": []}
    rules: dict = {}
    for coll in ("demo_rules", "formal_clauses"):
        for i, r in enumerate(rules_raw.get(coll) or []):
            loc = f"rules.{coll}[{i}]"
            if not isinstance(r, dict) or not r.get("rule_id"):
                rep.err(Code.DATASET_INVALID, "规则缺少 rule_id", loc)
                continue
            rid = r["rule_id"]
            loc = f"rules.{coll}[{rid}]"
            if rid in rules:
                rep.err(Code.DATASET_INVALID, f"规则 ID 重复：{rid}", loc)
                continue
            _check_unknown_keys(r, "rules", loc, rep)
            r = {k: v for k, v in r.items() if k in KNOWN_KEYS["rules"]}
            r["collection"] = coll
            et = r.get("evidence_type")
            pv = prov.get(r.get("provenance_id"))
            check_prov(r.get("provenance_id"), f"{loc}.provenance_id")
            formal_fields = ("standard_name", "standard_number", "edition", "clause_number")
            if coll == "demo_rules":
                if et != "demo_rule":
                    rep.err(Code.EVIDENCE_UNQUALIFIED, "demo_rules 集合中只能放 demo_rule", loc)
                if any(r.get(f) for f in formal_fields):
                    rep.err(Code.EVIDENCE_UNQUALIFIED, "演示规则不得填写正式标准名称、编号或条号", loc)
                if pv and pv["source_status"] != "synthetic":
                    rep.err(Code.EVIDENCE_UNQUALIFIED, "演示规则来源必须为 synthetic", loc)
            else:
                if et not in ("formal_clause", "enterprise_rule"):
                    rep.err(Code.EVIDENCE_UNQUALIFIED, "formal_clauses 集合只能放 formal_clause/enterprise_rule", loc)
                if pv and pv["source_status"] == "synthetic":
                    rep.err(Code.EVIDENCE_UNQUALIFIED, "synthetic 内容不得伪装成正式依据", loc)
                if not r.get("exact_text") or not r.get("source_ref"):
                    rep.warn(Code.EVIDENCE_UNQUALIFIED, "正式记录缺原文或来源，不具备判定资格", loc)
            if r.get("effect_status") not in EFFECT_STATUSES:
                rep.err(Code.VALUE_INVALID, f"effect_status 非法：{r.get('effect_status')}", f"{loc}.effect_status")
            pred = r.get("predicate")
            if r.get("machine_checkable"):
                if not isinstance(pred, dict):
                    rep.err(Code.DATASET_INVALID, "可机判规则必须有受控 predicate 对象", f"{loc}.predicate")
                else:
                    blob = json.dumps(pred, ensure_ascii=False)
                    if any(m in blob for m in CODE_MARKERS):
                        rep.err(Code.DATASET_INVALID, "规则条件含代码样式内容，拒绝启用（不执行数据中的代码）",
                                f"{loc}.predicate")
                    if set(pred) - {"lhs", "operator", "threshold"}:
                        rep.err(Code.DATASET_INVALID, "predicate 只允许 lhs/operator/threshold", f"{loc}.predicate")
                    if pred.get("lhs") not in PREDICATE_PARAMS | set(PREDICATE_METRICS):
                        rep.err(Code.DATASET_INVALID, f"predicate.lhs 不在白名单：{pred.get('lhs')}", f"{loc}.predicate.lhs")
                    if pred.get("operator") not in PREDICATE_OPERATORS:
                        rep.err(Code.DATASET_INVALID, f"操作符不受支持：{pred.get('operator')}", f"{loc}.predicate.operator")
                    th = pred.get("threshold") or {}
                    try:
                        dec(str(th.get("value")))
                    except ValueError:
                        rep.err(Code.VALUE_INVALID, "阈值必须是十进制数", f"{loc}.predicate.threshold")
            for p in r.get("required_parameters") or []:
                if p not in PREDICATE_PARAMS | set(PREDICATE_METRICS) | {"region", "check_date"}:
                    rep.err(Code.DATASET_INVALID, f"required_parameters 含未知字段 {p}", loc)
            rules[rid] = r

    # ---------- 必查清单
    rule_sets = _unique(raw["rule_sets"], "rule_set_id", "rule_sets", rep)
    for sid, r in rule_sets.items():
        loc = f"rule_sets[{sid}]"
        members = r.get("members") or []
        seen = set()
        for m in members:
            rid = m.get("rule_id")
            if rid in seen:
                rep.err(Code.DATASET_INVALID, f"必查清单中规则重复：{rid}", loc)
            seen.add(rid)
            if rid not in rules:
                rep.err(Code.DATASET_INVALID, f"必查清单引用的规则不存在：{rid}", loc)
            if m.get("required") is not True:
                rep.err(Code.DATASET_INVALID, f"必查项 {rid} 必须 required=true", loc)
        if r.get("required_count") is not None and int(r["required_count"]) != len(members):
            rep.err(Code.DATASET_INVALID,
                    f"必查清单声明 {r['required_count']} 项，实际 {len(members)} 项（缺项或多项）", loc)
        if not members:
            rep.warn(Code.DATASET_INVALID, "必查清单为空：不能视为已全面核查", loc)
        check_prov(r.get("provenance_id"), f"{loc}.provenance_id")

    # ---------- 方案种子
    seeds = _unique(raw["schemes"], "seed_id", "schemes", rep)
    for sid, r in seeds.items():
        loc = f"schemes[{sid}]"
        if r.get("parent_seed_id") and r["parent_seed_id"] not in seeds:
            rep.err(Code.DATASET_INVALID, f"父方案不存在：{r['parent_seed_id']}", loc)
        for fname, fv in (r.get("parameters") or {}).items():
            floc = f"{loc}.parameters.{fname}"
            fdef = FIELD_DEFS.get(fname)
            if fdef is None:
                rep.warn("UNKNOWN_FIELD", f"未知参数字段“{fname}”已忽略", floc)
                continue
            if not isinstance(fv, dict) or "value" not in fv:
                rep.err(Code.DATASET_INVALID, "参数必须是含 value/unit/provenance_ids 的对象", floc)
                continue
            check_prov(fv.get("provenance_ids") or [], f"{floc}.provenance_ids")
            if fdef["kind"] in NUMERIC_KINDS:
                d = _num(fv.get("value"), floc, rep, allow_zero=fdef["kind"] == "price",
                         allow_null=True, what=fdef["label"]) if fv.get("value") is not None else None
                if fv.get("value") is not None and (fv.get("unit") or None) != fdef["unit"]:
                    rep.err(Code.UNIT_UNSUPPORTED, f"{fdef['label']}单位必须为 {fdef['unit']}", floc)
                fv["value"] = None if d is None else str(d)
            if fdef["kind"] == "profile" and fv.get("value"):
                p = profiles.get(fv["value"])
                if p is None:
                    rep.err(Code.PROFILE_MISSING, f"型材不存在：{fv['value']}", floc)
                elif p.get("component_type") != fdef["component"]:
                    rep.err(Code.PROFILE_TYPE_MISMATCH, f"{fdef['label']}引用了 {p.get('component_type')} 型材", floc)
            if fname == "price_basis_id" and fv.get("value") and fv["value"] not in bases:
                rep.err(Code.DATASET_INVALID, f"价格基准不存在：{fv['value']}", floc)
    # 父链无环
    for sid in seeds:
        chain, cur = set(), sid
        while cur:
            if cur in chain:
                rep.err(Code.DATASET_INVALID, f"方案父链存在环：{sid}", f"schemes[{sid}]")
                break
            chain.add(cur)
            cur = seeds.get(cur, {}).get("parent_seed_id")

    # ---------- 案例
    cases = _unique(raw["cases"], "case_id", "cases", rep)
    for cid, r in cases.items():
        loc = f"cases[{cid}]"
        if r.get("required_rule_set_id") not in rule_sets:
            rep.err(Code.DATASET_INVALID, f"必查清单不存在：{r.get('required_rule_set_id')}", loc)
        if r.get("baseline_seed_id") not in seeds:
            rep.err(Code.DATASET_INVALID, f"基准方案种子不存在：{r.get('baseline_seed_id')}", loc)
        cwt = (r.get("curtain_wall_type") or {}).get("value")
        stage = (r.get("design_stage") or {}).get("value")
        if cwt not in CURTAIN_WALL_TYPES or stage not in DESIGN_STAGES:
            rep.warn(Code.SCOPE_UNSUPPORTED, f"案例类型 {cwt}/{stage} 超出 P0 覆盖范围，运行时将被拦截", loc)
        for k in ("case_name", "curtain_wall_type", "design_stage"):
            check_prov((r.get(k) or {}).get("provenance_ids") or [], f"{loc}.{k}")
    if manifest.get("default_case_id") not in cases:
        rep.err(Code.DATASET_INVALID, "默认案例不存在", "manifest.default_case_id")

    package = {
        "dataset_id": manifest.get("dataset_id"), "dataset_version": str(manifest.get("dataset_version")),
        "schema_version": sv, "name": manifest.get("name") or manifest.get("dataset_id"),
        "default_case_id": manifest.get("default_case_id"), "is_demo": bool(manifest.get("is_demo")),
        "provenance": prov, "cases": cases, "seeds": seeds, "profiles": profiles,
        "price_bases": bases, "prices": prices, "rule_sets": rule_sets, "rules": rules,
    }
    rep.content_hash = sha256_of(package)
    package["content_hash"] = rep.content_hash
    rep.counts = {"cases": len(cases), "schemes": len(seeds), "profiles": len(profiles),
                  "price_bases": len(bases), "prices": len(prices), "rule_sets": len(rule_sets),
                  "demo_rules": sum(1 for r in rules.values() if r["collection"] == "demo_rules"),
                  "formal_clauses": sum(1 for r in rules.values() if r["collection"] == "formal_clauses"),
                  "provenance": len(prov)}
    summary: dict = {}
    for r in prov.values():
        summary[r["source_status"]] = summary.get(r["source_status"], 0) + 1
    rep.source_summary = summary
    rep.package = package
    return rep


# ------------------------------------------------------------------ 种子解析
def resolve_seed_parameters(package: dict, seed_id: str) -> dict:
    """沿父链合并种子参数，返回 {field: {value, unit, provenance_ids, origin_seed}}。"""
    chain = []
    cur = seed_id
    while cur:
        seed = package["seeds"][cur]
        chain.append(seed)
        cur = seed.get("parent_seed_id")
    merged: dict = {}
    for seed in reversed(chain):
        for k, fv in (seed.get("parameters") or {}).items():
            if k in FIELD_DEFS:
                merged[k] = {"value": fv.get("value"), "unit": fv.get("unit"),
                             "provenance_ids": list(fv.get("provenance_ids") or []), "origin_seed": seed["seed_id"]}
    for k, fdef in FIELD_DEFS.items():
        merged.setdefault(k, {"value": None, "unit": fdef["unit"], "provenance_ids": [], "origin_seed": None})
    return merged
