"""Tests for BridgeState — the shared-state hub used by the bridge HTTP API."""

from __future__ import annotations

import json
import threading

import pytest

from quickhatch.bridge.server import BridgeState
from quickhatch.config import UserProfile


@pytest.fixture
def state():
    return BridgeState(UserProfile(), persist_path=None)


class TestSuggestions:
    def test_add_suggestion_returns_id(self, state):
        sid = state.add_suggestion("distro", "Arch", "pacman is nice")
        assert isinstance(sid, str) and len(sid) >= 4

    def test_add_suggestion_stores(self, state):
        state.add_suggestion("distro", "Arch", "pacman")
        assert len(state.suggestions) == 1
        assert state.suggestions[0].category == "distro"
        assert state.suggestions[0].title == "Arch"

    def test_suggestion_ids_are_unique(self, state):
        s1 = state.add_suggestion("a", "t1", "c1")
        s2 = state.add_suggestion("a", "t2", "c2")
        assert s1 != s2

    def test_on_suggestion_callback_fires(self, state):
        received = []
        state.on_suggestion(lambda s: received.append(s))
        state.add_suggestion("apps", "Apps", "vim")
        assert len(received) == 1
        assert received[0].title == "Apps"

    def test_multiple_callbacks_all_fire(self, state):
        a, b = [], []
        state.on_suggestion(lambda s: a.append(s))
        state.on_suggestion(lambda s: b.append(s))
        state.add_suggestion("x", "y", "z")
        assert len(a) == 1 and len(b) == 1

    def test_accept_suggestion_marks_accepted(self, state):
        sid = state.add_suggestion("x", "y", "z")
        assert state.accept_suggestion(sid, True) is True
        assert state.suggestions[0].accepted is True

    def test_accept_suggestion_missing_id_returns_false(self, state):
        assert state.accept_suggestion("bogus", True) is False

    def test_get_suggestions_by_category(self, state):
        state.add_suggestion("distro", "Arch", "")
        state.add_suggestion("apps", "Firefox", "")
        state.add_suggestion("distro", "Ubuntu", "")
        distros = state.get_suggestions_by_category("distro")
        assert len(distros) == 2
        assert all(s.category == "distro" for s in distros)


class TestQuestions:
    def test_add_question_returns_question(self, state):
        q = state.add_question("Favorite distro?")
        assert q.id
        assert q.question == "Favorite distro?"
        assert q.choices is None
        assert not q.answered.is_set()

    def test_question_with_choices(self, state):
        q = state.add_question("Pick one", choices=["a", "b", "c"])
        assert q.choices == ["a", "b", "c"]

    def test_pending_questions_excludes_answered(self, state):
        q1 = state.add_question("one")
        q2 = state.add_question("two")
        state.answer_question(q1.id, "done")
        pending = state.get_pending_questions()
        assert len(pending) == 1
        assert pending[0].id == q2.id

    def test_answer_question_sets_event(self, state):
        q = state.add_question("q?")
        assert state.answer_question(q.id, "yes") is True
        assert q.answered.is_set()
        assert q.answer == "yes"

    def test_answer_missing_question_returns_false(self, state):
        assert state.answer_question("nope", "x") is False

    def test_blocking_wait_on_answer(self, state):
        q = state.add_question("blocker")
        result = {}

        def waiter():
            result["answered"] = q.answered.wait(timeout=2)
            result["value"] = q.answer

        t = threading.Thread(target=waiter)
        t.start()
        # Briefly yield so the thread begins waiting
        t.join(timeout=0.1)
        state.answer_question(q.id, "42")
        t.join(timeout=2)
        assert result["answered"] is True
        assert result["value"] == "42"

    def test_on_question_callback_fires(self, state):
        received = []
        state.on_question(lambda q: received.append(q))
        state.add_question("hi")
        assert len(received) == 1

    def test_blocking_question_updates_setup_run_state(self, state):
        q = state.add_question(
            "Need confirmation?",
            blocking=True,
            phase="setup",
            context={"stage": "apps"},
        )

        setup_run = state.snapshot_setup_run()

        assert q.blocking is True
        assert q.phase == "setup"
        assert q.context == {"stage": "apps"}
        assert setup_run["waiting_for_input"] is True
        assert setup_run["active_question_id"] == q.id

    def test_answering_blocking_question_clears_wait_state(self, state):
        q = state.add_question("Need confirmation?", blocking=True, phase="setup")
        assert state.answer_question(q.id, "yes") is True

        setup_run = state.snapshot_setup_run()

        assert setup_run["waiting_for_input"] is False
        assert setup_run["active_question_id"] == ""


