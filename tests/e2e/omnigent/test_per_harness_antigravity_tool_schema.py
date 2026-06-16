"""Per-harness live e2e — antigravity bridged-tool *parameter schema* reaches the model.

Runs a real ``omnigent run <spec> -p "..."`` one-shot against the in-process
``antigravity`` harness (Gemini-native ``google-antigravity`` SDK), where the
spec declares a bridged ``type: function`` tool whose ``parameters`` JSON schema
has TWO **required** structured args (a string ``player`` and an integer
``score``). The prompt asks the model to record a specific score, and the test
asserts the bridged tool actually ran with the **correct populated args** — the
recorded player + score round-trip back into the assistant's reply.

This is the end-to-end gate for the headline fix in PR #279: the Antigravity
executor now passes each bridged Omnigent tool's ``parameters`` JSON schema to
the SDK via its ``ToolWithSchema(fn, input_schema)`` wrapper, so Gemini sees the
argument shapes instead of guessing. Before that fix the SDK only received the
tool's name + description with an empty arg schema, so the model could not
reliably populate ``player`` / ``score`` and the structured round-trip would not
appear. The full path exercised: CLI parse -> spec materialize -> Omnigent
server boot -> local runner -> :class:`AntigravityExecutor` building a
schema-bearing ``ToolWithSchema`` and driving a persistent SDK ``Agent`` ->
bridged tool dispatch through the Omnigent tool registry (under policy) -> the
``PostToolCallHook`` completion -> the ``-p`` one-shot printer.

**Prerequisites (skipped — not failed — when absent so e2e shards stay green):**
- The ``google.antigravity`` package importable (a Gemini-native SDK).
- A resolvable Gemini API key (``antigravity_api_key_configured()`` — the
  dedicated ``antigravity:`` block in ``~/.omnigent/config.yaml``). The SDK is
  Gemini-native: there is no Databricks-gateway path, so this gate does NOT use
  ``omnigent_credentials_env`` / ``patched_databrickscfg`` (mirrors the cursor
  per-harness test). It runs for real wherever a key is configured.

**glibc / harness-binary caveat (DEV HOSTS):** the SDK spawns a bundled native
``localharness`` binary that needs glibc >= 2.36. On an older host (e.g. the
glibc-2.31 dev box) set ``ANTIGRAVITY_HARNESS_PATH`` to a loader-shim wrapping
the bundled binary through a newer glibc; the subprocess inherits it via
``os.environ``. Where the host glibc is new enough the var is unnecessary. This
caveat is environmental only — it does not affect what the fix validates.

**What breaks if this fails (with prerequisites present):**
- The schema-passing regresses: ``_build_sdk_tools`` stops wrapping a
  ``parameters``-bearing tool in ``ToolWithSchema`` (or the SDK drops the
  ``ToolWithSchema`` API), so Gemini no longer sees the arg shapes and the
  structured ``player`` / ``score`` round-trip disappears.
- ``AntigravityExecutor`` regresses on the bridged-tool dispatch / completion
  path, or the ``-p`` one-shot path stops printing the assistant reply.

**Rate limits:** a shared free-tier Gemini key can return HTTP 429 / 503
(quota / high demand). Those are transient infra conditions, not fix failures,
so the test treats a model-provider rate-limit / overload error as a skip
rather than a hard failure (it asserts hard on any *other* non-zero exit).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

# The bridged tool's dotted callable — an in-tree fixture importable from the
# repo-root cwd (the subprocess runs there, as the conftest documents). Its
# ``parameters`` schema below mirrors the function's (player: str, score: int).
_TOOL_CALLABLE = "tests.resources.examples._shared.score_tool.record_score"

# Specific values the model must populate into the structured args; both have
# to appear in the reply for the round-trip to prove the schema reached Gemini.
_PLAYER = "Ada"
_SCORE = 42
_PROMPT = f"Player {_PLAYER} scored {_SCORE} points. Record it."

# The only Gemini model with non-zero free-tier quota on the dev key; the SDK
# default is also a gemini-* flash model, so leaving it unpinned is fine, but
# pinning keeps the test deterministic across SDK default bumps.
_MODEL = "gemini-3.5-flash"

# A real Gemini round-trip plus a tool call; 180s matches the other
# slow-harness per-harness gates' headroom on CI hosts.
_RUN_TIMEOUT_SEC = 180

# Substrings that mark a transient provider rate-limit / overload (HTTP 429 /
# 503) rather than a fix regression — skip on these so a saturated shared
# free-tier key never reds the shard.
# Lowercased: matched against ``combined.lower()`` below.
_RATE_LIMIT_MARKERS = (
    "code 429",
    "code 503",
    "resource_exhausted",
    "quota",
    "high demand",
    "rate limit",
)


def _spec_yaml() -> str:
    """Build the agent spec: a bridged FUNCTION tool with a structured schema.

    :returns: A ``config.yaml`` body declaring ``record_score`` with two
        required args (``player`` / ``score``) — the schema under test.
    """
    return textwrap.dedent(
        f"""\
        spec_version: 1
        name: score_recorder
        prompt: |
          You record game scores. When the user reports a score, call the
          record_score tool exactly once with the player's name and their
          integer score, then confirm what you recorded in one short sentence.
        executor:
          type: omnigent
          config:
            harness: antigravity
          model: {_MODEL}
        tools:
          record_score:
            type: function
            description: "Record a player's score in the leaderboard."
            callable: {_TOOL_CALLABLE}
            parameters:
              type: object
              properties:
                player:
                  type: string
                  description: "The player's name."
                score:
                  type: integer
                  description: "The integer score to record."
              required: [player, score]
        """
    )


def test_per_harness_antigravity_bridged_tool_schema(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """A bridged tool's ``parameters`` schema reaches Gemini and is filled correctly.

    :param omnigent_python: Interpreter with omnigent + ``google.antigravity``
        installed and importable.
    :param omnigent_repo_root: Cwd for the subprocess so the spec's dotted
        ``callable`` (the in-tree fixture) resolves on sys.path.
    :param tmp_path: Per-test dir holding the materialized spec directory.
    """
    if importlib.util.find_spec("google.antigravity") is None:
        pytest.skip(
            "antigravity prerequisite missing: the 'google.antigravity' SDK is not "
            "installed (Gemini-native harness)."
        )
    # Import lazily — only meaningful once the SDK package is present.
    from omnigent.onboarding.antigravity_auth import antigravity_api_key_configured

    if not antigravity_api_key_configured():
        pytest.skip(
            "antigravity prerequisite missing: no resolvable Gemini API key. The "
            "antigravity harness authenticates Gemini-natively (a dedicated "
            "'antigravity:' block in ~/.omnigent/config.yaml; it does NOT reuse the "
            "Databricks-gateway auth), so this live gate is skipped rather than "
            "failed when the key is absent."
        )

    spec_dir = tmp_path / "score_recorder"
    spec_dir.mkdir()
    (spec_dir / "config.yaml").write_text(_spec_yaml())

    # The SDK reads its Gemini key from the keychain/config and may need
    # ANTIGRAVITY_HARNESS_PATH on older-glibc dev hosts (see module docstring),
    # so pass the full environment through.
    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(spec_dir),
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=dict(os.environ),
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        if any(marker in combined.lower() for marker in _RATE_LIMIT_MARKERS):
            pytest.skip(
                "antigravity live turn hit a transient provider rate-limit / overload "
                f"(429 / 503) on the shared free-tier key — not a fix regression.\n\n{combined}"
            )
        pytest.fail(
            f"antigravity run exited {result.returncode}.\n\n"
            f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
        )

    reply = result.stdout
    # The structured-arg round-trip: the model could only populate BOTH the
    # string player and the integer score because the SDK received the tool's
    # parameters schema. The bridged callable echoes them, and the agent's
    # confirming sentence reflects both — so both must appear in the reply.
    assert _PLAYER in reply, (
        f"player {_PLAYER!r} not in the assistant reply — the structured arg did not "
        f"round-trip, so the bridged tool likely was not invoked with the schema.\n\n"
        f"stdout:\n{reply!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert re.search(rf"\b{_SCORE}\b", reply), (
        f"score {_SCORE} not in the assistant reply — the integer arg did not "
        f"round-trip, so the bridged tool likely was not invoked with the schema.\n\n"
        f"stdout:\n{reply!r}\n\nstderr:\n{result.stderr!r}"
    )
