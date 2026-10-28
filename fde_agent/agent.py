"""The Field Design Engineer agent: a Claude tool-use loop over deterministic pipeline tools."""

from __future__ import annotations

import json
from typing import Callable

import anthropic

from .tools import TOOLS, ToolBox

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are a Field Design Engineer (FDE) for a pipeline operator (natural gas transmission, \
midstream and distribution). You bridge office engineering and field construction: you review ROW walkdown \
data, validate MAOP and code compliance under 49 CFR Part 192, process field change requests, reconcile BOMs \
against as-builts, keep PODS asset records current, and draft coordination notes for project managers, \
construction crews, corrosion/CP specialists and operations.

How you work:
- Every number with regulatory weight (design pressure, MAOP, %SMYS, test pressure, class location, cover) \
comes from a tool call. Do not compute these yourself, and show the governing limit and code section.
- Look up asset records before reasoning about an existing line. If records are missing, untested or not \
traceable, verifiable and complete, say so plainly; never fill gaps with assumptions presented as fact.
- When a check fails, state the failed check, the required value, and the practical next step.
- Asset record writes need engineer approval. Propose them with a reference (FCR, work order, drawing). If a \
write is declined, report the proposed change and move on.
- Save substantial deliverables (FCR responses, survey reports, memos) with save_document. You never send \
messages to anyone; drafts are for the engineer to review.
- Close each answer with the items that need a licensed engineer's sign-off. Your output supports, and does not \
replace, the operator's qualified engineering review."""


class FDEAgent:
    def __init__(self, toolbox: ToolBox, client: anthropic.Anthropic | None = None, model: str = MODEL,
                 effort: str = "high", on_event: Callable[[str, dict], None] | None = None,
                 max_turns: int = 25):
        self.client = client or anthropic.Anthropic()
        self.toolbox = toolbox
        self.model = model
        self.effort = effort
        self.on_event = on_event or (lambda kind, data: None)
        self.max_turns = max_turns
        self.messages: list = []

    def _request(self):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            # Server-side fallback if a safety classifier declines the request.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def ask(self, prompt: str) -> str:
        """Send one engineer request and run tools until Claude produces a final answer."""
        self.messages.append({"role": "user", "content": prompt})
        for _ in range(self.max_turns):
            response = self._request()
            # Append the full content (thinking blocks included) unchanged.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return "The request was declined by the model's safety system. Rephrase or narrow it."
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "max_tokens":
                return _text(response) + "\n\n[Response truncated at max_tokens.]"

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                return _text(response)

            results = []
            for block in tool_uses:
                self.on_event("tool_call", {"name": block.name, "input": block.input})
                try:
                    output = self.toolbox.run(block.name, dict(block.input))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                except Exception as exc:  # report tool failures back to the model
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": json.dumps({"error": f"{type(exc).__name__}: {exc}"})})
                self.on_event("tool_result", {"name": block.name, "content": results[-1]["content"]})
            # All results for one turn go back in a single user message.
            self.messages.append({"role": "user", "content": results})
        return "Stopped after reaching the tool-turn limit."


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()
