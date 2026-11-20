"""tests/test_blocks.py — 积木展开的单元测试

思路借鉴 CircuitAgent：它的每个积木都有单测，强制校验「位号唯一、
每个元件都有封装、网络端点必须指向已声明元件、每个积木都必须接 GND」。

运行（无需 pytest）：
    .venv\\Scripts\\python.exe tests/test_blocks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blocks import expand_circuit_blocks  # noqa: E402
from schema import BomStage, ConnectionStage  # noqa: E402


def make_bom() -> BomStage:
    return BomStage(
        bom=[
            {"ref": "U1", "lib": "RF_Module", "symbol": "ESP32-WROOM-32",
             "value": "ESP32", "footprint": "RF_Module:ESP32-WROOM-32"},
            {"ref": "J1", "lib": "Connector_Generic", "symbol": "Conn_01x02",
             "value": "Power", "footprint": "Connector:PinHeader"},
        ],
        blocks={"mcu": ["U1"], "power": ["J1"]},
    )


def make_conn(blocks: list[dict]) -> ConnectionStage:
    return ConnectionStage(
        nets=[
            {"name": "GND", "nodes": ["J1.2", "U1.1"]},
            {"name": "+5V", "nodes": ["J1.1", "U1.2"]},
        ],
        circuit_blocks=blocks,
        no_connect=[],
        erc_power_flags=["+5V", "GND"],
    )


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        raise SystemExit(1)


def test_led_expansion() -> None:
    print("led 积木展开：")
    bom, conn = expand_circuit_blocks(
        make_bom(), make_conn([{"type": "led", "gpio": "U1.10", "color": "Red", "resistor": "220"}])
    )

    # 元件
    check("新增 2 个元件", len(bom.bom) == 4, f"实际 {len(bom.bom)}")
    new = [i for i in bom.bom if i.ref not in {"U1", "J1"}]
    check("位号是 R1 与 D1", {i.ref for i in new} == {"R1", "D1"}, str([i.ref for i in new]))
    check("电阻 220Ω", any(i.symbol == "R" and i.value == "220" for i in new))
    check("LED 为 Red", any(i.symbol == "LED" and i.value == "Red" for i in new))
    check("每个新元件都有封装", all(i.footprint for i in new))

    # 网络
    nets = {n.name: list(n.nodes) for n in conn.nets}
    check("GPIO 接电阻", nets.get("LED_D1_GPIO") == ["U1.10", "R1.1"], str(nets.get("LED_D1_GPIO")))
    check("电阻接 LED 阳极(2 脚)", nets.get("LED_D1_A") == ["R1.2", "D1.2"], str(nets.get("LED_D1_A")))

    # 关键：LED 阴极必须进 GND，且不能重复
    check("LED 阴极进 GND", "D1.1" in nets.get("GND", []), str(nets.get("GND")))
    check("GND 无重复节点", len(nets["GND"]) == len(set(nets["GND"])), str(nets["GND"]))

    # 分块信息
    check("分块记录了 led", bom.blocks.get("led") == ["R1", "D1"], str(bom.blocks.get("led")))


def test_multiple_leds_unique_refs() -> None:
    print("多颗 LED 位号不冲突：")
    blocks = [
        {"type": "led", "gpio": "U1.10"},
        {"type": "led", "gpio": "U1.11"},
        {"type": "led", "gpio": "U1.12"},
    ]
    bom, conn = expand_circuit_blocks(make_bom(), make_conn(blocks))
    refs = [i.ref for i in bom.bom]
    check("位号唯一", len(refs) == len(set(refs)), str(refs))
    leds = [i.ref for i in bom.bom if i.symbol == "LED"]
    check("三颗 LED", len(leds) == 3, str(leds))
    nets = {n.name: list(n.nodes) for n in conn.nets}
    check("三颗 LED 阴极都在 GND", all(f"{d}.1" in nets["GND"] for d in leds), str(nets["GND"]))
    check("GND 无重复节点", len(nets["GND"]) == len(set(nets["GND"])), str(nets["GND"]))


def test_errors() -> None:
    print("异常输入：")
    cases = [
        ([{"type": "led"}], "缺少 gpio"),
        ([{"type": "unknown_block", "gpio": "U1.10"}], "未知积木类型"),
    ]
    for bad, label in cases:
        try:
            expand_circuit_blocks(make_bom(), make_conn(bad))
        except ValueError:
            check(f"{label} 被拒绝", True)
        else:
            check(f"{label} 应被拒绝", False)


def test_no_blocks_passthrough() -> None:
    print("无积木时原样返回：")
    bom = make_bom()
    conn = make_conn([])
    bom2, conn2 = expand_circuit_blocks(bom, conn)
    check("BOM 未变", len(bom2.bom) == len(bom.bom))
    check("网络未变", len(conn2.nets) == len(conn.nets))


if __name__ == "__main__":
    test_led_expansion()
    test_multiple_leds_unique_refs()
    test_errors()
    test_no_blocks_passthrough()
    print("\n全部通过")
