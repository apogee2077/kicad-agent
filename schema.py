"""schema.py — 设计 JSON 的数据契约（pydantic）。

为什么要分两段
--------------
作业把生成拆成两步，这里如实对应：

    BomStage         步骤 02：自然语言 -> 元器件清单
    ConnectionStage  步骤 03：元器件清单 -> 引脚级连接

两段合并后才是完整的 Design，再交给 validate_design.py 与 generate.py。

这么做不只是为了对齐作业：分成两段后，第 03 步的提示词里能塞进**真实引脚表**
（从 KiCad 符号库查出来的），模型就没有编造引脚号的空间了。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

#: 位号规则：字母开头 + 数字，例如 U1 / R12 / SW3
_REF_RE = re.compile(r"^[A-Z]{1,3}\d+$")
#: 网络节点规则：REF.PIN，例如 U2.33
_NODE_RE = re.compile(r"^[A-Z]{1,3}\d+\.[A-Za-z0-9_]+$")


class BomItem(BaseModel):
    """一个元器件。"""

    ref: str = Field(description="位号，如 U1 / R3 / D5")
    lib: str = Field(description="KiCad 符号库名，如 Device、Regulator_Linear")
    symbol: str = Field(description="库内符号名，如 R、AMS1117-3.3")
    value: str = Field(default="", description="参数值，如 10k / 100nF / ESP32-WROOM-32")
    footprint: str = Field(default="", description="封装，如 Resistor_SMD:R_0603_1608Metric")
    rotation: int = Field(default=0, description="原理图旋转角度：0/90/180/270")

    @field_validator("ref")
    @classmethod
    def _check_ref(cls, v: str) -> str:
        if not _REF_RE.match(v):
            raise ValueError(f"位号格式非法: {v!r}（应为字母+数字，如 U1）")
        return v

    @field_validator("rotation")
    @classmethod
    def _check_rotation(cls, v: int) -> int:
        if v % 90 != 0:
            raise ValueError(f"旋转角度必须是 90 的倍数，收到 {v}")
        return v % 360


class Net(BaseModel):
    """一个网络：若干个引脚连在一起。"""

    name: str = Field(description="网络名，电源网络用 +5V / +3V3 / GND")
    nodes: list[str] = Field(description='引脚列表，形如 ["U2.33", "R3.2"]')

    @field_validator("nodes")
    @classmethod
    def _check_nodes(cls, v: list[str]) -> list[str]:
        if len(v) < 2:
            raise ValueError(f"网络至少要有 2 个引脚，收到 {len(v)} 个")
        for node in v:
            if not _NODE_RE.match(node):
                raise ValueError(f"引脚格式非法: {node!r}（应为 REF.PIN，如 U2.33）")
        if len(set(v)) != len(v):
            dup = [n for n in set(v) if v.count(n) > 1]
            raise ValueError(f"网络内引脚重复: {dup}")
        return v


class BomStage(BaseModel):
    """步骤 02 的产出：只要清单，不要连接。"""

    meta: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    bom: list[BomItem]
    blocks: dict[str, list[str]] = Field(
        default_factory=dict,
        description="按功能分块，键是块名，值是位号列表（用于原理图排版）",
    )

    @field_validator("bom")
    @classmethod
    def _check_unique_refs(cls, v: list[BomItem]) -> list[BomItem]:
        if not v:
            raise ValueError("BOM 不能为空")
        refs = [i.ref for i in v]
        dup = sorted({r for r in refs if refs.count(r) > 1})
        if dup:
            raise ValueError(f"位号重复: {dup}")
        return v

    @model_validator(mode="after")
    def _check_blocks(self):
        known = {i.ref for i in self.bom}
        for block, refs in self.blocks.items():
            unknown = [r for r in refs if r not in known]
            if unknown:
                raise ValueError(f"分块 {block} 引用了不存在的位号: {unknown}")
        return self


class ConnectionStage(BaseModel):
    """步骤 03 的产出：只要连接，不要清单。"""

    nets: list[Net]
    no_connect: list[str] = Field(default_factory=list, description="悬空引脚")
    erc_power_flags: list[str] = Field(
        default_factory=list,
        description="需要加 PWR_FLAG 的网络（有电源输入脚却没有电源输出驱动的网络）",
    )

    @field_validator("no_connect")
    @classmethod
    def _check_nc(cls, v: list[str]) -> list[str]:
        for node in v:
            if not _NODE_RE.match(node):
                raise ValueError(f"no_connect 格式非法: {node!r}")
        return v

    @model_validator(mode="after")
    def _check_conflicts(self):
        used: dict[str, str] = {}
        for net in self.nets:
            for node in net.nodes:
                if node in used:
                    raise ValueError(f"引脚 {node} 同时接在 {used[node]} 和 {net.name}")
                used[node] = net.name
        clash = sorted(set(self.no_connect) & set(used))
        if clash:
            raise ValueError(f"引脚既标了 no_connect 又接了网络: {clash}")
        names = [n.name for n in self.nets]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"网络名重复: {dup}")
        return self


class Design(BomStage, ConnectionStage):
    """完整设计 = 清单 + 连接。"""

    @model_validator(mode="after")
    def _check_refs_exist(self):
        known = {i.ref for i in self.bom}
        bad = []
        for net in self.nets:
            for node in net.nodes:
                ref = node.rsplit(".", 1)[0]
                if ref not in known:
                    bad.append(f"{net.name}:{node}")
        if bad:
            raise ValueError(f"网络里引用了 BOM 中不存在的器件: {bad[:8]}")
        return self


def merge_stages(bom: BomStage, conn: ConnectionStage) -> Design:
    """把两步的产出合并成完整设计。"""
    return Design(
        meta=bom.meta,
        params=bom.params,
        bom=bom.bom,
        blocks=bom.blocks,
        nets=conn.nets,
        no_connect=conn.no_connect,
        erc_power_flags=conn.erc_power_flags,
    )
