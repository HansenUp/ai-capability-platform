import json
from dataclasses import replace

import pytest

from runtime.api.auth import Principal
from runtime.run.continuation import ContinuationRejected, ConversationScope, HistoricalRun, SyntheticAttachment, compose

USER = Principal("human:alice", tenant_id="synthetic", auth_method="user")
SCOPE = ConversationScope("synthetic", "human:alice", "ticket")
RUN = HistoricalRun(
    "old", "synthetic", "human:alice", "ticket", "completed", (("user", "synthetic question"), ("assistant", "synthetic answer"))
)


def build(**changes):
    args = dict(
        scope=SCOPE,
        principal=USER,
        agent="ticket",
        question="continue",
        selected_run_ids=["old"],
        load_authorized_run=lambda run_id, principal: RUN,
        attachments=[],
        scan=lambda content: True,
        subject_locked=False,
    )
    args.update(changes)
    return compose(**args)


def test_explicit_history_and_scanned_attachment_are_deterministic():
    result = build(attachments=[SyntheticAttachment("sample.txt", b"synthetic")])
    value = json.loads(result)
    assert value["selected_history"][0]["run_id"] == "old"
    assert value["synthetic_attachments"][0]["text"] == "synthetic"
    assert result == build(attachments=[SyntheticAttachment("sample.txt", b"synthetic")])
    assert json.loads(build(selected_run_ids=[]))["selected_history"] == []


@pytest.mark.parametrize(
    "principal",
    [
        replace(USER, subject="human:bob"),
        replace(USER, tenant_id="other"),
        replace(USER, auth_method="dev"),
        replace(USER, auth_method="client_credentials"),
    ],
)
def test_identity_isolation(principal):
    with pytest.raises(ContinuationRejected):
        build(principal=principal)


@pytest.mark.parametrize(
    "run",
    [
        replace(RUN, tenant="other"),
        replace(RUN, subject="human:bob"),
        replace(RUN, agent="other"),
        replace(RUN, state="running"),
        replace(RUN, run_id="wrong"),
        replace(RUN, messages=(("system", "secret"),)),
    ],
)
def test_rejects_invalid_history(run):
    with pytest.raises(ContinuationRejected):
        build(load_authorized_run=lambda *_: run)


def test_lock_rejects_before_history_or_scanning():
    def forbidden(*_):
        pytest.fail("locked subject must not start work")

    with pytest.raises(ContinuationRejected):
        build(subject_locked=True, load_authorized_run=forbidden, scan=forbidden)


@pytest.mark.parametrize(
    "attachment",
    [
        SyntheticAttachment("x.txt", b"\xff"),
        SyntheticAttachment("../x.txt", b"x"),
        SyntheticAttachment("x.exe", b"x"),
        SyntheticAttachment("x.txt", b"x" * (1024 * 1024 + 1)),
    ],
)
def test_invalid_attachment(attachment):
    with pytest.raises(ContinuationRejected):
        build(attachments=[attachment])


def test_scan_failure_and_count_limit():
    with pytest.raises(ContinuationRejected):
        build(attachments=[SyntheticAttachment("x.txt", b"x")], scan=lambda _: False)
    with pytest.raises(ContinuationRejected):
        build(attachments=[SyntheticAttachment("x.txt", b"x")] * 4)
    with pytest.raises(ContinuationRejected):
        build(selected_run_ids=["old", "old"])
