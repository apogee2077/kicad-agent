"""validate_design.py — 机械校验一份设计 JSON，别靠人眼看。

检查项
------
1. 结构：必需字段齐全、ref / 网络名不重复
2. 符号：BOM 里每个 lib:symbol 都能在 KiCad 库里加载出引脚
3. 引脚存在性：每个 `REF.PIN` 里的 PIN 必须真实存在于该符号
4. 短路：同一个引脚不能出现在两个不同网络里
5. 冲突：标了 no_connect 的引脚不能又接进网络
6. 漏接：每个器件引脚要么接入网络，要么显式标 no_connect

用法
----
    python validate_design.py designs/light_alarm.json
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from symbol_lib import load_symbol, symbol_pins


def split_node(node: str) -> tuple[str, str]:
    """'U2.33' -> ('U2', '33')；器件号里允许有下划线但不应有小数点。"""
    if "." not in node:
        raise ValueError(f"节点必须写成 REF.PIN 形式，收到 {node!r}")
    ref, pin = node.rsplit(".", 1)
    return ref, pin


def validate(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []

    for key in ("bom", "nets"):
        if key not in data:
            print(f"致命: 缺少必需字段 {key!r}", file=sys.stderr)
            return 2

    items = data["bom"]
    refs = [item.get("ref", "") for item in items]
    for ref, count in Counter(refs).items():
        if count > 1:
            errors.append(f"器件号重复: {ref} 出现 {count} 次")
    bom = {item["ref"]: item for item in items if item.get("ref")}

    # ---- 加载符号，拿到真实引脚表 ----
    pin_table: dict[str, dict[str, str]] = {}
    for ref, item in bom.items():
        lib, sym_name = item.get("lib"), item.get("symbol")
        if not lib or not sym_name:
            errors.append(f"{ref}: 缺少 lib / symbol 字段")
            continue
        try:
            sym = load_symbol(lib, sym_name)
        except Exception as exc:  # noqa: BLE001 - 要把库错误汇总成报告
            errors.append(f"{ref}: 加载符号 {lib}:{sym_name} 失败 -> {exc}")
            continue
        pins = symbol_pins(sym)
        if not pins:
            errors.append(f"{ref}: 符号 {lib}:{sym_name} 展平后仍无引脚")
        pin_table[ref] = pins

    # ---- 网络检查 ----
    pin_owner: dict[tuple[str, str], str] = {}
    net_names = [net.get("name", "") for net in data["nets"]]
    for name, count in Counter(net_names).items():
        if count > 1:
            errors.append(f"网络名重复: {name} 出现 {count} 次")

    total_nodes = 0
    for net in data["nets"]:
        net_name = net.get("name", "<未命名>")
        nodes = net.get("nodes") or []
        if len(nodes) < 2:
            warnings.append(f"网络 {net_name} 只有 {len(nodes)} 个节点（单点网络）")
        for node in nodes:
            total_nodes += 1
            try:
                ref, pin = split_node(node)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if ref not in pin_table:
                errors.append(f"网络 {net_name}: 未知器件 {ref}")
                continue
            if pin not in pin_table[ref]:
                errors.append(
                    f"网络 {net_name}: {ref} 没有引脚 {pin}"
                    f"（该符号引脚: {sorted(pin_table[ref])[:12]}）"
                )
                continue
            key = (ref, pin)
            if key in pin_owner and pin_owner[key] != net_name:
                errors.append(
                    f"短路: {ref}.{pin} 同时出现在网络 {pin_owner[key]} 和 {net_name}"
                )
            pin_owner[key] = net_name

    # ---- no_connect 检查 ----
    nc_set: set[tuple[str, str]] = set()
    for node in data.get("no_connect", []):
        try:
            ref, pin = split_node(node)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if ref not in pin_table:
            errors.append(f"no_connect: 未知器件 {ref}")
            continue
        if pin not in pin_table[ref]:
            errors.append(f"no_connect: {ref} 没有引脚 {pin}")
            continue
        nc_set.add((ref, pin))
        if (ref, pin) in pin_owner:
            errors.append(f"冲突: {node} 既标 no_connect 又接入网络 {pin_owner[(ref, pin)]}")

    # ---- 漏接检查 ----
    for ref, pins in pin_table.items():
        for pin, pin_name in pins.items():
            if (ref, pin) in pin_owner or (ref, pin) in nc_set:
                continue
            warnings.append(f"未处理: {ref}.{pin} ({pin_name}) 既未连接也未标 no_connect")

    # ---- 汇报 ----
    print(f"文件      : {path}")
    print(f"器件      : {len(bom)}")
    print(f"网络      : {len(data['nets'])}")
    print(f"网络节点  : {total_nodes}")
    print(f"no_connect: {len(nc_set)}")

    blocks = data.get("blocks") or {}
    if blocks:
        print("分块      : " + "  ".join(f"{k}={len(v)}" for k, v in blocks.items()))

    if warnings:
        print(f"\n警告 {len(warnings)} 条：")
        for w in warnings:
            print(f"  ! {w}")
    if errors:
        print(f"\n错误 {len(errors)} 条：", file=sys.stderr)
        for e in errors:
            print(f"  x {e}", file=sys.stderr)
        return 1
    print("\n校验通过：无错误")
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "designs/light_alarm.json"
    sys.exit(validate(target))
