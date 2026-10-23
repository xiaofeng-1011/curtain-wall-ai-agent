"""SchemeParameters 字段词典（详细设计 02 §2.2）。"""
from __future__ import annotations

# kind: length / area / linear_mass / price / profile / id / text / date
FIELD_DEFS: dict[str, dict] = {
    "width_m": {"label": "立面宽 W", "noun": "立面宽度", "kind": "length", "unit": "m", "required": True,
                "purpose": "用于计算立面面积、横向格数和横梁总长"},
    "height_m": {"label": "立面高 H", "noun": "立面高度", "kind": "length", "unit": "m", "required": True,
                 "purpose": "用于计算立面面积、竖向格数和立柱总长"},
    "grid_width_m": {"label": "分格宽 b", "noun": "分格宽度", "kind": "length", "unit": "m", "required": True,
                     "purpose": "用于计算横向格数、立柱轴线数和板块面积"},
    "grid_height_m": {"label": "分格高 h", "noun": "分格高度", "kind": "length", "unit": "m", "required": True,
                      "purpose": "用于计算竖向格数、横梁道数和板块面积"},
    "floor_height_m": {"label": "层高", "noun": "层高", "kind": "length", "unit": "m", "required": False,
                       "purpose": "仅在核查规则需要时使用，不作结构跨度认定，不影响材料计算"},
    "reported_area_m2": {"label": "申报面积", "kind": "area", "unit": "m²", "required": False,
                         "purpose": "仅与 W×H 做一致性检查；计算面积由工具生成"},
    "column_profile_id": {"label": "立柱规格", "kind": "profile", "component": "column", "unit": None,
                          "required": True, "purpose": "确定立柱线重和立柱报价"},
    "beam_profile_id": {"label": "横梁规格", "kind": "profile", "component": "beam", "unit": None,
                        "required": True, "purpose": "确定横梁线重和横梁报价"},
    "column_mass_override": {"label": "立柱线重覆盖值", "kind": "linear_mass", "unit": "kg/m", "required": False,
                             "purpose": "仅在用户显式覆盖材料线重时使用"},
    "beam_mass_override": {"label": "横梁线重覆盖值", "kind": "linear_mass", "unit": "kg/m", "required": False,
                           "purpose": "仅在用户显式覆盖材料线重时使用"},
    "price_basis_id": {"label": "价格基准", "kind": "id", "unit": None, "required": False,
                       "purpose": "定位立柱、横梁两条报价及统一价格口径"},
    "column_price_override": {"label": "立柱单价覆盖值", "kind": "price", "unit": "CNY/kg", "required": False,
                              "purpose": "仅在用户显式调整价格时使用，继承基准口径"},
    "beam_price_override": {"label": "横梁单价覆盖值", "kind": "price", "unit": "CNY/kg", "required": False,
                            "purpose": "仅在用户显式调整价格时使用，继承基准口径"},
    "measurement_model_id": {"label": "计量模型", "kind": "id", "unit": None, "required": True,
                             "purpose": "确定边界计数与轴线计长口径"},
    "region": {"label": "地区", "kind": "text", "unit": None, "required": False,
               "purpose": "仅在正式依据明确依赖地区时需要"},
    "check_date": {"label": "规范核查日期", "kind": "date", "unit": None, "required": False,
                   "purpose": "仅在正式依据明确依赖核查日期时需要；不默认填写运行日期"},
}

NUMERIC_KINDS = {"length", "area", "linear_mass", "price"}
GEOMETRY_FIELDS = ["width_m", "height_m", "grid_width_m", "grid_height_m"]

# 规则谓词左值白名单：参数字段或几何工具派生指标
PREDICATE_PARAMS = {"width_m", "height_m", "grid_width_m", "grid_height_m", "floor_height_m"}
PREDICATE_METRICS = {"panel_area_m2": "TL07", "facade_area_m2": "TL07"}
PREDICATE_OPERATORS = {"<=", "<", ">=", ">", "=="}

CURTAIN_WALL_TYPES = {"component_glass": "构件式玻璃幕墙"}
DESIGN_STAGES = {"scheme_design": "方案设计阶段"}


# 面向用户的字段（对话、表单、导入都用这一套；材料与报价由系统转换为项目内的材料与报价记录）
USER_FIELDS: dict[str, dict] = {
    "width_m": {"label": "幕墙总宽", "kind": "length", "unit": "m", "level": "geometry"},
    "height_m": {"label": "幕墙总高", "kind": "length", "unit": "m", "level": "geometry"},
    "grid_width_m": {"label": "分格宽", "kind": "length", "unit": "m", "level": "grid"},
    "grid_height_m": {"label": "分格高", "kind": "length", "unit": "m", "level": "grid"},
    "floor_height_m": {"label": "层高", "kind": "length", "unit": "m", "level": "rule"},
    "reported_area_m2": {"label": "申报面积", "kind": "area", "unit": "m²", "level": "check"},
    "column_linear_mass": {"label": "立柱线重", "kind": "linear_mass", "unit": "kg/m", "level": "mass",
                           "component": "column", "attr": "linear_mass",
                           "question": "立柱每米多重？请输入线重和单位（kg/m）。"},
    "beam_linear_mass": {"label": "横梁线重", "kind": "linear_mass", "unit": "kg/m", "level": "mass",
                         "component": "beam", "attr": "linear_mass",
                         "question": "横梁每米多重？请输入线重和单位（kg/m）。"},
    "column_unit_price": {"label": "立柱单价", "kind": "price", "unit": "元/kg", "level": "cost",
                          "component": "column", "attr": "unit_price",
                          "question": "立柱型材单价是多少？请注明计价单位（元/kg 或 元/吨），按未税价。"},
    "beam_unit_price": {"label": "横梁单价", "kind": "price", "unit": "元/kg", "level": "cost",
                        "component": "beam", "attr": "unit_price",
                        "question": "横梁型材单价是多少？请注明计价单位（元/kg 或 元/吨），按未税价。"},
    "column_name": {"label": "立柱名称", "kind": "text", "unit": None, "level": "label", "component": "column",
                    "attr": "name"},
    "beam_name": {"label": "横梁名称", "kind": "text", "unit": None, "level": "label", "component": "beam",
                  "attr": "name"},
}
GEOMETRY_USER_FIELDS = ["width_m", "height_m", "grid_width_m", "grid_height_m", "floor_height_m", "reported_area_m2"]
MATERIAL_USER_FIELDS = ["column_linear_mass", "beam_linear_mass", "column_unit_price", "beam_unit_price",
                        "column_name", "beam_name"]
