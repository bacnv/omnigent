"""Bridged FUNCTION-tool fixture for the antigravity tool-schema e2e gate.

``record_score`` takes two REQUIRED structured args — a string ``player`` and an
integer ``score`` — declared as an explicit ``parameters`` JSON schema in the
e2e spec. It exists so a live antigravity turn can prove the schema reached the
model: the model can only populate both args when the SDK was handed the tool's
``parameters`` (PR #279) — otherwise the schema is empty and there are no arg
shapes to fill. The callable echoes the args back so the assistant's confirming
reply reflects the recorded values.

Referenced as the dotted ``callable``
``tests.resources.examples._shared.score_tool.record_score`` from
``tests/e2e/omnigent/test_per_harness_antigravity_tool_schema.py``.
"""

from __future__ import annotations


def record_score(player: str, score: int) -> dict[str, object]:
    """Record a player's score.

    :param player: The player's name.
    :param score: The integer score to record.
    :returns: An echo payload confirming what was recorded
        (``{"recorded": True, "player": <player>, "score": <score>}``).
    """
    return {"recorded": True, "player": player, "score": int(score)}
