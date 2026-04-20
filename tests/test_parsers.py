"""Tests for the JSON parsing helpers in gui.server.

These parsers handle LLM output, which is notoriously inconsistent — models
emit markdown fences, multiple JSON objects, text before/after JSON, literal
newlines inside string values, and so on. The parsers must tolerate all of it.
"""

from __future__ import annotations

import pytest

from quickhatch.gui.server import (
    _extract_json_objects,
    _parse_agent_response,
    _parse_analysis_section_response,
)


class TestExtractJsonObjects:
    """_extract_json_objects walks text extracting every top-level JSON object."""

    def test_single_object(self):
        result = _extract_json_objects('{"action": "done", "summary": "ok"}')
        assert len(result) == 1
        assert result[0]["action"] == "done"

    def test_two_concatenated_objects(self):
        """Gemma often emits a status then an ssh_exec in one response."""
        text = (
            '{"action": "status", "message": "Expanding cowspace"}\n'
            '{"action": "ssh_exec", "command": "mount -o remount,size=4G /run/archiso/cowspace"}'
        )
        result = _extract_json_objects(text)
        assert len(result) == 2
        assert result[0]["action"] == "status"
        assert result[1]["action"] == "ssh_exec"

    def test_object_with_surrounding_text(self):
        text = 'Here is the command:\n{"action": "ssh_exec", "command": "ls"}\nThat should work.'
        result = _extract_json_objects(text)
        assert len(result) == 1
        assert result[0]["command"] == "ls"

    def test_nested_braces_in_string(self):
        """Brace counting must respect strings."""
        text = '{"action": "ssh_exec", "command": "echo \\"hello {world}\\""}'
        result = _extract_json_objects(text)
        assert len(result) == 1
        assert "hello {world}" in result[0]["command"]

    def test_escape_inside_string(self):
        text = r'{"action": "ssh_exec", "command": "echo \"quoted\""}'
        result = _extract_json_objects(text)
        assert len(result) == 1

    def test_empty_string(self):
        assert _extract_json_objects("") == []

    def test_no_json(self):
        assert _extract_json_objects("just plain text, no objects") == []

    def test_ignores_arrays(self):
        """Top-level arrays are not dicts, should be ignored."""
        # Note: _extract_json_objects only pulls out dict objects
        result = _extract_json_objects('["one", "two"] {"action": "done"}')
        # The array start { is not a dict, but the second {} is
        assert any(o.get("action") == "done" for o in result)

    def test_malformed_json_is_skipped(self):
        text = '{bad json} {"action": "done"}'
        result = _extract_json_objects(text)
        # The first block is malformed and should be skipped
        assert len(result) >= 1
        assert result[-1]["action"] == "done"

    def test_three_objects_in_sequence(self):
        text = (
            '{"action":"status","message":"one"} '
            '{"action":"status","message":"two"} '
            '{"action":"done","summary":"three"}'
        )
        result = _extract_json_objects(text)
        assert len(result) == 3
        assert [o["action"] for o in result] == ["status", "status", "done"]


class TestParseAgentResponse:
    """_parse_agent_response handles the 5-section JSON the analysis agent produces."""

    def test_clean_json(self):
        text = (
            '{"distro": {"title": "Arch", "content": "Use pacman"},'
            ' "apps": {"title": "Apps", "content": "firefox"}}'
        )
        result = _parse_agent_response(text)
        assert "distro" in result
        assert "apps" in result

    def test_markdown_fenced(self):
        text = '```json\n{"distro": {"title": "Arch", "content": "Use pacman"}}\n```'
        result = _parse_agent_response(text)
        assert result["distro"]["title"] == "Arch"

    def test_fenced_anywhere_in_text(self):
        text = (
            "Here is my analysis:\n\n"
            "```json\n"
            '{"distro": {"title": "Arch", "content": "x"}}\n'
            "```\n\n"
            "Let me know if you need more."
        )
        result = _parse_agent_response(text)
        assert result["distro"]["title"] == "Arch"

    def test_literal_newlines_in_strings(self):
        """Models often emit real newlines inside JSON string values, which is
        technically invalid JSON. The parser has to handle this gracefully."""
        text = '{"distro": {"title": "Arch", "content": "Line 1\nLine 2\nLine 3"}}'
        result = _parse_agent_response(text)
        assert "distro" in result

    def test_mixed_text_and_json(self):
        text = (
            "Based on the user's profile, here is my plan:\n"
            '{"distro": {"title": "Arch", "content": "..."},'
            ' "plan": {"title": "Steps", "content": "..."}}\n'
            "Let me know if you want to adjust."
        )
        result = _parse_agent_response(text)
        assert "distro" in result
        assert "plan" in result

    def test_no_json_falls_back_to_plan_content(self):
        """If no JSON can be extracted, the full text becomes the plan content."""
        text = "I am unable to generate a structured response right now."
        result = _parse_agent_response(text)
        assert "plan" in result
        assert result["plan"]["content"] == text

    def test_empty_string(self):
        result = _parse_agent_response("")
        assert "plan" in result


class TestParseAnalysisSectionResponse:
    def test_structured_section_response_parses(self):
        raw = (
            '{"title":"Recommended Distribution",'
            '"markdown":"### Recommended Distribution\\n\\n**Fedora Workstation**",'
            '"metadata":{"recommended_distro":"Fedora Workstation"}}'
        )
        result = _parse_analysis_section_response("distro", raw)
        assert result["structured"] is True
        assert "Fedora Workstation" in result["markdown"]
        assert result["metadata"]["recommended_distro"] == "Fedora Workstation"

    def test_nested_legacy_section_object_still_parses(self):
        raw = (
            '{"distro":{"title":"Recommended Distribution",'
            '"markdown":"**Arch Linux**",'
            '"metadata":{"recommended_distro":"Arch Linux"}}}'
        )
        result = _parse_analysis_section_response("distro", raw)
        assert result["structured"] is True
        assert "Arch Linux" in result["markdown"]

    def test_plain_markdown_falls_back_cleanly(self):
        raw = "### Driver Notes\n\nInstall `akmod-nvidia` and reboot."
        result = _parse_analysis_section_response("drivers", raw)
        assert result["structured"] is False
        assert "akmod-nvidia" in result["markdown"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
