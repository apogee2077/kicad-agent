"""generate.py — 设计 JSON → 原理图 → 网表 → 连通性比对。

为什么要比对
------------
生成器最容易犯的错是「画出来了但没连上」：旋转角度算错一点，引脚坐标就偏
半个网格，导线接不到引脚，KiCad 不会报错，只是那个引脚变成悬空。肉眼很难
发现，网表里却清清楚楚。

所以这里不看图，只看**连通性分区**：把设计里的网络和网表里的网络都表示成
「引脚集合的集合」，两者必须完全相等。

    设计： { {BT1.1, R1.1}, {BT1.2, D1.1}, ... }
    网表： { {BT1.1, R1.1}, {BT1.2, D1.1}, ... }

这样比对与网络**名字**无关，只关心「哪些脚连在一起」——正是我们要验证的东西。

用法
----
    python generate.py designs/light_alarm.json
    python generate.py designs/light_alarm.json out/custom.kicad_sch
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from kicad_env import kicad_cli
from sch_writer import build_schematic
from sexpr import child, children, parse_file, value_of


def design_partition(design: dict) -> set[frozenset[str]]:
    """设计里的连通性：每个网络的引脚集合。"""
    part: set[frozenset[str]] = set()
    for net in design["nets"]:
        nodes = frozenset(net["nodes"])
        if len(nodes) >= 2:
            part.add(nodes)
    return part


def netlist_partition(net_path: Path) -> tuple[set[frozenset[str]], dict[frozenset[str], str]]:
    """从 kicad-cli 导出的网表重建连通性，并记下网络名（便于报错）。"""
    tree = parse_file(str(net_path))
    nets_node = child(tree, "nets")
    part: set[frozenset[str]] = set()
    names: dict[frozenset[str], str] = {}
    if nets_node is None:
        return part, names
    for net in children(nets_node, "net"):
        # 以 # 开头的虚器件（如 #FLG01 PWR_FLAG）不属于电路连接，排除掉
        nodes = frozenset(
            f"{value_of(n, 'ref')}.{value_of(n, 'pin')}"
            for n in children(net, "node")
            if not (value_of(n, "ref") or "").startswith("#")
        )
        if len(nodes) >= 2:
            part.add(nodes)
            names[nodes] = value_of(net, "name") or "<unnamed>"
    return part, names


def main(argv: list[str]) -> int:
    design_path = Path(argv[1] if len(argv) > 1 else "designs/light_alarm.json")
    design = json.loads(design_path.read_text(encoding="utf-8"))

    out_path = Path(argv[2]) if len(argv) > 2 else Path("out") / f"{design_path.stem}.kicad_sch"

    sch, warnings = build_schematic(design)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 必须显式 UTF-8：kiutils 的 to_file() 用系统 locale，中文 Windows 是 GBK，
    # 符号属性里含 µ 之类的字符会直接写崩。
    out_path.write_text(sch.to_sexpr(), encoding="utf-8")

    print(f"原理图    : {out_path}  ({out_path.stat().st_size} 字节)")
    print(f"图纸      : {sch.paper.paperSize}")
    print(f"器件      : {len(sch.schematicSymbols)}")
    print(f"内嵌符号  : {len(sch.libSymbols)}")
    print(f"导线 stub : {len(sch.graphicalItems)}")
    print(f"网络标签  : {len(sch.labels)}")
    print(f"no_connect: {len(sch.noConnects)}")

    if warnings:
        print(f"\n生成期问题 {len(warnings)} 条：")
        for w in warnings[:20]:
            print(f"  ! {w}")
        if len(warnings) > 20:
            print(f"  ... 还有 {len(warnings) - 20} 条")

    # ---- 导出网表 ----
    net_path = out_path.with_suffix(".net")
    r = subprocess.run(
        [str(kicad_cli()), "sch", "export", "netlist", "--format", "kicadsexpr",
         "-o", str(net_path), str(out_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0 or not net_path.exists():
        print(f"\n网表导出失败 rc={r.returncode}", file=sys.stderr)
        print((r.stderr or "")[-1500:], file=sys.stderr)
        return 1

    expected = design_partition(design)
    actual, names = netlist_partition(net_path)

    print(f"\n设计网络数(≥2节点): {len(expected)}")
    print(f"网表网络数(≥2节点): {len(actual)}")

    if expected == actual:
        print("\n连通性比对: PASS —— 网表分区与设计完全一致")
        return 0

    print("\n连通性比对: FAIL", file=sys.stderr)
    missing = expected - actual
    extra = actual - expected
    if missing:
        print(f"\n设计有、网表没有（通常是导线没接到引脚）{len(missing)} 个：", file=sys.stderr)
        for nodes in sorted(missing, key=lambda s: sorted(s)):
            print(f"  {sorted(nodes)}", file=sys.stderr)
    if extra:
        print(f"\n网表有、设计没有（可能是意外短接/重复）{len(extra)} 个：", file=sys.stderr)
        for nodes in sorted(extra, key=lambda s: sorted(s)):
            print(f"  {names.get(nodes, '?')}: {sorted(nodes)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
