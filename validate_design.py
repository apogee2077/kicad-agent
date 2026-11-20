"""validate_design.py — 机械校验一份设计，别靠人眼看。

检查项
------
1. 结构：必需字段齐全、ref / 网络名不重复
2. 符号：BOM 里每个 lib:symbol 都能在 KiCad 库里加载出引脚
3. 引脚存在性：每个 `REF.PIN` 里的 PIN 必须真实存在于该符号
4. 短路：同一个引脚不能出现在两个不同网络里
5. 冲突：标了 no_connect 的引脚不能又接进网络
6. 漏接：每个器件引脚要么接入网络，要么显式标 no_connect
   —— **严格模式下这是错误，不是警告**（v2 变更）

为什么「漏接」必须算错误
------------------------
早期版本把它当警告，结果模型产出的一份设计里四颗 LED 的阳极全悬空、
整板没有稳压器，却一路跑到生成原理图。**电气上根本不成立的设计，
不应该算"通过"。**

用法
----
    # 命令行：校验文件
    python validate_design.py designs/light_alarm.json

    # 代码：校验内存里的设计（供重试闭环使用）
    from validate_design import check_design
    errors, warnings, stats = check_design(data, strict=True)
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from symbol_lib import load_symbol, symbol_pins

#: 回灌给模型时最多列出的错误条数，避免提示词爆炸
MAX_FEEDBACK_ERRORS = 15


def split_node(node: str) -> tuple[str, str]:
    """'U2.33' -> ('U2', '33')；器件号里允许有下划线但不应有小数点。"""
    if "." not in node:
        raise ValueError(f"节点必须写成 REF.PIN 形式，收到 {node!r}")
    ref, pin = node.rsplit(".", 1)
    return ref, pin


def check_design(data: dict, *, strict: bool = True) -> tuple[list[str], list[str], dict]:
    """校验一份设计字典。

    返回 (errors, warnings, stats)。

    strict=True 时，「既未连接也未标 no_connect」的引脚算错误。
    """
    errors: list[str] = []
    warnings: list[str] = []

    if "bom" not in data or "nets" not in data:
        return ["缺少必需字段 bom / nets"], warnings, {}

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
    nets = data["nets"]
    net_names = [net.get("name", "") for net in nets]
    for name, count in Counter(net_names).items():
        if count > 1:
            errors.append(f"网络名重复: {name} 出现 {count} 次")

    total_nodes = 0
    for net in nets:
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

    # ---- 漏接检查：严格模式下算错误 ----
    dangling: list[str] = []
    for ref, pins in pin_table.items():
        for pin, pin_name in pins.items():
            if (ref, pin) in pin_owner or (ref, pin) in nc_set:
                continue
            dangling.append(f"未处理: {ref}.{pin}（{pin_name}）既未连接也未标 no_connect")
    if strict:
        errors.extend(dangling)
    else:
        warnings.extend(dangling)

    stats = {
        "components": len(bom),
        "nets": len(nets),
        "nodes": total_nodes,
        "no_connect": len(nc_set),
        "blocks": data.get("blocks") or {},
        "dangling": len(dangling),
    }
    return errors, warnings, stats


def format_feedback(errors: list[str], limit: int = MAX_FEEDBACK_ERRORS) -> str:
    """把错误列表整理成适合回灌给模型的文本。"""
    shown = errors[:limit]
    text = "\n".join(f"- {e}" for e in shown)
    if len(errors) > limit:
        text += f"\n- …… 还有 {len(errors) - limit} 条同类问题，请整体修正。"
    return text


def validate(path: str, *, strict: bool = True) -> int:
    """命令行入口：读文件 -> 校验 -> 打印报告。返回 0 通过 / 1 有错误。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"不是合法 JSON: {exc}", file=sys.stderr)
        return 2

    errors, warnings, stats = check_design(data, strict=strict)

    print(f"文件      : {path}")
    print(f"器件      : {stats.get('components', 0)}")
    print(f"网络      : {stats.get('nets', 0)}")
    print(f"网络节点  : {stats.get('nodes', 0)}")
    print(f"no_connect: {stats.get('no_connect', 0)}")
    blocks = stats.get("blocks") or {}
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
    allow_warn = "--lenient" in sys.argv
    sys.exit(validate(target, strict=not allow_warn))
