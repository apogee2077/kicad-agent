"""try_tools.py — 最小工具调用 Agent（W2 里程碑练习）

这个脚本只有 ~150 行，演示「工具调用」的全部要素：

    1. 工具说明书   —— 告诉模型有哪些工具、参数长什么样（TOOLS）
    2. 真正的函数   —— 你的代码怎么干活（lookup_symbol 等）
    3. 循环         —— 模型要调工具就执行、把结果回灌，直到它给最终答案
    4. 系统提示词   —— 告诉模型「需要事实先查工具，别凭记忆」

关键认知：**模型永远不会真的执行任何东西**。它只会返回一句「我想调用
lookup_symbol(lib="Device", symbol="LED")」，真正去查的是你自己的代码。

用法
----
    # 不调模型，直接测三个工具函数本身（没有 key 也能跑）
    .venv\\Scripts\\python.exe try_tools.py --selftest

    # 真跑：需要 .env 里有 LLM_API_KEY
    .venv\\Scripts\\python.exe try_tools.py "Device:LED 有几个引脚？分别叫什么？"

    # 换个问题试试模型会不会自己串工具
    .venv\\Scripts\\python.exe try_tools.py "designs/light_alarm.json 里有多少个器件？"

配置（.env）
-----------
    LLM_API_KEY=sk-xxxx
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=deepseek-chat
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
MAX_ROUNDS = 8          # 防止模型无限调工具

# ====================================================================
# 一、真正的工具函数（这就是「你的代码干活」的部分）
# ====================================================================

def lookup_symbol(lib: str, symbol: str) -> dict:
    """查一个 KiCad 符号的真实引脚：{引脚号: 引脚名}。"""
    from symbol_lib import load_symbol, symbol_pins

    pins = symbol_pins(load_symbol(lib, symbol))
    if not pins:
        return {"error": f"符号 {lib}:{symbol} 没有引脚（可能名字不对）"}
    return {"symbol": f"{lib}:{symbol}", "pin_count": len(pins), "pins": pins}


def search_symbol(lib: str, keyword: str) -> dict:
    """在一个符号库里按关键字搜符号名，用于模型不确定确切名字时。"""
    from symbol_lib import load_library

    lib_obj = load_library(lib)
    kw = keyword.lower()
    hits = [s.entryName for s in lib_obj.symbols if kw in s.entryName.lower()]
    return {
        "lib": lib,
        "keyword": keyword,
        "match_count": len(hits),
        "matches": hits[:30],       # 只给前 30 个，避免灌爆上下文
    }


def read_design(path: str) -> dict:
    """读取一份设计 JSON，返回摘要（不返回全文，避免上下文过载）。"""
    target = (PROJECT_ROOT / path).resolve()
    # 安全护栏：只允许读项目目录内的文件
    if PROJECT_ROOT not in target.parents and target != PROJECT_ROOT:
        return {"error": f"拒绝访问项目目录之外的路径: {target}"}
    if not target.is_file():
        return {"error": f"文件不存在: {path}"}

    data = json.loads(target.read_text(encoding="utf-8"))
    refs = [item["ref"] for item in data.get("bom", [])]
    by_prefix: dict[str, int] = {}
    for ref in refs:
        prefix = "".join(ch for ch in ref if ch.isalpha())
        by_prefix[prefix] = by_prefix.get(prefix, 0) + 1

    return {
        "file": path,
        "name": data.get("meta", {}).get("name", ""),
        "component_count": len(refs),
        "net_count": len(data.get("nets", [])),
        "no_connect_count": len(data.get("no_connect", [])),
        "components_by_prefix": by_prefix,
    }


# 函数名 -> 实际函数
FUNCTIONS = {
    "lookup_symbol": lookup_symbol,
    "search_symbol": search_symbol,
    "read_design": read_design,
}


# ====================================================================
# 二、工具说明书（模型看到的就是这个）
# ====================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_symbol",
            "description": (
                "查询一个 KiCad 符号的真实引脚号和引脚名。"
                "需要知道某个器件有哪些脚、哪个脚是什么功能时用它，"
                "不要凭记忆回答引脚号。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lib": {"type": "string", "description": "符号库名，如 Device、Timer_RTC"},
                    "symbol": {"type": "string", "description": "符号名，如 LED、DS3231M"},
                },
                "required": ["lib", "symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbol",
            "description": (
                "在一个符号库里按关键字模糊搜索符号名。"
                "当你不确定确切符号名时，先用它找。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lib": {"type": "string", "description": "符号库名，如 Device"},
                    "keyword": {"type": "string", "description": "关键字，如 Led、Regulator"},
                },
                "required": ["lib", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_design",
            "description": (
                "读取项目里的一份设计 JSON，返回器件数、网络数等摘要。"
                "路径相对于项目根目录，例如 designs/light_alarm.json。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "设计文件路径，如 designs/light_alarm.json"},
                },
                "required": ["path"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "你是一名硬件工程助手。"
    "当问题涉及 KiCad 符号、器件引脚、或项目里的设计文件时，"
    "必须先用工具查询，不要凭记忆作答。"
    "查到事实后再回答，回答用中文，简洁准确。"
)


# ====================================================================
# 三、自检：不调模型，只测工具函数
# ====================================================================

def selftest() -> int:
    print("=" * 60)
    print("工具函数自检（不调用模型）")
    print("=" * 60)

    cases = [
        ("lookup_symbol", {"lib": "Device", "symbol": "LED"}),
        ("lookup_symbol", {"lib": "Timer_RTC", "symbol": "DS3231M"}),
        ("search_symbol", {"lib": "Device", "keyword": "Regulator"}),
        ("read_design", {"path": "designs/light_alarm.json"}),
    ]
    for name, args in cases:
        print(f"\n>>> {name}({args})")
        try:
            result = FUNCTIONS[name](**args)
        except Exception as exc:  # noqa: BLE001
            print(f"    !! 调用失败: {type(exc).__name__}: {exc}")
            continue
        text = json.dumps(result, ensure_ascii=False)
        print("    " + (text[:300] + "..." if len(text) > 300 else text))

    print("\n" + "=" * 60)
    print("自检完成。工具正常就可以加 LLM_API_KEY 跑真模型了。")
    print("=" * 60)
    return 0


# ====================================================================
# 四、主循环：模型决定调什么工具
# ====================================================================

def run(question: str) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        print("缺少 LLM_API_KEY。\n")
        print("两种办法：")
        print("  1) 在项目根目录建 .env，写入：")
        print("       LLM_API_KEY=sk-xxxx")
        print("       LLM_BASE_URL=https://api.deepseek.com")
        print("       LLM_MODEL=deepseek-chat")
        print("  2) 先跑 --selftest 验证三个工具函数本身是否正常（不需要 key）")
        return 2

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com",
    )
    model = os.environ.get("LLM_MODEL") or "deepseek-chat"

    print(f"模型: {model}\n问题: {question}\n" + "=" * 60)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for round_no in range(1, MAX_ROUNDS + 1):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            temperature=0.2,
        )
        msg = resp.choices[0].message

        # 模型不需要工具了 —— 给出最终回答
        if not msg.tool_calls:
            print(f"\n[第 {round_no} 轮] 模型给出最终回答：\n")
            print(msg.content)
            return 0

        # 把「模型的调用请求」记进对话历史
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in msg.tool_calls
            ],
        })

        # 逐个执行工具，把结果作为 tool 消息回灌
        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                result = {"error": f"参数不是合法 JSON: {exc}"}
            else:
                print(f"[第 {round_no} 轮] 模型决定调用 {name}({args})")
                func = FUNCTIONS.get(name)
                if func is None:
                    result = {"error": f"没有名为 {name} 的工具"}
                else:
                    try:
                        result = func(**args)
                    except Exception as exc:  # noqa: BLE001
                        result = {"error": f"{type(exc).__name__}: {exc}"}

            text = json.dumps(result, ensure_ascii=False)
            print(f"          -> {text[:220]}{'...' if len(text) > 220 else ''}\n")

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": text,
            })

    print(f"\n超过 {MAX_ROUNDS} 轮仍未得出结论，已停止。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if sys.argv[1] == "--selftest":
        sys.exit(selftest())
    sys.exit(run(" ".join(sys.argv[1:])))
