"""Regression tests for ChatGPT's large-paste attachment boundary.

ChatGPT turns sufficiently large native pastes into a ``Pasted markdown`` file.
That leaves the editable top-level message empty, so instructions that worked
when pasted inline can be interpreted as inert document contents.  The relay
must restore an ordinary user instruction before Send, independently of the
current frontend threshold, and recognize the characteristic acknowledgement
response if the backend still declines to execute it.
"""

import pytest

from gpt_pro import cli


class _Composer:
    def __init__(self, text, *, accepts_fill=True):
        self.text = text
        self.accepts_fill = accepts_fill
        self.filled = []

    async def inner_text(self):
        return self.text

    async def fill(self, value):
        self.filled.append(value)
        if self.accepts_fill:
            self.text = value


@pytest.fixture
def _stages(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "log_stage", lambda stage, **kw: seen.append((stage, kw)))
    return seen


async def test_inline_prompt_keeps_existing_top_level_instruction(_stages):
    composer = _Composer("Do the task now")

    restored = await cli._ensure_top_level_instruction(composer, prompt_chars=15)

    assert restored is False
    assert composer.filled == []
    assert not any(stage == "instruction_boundary_restored" for stage, _ in _stages)


@pytest.mark.parametrize("prompt_chars", [12_000, 400_000, 1_000_000])
async def test_attachment_only_paste_restores_top_level_instruction(_stages, prompt_chars):
    # The mechanism is deliberately based on the resulting composer state, not
    # a hard-coded character threshold. These sizes cover the observed small,
    # medium, and very-large attachment paths.
    composer = _Composer("\n\u200b ")

    restored = await cli._ensure_top_level_instruction(
        composer, prompt_chars=prompt_chars,
    )

    assert restored is True
    assert composer.filled == [cli.ATTACHED_PROMPT_EXECUTION_INSTRUCTION]
    assert "Execute the task" in composer.text
    assert ("instruction_boundary_restored", {"prompt_chars": prompt_chars}) in _stages


async def test_attachment_only_paste_fails_closed_if_instruction_will_not_stick(_stages):
    composer = _Composer("", accepts_fill=False)

    with pytest.raises(cli.InstructionBoundaryLost):
        await cli._ensure_top_level_instruction(composer, prompt_chars=400_000)


@pytest.mark.parametrize(
    "response",
    [
        "已收到附件。请告诉我需要对该文件执行什么任务，我会以附件内容作为主要依据。",
        "文件已收到。请直接发送下一步指令，例如‘开始完整审阅’，我将继续给出完整技术评审。",
        "I received the attached file. Tell me what task you want me to perform and I will continue.",
        "已读取附件。" + ("这里复述评审范围、约束和预期产出。" * 80) + "我将继续给出完整技术评审。",
        "已读取你上传的文件内容。这里复述任务要求。如果你的意图是让我直接完成评审，我会按文件要求继续。",
        "已读取你上传的评审任务文件。我会基于该文件作为内部约束，另行结合公开文献检索后给出完整评审。",
    ],
)
def test_detects_attachment_acknowledgement_instead_of_completed_work(response):
    assert cli.looks_like_instruction_boundary_failure(response)


@pytest.mark.parametrize(
    "response",
    [
        "4",
        "The implementation is complete. I changed the parser and added regression tests.",
        "I reviewed the attachment and found three issues. First, the lock scope is too broad.",
    ],
)
def test_does_not_reject_short_completed_answers(response):
    assert not cli.looks_like_instruction_boundary_failure(response)
