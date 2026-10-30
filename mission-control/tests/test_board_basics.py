"""What every session's board shares: a blank state file is no run, the refresh never eats a form, and the
workspace name is always given."""

import inspect
from pathlib import Path

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


def test_clickhouse_diagnostic_logs_stay_off():
    # ClickHouse's own logs grew to 50M rows on an idle stack; Langfuse reads none of them.
    root = Path(__file__).resolve().parent.parent
    mount = "./deploy/clickhouse-quiet.xml:/etc/clickhouse-server/config.d/quiet.xml:ro"
    assert mount in (root / "compose.yaml").read_text()
    quiet = (root / "deploy/clickhouse-quiet.xml").read_text()
    for table in ("trace_log", "text_log", "metric_log", "asynchronous_metric_log"):
        assert f'<{table} remove="1"/>' in quiet
