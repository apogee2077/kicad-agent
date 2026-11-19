"""bisect_test.py — 逐层构建，定位是哪一层导致 KiCad 拒绝加载。"""
from __future__ import annotations

import subprocess
import uuid as _uuid
from pathlib import Path

from kiutils.items.common import Effects, Font, Property, Position
from kiutils.schematic import (
    Connection,
    HierarchicalSheetInstance,
    Schematic,
    SchematicSymbol,
    SymbolProjectInstance,
    SymbolProjectPath,
)
from kiutils.schematic import Symbol as SchSymbol
from kiutils.symbol import SymbolLib

from kicad_env import kicad_cli, kicad_symbols

KICAD_CLI = kicad_cli()
DEVICE_LIB = kicad_symbols() / "Device.kicad_sym"
OUT = Path(__file__).parent / "out"
LIB = SymbolLib().from_file(str(DEVICE_LIB))
SYMS = {s.entryName: s for s in LIB.symbols}


def u() -> str:
    return str(_uuid.uuid4())


def to_sch_symbol(src) -> SchSymbol:
    return SchSymbol(
        libraryNickname="Device", entryName=src.entryName, unitId=src.unitId,
        styleId=src.styleId, extends=src.extends, hidePinNumbers=src.hidePinNumbers,
        pinNames=src.pinNames, pinNamesHide=src.pinNamesHide,
        pinNamesOffset=src.pinNamesOffset, inBom=src.inBom, onBoard=src.onBoard,
        isPower=src.isPower, properties=src.properties, graphicItems=src.graphicItems,
        pins=src.pins, units=src.units,
    )


def build(*, use_id: bool, project_wrap: bool, sheet_inst: bool, with_wire: bool) -> Schematic:
    sch = Schematic()
    root = u()
    sch.uuid = root
    sch.version = 20211014
    sch.libSymbols.append(to_sch_symbol(SYMS["R"]))

    props = [
        Property(key="Reference", value="R1", id=0 if use_id else None,
                 position=Position(X=142.54, Y=96.19, angle=0),
                 effects=Effects(font=Font(height=1.27, width=1.27)), showName=False),
        Property(key="Value", value="330", id=1 if use_id else None,
                 position=Position(X=142.54, Y=103.81, angle=0),
                 effects=Effects(font=Font(height=1.27, width=1.27)), showName=False),
        Property(key="Footprint", value="", id=2 if use_id else None,
                 position=Position(X=140.0, Y=100.0, angle=0),
                 effects=Effects(font=Font(height=1.27, width=1.27), hide=True), showName=False),
        Property(key="Datasheet", value="~", id=3 if use_id else None,
                 position=Position(X=140.0, Y=100.0, angle=0),
                 effects=Effects(font=Font(height=1.27, width=1.27), hide=True), showName=False),
    ]

    if project_wrap:
        instances = [SymbolProjectInstance(
            name="kicad-agent",
            paths=[SymbolProjectPath(sheetInstancePath=f"/{root}", reference="R1", unit=1)],
        )]
    else:
        instances = []

    sch.schematicSymbols.append(SchematicSymbol(
        libraryNickname="Device", entryName="R", libName=None,
        position=Position(X=140.0, Y=100.0, angle=0), unit=1,
        inBom=True, onBoard=True, dnp=False, fieldsAutoplaced=False,
        uuid=u(), properties=props, pins={"1": u(), "2": u()},
        instances=instances,
    ))

    if with_wire:
        sch.graphicalItems.append(Connection(
            type="wire",
            points=[Position(X=140.0, Y=96.19, angle=0), Position(X=140.0, Y=90.0, angle=0)],
            uuid=u(),
        ))

    if sheet_inst:
        sch.sheetInstances.append(HierarchicalSheetInstance(instancePath="/", page="1"))

    return sch


CASES = [
    ("A 带id_无project_无sheet", dict(use_id=True,  project_wrap=False, sheet_inst=False, with_wire=False)),
    ("B 无id_无project_无sheet", dict(use_id=False, project_wrap=False, sheet_inst=False, with_wire=False)),
    ("C 无id_有project_无sheet", dict(use_id=False, project_wrap=True,  sheet_inst=False, with_wire=False)),
    ("D 无id_有project_有sheet", dict(use_id=False, project_wrap=True,  sheet_inst=True,  with_wire=False)),
    ("E D+导线",               dict(use_id=False, project_wrap=True,  sheet_inst=True,  with_wire=True)),
]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for i, (label, kw) in enumerate(CASES):
        p = OUT / f"case{i}.kicad_sch"
        try:
            build(**kw).to_file(str(p))
        except Exception as e:  # noqa: BLE001
            print(f"[{label:24s}] 写出异常: {e}")
            continue
        r = subprocess.run(
            [str(KICAD_CLI), "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", str(OUT / f"case{i}.net"), str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        print(f"[{label:24s}] {'OK  ' if r.returncode == 0 else 'FAIL'} rc={r.returncode} {r.stderr.strip()[:50]}")


if __name__ == "__main__":
    main()
