"""共享契约：枚举、十进制规则、Issue/Step/Metric、工具响应信封与哈希。

对应详细设计 02（数据表结构）与 04（工具接口定义）。本模块不依赖界面、模型或存储。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import Any, Iterable

CONTRACT_VERSION = "0.2"
SCHEMA_VERSION = "0.2"
MEASUREMENT_MODEL = "rect_uniform_axis_v1"

# 精确工具版本；复算时版本不一致返回 VERSION_UNAVAILABLE，不隐式换新版。
TOOL_VERSIONS: dict[str, str] = {
    "TL01": "1.0.0", "TL02": "1.0.0", "TL03": "1.0.0", "TL04": "1.0.0",
    "TL05": "1.0.0", "TL06": "1.0.0", "TL07": "1.0.0", "TL08": "1.0.0",
    "TL09": "1.0.0", "TL10": "1.0.0", "TL11": "1.0.0", "TL12": "1.0.0",
    "TL13": "1.0.0", "TL14": "1.0.0", "TL15": "1.0.0", "TL16": "1.0.0",
}

ROUNDING_POLICY = {
    "rounding": "ROUND_HALF_UP",
    "length": 3, "mass": 3, "area": 2, "money": 2,
    "mass_per_area": 4, "money_per_area": 2, "percent": 2, "count": 0,
}


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - 便于序列化
        return self.value


class SourceStatus(StrEnum):
    SYNTHETIC = "synthetic"
    PUBLIC = "public"
    VERIFIED = "verified"
    PENDING = "pending"


class SourceType(StrEnum):
    TEAM_CONSTRUCTED = "team_constructed"
    PUBLIC_DOCUMENT = "public_document"
    ENTERPRISE_RECORD = "enterprise_record"
    UNKNOWN = "unknown"


class ReviewStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    IN_REVIEW = "in_review"
    PENDING_PROFESSIONAL_REVIEW = "pending_professional_review"
    TECHNICALLY_REVIEWED = "technically_reviewed"
    REJECTED = "rejected"
    OUTDATED = "outdated"


class GenerationMethod(StrEnum):
    PROVIDED = "provided"
    LLM_EXTRACTED = "llm_extracted"
    PROGRAM_DERIVED = "program_derived"
    ENGINEER_ADJUSTED = "engineer_adjusted"


class BindingState(StrEnum):
    INHERITED = "inherited"
    MODIFIED = "modified"
    CLEARED = "cleared"
    MISSING = "missing"


class ModuleStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    ERROR = "error"


class RuleStatus(StrEnum):
    DEMO_MET = "DEMO_MET"
    DEMO_ATTENTION = "DEMO_ATTENTION"
    CONDITION_MET = "CONDITION_MET"
    CONDITION_NOT_MET = "CONDITION_NOT_MET"
    NEED_PARAMETERS = "NEED_PARAMETERS"
    NEED_ENGINEER = "NEED_ENGINEER"
    NEED_VERIFICATION = "NEED_VERIFICATION"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_COVERED = "NOT_COVERED"


RULE_STATUS_TEXT = {
    RuleStatus.DEMO_MET: "演示条件满足",
    RuleStatus.DEMO_ATTENTION: "演示关注项触发",
    RuleStatus.CONDITION_MET: "已满足该条款可机判条件",
    RuleStatus.CONDITION_NOT_MET: "未满足该条款可机判条件",
    RuleStatus.NEED_PARAMETERS: "待补参数",
    RuleStatus.NEED_ENGINEER: "需人工判断",
    RuleStatus.NEED_VERIFICATION: "待核实",
    RuleStatus.NOT_APPLICABLE: "不适用",
    RuleStatus.NOT_COVERED: "未覆盖",
}

DETERMINATE_RULE_STATUSES = {
    RuleStatus.DEMO_MET, RuleStatus.DEMO_ATTENTION,
    RuleStatus.CONDITION_MET, RuleStatus.CONDITION_NOT_MET,
}
# 这些状态使 rule_check 模块记为 blocked（需人工判断属于已执行并转人工，不算阻断）。
BLOCKING_RULE_STATUSES = {
    RuleStatus.NEED_PARAMETERS, RuleStatus.NEED_VERIFICATION, RuleStatus.NOT_COVERED,
}


class ExecState(StrEnum):
    IDLE = "IDLE"
    EDITING = "EDITING"
    VALIDATING = "VALIDATING"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    WAIT_INPUT = "WAIT_INPUT"
    WAIT_CONFIRM = "WAIT_CONFIRM"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


EXEC_STATE_TEXT = {
    ExecState.IDLE: "就绪", ExecState.EDITING: "编辑中", ExecState.VALIDATING: "校验中",
    ExecState.OUT_OF_SCOPE: "范围外", ExecState.WAIT_INPUT: "等待补参",
    ExecState.WAIT_CONFIRM: "等待确认", ExecState.RUNNING: "运行中",
    ExecState.COMPLETE: "本次运行完成", ExecState.PARTIAL: "部分完成", ExecState.FAILED: "执行失败",
}

MODULE_TEXT = {
    "query": "资料查询", "geometry": "基础几何", "length": "理论长度", "mass": "型材质量",
    "cost": "材料成本", "comparison": "两方案比较", "rule_check": "必查清单核查", "report": "报告",
}
MODULE_STATUS_TEXT = {
    ModuleStatus.NOT_REQUESTED: "未请求", ModuleStatus.READY: "就绪", ModuleStatus.RUNNING: "运行中",
    ModuleStatus.DONE: "已完成", ModuleStatus.BLOCKED: "阻断", ModuleStatus.ERROR: "失败",
}


# ---------------------------------------------------------------- 错误码
class Code:
    SCOPE_UNSUPPORTED = "SCOPE_UNSUPPORTED"
    FIELD_MISSING = "FIELD_MISSING"
    UNIT_REQUIRED = "UNIT_REQUIRED"
    VALUE_INVALID = "VALUE_INVALID"
    UNIT_UNSUPPORTED = "UNIT_UNSUPPORTED"
    NON_DIVISIBLE_GRID = "NON_DIVISIBLE_GRID"
    AREA_CONFLICT = "AREA_CONFLICT"
    PARAMETER_CONFLICT = "PARAMETER_CONFLICT"
    AMBIGUOUS_FIELD = "AMBIGUOUS_FIELD"
    PROFILE_MISSING = "PROFILE_MISSING"
    PROFILE_TYPE_MISMATCH = "PROFILE_TYPE_MISMATCH"
    MASS_MISSING = "MASS_MISSING"
    PRICE_MISSING = "PRICE_MISSING"
    PRICE_DUPLICATE = "PRICE_DUPLICATE"
    PRICE_BASIS_MISMATCH = "PRICE_BASIS_MISMATCH"
    ZERO_PRICE_UNCONFIRMED = "ZERO_PRICE_UNCONFIRMED"
    COMPARISON_INCOMPATIBLE = "COMPARISON_INCOMPATIBLE"
    EVIDENCE_UNQUALIFIED = "EVIDENCE_UNQUALIFIED"
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
    DATASET_INVALID = "DATASET_INVALID"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    ACTIVATION_STALE = "ACTIVATION_STALE"
    SOURCE_UNRESOLVED = "SOURCE_UNRESOLVED"
    UNCONFIRMED_INPUT = "UNCONFIRMED_INPUT"
    STALE_DRAFT = "STALE_DRAFT"
    INPUT_HASH_MISMATCH = "INPUT_HASH_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_FAILED = "TOOL_FAILED"
    STORAGE_FAILED = "STORAGE_FAILED"
    MODEL_UNSUPPORTED = "MODEL_UNSUPPORTED"
    VERSION_UNAVAILABLE = "VERSION_UNAVAILABLE"
    PARTIAL_EXPORT_NOT_CONFIRMED = "PARTIAL_EXPORT_NOT_CONFIRMED"
    ZERO_BASELINE = "ZERO_BASELINE"  # 非阻断 reason_code
    UPSTREAM_NOT_DONE = "UPSTREAM_NOT_DONE"


@dataclass
class Issue:
    code: str
    message: str
    field_path: str | None = None
    module: str | None = None
    blocking: bool = True
    retryable: bool = False
    required_action: str | None = None
    evidence_ref: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 十进制
_CTX = Context(prec=34, rounding=ROUND_HALF_UP)


def dec(value: Any) -> Decimal:
    """把字符串／整数／Decimal 转为有限 Decimal；拒绝 float、NaN、Infinity。"""
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为数值")
    if isinstance(value, float):
        raise ValueError("禁止以二进制浮点直接参与计算，请传入十进制字符串")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"无法解析的数值：{value!r}") from exc
    else:
        raise ValueError(f"不支持的数值类型：{type(value).__name__}")
    if not d.is_finite():
        raise ValueError("数值必须有限")
    return d


def dstr(d: Decimal | None) -> str | None:
    """未舍入的规范化十进制字符串（去掉多余尾零但不使用科学计数法）。"""
    if d is None:
        return None
    with localcontext(_CTX):
        n = d.normalize()
    s = format(n, "f")
    return s


def calc(op: str, a: Decimal, b: Decimal) -> Decimal:
    with localcontext(_CTX):
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b
    raise ValueError(op)


def quantize(d: Decimal, places: int) -> Decimal:
    q = Decimal(1).scaleb(-places)
    with localcontext(_CTX):
        return d.quantize(q, rounding=ROUND_HALF_UP)


def display(d: Decimal | str | None, kind: str, thousands: bool = False) -> str | None:
    if d is None:
        return None
    d = dec(d) if not isinstance(d, Decimal) else d
    places = ROUNDING_POLICY[kind]
    v = quantize(d, places)
    return f"{v:,.{places}f}" if thousands else f"{v:.{places}f}"


# ---------------------------------------------------------------- 计算记录
@dataclass
class Step:
    step_id: str
    formula_id: str
    operator: str
    operand_refs: list[str]
    expression: str
    exact_value: str
    unit: str
    display_value: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Metric:
    metric_key: str
    value: str | None
    unit: str
    display_value: str | None
    input_refs: list[str]
    source_statuses: list[str]
    formula_id: str
    tool_version: str
    step_refs: list[str] = field(default_factory=list)
    reason_code: str | None = None
    generation_method: str = GenerationMethod.PROGRAM_DERIVED.value
    review_status: str = ReviewStatus.PENDING_PROFESSIONAL_REVIEW.value

    @property
    def contains_synthetic(self) -> bool:
        return SourceStatus.SYNTHETIC.value in self.source_statuses

    @property
    def contains_pending(self) -> bool:
        return SourceStatus.PENDING.value in self.source_statuses

    def to_dict(self) -> dict:
        d = asdict(self)
        d["contains_synthetic"] = self.contains_synthetic
        d["contains_pending"] = self.contains_pending
        return d


# ---------------------------------------------------------------- 工具信封
@dataclass
class ToolResponse:
    tool_id: str
    status: str  # ok / partial / blocked / error
    data: dict | None
    issues: list[Issue]
    trace: dict
    request_id: str | None = None
    run_id: str | None = None
    input_hash: str | None = None
    tool_version: str | None = None

    def __post_init__(self) -> None:
        if self.tool_version is None:
            self.tool_version = TOOL_VERSIONS.get(self.tool_id)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id, "run_id": self.run_id,
            "tool_id": self.tool_id, "tool_version": self.tool_version,
            "input_hash": self.input_hash, "status": self.status, "data": self.data,
            "issues": [i.to_dict() for i in self.issues], "trace": self.trace,
        }


# ---------------------------------------------------------------- 杂项
def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_of(obj: Any) -> str:
    if isinstance(obj, (bytes, bytearray)):
        return hashlib.sha256(obj).hexdigest()
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def merge_statuses(*groups: Iterable[str]) -> list[str]:
    """合并来源集合，保留全部实际成员（不归并为单一等级）。"""
    order = [s.value for s in SourceStatus]
    seen = {s for g in groups for s in g}
    return [s for s in order if s in seen] + sorted(seen - set(order))
