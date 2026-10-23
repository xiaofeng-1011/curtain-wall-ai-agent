"""测试辅助：由种子构造已确认快照。预期值只从独立的 expected_results.json 读取。"""
import copy
import json

from pathlib import Path

from data.package_loader import resolve_seed_parameters, validate_package

DEMO_PACKAGE_DIR = Path(__file__).resolve().parent / "fixtures" / "demo_package"
from storage.snapshots import build_snapshot
from validation.parameters import apply_edit, params_from_seed

EXPECTED = json.loads((DEMO_PACKAGE_DIR / "expected_results.json").read_text(encoding="utf-8"))


def demo_package():
    rep = validate_package(DEMO_PACKAGE_DIR)
    assert rep.can_activate, rep.to_dict()
    return rep.package


def seed_params(pkg, seed_id):
    return params_from_seed(resolve_seed_parameters(pkg, seed_id), f"seed:{seed_id}", inherit_mode=False)


def snap(pkg, seed_id, edits=None, zero_acks=None, scheme_id=None):
    params = seed_params(pkg, seed_id)
    for e in edits or []:
        params = apply_edit(params, *e)
    return build_snapshot(pkg, "DEMO-CW-001", scheme_id or seed_id, params, zero_price_acknowledgements=zero_acks)


def mval(resp, key):
    m = resp.data["metrics"].get(key) if resp.data else None
    return None if m is None else m["value"]


def mdisp(resp, key):
    m = resp.data["metrics"].get(key) if resp.data else None
    return None if m is None else m["display_value"]


def mutate(pkg, fn):
    p = copy.deepcopy(pkg)
    fn(p)
    return p
