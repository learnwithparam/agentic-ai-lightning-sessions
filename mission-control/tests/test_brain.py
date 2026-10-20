"""Step 2: the company brain answers each user only from what they may see. The model is scripted to
make the same searches and claim the same sources for both users, so any difference comes from the
permission filter, not from the model behaving."""

import asyncio

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from workforce.analyst import answer
from workforce.sources import FIXTURES, Readers, docs, fixture_github, fixture_linear, people

QUESTION = "Why are customers charged twice on checkout retry, and is there a fix planned?"
CLAIM = (
    "Known bug [GH#412], fix in [PAY-88], rule in [doc:payments-retries]. Releases: [doc:release-process]."
)


def ask_as(user):
    model = ScriptedModel(
        [
            [
                function_call("search_github", {"query": "checkout retry charged twice"}, call_id="g"),
                function_call("search_linear", {"query": "checkout retry fix"}, call_id="l"),
                function_call("search_docs", {"query": "payment retries release process"}, call_id="d"),
            ],
            [assistant_message(CLAIM)],
        ]
    )
    reply = asyncio.run(answer(QUESTION, readers=Readers("acme", user), model=model))
    seen = " ".join(str(i) for i in model.calls[1].input)
    return reply, seen


@pytest.mark.lab("decide-what-your-company-brain-can-see")
def test_the_same_question_as_engineer_and_contractor_is_answered_from_different_sources():
    eng, eng_saw = ask_as("U_ENG")
    con, con_saw = ask_as("U_CONTRACTOR")

    assert [s.id for s in eng.sources] == ["GH#412", "PAY-88", "doc:payments-retries", "doc:release-process"]
    assert [s.id for s in con.sources] == ["doc:release-process"]
    assert con.dropped == ["GH#412", "PAY-88", "doc:payments-retries"]
    # The restricted text never reached the model while it answered the contractor.
    for secret in ["double charges", "Idempotency-Key", "new charge"]:
        assert secret in eng_saw and secret not in con_saw


def test_someone_in_no_group_reads_nothing():
    reply, saw = ask_as("U_STRANGER")
    assert reply.sources == [] and "No results." in saw


def test_every_fixture_row_says_who_may_read_it():
    groups = {g for gs in people("acme").values() for g in gs}
    rows = fixture_github("acme") + fixture_linear("acme") + docs(FIXTURES / "acme" / "docs")
    unlisted = [r.id for r in rows if not r.access]
    unknown = [r.id for r in rows if r.access - groups]
    assert not unlisted and not unknown, (unlisted, unknown)
