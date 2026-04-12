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
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from quickhatch.config import UserProfile


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

    def __init__(self, profile: UserProfile) -> None:
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

    def reset_for_rerun(self) -> None:
        """Clear suggestions and done state for a fresh agent run."""
        with self.lock:
            self.suggestions.clear()
            self.agent_done = False

    def add_suggestion(self, category: str, title: str, content: Any) -> str:
        sid = str(uuid.uuid4())[:8]
        suggestion = Suggestion(id=sid, category=category, title=title, content=content)
        with self.lock:
            self.suggestions.append(suggestion)
        for cb in self._on_suggestion:
            cb(suggestion)
        return sid

    def add_question(self, question: str, choices: list[str] | None = None) -> PendingQuestion:
        qid = str(uuid.uuid4())[:8]
        q = PendingQuestion(id=qid, question=question, choices=choices)
        with self.lock:
            self.questions[qid] = q
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
        return True

    def accept_suggestion(self, sid: str, accepted: bool) -> bool:
        with self.lock:
            for s in self.suggestions:
                if s.id == sid:
                    s.accepted = accepted
                    return True
        return False

    def add_message(self, role: str, content: str) -> None:
        with self.lock:
            self.messages.append({"role": role, "content": content, "time": time.time()})
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
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            return json.loads(body) if body else {}

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
                self._send_json({
                    "status": "ok",
                    "pending_questions": len(state.get_pending_questions()),
                    "suggestions": len(state.suggestions),
                    "agent_done": state.agent_done,
                })

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
                    self._send_json({"status": "timeout", "message": "User did not respond in time"})

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
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        """Start the server and return the actual port."""
        handler = _make_handler(self.state)
        self._server = HTTPServer(("127.0.0.1", self.port), handler)
        actual_port = self._server.server_address[1]
        self.port = actual_port

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return actual_port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
