"""blocks.py — 预验证电路积木（Pre-verified circuit blocks）

架构来源与致谢
==============
**本模块的「积木」设计思想借鉴自 CircuitAgent 项目，不是我们的原创。**

    CircuitAgent · Community Edition
    https://pypi.org/project/circuit-agent-client/0.1.3/
    许可证：MIT
    作者：sora（社区版）

其项目说明原文（中文）：

    让大语言模型直接生成底层走线、焊盘与封装，几乎必然产出非法几何或引脚短路。
    CircuitAgent 的做法是把 LLM 的输出约束在预验证的电路积木上，再用 Pydantic
    契约把它变成确定性网表 —— 模型只负责"选积木"，不负责"画线"。

其积木清单中与我们直接对应的一项：

    block_led | 状态指示灯 | 限流电阻 + 颜色/阻值可参数化

**借鉴范围声明**：我们只借鉴其「用预验证子电路约束模型输出」的设计思想，
未复制其任何源代码。积木的具体实现（元件选型、位号分配、网络命名）
由本项目依据 KiCad 符号库自行编写。

为什么要这么做
==============
实测证据：本项目两次让真实模型（qwen-plus）生成流水灯电路，
模型两次都把 LED 与限流电阻接错，且形态完全一致：

    GPIO ── 电阻 ── GND          （电阻白接）
    LED 阴极(K) ── GND，阳极(A) 悬空

成因是模型缺少「限流电阻必须与 LED 串联」这条拓扑知识 —— 这是**概念性错误**，
靠重试回灌错误信息无法纠正（已实测三次重试仍复现）。

积木的作用：把这段拓扑用代码写死，模型只需要说「这里要一个由 U1.10 驱动的
红色 LED」，展开出的连接必然正确。

用法
====
模型在步骤 03 输出 `circuit_blocks` 字段：

    {
      "circuit_blocks": [
        {"type": "led", "gpio": "U1.10", "color": "Red", "resistor": "220"}
      ]
    }

`expand_circuit_blocks()` 会把它展开成具体元件与网络：

    R1  220Ω
    D1  Red
    nets:
      [U1.10, R1.1]      GPIO -> 电阻
      [R1.2, D1.2]       电阻 -> LED 阳极(A)
      [D1.1, GND]        LED 阴极(K) -> 地
"""
from __future__ import annotations

from typing import Any, Callable

from schema import BomItem, BomStage, ConnectionStage, Net

#: 电阻与 LED 的默认封装（与项目其它位置保持一致）
_FOOTPRINT_R = "Resistor_SMD:R_0603_1608Metric"
_FOOTPRINT_LED = "LED_SMD:LED_0805_2012Metric"

#: 积木清单：给提示词与运行时校验共用同一份定义
BLOCK_CATALOG: dict[str, dict[str, Any]] = {
    "led": {
        "description": "由某个 GPIO 驱动的单颗 LED，含限流电阻，阴极接地",
        "params": {
            "gpio": "驱动该 LED 的引脚节点，形如 \"U1.10\"（必须是 BOM 中存在的引脚）",
            "color": "LED 颜色，如 Red / Green / Blue / Yellow，默认 Red",
            "resistor": "限流电阻值，如 \"220\"（单位 Ω），默认 220",
        },
        "topology": [
            "[gpio, R.1]         GPIO 接限流电阻",
            "[R.2, LED.2]        电阻接 LED 阳极 A",
            "[LED.1, GND]        LED 阴极 K 接地",
        ],
    },
}


def block_catalog_text() -> str:
    """渲染成提示词里给模型看的积木说明。"""
    lines: list[str] = []
    for name, spec in BLOCK_CATALOG.items():
        lines.append(f"### `{name}` — {spec['description']}")
        lines.append("")
        lines.append("参数：")
        for param, desc in spec["params"].items():
            lines.append(f"- `{param}`：{desc}")
        lines.append("")
        lines.append("展开后的连接（**由程序自动生成，你不需要写这些 nets**）：")
        lines.append("```")
        for rule in spec["topology"]:
            lines.append(rule)
        lines.append("```")
        lines.append("")
        lines.append("调用示例：")
        lines.append("```json")
        lines.append('{"type": "led", "gpio": "U1.10", "color": "Red", "resistor": "220"}')
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 内部工具

