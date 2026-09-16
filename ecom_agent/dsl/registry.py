"""输出模型的注册与解析。

YAML 里写的是字符串 `pdd.ProductRowList`，而 `Agent(output_model_schema=...)`
需要的是真正的类对象。这个模块负责把前者变成后者，并且【带上版本号】。

★ 为什么要版本化输出模型，而不是直接用最新的：
  库里已经落了一批按 v1 解析的数据。某天给 ProductRow 加一个必填字段变成 v2，
  如果用"永远解析到最新版"，那么拿 v1 跑过的历史 run 在回放时会用 v2 的规则去解析，
  而 v2 多出来的那个必填字段在 v1 的数据里根本不存在 —— 回放会报错，
  或者更糟：静默按默认值填上，于是历史数据看起来"当时就有这个字段"。
  版本号让"当时用的是什么"成为可追溯的事实。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# (name, version) -> 模型类
_REGISTRY: dict[tuple[str, int], type[BaseModel]] = {}


def register_output_model(name: str, version: int = 1):
    """把一个 pydantic 模型注册为可被 YAML 引用的输出模型。

    用法：
        @register_output_model("pdd.ProductRowList", version=1)
        class ProductRowList(BaseModel): ...
    """

    def deco(cls: type[BaseModel]) -> type[BaseModel]:
        key = (name, version)
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"输出模型 {name!r} v{version} 已被 {_REGISTRY[key]!r} 注册，不允许覆盖")
        _REGISTRY[key] = cls
        return cls

    return deco


def _ensure_site_models_imported() -> None:
    """★ 导入站点模型包，触发注册。

    为什么需要这个函数、而不能指望"反正别处会 import"：
      注册是【导入期副作用】。如果没有任何地方 import 过 sites.pinduoduo.output_models，
      那么 _REGISTRY 就是空的，resolve("pdd.ProductRowList") 会说"未注册"——
      而模型明明定义在那里，报错信息会把人引向"是不是名字写错了"。
      显式导入把这件事变成确定的，代价是一次 import。

    ★ 用 importlib 而不是顶层 import：顶层 import 会让 dsl 包在导入时就依赖
      sites 包，而 sites 包将来可能反过来依赖 dsl（比如站点自带的编译钩子），
      那就成了循环导入。放在函数里，导入时机可控。
    """
    import importlib
    import pkgutil

    import ecom_agent.sites as sites_pkg

    for m in pkgutil.walk_packages(sites_pkg.__path__, prefix="ecom_agent.sites."):
        if m.name.endswith(".output_models"):
            importlib.import_module(m.name)


def resolve_output_model(spec: str, version: int = 1) -> type[BaseModel]:
    """把 "pdd.ProductRowList" + 版本号解析成模型类。"""
    _ensure_site_models_imported()
    key = (spec, version)
    if key not in _REGISTRY:
        available = sorted(f"{n}@v{v}" for n, v in _REGISTRY)
        raise KeyError(
            f"未注册的输出模型 {spec!r} v{version}。已注册：{available or '（空）'}。"
            "如果你刚定义了新模型，确认它在一个名为 output_models.py 的模块里 —— "
            "注册表只扫描这类模块。"
        )
    return _REGISTRY[key]


def registered() -> dict[str, int]:
    """{名字: 最高版本}，给 /api/templates 之类的地方展示用。"""
    _ensure_site_models_imported()
    out: dict[str, int] = {}
    for (name, ver) in _REGISTRY:
        out[name] = max(out.get(name, 0), ver)
    return out


def _clear_for_tests() -> None:
    """仅测试用：清空注册表。

    ★ 存在的理由：注册表是模块级全局状态，测试之间会互相污染。
      没有这个钩子的话，"重复注册要报错"这类测试只能跑一次。
    """
    _REGISTRY.clear()
