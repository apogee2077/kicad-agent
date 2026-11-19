"""
spike_gen_sch.py — 可行性验证（Spike）
目标: 证明「结构化描述 -> 真实 .kicad_sch 文件 -> kicad-cli 可导出网表」这条链路走得通。

背景: KiCad 官方只提供 pcbnew（PCB 侧）Python API，没有 eeschema 模块，
      因此原理图只能通过直接生成 .kicad_sch（S-表达式）来做，kiutils 负责读写。

本脚本刻意从零构建，不依赖任何手工画的工程文件。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import uuid as _uuid
from pathlib import Path

from kiutils.items.common import Effects, Font, Property, Position
from kiutils.schematic import Connection, Schematic, SchematicSymbol
from kiutils.schematic import Symbol as SchSymbol
from kiutils.schematic import SymbolProjectInstance, SymbolProjectPath
from kiutils.symbol import SymbolLib

from kicad_env import kicad_cli, kicad_symbols
from sexpr import child, children, parse_file, value_of

OUT_DIR = Path(__file__).parent / "out"
OUT_SCH = OUT_DIR / "blinky.kicad_sch"

#: 预期的网络连接（元件引用, 引脚号），用于生成后自动校验
EXPECTED_NETS = {
    frozenset({("BT1", "1"), ("R1", "1")}),   # 电池正极 -> 电阻
    frozenset({("BT1", "2"), ("D1", "1")}),   # 电池负极 -> LED 阴极
    frozenset({("D1", "2"), ("R1", "2")}),    # 电阻 -> LED 阳极
}


def read_nets(net_path: Path) -> list[tuple[str | None, frozenset]]:
    """用 kicad-cli 导出的网表反推「每个网络连了哪些引脚」。

    网表是嵌套 S-表达式（`nets` 直接挂在 `export` 下），按行 grep 拿不到东西，
    所以交给 sexpr 解析。
    """
    tree = parse_file(str(net_path))
    nets_node = child(tree, "nets")
    if nets_node is None:
        return []
    result = []
    for net in children(nets_node, "net"):
        nodes = frozenset(
            (value_of(n, "ref"), value_of(n, "pin"))
            for n in children(net, "node")
        )
        result.append((value_of(net, "name"), nodes))
    return result


def new_uuid() -> str:
    return str(_uuid.uuid4())


def to_sch_symbol(src) -> SchSymbol:
    """把符号库里的 Symbol 转成原理图内嵌的 lib_symbols 条目。

    两个类字段完全一致（已核对），因此逐字段拷贝即可。
    """
    dst = SchSymbol(
        libraryNickname="Device",
        entryName=src.entryName,
        unitId=src.unitId,
        styleId=src.styleId,
        extends=src.extends,
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
    return dst


def collect_pins(sym_def, unit: int = 1) -> dict[str, object]:
    """收集某单元用到的引脚（unitId 0 是所有单元共用部分）。"""
    pins: dict[str, object] = {}
    for u in sym_def.units:
        if u.unitId in (0, unit):
            for p in u.pins:
                pins[p.number] = p
    return pins


def pin_point(sym_def, inst_x: float, inst_y: float, number: str, unit: int = 1,
              angle: int = 0) -> tuple[float, float]:
    """算引脚在原理图画布上的坐标。

    KiCad 符号库的 Y 轴与原理图画布相反，因此取负。
    旋转暂只支持 0 度（角度换算是后续迭代项）。
    """
    assert angle == 0, "spike 阶段只支持 0 度放置"
    pin = collect_pins(sym_def, unit)[number]
    return inst_x + pin.position.X, inst_y - pin.position.Y


def make_property(key: str, value: str, x: float, y: float, pid: int,
                  hide: bool = False) -> Property:
    return Property(
        key=key,
        value=value,
        id=None,
        position=Position(X=x, Y=y, angle=0),
        effects=Effects(font=Font(height=1.27, width=1.27), hide=hide),
        showName=False,
    )


def place_symbol(lib, sch: Schematic, lib_id: str, ref: str, value: str,
                 footprint: str, x: float, y: float, unit: int = 1) -> SchematicSymbol:
    """放置一个元件实例，并把它的符号定义挂进 lib_symbols。

    注意: instances 必须是 SymbolProjectInstance（带 project 包裹 + 根 uuid 路径），
    塞 SymbolInstance 会生成非法结构，KiCad 直接拒绝加载。
    """
    sym_def = next(s for s in lib.symbols if s.entryName == lib_id)

    # 同一符号只挂一次定义
    if not any(s.entryName == lib_id for s in sch.libSymbols):
        sch.libSymbols.append(to_sch_symbol(sym_def))

    props = [
        make_property("Reference", ref, x + 2.54, y - 3.81, 0),
        make_property("Value", value, x + 2.54, y + 3.81, 1),
        make_property("Footprint", footprint, x, y, 2, hide=True),
        make_property("Datasheet", "~", x, y, 3, hide=True),
    ]

    sym = SchematicSymbol(
        libraryNickname="Device",
        entryName=lib_id,
        libName=None,
        position=Position(X=x, Y=y, angle=0),
        unit=unit,
        inBom=True,
        onBoard=True,
        dnp=False,
        fieldsAutoplaced=False,
        uuid=new_uuid(),
        properties=props,
        pins={num: new_uuid() for num in collect_pins(sym_def, unit)},
        instances=[SymbolProjectInstance(
            name="kicad-agent",
            paths=[SymbolProjectPath(
                sheetInstancePath=f"/{sch.uuid}", reference=ref, unit=unit,
            )],
        )],
    )
    sch.schematicSymbols.append(sym)
    return sym


def wire(sch: Schematic, x1: float, y1: float, x2: float, y2: float) -> None:
    sch.graphicalItems.append(
        Connection(
            type="wire",
            points=[Position(X=x1, Y=y1, angle=0), Position(X=x2, Y=y2, angle=0)],
            uuid=new_uuid(),
        )
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # KiCad 安装位置自动探测（可用 KICAD_ROOT 覆盖），避免把路径写死
    cli = kicad_cli()
    device_lib = kicad_symbols() / "Device.kicad_sym"
    if not device_lib.is_file():
        print(f"找不到符号库文件: {device_lib}", file=sys.stderr)
        return 2
    print(f"KiCad CLI : {cli}")
    print(f"符号库    : {device_lib.parent}")
    lib = SymbolLib().from_file(str(device_lib))

    sch = Schematic()
    sch.uuid = new_uuid()
    # kiutils 输出的是 KiCad 6 语法（hide / (id N)），版本号必须与语法一致，
    # 否则 KiCad 10 解析器直接拒绝加载。用 20211014，KiCad 打开时会自动升级格式。
    sch.version = 20211014

    # ---- 电路: 电池 —— 电阻 —— LED ----
    BAT = (100.0, 100.0)
    RES = (140.0, 100.0)
    LED = (180.0, 100.0)

    place_symbol(lib, sch, "Battery_Cell", "BT1", "3V", "Battery:BatteryHolder_Keystone_3034", *BAT)
    place_symbol(lib, sch, "R", "R1", "330", "Resistor_SMD:R_0805_2012Metric", *RES)
    place_symbol(lib, sch, "LED", "D1", "RED", "LED_SMD:LED_0805_2012Metric", *LED)

    lib_syms = {s.entryName: s for s in lib.symbols}
    bt_p = {n: pin_point(lib_syms["Battery_Cell"], *BAT, n) for n in ("1", "2")}
    r_p = {n: pin_point(lib_syms["R"], *RES, n) for n in ("1", "2")}
    led_p = {n: pin_point(lib_syms["LED"], *LED, n) for n in ("1", "2")}

    print("引脚坐标:")
    print("  BT1", bt_p)
    print("  R1 ", r_p)
    print("  D1 ", led_p)

    # BT1+ -> R1.1
    wire(sch, *bt_p["1"], *r_p["1"])
    # R1.2 -> D1.A
    wire(sch, *r_p["2"], *led_p["2"])
    # D1.K -> BT1-
    x0, y0 = led_p["1"]
    x1, y1 = bt_p["2"]
    wire(sch, x0, y0, x1, y0)
    wire(sch, x1, y0, x1, y1)

    sch.to_file(str(OUT_SCH))
    print(f"\n已写出: {OUT_SCH}  ({OUT_SCH.stat().st_size} 字节)")

    # ---- 用 kicad-cli 验证: 导出网表 ----
    net = OUT_DIR / "blinky.net"
    r = subprocess.run(
        [str(cli), "sch", "export", "netlist", "--format", "kicadsexpr",
         "-o", str(net), str(OUT_SCH)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print("\n[kicad-cli 导出网表]")
    print("  returncode =", r.returncode)
    if r.stdout.strip():
        print("  stdout:", r.stdout.strip()[:1500])
    if r.stderr.strip():
        print("  stderr:", r.stderr.strip()[:1500])

    if r.returncode != 0 or not net.exists():
        print("网表导出失败，无法继续校验", file=sys.stderr)
        return r.returncode or 1

    nets = read_nets(net)
    print(f"\n网表已生成: {net.name}（{net.stat().st_size} 字节，{len(nets)} 个网络）")
    for name, nodes in sorted(nets, key=lambda item: item[0] or ""):
        pins = ", ".join(f"{ref}.{pin}" for ref, pin in sorted(nodes))
        print(f"  {name:<18} {pins}")

    # 连通性自动校验：生成出文件不算数，得确认连的是对的
    actual = {nodes for _, nodes in nets}
    if actual == EXPECTED_NETS:
        print("\n连通性校验: PASS —— 3 个网络与预期完全一致")
        return 0

    print("\n连通性校验: FAIL", file=sys.stderr)
    for missing in EXPECTED_NETS - actual:
        print("  缺少网络:", ", ".join(sorted(f"{a}.{b}" for a, b in missing)), file=sys.stderr)
    for extra in actual - EXPECTED_NETS:
        print("  多余网络:", ", ".join(sorted(f"{a}.{b}" for a, b in extra)), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
