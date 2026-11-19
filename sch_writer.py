"""sch_writer.py — 把设计 JSON 写成可打开的 KiCad 原理图。

两条关键设计决定
----------------
1) **连接用网络标签（label），不画长导线。**
   光闹钟设计里 GND 一个网络就有 31 个节点，实体导线必然乱成一团。做法是：
   每个引脚引出一小段短线（stub），短线末端放一个同名 label。KiCad 按 label
   名字合并网络，于是 128 个节点能保持原理图可读。

2) **坐标变换要处理 Y 轴翻转。**
   符号库坐标系 Y 向上，原理图画布 Y 向下，所以：
       schematic_x = sx + rx
       schematic_y = sy - ry     其中 (rx,ry) 是旋转后的库坐标
   旋转按标准逆时针：
       rx = px*cosθ - py*sinθ
       ry = px*sinθ + py*cosθ

引脚"朝外"方向
---------------
库中 `pin position.angle` 指向**从连接点走向符号体**的方向，所以朝外是它的反向。
向外引 stub 才能让 label 不压在符号图形上。

    outward_lib = (-cos a, -sin a)       a = pin.angle
    outward_sch = rotate(outward_lib, θ) 后再翻转 Y

用法
----
    from sch_writer import build_schematic

    sch = build_schematic(design, ref_angles={"U2": 0})
    sch.to_file("out/light_alarm.kicad_sch")
"""
from __future__ import annotations

import math
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any

from kiutils.items.common import Effects, Font, Position, Property
from kiutils.items.schitems import Connection, LocalLabel, NoConnect
from kiutils.schematic import Schematic, SchematicSymbol
from kiutils.schematic import Symbol as SchSymbol
from kiutils.schematic import SymbolProjectInstance, SymbolProjectPath

from symbol_lib import load_symbol, symbol_pins

GRID = 1.27          # 原理图网格（mm），KiCad 默认
STUB_LEN = 2.54      # 引脚外引导线长度
SYMBOL_GAP = 12.7    # 同排器件间距
ROW_GAP = 25.4       # 不同功能块之间的行距
MARGIN_LEFT = 25.4
MARGIN_TOP = 25.4

#: KiCad 6 文件版本号，与 kiutils 输出的语法配套（详见 README 第 7 节）
SCH_VERSION = 20211014

#: 可用图纸尺寸（横向，单位 mm），用于自动选纸
_PAPER_SIZES = (
    ("A4", 297.0, 210.0),
    ("A3", 420.0, 297.0),
    ("A2", 594.0, 420.0),
    ("A1", 841.0, 594.0),
    ("A0", 1189.0, 841.0),
)
PAGE_MARGIN = 10.0


def choose_paper(max_x: float, max_y: float) -> str:
    """挑能装下内容的最小图纸。48 个器件的板子 A4 肯定不够。"""
    for name, width, height in _PAPER_SIZES:
        if max_x + PAGE_MARGIN <= width and max_y + PAGE_MARGIN <= height:
            return name
    return "A0"


def new_uuid() -> str:
    return str(_uuid.uuid4())


def snap(value: float, grid: float = GRID) -> float:
    """吸附到网格，避免 ERC 的 endpoint_off_grid 警告。"""
    return round(value / grid) * grid


# ---------------------------------------------------------------- 几何

def rotate_point(px: float, py: float, angle: int) -> tuple[float, float]:
    """按逆时针 angle 度旋转（库坐标系，Y 向上）。"""
    if angle % 360 == 0:
        return px, py
    a = math.radians(angle % 360)
    ca, sa = math.cos(a), math.sin(a)
    return px * ca - py * sa, px * sa + py * ca


def to_schematic(px: float, py: float, sx: float, sy: float, angle: int) -> tuple[float, float]:
    """库坐标 -> 原理图画布坐标（含 Y 轴翻转）。"""
    rx, ry = rotate_point(px, py, angle)
    return sx + rx, sy - ry


def outward_direction(pin_angle: float, sym_angle: int) -> tuple[float, float]:
    """引脚朝外的单位方向（原理图画布坐标系，Y 向下）。"""
    a = math.radians(pin_angle)
    dx, dy = -math.cos(a), -math.sin(a)     # 库中朝外 = 库角度的反向
    rx, ry = rotate_point(dx, dy, sym_angle)
    return rx, -ry                           # Y 翻转


@dataclass
class PinPlacement:
    number: str
    name: str
    x: float                 # 引脚连接点（画布坐标）
    y: float
    dx: float                # 朝外单位方向
    dy: float

    @property
    def stub_end(self) -> tuple[float, float]:
        return self.x + self.dx * STUB_LEN, self.y + self.dy * STUB_LEN


@dataclass
class PlacedSymbol:
    ref: str
    lib: str
    symbol_name: str
    value: str
    footprint: str
    x: float
    y: float
    angle: int
    pins: dict[str, PinPlacement] = field(default_factory=dict)


