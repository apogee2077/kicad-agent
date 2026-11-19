"""symbol_lib.py — 读 KiCad 符号库，顺手解决两个坑。

坑一：编码
----------
kiutils 的 `SymbolLib().from_file()` 内部用 `open(path)`，不指定编码，
于是吃系统 locale 默认值。中文 Windows 上是 GBK，遇到含非 ASCII 字符的
库（例如 `RF_Module.kicad_sym` 里有 µ、° 之类）直接：

    UnicodeDecodeError: 'gbk' codec can't decode byte 0xa2

当前 spike 没暴露这个问题，只因为它读的 `Device.kicad_sym` 恰好是纯 ASCII。
-> 我们自己按 UTF-8 读文本，再交给 kiutils 的 `from_sexpr` 解析。

坑二：派生符号
--------------
KiCad 库里大量符号是「派生符号」（`extends` 父符号），引脚和图形都定义在
父符号中，子符号自身 `units` 为空。直接拿去生成原理图会得到 0 个引脚。

实测受影响：`Regulator_Linear:AMS1117-3.3`（extends `AP1117-15`）、
`Transistor_FET:AO3400A`（extends `Q_NMOS_GSD`）。

-> 加载时展平：把父符号的 units / graphicItems / 缺失属性合并进子符号，
并把 `extends` 清空，得到一个自包含的符号。

用法
----
    from symbol_lib import load_symbol, symbol_pins

    sym = load_symbol("Regulator_Linear", "AMS1117-3.3")
    symbol_pins(sym)      # {'1': 'GND', '2': 'VO', '3': 'VI'}
"""
from __future__ import annotations

import copy
from pathlib import Path

from kiutils.symbol import SymbolLib
from kiutils.utils import sexpr

from kicad_env import kicad_symbols

__all__ = ["load_library", "load_symbol", "symbol_pins", "symbol_pins_by_unit"]

#: 派生符号需要从父符号继承的标量属性
_INHERIT_ATTRS = (
    "hidePinNumbers",
    "pinNames",
    "pinNamesHide",
    "pinNamesOffset",
    "inBom",
    "onBoard",
    "isPower",
)

_libraries: dict[str, SymbolLib] = {}


def load_library(lib_name: str) -> SymbolLib:
    """按 UTF-8 读入并缓存一个符号库（绕开 kiutils 的 locale 编码问题）。"""
    if lib_name not in _libraries:
        path = kicad_symbols() / f"{lib_name}.kicad_sym"
        if not path.is_file():
            raise FileNotFoundError(
                f"符号库不存在: {path}（确认 KiCad 安装完整，或设置 KICAD_SYMBOLS_DIR）"
            )
        text = Path(path).read_text(encoding="utf-8")
        _libraries[lib_name] = SymbolLib.from_sexpr(sexpr.parse_sexp(text))
    return _libraries[lib_name]


def _find_raw(lib_name: str, sym_name: str):
    for sym in load_library(lib_name).symbols:
        if sym.entryName == sym_name:
            return sym
    names = [s.entryName for s in load_library(lib_name).symbols]
    raise KeyError(
        f"符号库 {lib_name} 中找不到 {sym_name!r}。"
        f"同库前 10 个可用符号: {names[:10]}"
    )


def load_symbol(lib_name: str, sym_name: str, *, flatten_derived: bool = True):
    """取一个符号，默认把派生符号展平成自包含版本。"""
    sym = _find_raw(lib_name, sym_name)
    if not flatten_derived or not getattr(sym, "extends", None):
        return sym
    return _flatten(sym, lib_name, seen={sym_name})


def _flatten(sym, lib_name: str, seen: set[str]):
    """把 src 的父符号内容合并进来，返回新的独立对象。"""
    parent_name = sym.extends
    if parent_name in seen:
        raise ValueError(f"符号 {sym.entryName} 的 extends 链存在循环: {seen} -> {parent_name}")
    seen = seen | {parent_name}

    parent = _find_raw(lib_name, parent_name)
    if getattr(parent, "extends", None):
        parent = _flatten(parent, lib_name, seen)

    out = copy.deepcopy(sym)
    out.extends = None

    # 引脚与图形：子符号没有就继承父符号的
    if not any(u.pins for u in out.units):
        out.units = copy.deepcopy(parent.units)
        # 关键：嵌套 unit 的名字必须以子符号自身名字开头。
        # 否则 AMS1117-3.3 里会出现 (symbol "AP1117-15_0_1")，
        # KiCad 认不出这个 unit 属于谁，整份原理图直接判「加载失败」。
        for unit in out.units:
            unit.entryName = out.entryName
    if not out.graphicItems:
        out.graphicItems = copy.deepcopy(parent.graphicItems)

    # 标量属性：只补 None 的
    for attr in _INHERIT_ATTRS:
        if getattr(out, attr, None) is None:
            setattr(out, attr, getattr(parent, attr, None))

    # Property：补缺失的键（保留子符号自己的 Value / Footprint 等）
    have = {p.key for p in out.properties}
    for prop in parent.properties:
        if prop.key not in have:
            out.properties.append(copy.deepcopy(prop))

    return out


def symbol_pins(sym) -> dict[str, str]:
    """{引脚号: 引脚名}，跨所有 unit 合并（同号以后出现的为准）。"""
    pins: dict[str, str] = {}
    for unit in sym.units:
        for pin in unit.pins:
            pins[str(pin.number)] = pin.name
    return pins


def symbol_pins_by_unit(sym) -> dict[int, dict[str, str]]:
    """{unitId: {引脚号: 引脚名}}，用于多单元符号（如双运放）。"""
    result: dict[int, dict[str, str]] = {}
    for unit in sym.units:
        result.setdefault(unit.unitId, {}).update(
            {str(p.number): p.name for p in unit.pins}
        )
    return result
