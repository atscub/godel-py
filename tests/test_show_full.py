"""Tests for godel show --full <event_id>.

Acceptance criteria (from godel-py-6wg):
  AC-1  Full payload reconstruction: given a small transcript.jsonl fixture,
        get_full_payload returns the untruncated prompt and assembled response.
  AC-2  Rotation chain traversal: if transcript events span archived files,
        all chunks are collected.
  AC-3  CLI --full flag: invoking `godel show <run> --full <event_id>` prints
        the full content without truncation.
  AC-4  Missing transcript: FileNotFoundError raised (and CLI exits non-zero).
  AC-5  Unknown event_id: KeyError raised (and CLI exits non-zero).
  AC-6  Audit log default view unchanged (no truncation change to normal show).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from godel._event_log import EventLog
from godel.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_audit_event(
    event_id: str,
    run_id: str,
    op: str,
    stream_path: list[str],
    *,
    seq: int = 0,
    step_path: list[str] | None = None,
    status: str = "FINISHED",
) -> dict:
    """Build a minimal audit log event dict."""
    return {
        "event_id": event_id,
        "run_id": run_id,
        "seq": seq,
        "children_ids": [],
        "step_path": step_path or [],
        "invocation_seq": 0,
        "step_local_seq": 0,
        "op": op,
        "request_hash": "",
        "request": {"prompt": "short truncated..."},
        "response": {"type": "text", "value": "short truncated..."},
        "status": status,
        "ts_start": "2026-01-01T00:00:00+00:00",
        "ts_end": "2026-01-01T00:00:01+00:00",
        "stream_path": stream_path,
    }


def _write_audit_jsonl(runs_dir: Path, run_id: str, events: list[dict]) -> Path:
    """Write audit JSONL file and return path."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    p = runs_dir / f"{run_id}.jsonl"
    with open(p, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def _write_transcript(transcript_dir: Path, events: list[dict]) -> None:
    """Write a minimal transcript.jsonl with a header + event lines."""
    transcript_dir.mkdir(parents=True, exist_ok=True)
    p = transcript_dir / "transcript.jsonl"
    with open(p, "w") as f:
        header = {"header": {"v": 1, "run_id": transcript_dir.name, "started_at": "2026-01-01T00:00:00+00:00"}}
        f.write(json.dumps(header) + "\n")
        for seq, evt in enumerate(events, start=1):
            wrapped = {"event": {**evt, "seq": seq, "ts": "2026-01-01T00:00:00+00:00"}}
            f.write(json.dumps(wrapped) + "\n")


# ---------------------------------------------------------------------------
# Unit tests: EventLog.get_full_payload
# ---------------------------------------------------------------------------


class TestGetFullPayload:
    def test_returns_full_prompt_and_response(self, tmp_path):
        """AC-1: full payload reconstructed from transcript."""
        run_id = "run-abc123"
        stream_path = ["ULID_LAUNCH_01"]
        long_prompt = "A" * 2000
        response_part1 = "B" * 1000
        response_part2 = "C" * 500

        # Write audit log
        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVT001", run_id, "agent.call", stream_path)]
        _write_audit_jsonl(runs_dir, run_id, events)

        # Write transcript with agent.prompt + two agent.response chunks
        transcript_dir = runs_dir / run_id
        transcript_events = [
            {
                "op": "agent.prompt",
                "step_path": ["investigate"],
                "stream_path": stream_path,
                "model": "claude-test",
                "prompt": long_prompt,
                "session_id": None,
            },
            {
                "op": "agent.response",
                "step_path": ["investigate"],
                "stream_path": stream_path,
                "text": response_part1,
            },
            {
                "op": "agent.response",
                "step_path": ["investigate"],
                "stream_path": stream_path,
                "text": response_part2,
            },
        ]
        _write_transcript(transcript_dir, transcript_events)

        log = EventLog.load(run_id, runs_dir=str(runs_dir))
        payload = log.get_full_payload("EVT001", runs_dir=str(runs_dir))
        log.close()

        assert payload["event_id"] == "EVT001"
        assert payload["op"] == "agent.call"
        assert payload["request"] == long_prompt
        assert payload["response"] == response_part1 + response_part2
        assert payload["model"] == "claude-test"

    def test_filters_by_stream_path(self, tmp_path):
        """Only transcript events with matching stream_path are collected."""
        run_id = "run-filter"
        target_stream = ["STREAM_A"]
        other_stream = ["STREAM_B"]

        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVTA", run_id, "agent.call", target_stream)]
        _write_audit_jsonl(runs_dir, run_id, events)

        transcript_dir = runs_dir / run_id
        transcript_events = [
            {
                "op": "agent.prompt",
                "step_path": ["s1"],
                "stream_path": target_stream,
                "model": "claude-test",
                "prompt": "correct prompt",
                "session_id": None,
            },
            {
                "op": "agent.prompt",
                "step_path": ["s2"],
                "stream_path": other_stream,
                "model": "claude-test",
                "prompt": "wrong prompt",
                "session_id": None,
            },
            {
                "op": "agent.response",
                "step_path": ["s1"],
                "stream_path": target_stream,
                "text": "correct response",
            },
            {
                "op": "agent.response",
                "step_path": ["s2"],
                "stream_path": other_stream,
                "text": "wrong response",
            },
        ]
        _write_transcript(transcript_dir, transcript_events)

        log = EventLog.load(run_id, runs_dir=str(runs_dir))
        payload = log.get_full_payload("EVTA", runs_dir=str(runs_dir))
        log.close()

        assert payload["request"] == "correct prompt"
        assert payload["response"] == "correct response"

    def test_raises_key_error_for_unknown_event(self, tmp_path):
        """AC-5: KeyError raised for unknown event_id."""
        run_id = "run-missing-evt"
        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVT999", run_id, "agent.call", ["SP1"])]
        _write_audit_jsonl(runs_dir, run_id, events)

        # Need transcript dir too so we don't fail on that
        transcript_dir = runs_dir / run_id
        _write_transcript(transcript_dir, [])

        log = EventLog.load(run_id, runs_dir=str(runs_dir))
        with pytest.raises(KeyError, match="NONEXISTENT"):
            log.get_full_payload("NONEXISTENT", runs_dir=str(runs_dir))
        log.close()

    def test_raises_file_not_found_when_no_transcript(self, tmp_path):
        """AC-4: FileNotFoundError raised if transcript.jsonl does not exist."""
        run_id = "run-no-transcript"
        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVT001", run_id, "agent.call", ["SP1"])]
        _write_audit_jsonl(runs_dir, run_id, events)
        # Deliberately do NOT write transcript

        log = EventLog.load(run_id, runs_dir=str(runs_dir))
        with pytest.raises(FileNotFoundError):
            log.get_full_payload("EVT001", runs_dir=str(runs_dir))
        log.close()

    def test_response_none_when_no_chunks(self, tmp_path):
        """Response is None when no agent.response events exist."""
        run_id = "run-no-resp"
        stream_path = ["SP1"]

        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVTA", run_id, "agent.call", stream_path)]
        _write_audit_jsonl(runs_dir, run_id, events)

        transcript_dir = runs_dir / run_id
        _write_transcript(transcript_dir, [
            {
                "op": "agent.prompt",
                "step_path": [],
                "stream_path": stream_path,
                "model": "m",
                "prompt": "hello",
                "session_id": None,
            }
        ])

        log = EventLog.load(run_id, runs_dir=str(runs_dir))
        payload = log.get_full_payload("EVTA", runs_dir=str(runs_dir))
        log.close()

        assert payload["request"] == "hello"
        assert payload["response"] is None

    def test_rotation_chain_traversal(self, tmp_path):
        """AC-2: response chunks spanning archived transcript files are all collected."""
        run_id = "run-rotation"
        stream_path = ["SP_ROT"]

        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVTROT", run_id, "agent.call", stream_path)]
        _write_audit_jsonl(runs_dir, run_id, events)

        transcript_dir = runs_dir / run_id
        transcript_dir.mkdir(parents=True, exist_ok=True)

        # Write archived file (older): transcript.jsonl.1
        chunk1 = "CHUNK1_" * 100
        chunk2 = "CHUNK2_" * 100
        chunk3 = "CHUNK3_" * 100

        archive_path = transcript_dir / "transcript.jsonl.1"
        with open(archive_path, "w") as f:
            f.write(json.dumps({"header": {"v": 1}}) + "\n")
            f.write(json.dumps({"event": {
                "seq": 1, "ts": "2026-01-01T00:00:00+00:00",
                "op": "agent.prompt",
                "step_path": [],
                "stream_path": stream_path,
                "model": "claude-rotation-test",
                "prompt": "archived prompt",
                "session_id": None,
            }}) + "\n")
            f.write(json.dumps({"event": {
                "seq": 2, "ts": "2026-01-01T00:00:00+00:00",
                "op": "agent.response",
                "step_path": [],
                "stream_path": stream_path,
                "text": chunk1,
            }}) + "\n")

        # Write live file (newer): transcript.jsonl
        live_path = transcript_dir / "transcript.jsonl"
        with open(live_path, "w") as f:
            f.write(json.dumps({"header": {"v": 1}}) + "\n")
            f.write(json.dumps({"event": {
                "seq": 3, "ts": "2026-01-01T00:00:01+00:00",
                "op": "agent.response",
                "step_path": [],
                "stream_path": stream_path,
                "text": chunk2,
            }}) + "\n")
            f.write(json.dumps({"event": {
                "seq": 4, "ts": "2026-01-01T00:00:02+00:00",
                "op": "agent.response",
                "step_path": [],
                "stream_path": stream_path,
                "text": chunk3,
            }}) + "\n")

        log = EventLog.load(run_id, runs_dir=str(runs_dir))
        payload = log.get_full_payload("EVTROT", runs_dir=str(runs_dir))
        log.close()

        assert payload["request"] == "archived prompt"
        # All three chunks collected regardless of file boundary
        assert chunk1 in payload["response"]
        assert chunk2 in payload["response"]
        assert chunk3 in payload["response"]


# ---------------------------------------------------------------------------
# CLI integration tests: godel show --full
# ---------------------------------------------------------------------------


class TestShowFullCLI:
    def _setup_run(
        self,
        tmp_path: Path,
        run_id: str,
        stream_path: list[str],
        prompt: str,
        response: str,
    ) -> tuple[Path, str]:
        """Create audit + transcript for a single agent.call event."""
        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVTCLI", run_id, "agent.call", stream_path, step_path=["my_step"])]
        _write_audit_jsonl(runs_dir, run_id, events)

        transcript_dir = runs_dir / run_id
        _write_transcript(transcript_dir, [
            {
                "op": "agent.prompt",
                "step_path": ["my_step"],
                "stream_path": stream_path,
                "model": "claude-test",
                "prompt": prompt,
                "session_id": None,
            },
            {
                "op": "agent.response",
                "step_path": ["my_step"],
                "stream_path": stream_path,
                "model": "claude-test",
                "text": response,
            },
        ])
        return runs_dir, "EVTCLI"

    def test_full_flag_shows_untruncated_content(self, tmp_path, monkeypatch):
        """AC-3: --full flag outputs full prompt and response."""
        monkeypatch.chdir(tmp_path)
        long_prompt = "PROMPT_" * 300  # 2100 chars, well beyond 500
        long_response = "RESP_" * 300

        runs_dir, event_id = self._setup_run(
            tmp_path, "run-full-test", ["ULID001"], long_prompt, long_response
        )

        runner = CliRunner()
        result = runner.invoke(main, ["show", "run-full-test", "--full", event_id])
        assert result.exit_code == 0, result.output
        assert long_prompt in result.output
        assert long_response in result.output

    def test_full_flag_shows_metadata_header(self, tmp_path, monkeypatch):
        """--full output includes event ID, op, step path, and model."""
        monkeypatch.chdir(tmp_path)
        runs_dir, event_id = self._setup_run(
            tmp_path, "run-meta-test", ["ULID002"], "hello prompt", "hello response"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["show", "run-meta-test", "--full", event_id])
        assert result.exit_code == 0, result.output
        assert "EVTCLI" in result.output
        assert "agent.call" in result.output
        assert "my_step" in result.output
        assert "claude-test" in result.output

    def test_full_flag_unknown_event_id(self, tmp_path, monkeypatch):
        """AC-5: --full with unknown event_id exits non-zero."""
        monkeypatch.chdir(tmp_path)
        runs_dir, _ = self._setup_run(
            tmp_path, "run-unk", ["ULID003"], "p", "r"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["show", "run-unk", "--full", "NO_SUCH_EVENT"])
        assert result.exit_code != 0

    def test_full_flag_missing_transcript(self, tmp_path, monkeypatch):
        """AC-4: --full exits non-zero when transcript.jsonl is absent."""
        monkeypatch.chdir(tmp_path)
        run_id = "run-no-trans"
        runs_dir = tmp_path / "runs"
        events = [_make_audit_event("EVTX", run_id, "agent.call", ["SP1"])]
        _write_audit_jsonl(runs_dir, run_id, events)
        # No transcript directory

        runner = CliRunner()
        result = runner.invoke(main, ["show", run_id, "--full", "EVTX"])
        assert result.exit_code != 0

    def test_default_show_unchanged(self, tmp_path, monkeypatch):
        """AC-6: godel show without --full works exactly as before."""
        monkeypatch.chdir(tmp_path)
        runs_dir = tmp_path / "runs"
        events = [
            {
                "event_id": "WF001",
                "run_id": "plain-run",
                "seq": 0,
                "children_ids": [],
                "step_path": [],
                "invocation_seq": 0,
                "step_local_seq": 0,
                "op": "WORKFLOW_STARTED",
                "request_hash": "",
                "request": {},
                "response": None,
                "status": "FINISHED",
                "ts_start": "2026-01-01T00:00:00+00:00",
                "ts_end": "2026-01-01T00:00:01+00:00",
                "stream_path": [],
            }
        ]
        runs_dir.mkdir(parents=True, exist_ok=True)
        with open(runs_dir / "plain-run.jsonl", "w") as f:
            f.write(json.dumps(events[0]) + "\n")

        runner = CliRunner()
        result = runner.invoke(main, ["show", "plain-run"])
        assert result.exit_code == 0
        assert "WORKFLOW_STARTED" in result.output