# ---------------------------------------------------------------- 元件放置

def _rotated_pin_extent(sym, angle: int) -> tuple[float, float, float, float]:
    """返回旋转后引脚坐标的 (minx, maxx, miny, maxy)。"""
    xs: list[float] = []
    ys: list[float] = []
    for unit in sym.units:
        for pin in unit.pins:
            rx, ry = rotate_point(pin.position.X, pin.position.Y, angle)
            xs.append(rx)
            ys.append(ry)
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), max(xs), min(ys), max(ys)


def _make_property(key: str, value: str, x: float, y: float, hide: bool = False) -> Property:
    return Property(
        key=key,
        value=value,
        id=None,
        position=Position(X=x, Y=y, angle=0),
        effects=Effects(font=Font(height=1.27, width=1.27), hide=hide),
        showName=False,
    )


def _to_sch_symbol(src, lib_name: str) -> SchSymbol:
    """库符号 -> 原理图内嵌 lib_symbols 条目（字段逐项拷贝）。"""
    return SchSymbol(
        libraryNickname=lib_name,
        entryName=src.entryName,
        unitId=src.unitId,
        styleId=src.styleId,
        extends=None,                 # 已展平，不再引用父符号
        hidePinNumbers=src.hidePinNumbers,
        pinNames=src.pinNames,
        pinNamesHide=src.pinNamesHide,
        pinNamesOffset=src.pinNamesOffset,
        inBom=src.inBom,
        onBoard=src.onBoard,
        isPower=src.isPower,
        properties=src.properties,
        graphicItems=src.graphicItems,
        pins=src.pins,
        units=src.units,
    )


def place_symbol(lib: str, symbol_name: str, ref: str, value: str, footprint: str,
                 x: float, y: float, angle: int, sch: Schematic, project: str) -> PlacedSymbol:
    sym = load_symbol(lib, symbol_name)

    placed = PlacedSymbol(
        ref=ref, lib=lib, symbol_name=symbol_name, value=value,
        footprint=footprint, x=x, y=y, angle=angle,
    )

    for unit in sym.units:
        for pin in unit.pins:
            num = str(pin.number)
            px, py = to_schematic(pin.position.X, pin.position.Y, x, y, angle)
            dx, dy = outward_direction(pin.position.angle, angle)
            # 同一个引脚可能同时出现在 unit 0 与具体 unit 中，以后者为准
            placed.pins[num] = PinPlacement(num, pin.name, px, py, dx, dy)

    sch.schematicSymbols.append(SchematicSymbol(
        libraryNickname=lib,
        entryName=symbol_name,
        libName=None,
        position=Position(X=x, Y=y, angle=angle),
        unit=1,
        inBom=True,
        onBoard=True,
        dnp=False,
        fieldsAutoplaced=False,
        uuid=new_uuid(),
        properties=[
            _make_property("Reference", ref, x + 2.54, y - 3.81),
            _make_property("Value", value, x + 2.54, y + 3.81),
            _make_property("Footprint", footprint, x, y, hide=True),
            _make_property("Datasheet", "~", x, y, hide=True),
        ],
        pins={num: new_uuid() for num in placed.pins},
        instances=[SymbolProjectInstance(
            name=project,
            paths=[SymbolProjectPath(
                sheetInstancePath=f"/{sch.uuid}", reference=ref, unit=1,
            )],
        )],
    ))
    return placed


# ---------------------------------------------------------------- 网络连接

def attach_stub_and_label(sch: Schematic, net_name: str, pin: PinPlacement,
                          endpoint_owner: dict[tuple[float, float], str],
                          warnings: list[str]) -> None:
    """从一个引脚引出 stub，并在末端放同名 label —— 这就是「连接」。"""
    ex, ey = pin.stub_end
    ex, ey = snap(ex), snap(ey)
    key = (ex, ey)
    if key in endpoint_owner and endpoint_owner[key] != net_name:
        warnings.append(
            f"坐标冲突: {pin.number} 所属网络 {net_name} 与 "
            f"{endpoint_owner[key]} 的 stub 端点重合于 ({ex}, {ey})，可能误短接"
        )
    endpoint_owner[key] = net_name

    sch.graphicalItems.append(Connection(
        type="wire",
        points=[Position(X=pin.x, Y=pin.y, angle=0),
                Position(X=ex, Y=ey, angle=0)],
        uuid=new_uuid(),
    ))
    sch.labels.append(LocalLabel(
        text=net_name,
        position=Position(X=ex, Y=ey, angle=0),
        effects=Effects(font=Font(height=1.27, width=1.27)),
        uuid=new_uuid(),
    ))


# ---------------------------------------------------------------- 主构建

