"""Local HTTP API server for LLM agent <-> QuickHatch wizard communication.

The LLM reads the user profile, posts its analysis/suggestions, and asks
questions — all through this API. The wizard TUI displays everything and
collects user responses.

No external dependencies — uses stdlib http.server + json.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from quickhatch.config import UserProfile

# On-disk persistence for suggestions and messages. Survives server restarts
# so an accidentally-killed process doesn't nuke a 20-minute analysis run.
_SUGGESTIONS_FILE = Path.home() / ".quickhatch-suggestions.json"


@dataclass
class PendingQuestion:
    """A question the LLM is asking the user."""

    id: str
    question: str
    choices: list[str] | None = None  # None = free text, list = pick from choices
    answer: str | None = None
    answered: threading.Event = field(default_factory=threading.Event)


@dataclass
class Suggestion:
    """A suggestion/analysis posted by the LLM."""

    id: str
    category: str  # "distro", "apps", "drivers", "desktop", "message", "plan"
    title: str
    content: Any  # structured data or markdown string
    timestamp: float = field(default_factory=time.time)
    accepted: bool | None = None  # None = pending, True/False = user responded


class BridgeState:
    """Shared state between the HTTP server and the wizard TUI."""

    def __init__(
        self, profile: UserProfile, persist_path: Path | str | None = _SUGGESTIONS_FILE
    ) -> None:
        self.profile = profile
        self.suggestions: list[Suggestion] = []
        self.questions: dict[str, PendingQuestion] = {}
        self.messages: list[dict[str, str]] = []
        self.lock = threading.Lock()
        self._on_suggestion: list[Any] = []
        self._on_question: list[Any] = []
        self._on_message: list[Any] = []
        self.agent_connected = False
        self.agent_done = False
        self.stop_requested = False
        # Where to persist suggestions/messages. Pass None to disable
        # persistence (e.g. in unit tests).
        self._persist_path: Path | None = Path(persist_path) if persist_path else None
        # Monotonic version counter incremented under the lock on every
        # state mutation. Split-pattern writers snapshot this value
        # alongside their payload, then a per-write lock + version
        # check ensures an older snapshot can never overwrite a newer
        # one. Prevents reordering when two mutators snapshot-then-
        # write interleave.
        self._persist_version: int = 0
        self._persist_last_written: int = 0
        self._persist_write_lock = threading.Lock()
        # Restore previously persisted suggestions/messages so restarts
        # don't lose a finished analysis run.
        self._load_persisted()

    def _profile_fingerprint(self) -> str:
        """Short hash of the profile fields that the persisted suggestions
        depend on. If any of these change (distro, desktop, apps picked,
        another-machine toggle), the on-disk suggestions are stale and
        must not be restored — they'd contradict the new preferences."""
        import hashlib

        prefs = getattr(self.profile, "preferences", None)
        parts = [
            getattr(prefs, "distro_preference", "") or "",
            getattr(prefs, "desktop_look", "") or "",
            getattr(prefs, "another_machine", "") or "",
            ",".join(sorted(getattr(prefs, "gallery_picks", []) or [])),
            ",".join(sorted(getattr(self.profile, "apps_to_install", []) or [])),
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def _load_persisted(self) -> None:
        """Load suggestions + messages + pending questions from disk.

        Skips the restore if the persisted profile fingerprint doesn't
        match the current one (e.g. user changed distro preference
        since the last run) — those suggestions would describe a
        different migration."""
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Move the corrupt file aside so the next run starts clean —
            # mirrors what load_checkpoint does for the profile.
            import sys as _sys

            backup = self._persist_path.with_suffix(self._persist_path.suffix + ".corrupt")
            try:
                self._persist_path.rename(backup)
            except OSError:
                pass
            print(
                f"[quickhatch] persisted suggestions corrupt ({exc}); moved to {backup}.",
                file=_sys.stderr,
            )
            return
        saved_fp = data.get("profile_fingerprint")
        current_fp = self._profile_fingerprint()
        # Legacy files written before the fingerprint scheme have no
        # profile_fingerprint. Those are guaranteed stale relative to
        # the current profile — accept them only when the current
        # profile itself is also empty (fresh-wizard state).
        EMPTY_PROFILE_FP = "45ca31c3315a5978"
        if saved_fp is None and current_fp != EMPTY_PROFILE_FP:
            import sys as _sys

            print(
                "[quickhatch] persisted suggestions have no profile "
                "fingerprint (pre-versioning format). Starting fresh.",
                file=_sys.stderr,
            )
            return
        if saved_fp and saved_fp != current_fp:
            import sys as _sys

            print(
                f"[quickhatch] persisted suggestions were for a different "
                f"profile ({saved_fp} != {current_fp}). Starting fresh.",
                file=_sys.stderr,
            )
            return
        for s in data.get("suggestions", []):
            try:
                self.suggestions.append(Suggestion(**s))
            except TypeError:
                continue
        self.messages = list(data.get("messages", []))
        # Restore only ANSWERED questions (history) — unanswered
        # questions from a prior run belong to a dead agent thread.
        # Reviving them would block the next run on a question nobody
        # wants an answer to, and in local-bridge mode the wizard
        # immediately re-prompts for every pending Q which creates UI
        # noise. The agent can always re-ask if it still needs input.
        for qdata in data.get("questions", []):
            if not qdata.get("answered"):
                continue
            try:
                q = PendingQuestion(
                    id=qdata["id"],
                    question=qdata["question"],
                    choices=qdata.get("choices"),
                    answer=qdata.get("answer"),
                )
                if qdata.get("answered"):
                    q.answered.set()
                    q.answer = qdata.get("answer")
                self.questions[q.id] = q
            except (KeyError, TypeError):
                continue
        # agent_done stays False so the UI doesn't think a new run is
        # already complete — but the previous suggestions are visible.

    def _serialize_payload_locked(self) -> tuple[str, int] | None:
        """Build the JSON payload string + its version. MUST be called
        with ``self.lock`` held. Returns None when persistence is
        disabled.
        """
        if self._persist_path is None:
            return None
        self._persist_version += 1
        version = self._persist_version
        payload = {
            "profile_fingerprint": self._profile_fingerprint(),
            "suggestions": [asdict(s) for s in self.suggestions],
            "messages": list(self.messages),
            "questions": [
                {
                    "id": q.id,
                    "question": q.question,
                    "choices": q.choices,
                    "answered": q.answered.is_set(),
                    "answer": q.answer,
                }
                for q in self.questions.values()
            ],
            "saved_at": time.time(),
        }
        try:
            return (json.dumps(payload, indent=2), version)
        except (TypeError, ValueError):
            return None

    def _write_payload_unlocked(self, snapshot: tuple[str, int] | None) -> None:
        """Write a pre-serialized payload to disk. MUST NOT hold the main
        state lock. Atomic via tmp-file + os.replace.

        A separate write-lock + monotonic version check ensures that if
        two threads interleave (A serializes v1, B serializes v2, B
        writes v2, A tries to write v1), the older snapshot is dropped
        rather than overwriting the newer state on disk.
        """
        if self._persist_path is None or snapshot is None:
            return
        data, version = snapshot
        import os as _os

        with self._persist_write_lock:
            if version <= self._persist_last_written:
                return  # a newer snapshot already landed; our write is stale
            try:
                tmp = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
                tmp.write_text(data, encoding="utf-8")
                _os.replace(tmp, self._persist_path)
                self._persist_last_written = version
            except OSError:
                pass  # best-effort

    def _persist(self) -> None:
        """Serialize + flush to disk. Callers either:
         - Hold the lock and call this (LEGACY — holds lock during I/O).
         - Prefer the split pattern: grab ``_serialize_payload_locked``
           under lock, release, then call ``_write_payload_unlocked``.
        Kept for back-compat with call sites we haven't migrated; new
        mutators should use the split pattern to avoid blocking other
        threads on disk I/O.
        """
        data = self._serialize_payload_locked()
        self._write_payload_unlocked(data)

    def reset_for_rerun(self) -> None:
        """Clear suggestions and done state for a fresh agent run.

        Keeps the chat history so user feedback remains visible when the
        agent regenerates.
        """
        with self.lock:
            self.suggestions.clear()
            self.agent_done = False
            self.stop_requested = False
            self._persist()

    def request_stop(self) -> None:
        """Signal any running agent to stop at the next iteration."""
        with self.lock:
            self.stop_requested = True

    def prepare_for_launch(self) -> None:
        """Clear stop_requested + agent_done under the lock so the loop
        doesn't exit on its first iteration when an earlier Stop click
        or a stale post-restart state left the flag set."""
        with self.lock:
            self.stop_requested = False
            self.agent_done = False
            self._persist()

    def snapshot_suggestions(self) -> list[Suggestion]:
        """SHALLOW copy of the suggestions list, atomically, under the lock."""
        with self.lock:
            return list(self.suggestions)

    def replace_suggestions(self, new_list: list[Suggestion]) -> None:
        """Atomically replace the suggestions list under the lock + persist."""
        with self.lock:
            self.suggestions[:] = new_list
            self._persist()

    def add_suggestion(self, category: str, title: str, content: Any) -> str:
        sid = str(uuid.uuid4())[:8]
        suggestion = Suggestion(id=sid, category=category, title=title, content=content)
        with self.lock:
            self.suggestions.append(suggestion)
            data = self._serialize_payload_locked()
        self._write_payload_unlocked(data)
        for cb in self._on_suggestion:
            cb(suggestion)
        return sid

    def add_question(self, question: str, choices: list[str] | None = None) -> PendingQuestion:
        qid = str(uuid.uuid4())[:8]
        q = PendingQuestion(id=qid, question=question, choices=choices)
        with self.lock:
            self.questions[qid] = q
            data = self._serialize_payload_locked()
        self._write_payload_unlocked(data)
        for cb in self._on_question:
            cb(q)
        return q

    def answer_question(self, qid: str, answer: str) -> bool:
        with self.lock:
            q = self.questions.get(qid)
            if not q:
                return False
            q.answer = answer
            q.answered.set()
            data = self._serialize_payload_locked()
        self._write_payload_unlocked(data)
        return True

    def accept_suggestion(self, sid: str, accepted: bool) -> bool:
        found = False
        data: str | None = None
        with self.lock:
            for s in self.suggestions:
                if s.id == sid:
                    s.accepted = accepted
                    data = self._serialize_payload_locked()
                    found = True
                    break
        if found:
            self._write_payload_unlocked(data)
        return found

    def add_message(self, role: str, content: str) -> None:
        with self.lock:
            self.messages.append({"role": role, "content": content, "time": time.time()})
            data = self._serialize_payload_locked()
        self._write_payload_unlocked(data)
        for cb in self._on_message:
            cb(role, content)

    def on_suggestion(self, callback: Any) -> None:
        self._on_suggestion.append(callback)

    def on_question(self, callback: Any) -> None:
        self._on_question.append(callback)

    def on_message(self, callback: Any) -> None:
        self._on_message.append(callback)

    def get_pending_questions(self) -> list[PendingQuestion]:
        with self.lock:
            return [q for q in self.questions.values() if not q.answered.is_set()]

    def get_suggestions_by_category(self, category: str) -> list[Suggestion]:
        with self.lock:
            return [s for s in self.suggestions if s.category == category]


def _make_handler(state: BridgeState):
    """Create a request handler class with access to shared state."""

    class BridgeHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default logging

        def _send_json(self, data: Any, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def _read_json(self) -> Any:
            # Cap body size — prevents a malicious Content-Length of 2 GB
            # from forcing us to read / allocate that much.
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            _MAX_BODY = 8 * 1024 * 1024  # 8 MiB
            if length > _MAX_BODY:
                return {}
            body = self.rfile.read(max(length, 0))
            if not body:
                return {}
            try:
                return json.loads(body)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
                return {}

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path == "/api/profile":
                # Return the full user profile for the LLM to read
                profile_dict = asdict(state.profile)
                # Redact API key
                if profile_dict.get("ai_config", {}).get("api_key"):
                    profile_dict["ai_config"]["api_key"] = "***REDACTED***"
                self._send_json({"status": "ok", "profile": profile_dict})

            elif self.path == "/api/status":
                state.agent_connected = True
                self._send_json(
                    {
                        "status": "ok",
                        "pending_questions": len(state.get_pending_questions()),
                        "suggestions": len(state.suggestions),
                        "agent_done": state.agent_done,
                    }
                )

            elif self.path.startswith("/api/response/"):
                qid = self.path.split("/")[-1]
                with state.lock:
                    q = state.questions.get(qid)
                if not q:
                    self._send_json({"status": "error", "message": "Question not found"}, 404)
                elif q.answered.is_set():
                    self._send_json({"status": "ok", "answer": q.answer})
                else:
                    self._send_json({"status": "waiting", "message": "User hasn't answered yet"})

            elif self.path == "/api/suggestions":
                with state.lock:
                    data = [
                        {
                            "id": s.id,
                            "category": s.category,
                            "title": s.title,
                            "content": s.content,
                            "accepted": s.accepted,
                        }
                        for s in state.suggestions
                    ]
                self._send_json({"status": "ok", "suggestions": data})

            else:
                self._send_json({"status": "error", "message": "Not found"}, 404)

        def do_POST(self):
            data = self._read_json()

            if self.path == "/api/suggest":
                # LLM posts a suggestion/analysis
                category = data.get("category", "message")
                title = data.get("title", "")
                content = data.get("content", "")
                sid = state.add_suggestion(category, title, content)
                self._send_json({"status": "ok", "suggestion_id": sid})

            elif self.path == "/api/ask":
                # LLM asks the user a question and waits for answer
                question = data.get("question", "")
                choices = data.get("choices")  # optional list
                timeout = data.get("timeout", 300)  # default 5 min

                q = state.add_question(question, choices)

                # Block until user answers or timeout
                answered = q.answered.wait(timeout=timeout)
                if answered:
                    self._send_json({"status": "ok", "answer": q.answer})
                else:
                    self._send_json(
                        {"status": "timeout", "message": "User did not respond in time"}
                    )

            elif self.path == "/api/ask/async":
                # Non-blocking ask — LLM polls /api/response/{id} later
                question = data.get("question", "")
                choices = data.get("choices")
                q = state.add_question(question, choices)
                self._send_json({"status": "ok", "question_id": q.id})

            elif self.path == "/api/message":
                # LLM sends a message to display to the user
                content = data.get("content", "")
                role = data.get("role", "agent")
                state.add_message(role, content)
                self._send_json({"status": "ok"})

            elif self.path == "/api/done":
                # LLM signals it has finished
                state.agent_done = True
                summary = data.get("summary", "")
                state.add_message("agent", f"Migration setup complete! {summary}")
                self._send_json({"status": "ok"})

            else:
                self._send_json({"status": "error", "message": "Not found"}, 404)

    return BridgeHandler


class BridgeServer:
    """Manages the local HTTP server lifecycle."""

    def __init__(self, state: BridgeState, port: int = 0) -> None:
        self.state = state
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        """Start the server and return the actual port."""
        handler = _make_handler(self.state)
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        actual_port = self._server.server_address[1]
        self.port = actual_port

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return actual_port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