def _next_ref(used: set[str], prefix: str) -> str:
    """分配一个未被占用的位号，如 R1 / R2。"""
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    ref = f"{prefix}{n}"
    used.add(ref)
    return ref


def _merge_nets(existing: list[dict], extra: list[dict]) -> list[dict]:
    """按网络名合并，并对同一网络内的重复节点去重（保持顺序）。"""
    order: list[str] = []
    buckets: dict[str, list[str]] = {}
    for net in [*existing, *extra]:
        name = net["name"]
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        for node in net["nodes"]:
            if node not in buckets[name]:
                buckets[name].append(node)
    return [{"name": name, "nodes": buckets[name]} for name in order]


# ---------------------------------------------------------------- 各积木展开

def _block_led(call: dict[str, Any], used: set[str]) -> tuple[list[dict], list[dict], str]:
    gpio = call.get("gpio")
    if not gpio or "." not in str(gpio):
        raise ValueError(
            f"led 积木必须给出 gpio 参数（形如 \"U1.10\"），收到 {gpio!r}"
        )
    color = str(call.get("color") or "Red")
    resistor = str(call.get("resistor") or "220")

    r_ref = _next_ref(used, "R")
    d_ref = _next_ref(used, "D")

    components = [
        {
            "ref": r_ref,
            "lib": "Device",
            "symbol": "R",
            "value": resistor,
            "footprint": _FOOTPRINT_R,
        },
        {
            "ref": d_ref,
            "lib": "Device",
            "symbol": "LED",
            "value": color,
            "footprint": _FOOTPRINT_LED,
        },
    ]

    nets = [
        {"name": f"LED_{d_ref}_GPIO", "nodes": [str(gpio), f"{r_ref}.1"]},
        {"name": f"LED_{d_ref}_A", "nodes": [f"{r_ref}.2", f"{d_ref}.2"]},
        {"name": "GND", "nodes": [f"{d_ref}.1"]},
    ]
    return components, nets, "led"


_HANDLERS: dict[str, Callable[[dict[str, Any], set[str]], tuple[list[dict], list[dict], str]]] = {
    "led": _block_led,
}


# ---------------------------------------------------------------- 对外入口

def expand_circuit_blocks(
    bom: BomStage, conn: ConnectionStage
) -> tuple[BomStage, ConnectionStage]:
    """把 `circuit_blocks` 展开成具体元件与网络，返回新的 (BOM, 连接)。

    不修改传入对象。若 BOM 里没有积木调用，原样返回。
    """
    calls = getattr(conn, "circuit_blocks", None) or []
    if not calls:
        return bom, conn

    used = {item.ref for item in bom.bom}
    new_components: list[dict] = []
    extra_nets: list[dict] = []
    layout_groups: dict[str, list[str]] = {}

    for index, call in enumerate(calls, start=1):
        if not isinstance(call, dict):
            raise ValueError(f"circuit_blocks[{index}] 必须是对象，收到 {type(call).__name__}")
        kind = call.get("type")
        handler = _HANDLERS.get(str(kind))
        if handler is None:
            raise ValueError(
                f"未知积木类型 {kind!r}，可用类型：{sorted(_HANDLERS)}。"
                "请不要自己画积木内部的连线，改用积木。"
            )
        comps, nets, group = handler(call, used)
        new_components.extend(comps)
        extra_nets.extend(nets)
        layout_groups.setdefault(group, []).extend(c["ref"] for c in comps)

    # 合并 BOM 与分块信息
    new_bom = bom.model_copy(deep=True)
    new_bom.bom = [*new_bom.bom, *[BomItem(**c) for c in new_components]]
    blocks = dict(new_bom.blocks)
    for group, refs in layout_groups.items():
        blocks[group] = [*blocks.get(group, []), *refs]
    new_bom.blocks = blocks

    # 合并网络。
    # 注意用 model_construct 跳过「网络至少 2 个引脚」的校验：
    # 例如模型没给 GND 时，积木展开出的 GND 只有 1 个节点。
    # 这种缺陷应该由 check_design 统一报告（并触发重试），
    # 而不是在这里抛 pydantic 异常、丢失上下文。
    merged = _merge_nets([n.model_dump() for n in conn.nets], extra_nets)
    new_conn = conn.model_copy(deep=True)
    new_conn.nets = [Net.model_construct(name=n["name"], nodes=n["nodes"]) for n in merged]
    return new_bom, new_conn
