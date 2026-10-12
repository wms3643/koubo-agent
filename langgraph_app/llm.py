"""LLM 访问层 —— 编排层里**唯一**直接接触模型的地方。

职责刻意很薄：造客户端、把帧图编码成多模态消息、把模型输出里的 JSON 取出来。
"该判断什么"写在 `personas.py`（角色）与各判断节点（任务）里；
"怎么调模型"只在这里一份实现 —— 免得每个节点各写一遍温度/超时/解析。
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .config import ModelConfig


class LLMError(RuntimeError):
    """模型侧失败（缺密钥 / 网络 / 输出无法解析）。

    刻意与 `kernel.KernelResult` 分开：这是**编排层自己**的失败，要由节点翻成
    可路由的 cause（`llm_unavailable` / `llm_bad_output`），而不是假装成内核失败。
    """


def build_chat(model: ModelConfig, *, vision: bool = False) -> ChatOpenAI:
    """造一个 ChatOpenAI。`vision=True` 时用视觉模型（审片必须能看图）。"""
    if not model.api_key:
        raise LLMError("缺少模型网关密钥：请设 LK_API_KEY 或 YUANQI_API_KEY")
    return ChatOpenAI(
        model=model.vision_model if vision else model.text_model,
        base_url=model.base_url,
        api_key=model.api_key,
        temperature=model.temperature,
        max_tokens=model.max_tokens,
        timeout=model.timeout_s,
        max_retries=model.max_retries,
    )


def image_part(path: Path) -> dict:
    """把帧图编码成多模态消息里的 `image_url`。

    ⚠️ 必须内联 base64：模型看不到本地文件系统。审片子的**全部依据**就是这几张图，
    形态给错等于让质检"凭感觉"（而质检"凭感觉"正是本项目最反对的事）。
    """
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def ask(
    llm: ChatOpenAI,
    *,
    system: str,
    user: str,
    images: list[Path] | None = None,
) -> str:
    """问一次。返回纯文本（调用方负责解析）。异常统一翻成 `LLMError`。"""
    content: list[dict] = [{"type": "text", "text": user}]
    for p in images or []:
        content.append(image_part(p))
    try:
        msg = llm.invoke([SystemMessage(content=system), HumanMessage(content=content)])
    except Exception as e:  # noqa: BLE001 —— 网络/鉴权/限流都在这里，统一成一种失败
        raise LLMError(f"模型调用失败：{type(e).__name__}: {e}") from e
    return _text_of(msg.content)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
        return "".join(parts)
    return str(content)


def parse_json(text: str) -> dict:
    """从模型输出里取出 JSON 对象；取不到就抛 `LLMError`。

    ⚠️ 不能只 `json.loads`：模型几乎总会裹一层 ``` 围栏，或者前后加一句解释。
    依次尝试「整体解析 → 剥围栏 → 取第一个 `{` 到最后一个 `}`」。
    这和内核侧解析 stdout 是同一个思路 —— 都在对付"实际形态比承诺更脏"。
    """
    raw = (text or "").strip()
    if not raw:
        raise LLMError("模型返回空内容")

    for candidate in _candidates(raw):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise LLMError(f"模型输出里没有可解析的 JSON 对象（前 200 字：{raw[:200]!r}）")


def _candidates(raw: str) -> list[str]:
    out = [raw]
    if raw.startswith("```"):
        # ```json\n{...}\n``` —— 去掉首行与结尾围栏
        body = raw.split("\n", 1)[1] if "\n" in raw else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        out.append(body.strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        out.append(raw[start:end + 1])
    return out


def extract_html(text: str, *, marker: str = "<!--OVERLAY_HTML-->") -> str:
    """从模型输出里取出**整段 HTML**，而不是塞进 JSON 字符串。

    ⚠️ 为什么不让它包在 JSON 里：动效工程是一整份 HTML（含 GSAP timeline），
    塞进 JSON 字符串要转义几十处引号/换行，是纯粹的出错风险来源 —— 模型多漏一个转义符，
    整份工程就废了。所以约定：先写一行标记，标记之后**原文照抄**。
    """
    raw = (text or "").strip()
    if marker in raw:
        return raw.split(marker, 1)[1].strip()
    # 没有标记时兜一个更常见的形态：整个 ```html 围栏
    if "```" in raw:
        body = raw.split("```", 2)
        if len(body) >= 3:
            inner = body[1]
            if inner.lstrip().lower().startswith("html"):
                inner = inner.lstrip()[4:]
            return inner.strip()
    if raw.lstrip().lower().startswith("<!doctype") or "<html" in raw[:200].lower():
        return raw
    raise LLMError("模型输出里没有找到 HTML 工程（也没有约定标记）")
