"""Step 1: the analyst tagged in a Slack thread. Runs the real Agents SDK with a scripted model, so it
needs no keys and spends nothing; the tools, the citations and the Slack round trip are real code."""

import asyncio

import pytest
from agents import SQLiteSession
from agents.testing import ScriptedModel, assistant_message, function_call

from workforce.analyst import answer, check_citations
from workforce.slack_app import on_mention
from workforce.sources import Readers, Source, fixture_github, rank

ENG = frozenset({"engineering"})
THREAD = [
    {
        "ts": "1.0",
        "user": "U_PM",
        "text": "A customer says their weekly CSV export fails with a 504 since Monday.",
    },
    {"ts": "2.0", "user": "U_ENG", "text": "Their workspace has 61,000 tasks."},
    {"ts": "3.0", "user": "U_ENG", "text": "<@U_BOT> is this a known bug, and who is on it?"},
]


class FakeSlack:
    def __init__(self, messages):
        self.messages, self.posted = messages, []

    async def conversations_replies(self, channel, **thread):
        return {"messages": self.messages}

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)


def searches_then(text):
    return ScriptedModel(
        [
            [
                function_call("search_github", {"query": "csv export timeout 504"}, call_id="c1"),
                function_call("search_linear", {"query": "csv export timeout"}, call_id="c2"),
                function_call("search_docs", {"query": "large export workaround"}, call_id="c3"),
            ],
            [assistant_message(text)],
        ]
    )


@pytest.mark.lab("build-a-slack-bot-that-remembers")
def test_tagged_in_a_thread_it_answers_from_github_linear_and_docs_with_sources(tmp_path):
    model = searches_then(
        "Known bug [GH#412]: the export runs past the load balancer's 30 seconds. The Data squad is moving "
        "it to a background job in [EXP-41], and the workaround is in [doc:exports]. Also see [GH#999]."
    )
    slack = FakeSlack(THREAD)
    event = {"channel": "C1", "ts": "3.0", "thread_ts": "1.0", "user": "U_ENG", "text": THREAD[2]["text"]}

    text = asyncio.run(
        on_mention(event, slack, Readers("kaas", "U_ENG"), memory=tmp_path / "m.sqlite", model=model)
    )

    # It read the thread it was tagged in, not just the mention.
    first_input = str(model.calls[0].input)
    assert "fails with a 504" in first_input and "61,000 tasks" in first_input
    # It replied in that thread, linking each source it read and cited.
    assert slack.posted == [{"channel": "C1", "thread_ts": "1.0", "text": text}]
    for sid, url in [
        ("GH#412", "https://github.com/kaas/app/issues/412"),
        ("EXP-41", "https://linear.app/kaas/issue/EXP-41"),
        ("doc:exports", "exports.md"),
    ]:
        assert sid in text and url in text
    # A citation no tool returned never reaches Slack.
    assert "GH#999" not in text


def test_a_follow_up_in_the_same_thread_remembers_the_first_answer():
    session = SQLiteSession("C1:1.0")
    first = searches_then("It is [GH#412], fixed in [EXP-41].")
    asyncio.run(
        answer("Is the export timeout known?", readers=Readers("kaas", "U_ENG"), session=session, model=first)
    )

    second = ScriptedModel([[assistant_message("The Data squad owns [EXP-41].")]])
    reply = asyncio.run(
        answer("Who owns the fix?", readers=Readers("kaas", "U_ENG"), session=session, model=second)
    )

    assert "It is [GH#412], fixed in [EXP-41]." in str(second.calls[0].input)
    # Memory carries the conversation, not trust: EXP-41 was not read in this run, so it is not cited.
    assert reply.sources == [] and reply.dropped == ["EXP-41"]


def test_citations_are_kept_only_for_sources_read_in_this_run():
    read = {"EXP-41": Source("EXP-41", "linear", "t", "x", "u")}
    checked = check_citations("Fixed in [EXP-41] and [MADE-UP].", read)
    assert [source.source_id for source in checked.sources] == ["EXP-41"] and checked.dropped == ["MADE-UP"]
    assert checked.text == "Fixed in [EXP-41] and."


def test_a_read_source_counts_with_a_typographic_hyphen_or_without_brackets():
    read = {
        "doc:release-process": Source("doc:release-process", "docs", "t", "x", "u"),
        "GH#412": Source("GH#412", "github", "t", "x", "u"),
    }
    checked = check_citations("See [doc:release\u2011process], tracked as (GH#412), not [GH\u2011999].", read)
    assert [source.source_id for source in checked.sources] == [
        "doc:release-process",
        "GH#412",
    ] and checked.dropped == ["GH-999"]
    assert check_citations("Nothing found for GH#4120.", read).sources == []


def test_search_never_returns_a_row_that_matches_no_term():
    assert rank(fixture_github("kaas"), "kubernetes helm chart", ENG) == []
    assert [source.source_id for source in rank(fixture_github("kaas"), "csv export timeout", ENG)][
        0
    ] == "GH#412"
