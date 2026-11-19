"""sexpr.py — 一个够用就好的 S-表达式解析器。

干吗用的
--------
`kicad-cli sch export netlist --format kicadsexpr` 导出的是嵌套多行的
S-表达式，直接按行 grep 拿不到东西（一行可能只有 `(net`）。所以把整份
文件解析成嵌套 list，再按 key 取值，比正则可靠得多。

表示方式
--------
- 括号 -> list
- 原子 -> str（`code`、`1`、`passive`）
- 字符串 -> str（去掉引号，处理 \\" 转义）

因此 `(code "1")` 解析为 `['code', '1']`，`(net (code "1") (name "A"))`
解析为 `['net', ['code', '1'], ['name', 'A']]`。

配套小工具
----------
    child(node, "name")     -> 第一个匹配的子节点
    children(node, "node")  -> 所有匹配的子节点
    value_of(node, "name")  -> 子节点的第一个值（取不到返回 None）

用法
----
    from sexpr import parse, children, value_of

    tree = parse(text)
    nets = child(tree, "nets")            # ['nets', ['net', ...], ...]
    for net in children(nets, "net"):
        print(value_of(net, "name"))
"""
from __future__ import annotations

from typing import Iterator

__all__ = ["parse", "parse_file", "child", "children", "value_of"]


def _tokens(text: str) -> Iterator[object]:
    """把文本切成 '(' / ')' / 原子 / 字符串记号。"""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "()":
            yield ch
            i += 1
        elif ch == '"':
            j = i + 1
            buf: list[str] = []
            while j < n:
                cj = text[j]
                if cj == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if cj == '"':
                    break
                buf.append(cj)
                j += 1
            yield "".join(buf)
            i = j + 1
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            yield text[i:j]
            i = j


def parse(text: str) -> list:
    """解析成嵌套 list；返回顶层第一个表达式。"""
    stack: list[list] = [[]]
    for tok in _tokens(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) < 2:
                raise ValueError("S-表达式括号不匹配：多余的 ')'")
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError(f"S-表达式括号不匹配：还有 {len(stack) - 1} 层未闭合")
    if not stack[0]:
        raise ValueError("空文档")
    return stack[0][0]


def parse_file(path: str) -> list:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse(fh.read())


def children(node: list, key: str) -> list[list]:
    """返回所有首元素等于 key 的子节点。"""
    return [c for c in node[1:]
            if isinstance(c, list) and c and c[0] == key]


def child(node: list, key: str) -> list | None:
    """返回第一个首元素等于 key 的子节点。"""
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == key:
            return c
    return None


def value_of(node: list, key: str) -> str | None:
    """取 `(key value)` 里的 value；value 本身是列表时返回 None。"""
    c = child(node, key)
    if c is None or len(c) < 2 or isinstance(c[1], list):
        return None
    return c[1]
