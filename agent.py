"""agent.py — 五步闭环的命令行入口。

    python agent.py design "帮我设计一块光闹钟，到点控制LED灯组发光，亮度线性增长..."
    python agent.py revise designs/light_alarm.json "把 LED 限流电阻从 82Ω 换成 100Ω"
    python agent.py check  designs/light_alarm.json

对应作业基础任务的五个步骤：

    01 环境搭建         kicad_env.py / catalog.py  -- 自检
    02 自然语言 -> BOM   prompts/bom.md
    03 连接关系生成      prompts/connections.md（注入真实引脚表）
    04 JSON -> 原理图    generate.py
    05 交互修正          prompts/revise.md

反馈闭环体现在两层：
  - 模型输出不合规（JSON 坏了 / 违反 schema / 用了白名单外的符号）
    -> 把具体错误回灌给模型，重新请求，最多 MAX_RETRIES 次
  - 原理图生成后 -> 导出网表 -> 连通性分区比对，不一致就是硬失败
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from blocks import expand_circuit_blocks
from catalog import allowed_pairs
from generate import main as generate_main
from pipeline import (build_bom_request, build_connection_request,
                      build_revise_request, extract_json, parse_bom,
                      parse_connections)
from providers import make_provider
from schema import BomStage, Design, ConnectionStage, merge_stages
from validate_design import check_design, format_feedback

MAX_RETRIES = 2


# ---------------------------------------------------------------- 反馈闭环

def call_with_retry(provider, step: str, system: str, user: str, parser):
    """调一次模型，解析失败就把错误回灌重试。"""
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        raw = provider.complete(system, user, step=step)
        try:
            return parser(raw)
        except Exception as exc:  # noqa: BLE001 - 任何不合规都要能回灌
            last_error = exc
            # 保留模型原始输出，重试失败时这是唯一的排查依据
            try:
                Path("out").mkdir(exist_ok=True)
                Path(f"out/last_raw_{step}.txt").write_text(raw, encoding="utf-8")
            except OSError:
                pass
            print(f"  [重试 {attempt + 1}/{MAX_RETRIES}] {step} 输出不合规：" f"{type(exc).__name__}: {str(exc)[:200]}",
                  file=sys.stderr)
            user = (
                f"{user}\n\n# 上一次输出无效\n\n"
                f"错误信息：\n{exc}\n\n"
                "请修正上述问题，重新只输出合法 JSON。"
            )
    raise SystemExit(f"{step} 重试 {MAX_RETRIES} 次后仍失败：{last_error}")


def parse_bom_checked(text: str) -> BomStage:
    """解析 BOM，并检查所有符号都在白名单内。"""
    bom = parse_bom(text)
    allowed = allowed_pairs()
    bad = [f"{i.ref}({i.lib}:{i.symbol})" for i in bom.bom
           if (i.lib, i.symbol) not in allowed]
    if bad:
        raise ValueError(
            f"使用了白名单外的符号: {bad}。"
            "请只从「可用符号」表中选取，库名和符号名都要逐字一致。"
        )
    return bom


def make_connection_parser(bom: BomStage):
    """构造步骤 03 的解析器：解析 -> 合并 -> 电气校验，不合格就抛错触发重试。

    这是「把校验提前」的关键。以前 validate_design 跑在生成原理图之后，
    模型永远看不到自己的电气错误（例如四颗 LED 阳极全部悬空），
    于是一份根本不能工作的设计也能一路跑到出图。
    """

    def parse_and_check(text: str):
        conn = parse_connections(text)
        # 先把积木展开成真实元件与网络，再校验 —— 校验的是「最终电路」
        bom_expanded, conn_expanded = expand_circuit_blocks(bom, conn)
        design = merge_stages(bom_expanded, conn_expanded)
        errors, _warnings, _stats = check_design(
            design.model_dump(exclude_none=True), strict=True
        )
        if errors:
            raise ValueError(
                "电气校验未通过，请修正后重新输出：\n" + format_feedback(errors)
            )
        return bom_expanded, conn_expanded

    return parse_and_check


def parse_design(text: str) -> Design:
    """步骤 05 用：解析完整设计，并做同样的电气校验。"""
    design = Design.model_validate(json.loads(extract_json(text)))
    errors, _warnings, _stats = check_design(
        design.model_dump(exclude_none=True), strict=True
    )
    if errors:
        raise ValueError(
            "电气校验未通过，请修正后重新输出：\n" + format_feedback(errors)
        )
    return design


# ---------------------------------------------------------------- 各步骤

def _finish(design: Design, save_path: Path, sch_path: Path | None) -> int:
    """保存设计 JSON -> 校验 -> 生成原理图 -> 网表比对。"""
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_text(
        json.dumps(design.model_dump(exclude_none=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n设计已保存: {save_path}")

    print("\n===== 04 结构校验 =====")
    from validate_design import validate as validate_design
    rc = validate_design(str(save_path))
    if rc != 0:
        print("结构校验未通过，停止。", file=sys.stderr)
        return rc

    sch_path = sch_path or save_path.with_suffix(".kicad_sch")
    print("\n===== 04 生成原理图 + 网表比对 =====")
    return generate_main(["generate", str(save_path), str(sch_path)])


def cmd_design(args: argparse.Namespace) -> int:
    provider = make_provider(args.provider)
    requirement = " ".join(args.requirement)
    print(f"提供方: {provider.name}")
    print(f"需求  : {requirement}\n")

    print("===== 02 自然语言 -> BOM =====")
    sys_p, usr_p = build_bom_request(requirement)
    bom = call_with_retry(provider, "bom", sys_p, usr_p, parse_bom_checked)
    print(f"得到 {len(bom.bom)} 个器件：")
    for item in bom.bom:
        print(f"  {item.ref:5s} {item.lib}:{item.symbol:22s} {item.value}")

    print("\n===== 03 BOM + 真实引脚表 -> 连接关系 =====")
    sys_c, usr_c = build_connection_request(requirement, bom)
    print(f"（提示词长度 {len(usr_c)} 字符，含 {len(bom.bom)} 个器件的真实引脚表）")
    bom_final, conn = call_with_retry(provider, "connections", sys_c, usr_c,
                                      make_connection_parser(bom))
    print(f"得到 {len(conn.nets)} 个网络、{len(conn.no_connect)} 个悬空引脚、"
          f"{len(conn.erc_power_flags)} 个电源标志网络")
    if conn.circuit_blocks:
        kinds = ", ".join(str(b.get("type")) for b in conn.circuit_blocks)
        print(f"使用了 {len(conn.circuit_blocks)} 个电路积木：{kinds}")

    design = merge_stages(bom_final, conn)
    save = Path(args.save) if args.save else Path("out/design.json")
    return _finish(design, save, Path(args.sch) if args.sch else None)


def cmd_revise(args: argparse.Namespace) -> int:
    provider = make_provider(args.provider)
    current_path = Path(args.design)
    current = current_path.read_text(encoding="utf-8")
    print(f"提供方: {provider.name}")
    print(f"原设计: {current_path}")
    print(f"修改  : {args.instruction}\n")

    print("===== 05 交互修正 =====")
    sys_r, usr_r = build_revise_request(current, args.instruction)
    design = call_with_retry(provider, "revise", sys_r, usr_r, parse_design)

    save = Path(args.save) if args.save else current_path.with_name(
        current_path.stem + "_revised.json"
    )
    return _finish(design, save, Path(args.sch) if args.sch else None)


def cmd_check(args: argparse.Namespace) -> int:
    from validate_design import validate as validate_design
    return validate_design(args.design)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="AI Agent 电路板设计 —— 五步闭环命令行",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_design = sub.add_parser("design", help="自然语言 -> BOM -> 连接 -> 原理图")
    p_design.add_argument("requirement", nargs="+", help="自然语言需求（可带空格，用引号括起来）")
    p_design.add_argument("--provider", default="auto", choices=["auto", "fixture", "openai"])
    p_design.add_argument("--save", help="设计 JSON 输出路径")
    p_design.add_argument("--sch", help="原理图输出路径")
    p_design.set_defaults(func=cmd_design)

    p_revise = sub.add_parser("revise", help="按要求修改已有设计")
    p_revise.add_argument("design", help="现有设计 JSON 路径")
    p_revise.add_argument("instruction", help="修改要求，如「把电容换成 100nF」")
    p_revise.add_argument("--provider", default="auto", choices=["auto", "fixture", "openai"])
    p_revise.add_argument("--save", help="输出路径")
    p_revise.add_argument("--sch", help="原理图输出路径")
    p_revise.set_defaults(func=cmd_revise)

    p_check = sub.add_parser("check", help="只做结构校验")
    p_check.add_argument("design")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if args.command == "design":
        args.requirement = args.requirement
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
