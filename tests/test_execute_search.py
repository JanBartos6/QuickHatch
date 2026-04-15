"""Tests for _execute_web_search and _execute_image_search.

The ddgs library is mocked so these tests don't actually hit the network.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from quickhatch.gui.server import _execute_image_search, _execute_web_search


class TestWebSearch:
    def test_formats_results_as_json_with_list(self):
        fake_results = [
            {"title": "A", "href": "https://a.com", "body": "About A"},
            {"title": "B", "href": "https://b.com", "body": "About B"},
        ]
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.text = MagicMock(return_value=fake_results)
            out = _execute_web_search("linux arch", max_results=5)
        data = json.loads(out)
        assert "results" in data
        assert len(data["results"]) == 2
        assert data["results"][0]["title"] == "A"
        assert data["results"][0]["url"] == "https://a.com"
        assert data["results"][0]["snippet"] == "About A"

    def test_empty_results(self):
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.text = MagicMock(return_value=[])
            out = _execute_web_search("nothing", max_results=5)
        data = json.loads(out)
        assert data["results"] == []
        assert "No results" in data["message"]

    def test_clamps_max_results_high(self):
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.text = MagicMock(return_value=[])
            _execute_web_search("x", max_results=999)
            # Should have clamped to 10
            DDGS.return_value.text.assert_called_once()
            args, kwargs = DDGS.return_value.text.call_args
            assert kwargs.get("max_results", args[1] if len(args) > 1 else None) == 10

    def test_clamps_max_results_low(self):
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.text = MagicMock(return_value=[])
            _execute_web_search("x", max_results=0)
            args, kwargs = DDGS.return_value.text.call_args
            assert kwargs.get("max_results", args[1] if len(args) > 1 else None) == 1

    def test_missing_fields_handled_gracefully(self):
        fake_results = [{"title": "A"}]  # no href, no body
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.text = MagicMock(return_value=fake_results)
            out = _execute_web_search("x", max_results=1)
        data = json.loads(out)
        assert data["results"][0]["title"] == "A"
        assert data["results"][0]["url"] == ""
        assert data["results"][0]["snippet"] == ""

    def test_exception_returns_error_json(self):
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.text = MagicMock(side_effect=RuntimeError("boom"))
            out = _execute_web_search("x", max_results=1)
        data = json.loads(out)
        assert "error" in data
        assert "boom" in data["error"]

    def test_import_error_returns_error_json(self):
        """Simulate ddgs not installed."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "ddgs":
                raise ImportError("No module named ddgs")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            out = _execute_web_search("x")
        data = json.loads(out)
        assert "error" in data
        assert "not installed" in data["error"]


class TestImageSearch:
    def test_formats_images_as_json(self):
        fake = [
            {"title": "Screenshot", "image": "https://example.com/a.png", "source": "example.com"},
            {"title": "Another", "image": "https://example.com/b.jpg", "source": "example.com"},
        ]
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.images = MagicMock(return_value=fake)
            out = _execute_image_search("kde plasma", max_results=3)
        data = json.loads(out)
        assert "images" in data
        assert len(data["images"]) == 2
        assert data["images"][0]["url"] == "https://example.com/a.png"
        assert data["images"][0]["title"] == "Screenshot"

    def test_empty_images(self):
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.images = MagicMock(return_value=[])
            out = _execute_image_search("nothing", max_results=3)
        data = json.loads(out)
        assert data["images"] == []

    def test_clamps_max_results(self):
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.images = MagicMock(return_value=[])
            _execute_image_search("x", max_results=999)
            args, kwargs = DDGS.return_value.images.call_args
            # Image search clamps to 5
            assert kwargs.get("max_results", args[1] if len(args) > 1 else None) == 5

    def test_exception_returns_error_json(self):
        with patch("ddgs.DDGS") as DDGS:
            DDGS.return_value.images = MagicMock(side_effect=RuntimeError("nope"))
            out = _execute_image_search("x")
        data = json.loads(out)
        assert "error" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
