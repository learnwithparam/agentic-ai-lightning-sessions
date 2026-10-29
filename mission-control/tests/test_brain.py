"""Step 2: the company brain answers each user only from what they may see. The model is scripted to
make the same searches and claim the same sources for both users, so any difference comes from the
permission filter, not from the model behaving."""

import asyncio

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from workforce.analyst import answer
from workforce.sources import FIXTURES, Readers, docs, fixture_github, fixture_linear, people

QUESTION = "Is the CSV export timeout a known bug, and is a fix planned?"
CLAIM = "Known bug [GH#412], fix in [EXP-41], workaround in [doc:exports]. Releases: [doc:release-process]."


def ask_as(user):
    model = ScriptedModel(
        [
            [
                function_call("search_github", {"query": "csv export timeout"}, call_id="g"),
                function_call("search_linear", {"query": "export timeout fix"}, call_id="l"),
                function_call("search_docs", {"query": "large export release process"}, call_id="d"),
            ],
            [assistant_message(CLAIM)],
        ]
    )
    reply = asyncio.run(answer(QUESTION, readers=Readers("kaas", user), model=model))
    seen = " ".join(str(item) for item in model.calls[1].input)
    return reply, seen


@pytest.mark.lab("decide-what-your-company-brain-can-see")
def test_the_same_question_as_engineer_and_contractor_is_answered_from_different_sources():
    eng, eng_saw = ask_as("U_ENG")
    con, con_saw = ask_as("U_CONTRACTOR")

    assert [source.source_id for source in eng.sources] == [
        "GH#412",
        "EXP-41",
        "doc:exports",
        "doc:release-process",
    ]
    assert [source.source_id for source in con.sources] == ["doc:release-process"]
    assert con.dropped == ["GH#412", "EXP-41", "doc:exports"]
    # The restricted text never reached the model while it answered the contractor.
    for secret in ["load balancer", "background job", "504"]:
        assert secret in eng_saw and secret not in con_saw


def test_someone_in_no_group_reads_nothing():
    reply, saw = ask_as("U_STRANGER")
    assert reply.sources == [] and "No results." in saw


def test_every_fixture_row_says_who_may_read_it():
    groups = {group for members in people("kaas").values() for group in members}
    rows = fixture_github("kaas") + fixture_linear("kaas") + docs(FIXTURES / "kaas" / "docs")
    unlisted = [row.source_id for row in rows if not row.access]
    unknown = [row.source_id for row in rows if row.access - groups]
    assert not unlisted and not unknown, (unlisted, unknown)
