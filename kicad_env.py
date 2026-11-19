"""kicad_env.py — 定位本机 KiCad 安装，避免把安装路径写死在代码里。

为什么需要它
------------
原始 spike 里写死了 `D:\\apps new\\KiCad10.0\\10.0`。这在作者本机没问题，
但课程要求「助教按 README 5 分钟跑起来」——别人机器上 KiCad 装在别处，
脚本就直接崩。所以安装位置必须自动探测，或由环境变量显式指定。

解析优先级（高 → 低）
--------------------
1. 环境变量 `KICAD_ROOT`       —— 显式指定，覆盖一切
2. PATH 上的 `kicad-cli`       —— Windows 安装器默认会加进 PATH
3. 各平台常见安装目录           —— Program Files / /Applications / /usr

符号库另有 `KICAD_SYMBOLS_DIR` 可单独覆盖。

背景备注
--------
KiCad 官方只提供 PCB 侧的 Python API（`pcbnew`），从来没有 eeschema 模块。
因此本项目的原理图不靠「脚本接口」生成，而是由 kiutils 直接写 `.kicad_sch`，
再用 `kicad-cli` 做网表/BOM/ERC 校验。详见 README。

用法
----
    from kicad_env import kicad_cli, kicad_symbols, kicad_python, describe

    cli = kicad_cli()                       # -> Path(.../bin/kicad-cli.exe)
    lib = kicad_symbols() / "Device.kicad_sym"

命令行自检：
    python kicad_env.py
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ENV_ROOT = "KICAD_ROOT"
ENV_SYMBOLS = "KICAD_SYMBOLS_DIR"

#: 符号库相对安装根目录的候选位置（Windows/Linux 与 macOS 应用包不同）
_SYMBOL_RELS = (
    "share/kicad/symbols",            # Windows / Linux
    "Contents/SharedSupport/symbols",  # macOS .app
    "SharedSupport/symbols",           # macOS Contents 目录
)

CLI_NAME = "kicad-cli"


class KiCadNotFound(RuntimeError):
    """找不到 KiCad 安装或符号库。"""


def _cli_filename() -> str:
    return CLI_NAME + (".exe" if platform.system() == "Windows" else "")


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        try:
            key = p.resolve()
        except OSError:          # 路径存在性不影响去重
            key = p
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _candidate_roots() -> list[Path]:
    """按优先级列出可能的安装根目录。"""
    exe = _cli_filename()
    roots: list[Path] = []

    env_root = os.environ.get(ENV_ROOT)
    if env_root:
        roots.append(Path(env_root).expanduser())

    on_path = shutil.which(CLI_NAME)
    if on_path:
        # <root>/bin/kicad-cli  -> 根目录是 bin 的上一级
        roots.append(Path(on_path).resolve().parent.parent)

    system = platform.system()
    if system == "Windows":
        bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            bases.append(str(Path(local) / "Programs"))
        for base in bases:
            if not base:
                continue
            b = Path(base)
            # C:\Program Files\KiCad\8.0\bin\kicad-cli.exe
            roots.extend(p.parent.parent for p in b.glob(f"KiCad/*/bin/{exe}"))
            # C:\Program Files\KiCad8\bin\kicad-cli.exe
            roots.extend(p.parent.parent for p in b.glob(f"KiCad*/bin/{exe}"))
    elif system == "Darwin":
        roots.append(Path("/Applications/KiCad"))
        roots.extend(p.parent.parent for p in
                     Path("/Applications").glob(f"KiCad*.app/Contents/MacOS/{CLI_NAME}"))
    else:
        roots.extend([Path("/usr"), Path("/usr/local"), Path("/opt/kicad")])

    return _dedupe(roots)


def kicad_cli() -> Path:
    """返回 kicad-cli 可执行文件的绝对路径。"""
    exe = _cli_filename()
    candidates: list[Path] = []
    for root in _candidate_roots():
        candidates.extend([
            root / "bin" / exe,          # <root>/bin/kicad-cli
            root / "Contents" / "MacOS" / CLI_NAME,
            root / exe,                  # 直接指到 bin 目录的情况
        ])
    for p in candidates:
        if p.is_file():
            return p
    raise KiCadNotFound(
        "找不到 kicad-cli。请安装 KiCad 8 或更高版本，"
        f"或设置环境变量 {ENV_ROOT} 指向安装根目录（含 bin/ 的那一级）。\n"
        "  Windows 示例:  set KICAD_ROOT=C:\\Program Files\\KiCad\\8.0\n"
        "  PowerShell:    $env:KICAD_ROOT='C:\\Program Files\\KiCad\\8.0'\n"
        "已尝试的根目录: " + (", ".join(str(r) for r in _candidate_roots()) or "（无）")
    )


def kicad_symbols() -> Path:
    """返回 KiCad 官方符号库目录（内含 Device.kicad_sym 等）。"""
    env_dir = os.environ.get(ENV_SYMBOLS)
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir)

    cli = kicad_cli()
    for anchor in _dedupe([cli.parent.parent, cli.parent.parent.parent]):
        for rel in _SYMBOL_RELS:
            cand = anchor / rel
            if cand.is_dir():
                return cand
    raise KiCadNotFound(
        "找不到 KiCad 符号库（share/kicad/symbols）。"
        f"请确认 KiCad 安装完整，或设置环境变量 {ENV_SYMBOLS}。"
    )


def kicad_python() -> Path | None:
    """返回 KiCad 自带的 Python（能 import pcbnew），找不到返回 None。

    注意：只有它才能 import pcbnew；项目 .venv 里的 Python 不行。
    封装/PCB/DRC 相关功能必须用它跑。
    """
    cli = kicad_cli()
    for name in ("python.exe", "pythonw.exe", "python3", "python"):
        cand = cli.parent / name
        if cand.is_file():
            return cand
    return None


def kicad_version() -> str | None:
    """调用 kicad-cli 取版本号；失败返回 None（不影响主流程）。"""
    try:
        r = subprocess.run([str(kicad_cli()), "version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line and line[0].isdigit():
            return line
    return None


def describe() -> str:
    """人类可读的环境摘要，供自检与 README 排错使用。"""
    lines: list[str] = []
    try:
        cli = kicad_cli()
    except KiCadNotFound as exc:
        return f"KiCad：未找到\n{exc}"
    lines.append(f"kicad-cli : {cli}")
    ver = kicad_version()
    lines.append(f"版本      : {ver or '（无法获取）'}")
    try:
        lines.append(f"符号库    : {kicad_symbols()}")
    except KiCadNotFound as exc:
        lines.append(f"符号库    : 未找到（{exc}）")
    py = kicad_python()
    lines.append(f"内置Python: {py}" if py else "内置Python: 未找到（pcbnew 功能不可用）")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
    sys.exit(0)
