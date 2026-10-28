"""What every session's board shares: a blank state file is no run, the refresh never eats a form, and the
workspace name is always given."""

import inspect

import pytest

from workforce.board import Board, is_blank, page, read_json


def test_a_blank_state_file_is_no_run(tmp_path):
    empty = tmp_path / "board.json"
    assert is_blank(empty)
    assert read_json(empty, {"cards": []}) == {"cards": []}

    empty.write_text("")
    assert is_blank(empty)
    assert read_json(empty, []) == []
    assert Board.open(empty).cards == []

    empty.write_text('{"a": 1}')
    assert not is_blank(empty)
    assert read_json(empty, {}) == {"a": 1}


def test_the_refresh_pauses_while_a_form_has_focus():
    html = page("Board", "<p>live</p>", "board", workspace="KaaS Test")
    assert "document.hidden" in html
    assert "form:focus-within,.decide:hover" in html


def test_a_page_has_no_default_workspace():
    assert inspect.signature(page).parameters["workspace"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        page("Board", "", "board")