class TestMessages:
    def test_add_message_stores_with_timestamp(self, state):
        state.add_message("agent", "hello")
        assert len(state.messages) == 1
        m = state.messages[0]
        assert m["role"] == "agent"
        assert m["content"] == "hello"
        assert "time" in m and isinstance(m["time"], float)

    def test_on_message_callback_receives_role_and_content(self, state):
        received = []
        state.on_message(lambda role, content: received.append((role, content)))
        state.add_message("user", "what's next?")
        assert received == [("user", "what's next?")]

    def test_multiple_messages_preserve_order(self, state):
        state.add_message("agent", "one")
        state.add_message("user", "two")
        state.add_message("agent", "three")
        assert [m["content"] for m in state.messages] == ["one", "two", "three"]


class TestStopRequested:
    def test_default_not_stopped(self, state):
        assert state.stop_requested is False

    def test_request_stop_flips_flag(self, state):
        state.request_stop()
        assert state.stop_requested is True

    def test_request_stop_is_idempotent(self, state):
        state.request_stop()
        state.request_stop()
        assert state.stop_requested is True


class TestResetForRerun:
    def test_clears_suggestions(self, state):
        state.add_suggestion("distro", "Arch", "")
        state.add_suggestion("apps", "Firefox", "")
        state.reset_for_rerun()
        assert state.suggestions == []

    def test_clears_done_flag(self, state):
        state.agent_done = True
        state.reset_for_rerun()
        assert state.agent_done is False

    def test_clears_stop_flag(self, state):
        state.request_stop()
        state.reset_for_rerun()
        assert state.stop_requested is False

    def test_does_not_clear_messages(self, state):
        """Chat history should persist across re-runs so the user feedback
        is visible when the agent regenerates."""
        state.add_message("user", "Actually use Arch")
        state.reset_for_rerun()
        assert len(state.messages) == 1

    def test_does_not_clear_profile(self, state):
        """The profile is what drives the next run — must not be wiped."""
        state.profile.additional_info = "Something important"
        state.reset_for_rerun()
        assert state.profile.additional_info == "Something important"

    def test_clears_unanswered_questions(self, state):
        q = state.add_question("Still there?")
        state.reset_for_rerun()
        assert q.id not in state.questions

    def test_keeps_answered_questions_as_history(self, state):
        q = state.add_question("Keep me?")
        state.answer_question(q.id, "yes")
        state.reset_for_rerun()
        assert q.id in state.questions
        assert state.questions[q.id].answer == "yes"


class TestPrepareForLaunch:
    def test_clears_transient_flags(self, state):
        state.stop_requested = True
        state.agent_done = True
        state.prepare_for_launch()
        assert state.stop_requested is False
        assert state.agent_done is False

    def test_clears_only_unanswered_questions(self, state):
        stale = state.add_question("Stale?")
        kept = state.add_question("Answered?")
        state.answer_question(kept.id, "done")
        state.prepare_for_launch()
        assert stale.id not in state.questions
        assert kept.id in state.questions


class TestSetupRunState:
    def test_update_setup_run_persists_snapshot(self, state):
        state.update_setup_run(
            run_id="run123",
            stage="research_and_plan",
            status="running",
            blockers=["Need package resolution"],
            waiting_for_input=True,
            active_question_id="q1",
            planning_complete=False,
        )

        snapshot = state.snapshot_setup_run()

        assert snapshot["run_id"] == "run123"
        assert snapshot["stage"] == "research_and_plan"
        assert snapshot["blockers"] == ["Need package resolution"]
        assert snapshot["waiting_for_input"] is True


