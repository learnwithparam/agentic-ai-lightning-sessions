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
        "text": "Two customers say they were charged twice at checkout this morning.",
    },
    {"ts": "2.0", "user": "U_ENG", "text": "Gateway had timeouts around 9:00."},
    {"ts": "3.0", "user": "U_ENG", "text": "<@U_BOT> is this a known bug, and who is on it?"},
]


class FakeSlack:
    def __init__(self, messages):
        self.messages, self.posted = messages, []

    async def conversations_replies(self, channel, ts):
        return {"messages": self.messages}

    async def chat_postMessage(self, **kw):
        self.posted.append(kw)


def searches_then(text):
    return ScriptedModel(
        [
            [
                function_call("search_github", {"query": "checkout charged twice retry"}, call_id="c1"),
                function_call("search_linear", {"query": "checkout retry double charge"}, call_id="c2"),
                function_call("search_docs", {"query": "payment retries idempotency"}, call_id="c3"),
            ],
            [assistant_message(text)],
        ]
    )


@pytest.mark.lab("build-a-slack-bot-that-remembers")
def test_tagged_in_a_thread_it_answers_from_github_linear_and_docs_with_sources(tmp_path):
    model = searches_then(
        "Known bug [GH#412]: a retry makes a new idempotency key. The Payments squad is fixing it in "
        "[PAY-88], and the rule is in [doc:payments-retries]. Also see [GH#999]."
    )
    slack = FakeSlack(THREAD)
    event = {"channel": "C1", "ts": "3.0", "thread_ts": "1.0", "user": "U_ENG", "text": THREAD[2]["text"]}

    text = asyncio.run(
        on_mention(event, slack, Readers("acme", "U_ENG"), memory=tmp_path / "m.sqlite", model=model)
    )

    # It read the thread it was tagged in, not just the mention.
    first_input = str(model.calls[0].input)
    assert "charged twice at checkout" in first_input and "timeouts around 9:00" in first_input
    # It replied in that thread, linking each source it read and cited.
    assert slack.posted == [{"channel": "C1", "thread_ts": "1.0", "text": text}]
    for sid, url in [
        ("GH#412", "https://github.com/acme/shop/issues/412"),
        ("PAY-88", "https://linear.app/acme/issue/PAY-88"),
        ("doc:payments-retries", "payments-retries.md"),
    ]:
        assert sid in text and url in text
    # A citation no tool returned never reaches Slack.
    assert "GH#999" not in text


def test_a_follow_up_in_the_same_thread_remembers_the_first_answer():
    session = SQLiteSession("C1:1.0")
    first = searches_then("It is [GH#412], fixed in [PAY-88].")
    asyncio.run(
        answer("Is the double charge known?", readers=Readers("acme", "U_ENG"), session=session, model=first)
    )

    second = ScriptedModel([[assistant_message("The Payments squad owns [PAY-88].")]])
    reply = asyncio.run(
        answer("Who owns the fix?", readers=Readers("acme", "U_ENG"), session=session, model=second)
    )

    assert "It is [GH#412], fixed in [PAY-88]." in str(second.calls[0].input)
    # Memory carries the conversation, not trust: PAY-88 was not read in this run, so it is not cited.
    assert reply.sources == [] and reply.dropped == ["PAY-88"]


def test_citations_are_kept_only_for_sources_read_in_this_run():
    read = {"PAY-88": Source("PAY-88", "linear", "t", "x", "u")}
    r = check_citations("Fixed in [PAY-88] and [MADE-UP].", read)
    assert [s.id for s in r.sources] == ["PAY-88"] and r.dropped == ["MADE-UP"]
    assert r.text == "Fixed in [PAY-88] and."


def test_a_read_source_counts_with_a_typographic_hyphen_or_without_brackets():
    read = {
        "doc:payments-retries": Source("doc:payments-retries", "docs", "t", "x", "u"),
        "GH#412": Source("GH#412", "github", "t", "x", "u"),
    }
    r = check_citations("See [doc:payments\u2011retries], tracked as (GH#412), not [GH\u2011999].", read)
    assert [s.id for s in r.sources] == ["doc:payments-retries", "GH#412"] and r.dropped == ["GH-999"]
    assert check_citations("Nothing found for GH#4120.", read).sources == []


def test_search_never_returns_a_row_that_matches_no_term():
    assert rank(fixture_github("acme"), "kubernetes helm chart", ENG) == []
    assert [s.id for s in rank(fixture_github("acme"), "checkout retry charges", ENG)][0] == "GH#412"
