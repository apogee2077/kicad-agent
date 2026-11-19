"""catalog.py — 允许 LLM 使用的符号白名单。

为什么需要白名单
----------------
无限自由地让模型挑符号，它会给你编一个 `Device:LED_White_5mm` 出来——
KiCad 库里根本没有，生成阶段必然失败。所以把**已验证存在**的符号列成清单
塞进提示词，模型只能在这个集合里选。这是「工具调用」里最基础的一种约束：
把外部世界的真实能力告诉模型，而不是让它凭记忆猜。

新增器件时，先确认库里有、再用 `python -c "from catalog import validate_catalog"` 验一遍。
"""
from __future__ import annotations

#: (库名, 符号名, 用途说明)
CATALOG: list[tuple[str, str, str]] = [
    # ---- 无源器件 ----
    ("Device", "R", "电阻"),
    ("Device", "C", "电容"),
    ("Device", "Polyfuse", "自恢复保险丝"),
    # ---- 分立半导体 ----
    ("Device", "LED", "发光二极管（引脚 1=K 阴极，2=A 阳极）"),
    ("Device", "D_Schottky", "肖特基二极管（引脚 1=K 阴极，2=A 阳极）"),
    ("Transistor_FET", "AO3400A", "N 沟道 MOSFET，3A（引脚 1=G 2=S 3=D）"),
    # ---- 电源 ----
    ("Regulator_Linear", "AMS1117-3.3", "3.3V/1A LDO（引脚 1=GND 2=VO 3=VI）"),
    ("Device", "Battery_Cell", "单节电池（引脚 1=+ 2=-）"),
    # ---- 主控与时钟 ----
    ("RF_Module", "ESP32-WROOM-32", "ESP32 无线模组，39 脚"),
    ("Timer_RTC", "DS3231M", "高精度 I2C 实时时钟，带硬件闹钟，SO-16"),
    # ---- 人机交互 ----
    ("Switch", "SW_Push", "轻触按键"),
    ("Connector_Generic", "Conn_01x02", "2 针排针"),
    ("Connector_Generic", "Conn_01x04", "4 针排针"),
    ("Connector_Generic", "Conn_01x06", "6 针排针"),
    # ---- 声光 ----
    ("Device", "Buzzer", "有源蜂鸣器（引脚 1=+ 2=-）"),
]


def catalog_text() -> str:
    """渲染成提示词里给模型看的清单。"""
    lines = ["| 库名 | 符号名 | 说明 |", "| --- | --- | --- |"]
    for lib, sym, desc in CATALOG:
        lines.append(f"| `{lib}` | `{sym}` | {desc} |")
    return "\n".join(lines)


def allowed_pairs() -> set[tuple[str, str]]:
    return {(lib, sym) for lib, sym, _ in CATALOG}


def validate_catalog() -> int:
    """自检：清单里每个符号都能在 KiCad 库里加载出来（防止白名单本身是错的）。"""
    from symbol_lib import load_symbol, symbol_pins

    bad = 0
    for lib, sym, _ in CATALOG:
        try:
            pins = symbol_pins(load_symbol(lib, sym))
        except Exception as exc:  # noqa: BLE001
            print(f"x {lib}:{sym} -> {exc}")
            bad += 1
            continue
        print(f"ok {lib}:{sym}  ({len(pins)} 脚)")
    print(f"\n共 {len(CATALOG)} 个符号，失败 {bad} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(validate_catalog())
