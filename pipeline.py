"""pipeline.py — 提示词渲染、工具查询、结构化解析。

这一层是作业第 3 页说的「三个要素」的落点：

  提示词     prompts/*.md，用 {{占位符}} 注入上下文
  工具调用   从 KiCad 符号库实时查引脚表，作为事实喂给模型
  反馈闭环   JSON 解析 / schema 校验失败时，把错误回灌给模型重试

为什么要把引脚表塞进提示词
--------------------------
模型最容易犯的错是「凭记忆写引脚号」。DS3231 的 GND 是 5~13 脚、
ESP32 的 IO21 是第 33 脚——这些它记不准。与其祈祷它记住，不如把符号库
查出来的真实引脚表明明白白给它，把「记忆题」变成「抄写题」。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from catalog import catalog_text
from schema import BomStage, ConnectionStage
from symbol_lib import load_symbol, symbol_pins

PROMPT_DIR = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------- 提示词

def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"找不到提示词模板: {path}")
    return path.read_text(encoding="utf-8")


def render(template: str, **values: str) -> str:
    """把 {{KEY}} 替换成对应内容。"""
    out = template
    for key, val in values.items():
        out = out.replace(f"{{{{{key}}}}}", val)
    return out


def build_bom_request(requirement: str) -> tuple[str, str]:
    """步骤 02：自然语言 -> BOM。返回 (system, user)。"""
    template = load_prompt("bom")
    user = render(template, REQUIREMENT=requirement.strip(), CATALOG=catalog_text())
    system = "你是资深硬件工程师。你只输出严格合法的 JSON，不输出任何多余文字。"
    return system, user


def pin_tables(bom: BomStage) -> str:
    """从符号库查出每个器件的真实引脚，渲染成 Markdown 表。"""
    blocks: list[str] = []
    for item in bom.bom:
        try:
            pins = symbol_pins(load_symbol(item.lib, item.symbol))
        except Exception as exc:  # noqa: BLE001 - 白名单外的符号要显式报出来
            blocks.append(
                f"### {item.ref}  ({item.lib}:{item.symbol})\n"
                f"**错误：无法从符号库加载该符号（{exc}）**"
            )
            continue
        rows = "，".join(
            f"`{num}`={name}" if name else f"`{num}`"
            for num, name in sorted(pins.items(), key=lambda kv: _pin_sort_key(kv[0]))
        )
        blocks.append(f"### {item.ref}  （{item.lib}:{item.symbol}，{len(pins)} 脚）\n{rows}")
    return "\n\n".join(blocks)


def _pin_sort_key(pin: str):
    """引脚号按数字排序：1,2,10 而不是 1,10,2。"""
    return (0, int(pin)) if pin.isdigit() else (1, pin)


def bom_summary(bom: BomStage) -> str:
    """给 03 步看的清单摘要（含位号、符号、参数）。"""
    lines = ["| 位号 | 符号 | 参数值 |", "| --- | --- | --- |"]
    for item in bom.bom:
        lines.append(f"| {item.ref} | `{item.lib}:{item.symbol}` | {item.value} |")
    return "\n".join(lines)


def build_connection_request(requirement: str, bom: BomStage) -> tuple[str, str]:
    """步骤 03：BOM + 真实引脚表 -> 引脚级连接。"""
    template = load_prompt("connections")
    user = render(
        template,
        REQUIREMENT=requirement.strip(),
        BOM=bom_summary(bom),
        PIN_TABLES=pin_tables(bom),
    )
    system = "你是资深硬件工程师。你只输出严格合法的 JSON，不输出任何多余文字。"
    return system, user


def build_revise_request(design_json: str, instruction: str) -> tuple[str, str]:
    """步骤 05：按追问修改已有设计。"""
    template = load_prompt("revise")
    user = render(template, DESIGN=design_json.strip(), INSTRUCTION=instruction.strip())
    system = "你是资深硬件工程师。你只输出严格合法的 JSON，不输出任何多余文字。"
    return system, user


# ---------------------------------------------------------------- 解析

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def extract_json(text: str) -> str:
    """从模型回答里挖出 JSON：容忍代码围栏和前后废话。"""
    cleaned = _FENCE_RE.sub("", text).strip()
    start = cleaned.find("{")
    if start < 0:
        raise ValueError(f"回答里找不到 JSON 对象，开头是：{text[:120]!r}")
    # 括号配平，找到与首个 { 匹配的 }
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(cleaned[start:], start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:i + 1]
    raise ValueError("JSON 括号未闭合，回答可能被截断了")


def parse_bom(text: str) -> BomStage:
    return BomStage.model_validate(json.loads(extract_json(text)))


def parse_connections(text: str) -> ConnectionStage:
    return ConnectionStage.model_validate(json.loads(extract_json(text)))