def build_schematic(design: dict[str, Any], *, project: str = "kicad-agent") -> tuple[Schematic, list[str]]:
    """把设计 JSON 构建成 Schematic 对象，返回 (原理图, 问题列表)。"""
    warnings: list[str] = []

    bom = {item["ref"]: item for item in design["bom"]}
    angles = design.get("symbol_angles") or {}

    blocks = design.get("blocks") or {"all": list(bom)}
    ordered: list[str] = []
    for refs in blocks.values():
        ordered.extend(r for r in refs if r in bom)
    ordered.extend(r for r in bom if r not in ordered)   # 兜底：未分块的器件

    sch = Schematic()
    sch.uuid = new_uuid()
    sch.version = SCH_VERSION

    placed: dict[str, PlacedSymbol] = {}
    emitted_libs: set[str] = set()

    cursor_x = MARGIN_LEFT
    row_top = MARGIN_TOP
    row_height = 0.0

    for ref in ordered:
        item = bom[ref]
        angle = int(angles.get(ref, 0))
        sym = load_symbol(item["lib"], item["symbol"])

        # 同一 lib:symbol 只内嵌一次
        lib_key = f'{item["lib"]}:{item["symbol"]}'
        if lib_key not in emitted_libs:
            sch.libSymbols.append(_to_sch_symbol(sym, item["lib"]))
            emitted_libs.add(lib_key)

        minx, maxx, miny, maxy = _rotated_pin_extent(sym, angle)
        width = maxx - minx
        height = maxy - miny

        if cursor_x + width > MARGIN_LEFT + 380:      # 换行
            cursor_x = MARGIN_LEFT
            row_top += row_height + ROW_GAP
            row_height = 0.0

        sx = snap(cursor_x - minx)
        sy = snap(row_top + maxy)                     # 使引脚 bbox 顶边对齐 row_top

        placed[ref] = place_symbol(
            item["lib"], item["symbol"], ref, item.get("value", ""),
            item.get("footprint", ""), sx, sy, angle, sch, project,
        )

        cursor_x += width + SYMBOL_GAP
        row_height = max(row_height, height)

    # ---- 网络：每引脚一根 stub + 一个同名 label ----
    endpoint_owner: dict[tuple[float, float], str] = {}
    for net in design["nets"]:
        net_name = net["name"]
        for node in net["nodes"]:
            ref, pin_num = node.rsplit(".", 1)
            if ref not in placed:
                warnings.append(f"网络 {net_name}: 未知器件 {ref}")
                continue
            pin = placed[ref].pins.get(pin_num)
            if pin is None:
                warnings.append(f"网络 {net_name}: {ref} 无引脚 {pin_num}")
                continue
            attach_stub_and_label(sch, net_name, pin, endpoint_owner, warnings)

    # ---- ERC 电源标志 ----
    # +5V / +3V3 / GND 上挂着电源输入脚，但没有任何「电源输出」去驱动它们，
    # ERC 会报 power_pin_not_driven。标准解法是给这些网络各加一个 PWR_FLAG。
    declared_nets = {n["name"] for n in design["nets"]}
    # 只给「有电源输入脚、却没有电源输出脚」的网络加标志。
    # 注意 +3V3 不能加：U1 的 VO 已是 Power output，再加会报 pin_to_pin。
    flag_nets = design.get("erc_power_flags", ["+5V", "GND", "VBAT"])
    flag_y = snap(row_top + row_height + ROW_GAP * 2)
    flag_idx = 0
    for net_name in flag_nets:
        if net_name not in declared_nets:
            warnings.append(f"erc_power_flags: 设计中不存在网络 {net_name}，已跳过")
            continue
        flag_idx += 1
        if "power:PWR_FLAG" not in emitted_libs:
            sch.libSymbols.append(_to_sch_symbol(load_symbol("power", "PWR_FLAG"), "power"))
            emitted_libs.add("power:PWR_FLAG")
        flag = place_symbol(
            "power", "PWR_FLAG", f"#FLG{flag_idx:02d}", "PWR_FLAG", "",
            snap(MARGIN_LEFT + (flag_idx - 1) * SYMBOL_GAP * 2), flag_y, 0, sch, project,
        )
        attach_stub_and_label(sch, net_name, flag.pins["1"], endpoint_owner, warnings)

    # ---- 悬空引脚标记 ----
    for node in design.get("no_connect", []):
        ref, pin_num = node.rsplit(".", 1)
        if ref not in placed or pin_num not in placed[ref].pins:
            warnings.append(f"no_connect: 无效节点 {node}")
            continue
        pin = placed[ref].pins[pin_num]
        sch.noConnects.append(NoConnect(
            position=Position(X=pin.x, Y=pin.y, angle=0),
            uuid=new_uuid(),
        ))

    # ---- 自动选纸：内容不能跑到图纸外面 ----
    if sch.labels:
        max_x = max(lab.position.X for lab in sch.labels)
        max_y = max(lab.position.Y for lab in sch.labels)
        sch.paper.paperSize = choose_paper(max_x, max_y)

    return sch, warnings
