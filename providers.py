"""providers.py — LLM 提供方抽象。

为什么要抽象
------------
同一个 `complete()` 接口，两种实现：

  FixtureProvider       离线，读 fixtures/<step>.json。没有 API key 时用它跑通闭环、做回归。
  OpenAICompatProvider  真的调 API。DeepSeek / OpenAI / 任何 OpenAI 兼容端点都能用。

这样「提示词、结构化解析、校验、生成」这几块完全不依赖具体模型，
换模型只改环境变量。开发期也不需要联网就能验证整条流水线。

配置（写进 .env，不要提交）
---------------------------
    LLM_API_KEY=sk-xxxx
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=deepseek-chat
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Provider(Protocol):
    name: str

    def complete(self, system: str, user: str, *, step: str) -> str:
        """给定 system/user 提示词，返回模型原始文本（step 用于 fixture 定位）。"""


class FixtureProvider:
    """离线提供方：直接返回预先存好的模型回答。

    用途有两个：
    1. 没有 key 时把闭环跑通（06 之前的所有联调）。
    2. 回归测试：提示词或下游代码改动后，确认解析/校验/生成没被改坏。
    """

    name = "fixture"

    def __init__(self, root: str | Path = "fixtures"):
        self.root = Path(root)

    def complete(self, system: str, user: str, *, step: str) -> str:
        path = self.root / f"{step}.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"缺少 fixture: {path}。离线模式需要 fixtures/{step}.json 作为模型回答。"
            )
        return path.read_text(encoding="utf-8")


class OpenAICompatProvider:
    """真实模型：任何 OpenAI 兼容的 /chat/completions 端点。"""

    name = "openai"

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, temperature: float = 0.0):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:      # dotenv 是可选依赖
            pass

        self.api_key = api_key or os.environ.get("LLM_API_KEY")
        self.base_url = base_url or os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com"
        self.model = model or os.environ.get("LLM_MODEL") or "deepseek-chat"
        self.temperature = temperature
        self._client = None

        if not self.api_key:
            raise RuntimeError(
                "缺少 LLM_API_KEY。请在项目根目录建 .env 写入：\n"
                "  LLM_API_KEY=sk-xxxx\n"
                "  LLM_BASE_URL=https://api.deepseek.com\n"
                "  LLM_MODEL=deepseek-chat\n"
                "或者用 --provider fixture 跑离线模式。"
            )

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def complete(self, system: str, user: str, *, step: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


def make_provider(kind: str = "auto", **kwargs) -> Provider:
    """工厂。auto = 有 key 就真调，没有就离线。"""
    if kind == "fixture":
        return FixtureProvider(**kwargs)
    if kind == "openai":
        return OpenAICompatProvider(**kwargs)
    if kind == "auto":
        if os.environ.get("LLM_API_KEY"):
            return OpenAICompatProvider(**kwargs)
        print("[提示] 未检测到 LLM_API_KEY，自动使用离线 fixture 模式。")
        return FixtureProvider(**kwargs)
    raise ValueError(f"未知 provider: {kind}（可选 fixture / openai / auto）")