class TestThreadSafety:
    def test_concurrent_add_suggestion(self, state):
        """50 threads each adding a suggestion should result in 50 suggestions."""

        def worker(n):
            for i in range(10):
                state.add_suggestion(f"cat{n}", f"title{i}", f"content{i}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(state.suggestions) == 50


class TestPersistedRestore:
    def test_legacy_state_without_fingerprint_is_ignored_for_nonempty_profile(self, tmp_path):
        path = tmp_path / "bridge.json"
        path.write_text(
            json.dumps(
                {
                    "suggestions": [
                        {
                            "id": "s1",
                            "category": "distro",
                            "title": "Old",
                            "content": "stale",
                        }
                    ],
                    "messages": [{"role": "agent", "content": "stale"}],
                    "questions": [
                        {
                            "id": "q1",
                            "question": "Old question?",
                            "answered": True,
                            "answer": "yes",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        profile = UserProfile()
        profile.preferences.distro_preference = "arch"
        restored = BridgeState(profile, persist_path=path)
        assert restored.suggestions == []
        assert restored.messages == []
        assert restored.questions == {}

    def test_legacy_state_without_fingerprint_still_restores_for_empty_profile(self, tmp_path):
        profile = UserProfile()
        path = tmp_path / "bridge.json"
        baseline = BridgeState(profile, persist_path=None)
        q = baseline.add_question("Answered before restart?")
        baseline.answer_question(q.id, "yes")
        path.write_text(
            json.dumps(
                {
                    "suggestions": [
                        {
                            "id": "s1",
                            "category": "distro",
                            "title": "Recovered",
                            "content": "arch",
                        }
                    ],
                    "messages": [{"role": "agent", "content": "resume"}],
                    "questions": [
                        {
                            "id": q.id,
                            "question": q.question,
                            "choices": None,
                            "answered": True,
                            "answer": "yes",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        restored = BridgeState(profile, persist_path=path)
        assert [s.title for s in restored.suggestions] == ["Recovered"]
        assert [m["content"] for m in restored.messages] == ["resume"]
        assert list(restored.questions) == [q.id]

    def test_empty_profile_restore_does_not_depend_on_magic_fingerprint_literal(self, tmp_path):
        profile = UserProfile()
        path = tmp_path / "bridge.json"
        path.write_text(
            json.dumps(
                {
                    "suggestions": [
                        {
                            "id": "s1",
                            "category": "distro",
                            "title": "Recovered",
                            "content": "arch",
                        }
                    ],
                    "messages": [],
                    "questions": [],
                }
            ),
            encoding="utf-8",
        )
        restored = BridgeState(profile, persist_path=path)
        assert [s.title for s in restored.suggestions] == ["Recovered"]

    def test_restore_keeps_answered_questions_only(self, tmp_path):
        profile = UserProfile()
        path = tmp_path / "bridge.json"
        fingerprint = BridgeState(profile, persist_path=None)._profile_fingerprint()
        path.write_text(
            json.dumps(
                {
                    "profile_fingerprint": fingerprint,
                    "suggestions": [],
                    "messages": [],
                    "questions": [
                        {
                            "id": "answered",
                            "question": "Done?",
                            "choices": None,
                            "answered": True,
                            "answer": "yes",
                        },
                        {
                            "id": "pending",
                            "question": "Still waiting?",
                            "choices": None,
                            "answered": False,
                            "answer": None,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        restored = BridgeState(profile, persist_path=path)
        assert "answered" in restored.questions
        assert restored.questions["answered"].answered.is_set()
        assert "pending" not in restored.questions

    def test_reload_persisted_state_recovers_after_profile_fingerprint_changes(self, tmp_path):
        wanted = UserProfile()
        wanted.preferences.distro_preference = "arch"
        path = tmp_path / "bridge.json"
        fingerprint = BridgeState(wanted, persist_path=None)._profile_fingerprint()
        path.write_text(
            json.dumps(
                {
                    "profile_fingerprint": fingerprint,
                    "suggestions": [
                        {
                            "id": "s1",
                            "category": "plan",
                            "title": "Migration Plan",
                            "content": "persisted analysis",
                        }
                    ],
                    "messages": [{"role": "agent", "content": "persisted message"}],
                    "questions": [],
                }
            ),
            encoding="utf-8",
        )
        wrong = UserProfile()
        wrong.preferences.distro_preference = "fedora"
        restored = BridgeState(wrong, persist_path=path)
        assert restored.suggestions == []
        assert restored.messages == []

        restored.profile.preferences.distro_preference = "arch"
        restored.reload_persisted_state()

        assert [s.title for s in restored.suggestions] == ["Migration Plan"]
        assert [m["content"] for m in restored.messages] == ["persisted message"]
