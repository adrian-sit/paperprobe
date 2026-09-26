from types import SimpleNamespace

from app.services.openreview import _content_text


def test_content_text_supports_plain_and_wrapped_openreview_fields() -> None:
    note = SimpleNamespace(content={"title": {"value": " A title "}, "abstract": " An abstract "})

    assert _content_text(note, "title") == "A title"
    assert _content_text(note, "abstract") == "An abstract"
    assert _content_text(note, "missing") == ""
