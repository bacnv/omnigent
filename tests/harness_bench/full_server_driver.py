"""Full-server transport driver (phase-2).

Unlike :class:`tests.harness_bench.driver.SdkInprocDriver` (which drives a
harness wrap subprocess directly), this driver spins up a REAL Omnigent
``server`` + ``runner`` pair, registers an agent, and drives turns through
the full session path — so policy enforcement and server-dispatched tools
are exercised the way production does, not simulated at the wrap boundary.

It reuses the exact spawn recipe of the e2e ``live_server`` fixture
(``tests/e2e/conftest.py``) via the shared compat helpers, but packaged as
a plain async context manager so the bench CLI can drive it without pytest.

Status: walking skeleton — lifecycle + a basic turn (send message, poll to
terminal, extract assistant text). Streaming-delta counting, policy DENY
pre-attach, server-dispatched tools, and interrupt are layered on next; a
:class:`TurnResult` is returned so the existing probes can consume it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e.helpers import lookup_databricks_host
from tests.harness_bench.driver import PHASE_TOOL_CALL, TurnResult
from tests.harness_bench.profile import BenchProfile

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_HEALTH_TIMEOUT_S = 90.0
_POLL_INTERVAL_S = 0.2


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _mint_bearer(profile: str) -> str:
    """Mint a Databricks bearer for *profile* via the CLI (isolated from ambient token env).

    ``env -u DATABRICKS_TOKEN -u DATABRICKS_BEARER`` guards against a stale
    ambient credential shadowing profile auth (see omnigent issue #1781).
    """
    proc = subprocess.run(
        ["databricks", "auth", "token", "--profile", profile, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in ("DATABRICKS_TOKEN", "DATABRICKS_BEARER")
        },
    )
    return str(json.loads(proc.stdout)["access_token"])


class FullServerDriver:
    """Drive turns through a live Omnigent server + runner.

    Async context manager: on enter it spawns the server and runner,
    waits for both to report healthy, registers *profile*'s harness as an
    agent, and creates a runner-bound session. ``run_turn`` drives one turn
    through that session.
    """

    transport = "full-server"

    def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
        self._profile = profile
        self._db_profile = databricks_profile
        self._proc: subprocess.Popen[bytes] | None = None
        self._runner: subprocess.Popen[bytes] | None = None
        self._logs: list[Path] = []
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        self._base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-fs-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else ``None``."""
        if not databricks_profile:
            return "no --profile / databricks profile provided; full-server needs a gateway route"
        if lookup_databricks_host(databricks_profile) is None:
            return (
                f"databricks profile {databricks_profile!r} missing/hostless in ~/.databrickscfg"
            )
        # Reuse the wrap driver's CLI gate (same binary requirement).
        from tests.harness_bench.driver import SdkInprocDriver

        return SdkInprocDriver.unavailable(profile, databricks_profile=databricks_profile)

    def __enter__(self) -> FullServerDriver:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        host = lookup_databricks_host(self._db_profile)
        assert host is not None  # guaranteed by unavailable()
        bearer = _mint_bearer(self._db_profile)
        port = _find_free_port()
        self._base_url = f"http://localhost:{port}"

        binding_token = uuid.uuid4().hex
        runner_id = token_bound_runner_id(binding_token)

        base_env = {
            **os.environ,
            "OPENAI_API_KEY": bearer,
            "OPENAI_BASE_URL": f"{host}/serving-endpoints",
            "DATABRICKS_CONFIG_PROFILE": self._db_profile,
        }
        apply_server_env(base_env, _REPO_ROOT)

        self._proc = self._spawn_server(port, base_env, binding_token)
        self._runner = self._spawn_runner(base_env, runner_id, binding_token)
        self._wait_ready(runner_id)

        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        agent_name = self._register_agent()
        self._session_id = self._create_session(agent_name, runner_id)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.close()
        for proc in (self._runner, self._proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── spawn ────────────────────────────────────────────────

    def _spawn_server(
        self, port: int, base_env: dict[str, str], binding_token: str
    ) -> subprocess.Popen[bytes]:
        db_path = self._tmp / "bench.db"
        artifact_dir = self._tmp / "artifacts"
        artifact_dir.mkdir(exist_ok=True)
        log = self._tmp / "server.log"
        self._logs.append(log)
        args = [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ]
        return subprocess.Popen(
            args,
            env={**base_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
            cwd=compat_server_cwd(),
            stdout=log.open("wb"),
            stderr=subprocess.STDOUT,
        )

    def _spawn_runner(
        self, base_env: dict[str, str], runner_id: str, binding_token: str
    ) -> subprocess.Popen[bytes]:
        log = self._tmp / "runner.log"
        self._logs.append(log)
        runner_env = apply_runner_env(
            {
                **base_env,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": self._base_url,
            }
        )
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=runner_env,
            cwd=compat_runner_cwd(),
            stdout=log.open("wb"),
            stderr=subprocess.STDOUT,
        )

    def _wait_ready(self, runner_id: str) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{self._base_url}/health", timeout=2)
                status = httpx.get(f"{self._base_url}/v1/runners/{runner_id}/status", timeout=2)
                if (
                    health.status_code == 200
                    and status.status_code == 200
                    and status.json().get("online") is True
                ):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(
            f"server+runner not ready within {_HEALTH_TIMEOUT_S}s; logs in {self._tmp}"
        )

    # ── agent + session ──────────────────────────────────────

    def _register_agent(self) -> str:
        import io
        import tarfile

        import yaml

        assert self._client is not None
        name = f"bench-{self._profile.harness}"
        config = {
            "name": name,
            "prompt": "You are a helpful assistant used for capability testing.",
            "executor": {
                "harness": self._profile.harness,
                "model": self._profile.model,
                "profile": self._db_profile,
            },
        }
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = yaml.safe_dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        resp = self._client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        )
        if resp.status_code not in (200, 201, 409):
            raise RuntimeError(f"agent register failed: {resp.status_code} {resp.text[:400]}")
        return name

    def _create_session(self, agent_name: str, runner_id: str) -> str:
        assert self._client is not None
        listing = self._client.get("/v1/sessions", params={"agent_name": agent_name, "limit": 1})
        listing.raise_for_status()
        agent_id = str(listing.json()["data"][0]["agent_id"])
        created = self._client.post("/v1/sessions", json={"agent_id": agent_id})
        created.raise_for_status()
        session_id = str(created.json()["id"])
        bound = self._client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
        bound.raise_for_status()
        return session_id

    # ── turn ─────────────────────────────────────────────────

    def run_turn(
        self,
        prompt: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        deny_phases: frozenset[str] = frozenset(),
        policy_reason: str | None = None,
        auto_tool_output: str | None = None,
        interrupt_on_first_delta: bool = False,
        timeout: float = 180.0,
    ) -> TurnResult:
        """Drive one turn through the full server and return a :class:`TurnResult`.

        Unlike the wrap driver, policy is enforced by the *server*: when
        *deny_phases* names the tool-call phase, a fixed-action deny policy
        scoped to ``tool_call`` is attached to the session before the turn,
        so the server blocks the call the way production does.

        Tools are passed on the message; tool calls and their outputs are
        read from the session snapshot. Delta-level streaming is not
        measured here (that needs the SSE subscribe stream) — this driver
        targets the dimensions the wrap path cannot prove: real
        server-enforced policy and server-dispatched tools.

        :param interrupt_on_first_delta: Approximated on the full-server
            path — the interrupt is posted once the session is running,
            since snapshot polling has no per-delta signal.
        """
        assert self._client is not None and self._session_id is not None
        result = TurnResult()

        if PHASE_TOOL_CALL in deny_phases:
            self._attach_deny_policy(["tool_call"], policy_reason)

        body: dict[str, Any] = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        }
        if tools is not None:
            body["tools"] = tools
        posted = self._client.post(f"/v1/sessions/{self._session_id}/events", json=body)
        if posted.status_code == 202 and posted.json().get("denied"):
            # A synchronous (request-phase) DENY short-circuited the turn.
            result.failed = True
            result.error = {"denied": True, "reason": posted.json().get("reason")}
            return result
        posted.raise_for_status()

        deadline = time.monotonic() + timeout
        seen_running = False
        interrupted = False
        answered: set[str] = set()
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{self._session_id}")
            snap.raise_for_status()
            body = snap.json()
            status = body.get("status")
            items = body.get("items", [])
            self._scan_items(items, result, policy_reason, auto_tool_output, answered)

            if status in ("running", "waiting"):
                seen_running = True
                if interrupt_on_first_delta and not interrupted:
                    interrupted = True
                    self._client.post(
                        f"/v1/sessions/{self._session_id}/events", json={"type": "interrupt"}
                    )
            if status == "failed":
                result.failed = True
                result.error = body.get("last_task_error") or body.get("error")
                break
            if status == "idle" and seen_running:
                if interrupted:
                    result.cancelled = True
                else:
                    result.completed = True
                result.text = _assistant_text(items)
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result

    def _attach_deny_policy(self, on_phases: list[str], reason: str | None) -> None:
        """Attach a fixed-action deny policy scoped to *on_phases* to the session."""
        assert self._client is not None
        self._client.post(
            f"/v1/sessions/{self._session_id}/policies",
            json={
                "name": "bench_deny",
                "type": "function",
                "function": {
                    "path": "omnigent.policies.function.make_fixed_action_callable",
                    "arguments": {
                        "action": "deny",
                        "reason": reason or "bench-policy-deny",
                        "on_phases": on_phases,
                    },
                },
            },
        )

    def _scan_items(
        self,
        items: list[dict],
        result: TurnResult,
        policy_reason: str | None,
        auto_tool_output: str | None,
        answered: set[str],
    ) -> None:
        """Update *result* from session items: tool calls, blocks, tool outputs."""
        assert self._client is not None
        seen_calls = {tc.get("call_id") for tc in result.tool_calls}
        for raw in items:
            data = raw.get("data", raw)
            itype = raw.get("type") or data.get("type")
            if itype == "function_call":
                call_id = data.get("call_id") or raw.get("call_id")
                if call_id and call_id not in seen_calls:
                    result.tool_calls.append(
                        {
                            "call_id": call_id,
                            "name": data.get("name"),
                            "arguments": data.get("arguments"),
                        }
                    )
                    seen_calls.add(call_id)
                # Answer an outstanding call if the probe supplied an output
                # and the server is waiting on the client (ALLOW path).
                if (
                    auto_tool_output is not None
                    and raw.get("status") == "action_required"
                    and call_id
                    and call_id not in answered
                ):
                    answered.add(call_id)
                    self._client.post(
                        f"/v1/sessions/{self._session_id}/events",
                        json={
                            "type": "tool_result",
                            "call_id": call_id,
                            "output": auto_tool_output,
                        },
                    )
            elif itype == "function_call_output":
                out = str(data.get("output", ""))
                reason = policy_reason or "bench-policy-deny"
                if data.get("status") == "blocked" or reason in out:
                    result.tool_call_denied = True


def _assistant_text(items: list[dict]) -> str:
    """Concatenate assistant output_text from session items."""
    out: list[str] = []
    for item in items:
        data = item.get("data", item)
        if data.get("role") == "assistant" or item.get("role") == "assistant":
            for block in data.get("content", []) or []:
                if block.get("type") in ("output_text", "text"):
                    out.append(block.get("text", ""))
    return "\n".join(t for t in out if t)
