"""menu.py — 项目操作菜单（双击 run.bat 就会运行它）

为什么要这个文件
----------------
把命令行的东西包装成「选数字」的操作，这样不需要记命令也能用。
你也可以继续用命令行，两种方式等价。

每一项目录下方都标注了它等价的命令行，方便你在报告或演示视频里说明。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PY = ROOT / ".venv" / "Scripts" / "python.exe"

# 统一用 UTF-8 输出：Windows 控制台默认是 cp936(GBK)，
# 与批处理里 chcp 65001 配合，避免中文变乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _env() -> dict[str, str]:
    """准备子进程环境：确保能找到 KiCad，并统一编码。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if not env.get("KICAD_ROOT"):
        # 1) 用户环境变量 2) 常见安装位置
        for cand in (r"D:\apps new\KiCad\10.0", r"C:\Program Files\KiCad\10.0"):
            if Path(cand, "bin", "kicad-cli.exe").is_file():
                env["KICAD_ROOT"] = cand
                break
    return env


def run(args: list[str]) -> int:
    """运行一个子命令，输出直接显示在窗口里。"""
    print()
    print("-" * 62)
    try:
        result = subprocess.run([str(PY), *args], cwd=ROOT, env=_env())
        return result.returncode
    except FileNotFoundError:
        print(f"找不到 Python 解释器：{PY}")
        print("请先在项目目录执行：python -m venv .venv")
        return 1
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


def need_python() -> bool:
    if not PY.is_file():
        print(f"\n[!] 缺少虚拟环境：{PY}")
        print("    在项目目录打开终端，执行：python -m venv .venv")
        print("    然后：.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return True
    return False


# ---------------------------------------------------------------- 各功能

def do_env_check() -> None:
    run(["kicad_env.py"])


def do_tool_selftest() -> None:
    run(["try_tools.py", "--selftest"])


def do_design() -> None:
    print()
    print("请输入你的需求（一句话描述你要做什么电路），例如：")
    print("  设计一块 5V 供电的指示灯板，3 颗 LED 并联，带限流电阻和电源接口")
    req = input("\n需求: ").strip()
    if not req:
        print("没有输入，已取消。")
        return
    print("\n正在调用模型，可能需要 1~3 分钟，请稍候……")
    run(["agent.py", "design", req, "--provider", "openai"])
    print("\n产物在 out/ 目录：design.json、design.kicad_sch")


def do_revise() -> None:
    print()
    default = "designs/light_alarm.json"
    path = input(f"要修改哪个设计文件？[回车使用 {default}]: ").strip() or default
    instruction = input("修改要求（如：把 LED 全换成红色）: ").strip()
    if not instruction:
        print("没有输入修改要求，已取消。")
        return
    print("\n正在调用模型……")
    run(["agent.py", "revise", path, instruction, "--provider", "openai"])


def do_check() -> None:
    print()
    default = "designs/light_alarm.json"
    path = input(f"要校验哪个设计文件？[回车使用 {default}]: ").strip() or default
    run(["agent.py", "check", path])


def do_open_samples() -> None:
    target = ROOT / "samples"
    print(f"\n正在打开目录：{target}")
    print("里面是已经生成好的原理图，可以直接用 KiCad 打开看效果。")
    try:
        os.startfile(target)      # 仅 Windows
    except Exception as exc:      # noqa: BLE001
        print(f"打开失败（{exc}），你可以手动打开这个路径。")


MENU = [
    ("1", "环境自检（检查 KiCad 是否装好）        [无需密钥]", do_env_check),
    ("2", "工具自检（检查三个工具函数是否正常）    [无需密钥]", do_tool_selftest),
    ("3", "一句话生成电路板                      [需要密钥]", do_design),
    ("4", "修改已有设计（如把 LED 换红色）        [需要密钥]", do_revise),
    ("5", "校验设计文件（引脚/短路/漏接）          [无需密钥]", do_check),
    ("6", "打开 samples 目录看现成的原理图        [无需密钥]", do_open_samples),
]


def show_menu() -> None:
    print()
    print("=" * 62)
    print("  kicad-agent 操作菜单")
    print("=" * 62)
    for key, text, _ in MENU:
        print(f"  {key}. {text}")
    print("  0. 退出")
    print("=" * 62)


def main() -> int:
    if "--list" in sys.argv:          # 非交互自检用
        show_menu()
        return 0

    if need_python():
        return 1

    while True:
        show_menu()
        choice = input("请输入编号: ").strip()
        if choice in ("0", "q", ""):
            print("再见。")
            return 0
        handler = next((h for k, _, h in MENU if k == choice), None)
        if handler is None:
            print("没有这个编号，请重新输入。")
            continue
        try:
            handler()
        except KeyboardInterrupt:
            print("\n已中断。")
        except Exception as exc:      # noqa: BLE001
            print(f"\n出错了：{type(exc).__name__}: {exc}")
        input("\n按回车返回菜单……")


if __name__ == "__main__":
    sys.exit(main())
