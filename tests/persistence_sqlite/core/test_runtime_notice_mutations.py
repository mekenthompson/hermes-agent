"""Externally supervised operation notices use the canonical mutation receipt (house ``notice``)."""
import sqlite3

import pytest

from persistence.authority.runtime import Notice
from persistence.core.errors import AuthorityRefusal
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_read import session_row
from tests.fixtures.storage_seed import seed_session


def _store(home):
    return open_persistence(profile_scope(home=home))


def _messages(store, session_id):
    return [m.as_openai_message() for m in store.messages.read_conversation(session_id)]


def test_notice_and_receipt_survive_owner_restart_without_duplicate(tmp_path):
    text = '[Delivery test] operation passed'
    args = dict(principal_id='operator', session_id='s', request_id='rollout:final',
                expected_revision=0, expected_generation=0, owner=None)
    store = _store(tmp_path)
    try:
        seed_session(store, 's', 'test')
        epoch = store.runtime.begin_epoch(instance_id='first')
        receipt = store.runtime.mutate(epoch, command=Notice(text), **args)
        assert receipt.result['notice_row_id'] > 0
    finally:
        store.close()
    store = _store(tmp_path)
    try:
        epoch = store.runtime.begin_epoch(instance_id='replacement')
        replay = store.runtime.mutate(epoch, command=Notice(text), **args)
        assert replay.replayed and replay.result == receipt.result
        rows = _messages(store, 's')
        assert len(rows) == 1
        assert rows[0]['content'] == text
        assert rows[0]['display_kind'] == 'internal_notification'
        with pytest.raises(AuthorityRefusal, match='admission_conflict'):
            store.runtime.mutate(epoch, command=Notice('changed'), **args)
    finally:
        store.close()


@pytest.mark.parametrize('outstanding', ['queued', 'started', 'unknown'])
def test_notice_never_writes_through_an_outstanding_turn(tmp_path, outstanding):
    store = _store(tmp_path)
    try:
        seed_session(store, 's', 'test')
        epoch = store.runtime.begin_epoch(instance_id='first')
        store.admissions.admit(epoch=epoch, principal_id='human', session_id='s', request_id='input',
                               payload={'text': 'busy'})
        if outstanding != 'queued':
            store.admissions.claim(epoch=epoch, session_id='s')
        if outstanding == 'unknown':
            epoch = store.runtime.begin_epoch(instance_id='replacement')
            store.admissions.recover(epoch=epoch)
        row = session_row(store, 's')
        with pytest.raises(AuthorityRefusal, match='session_busy|unknown_execution'):
            store.runtime.mutate(epoch, principal_id='operator', session_id='s', request_id='notice',
                                 expected_revision=row['runtime_revision'],
                                 expected_generation=row['runtime_generation'], command=Notice('notice'), owner=None)
        assert _messages(store, 's') == []
    finally:
        store.close()


def test_notice_requires_generation_and_rolls_back_with_receipt(tmp_path):
    store = _store(tmp_path)
    try:
        seed_session(store, 's', 'test')
        epoch = store.runtime.begin_epoch(instance_id='first')
        args = dict(principal_id='operator', session_id='s', request_id='notice', expected_revision=0,
                    command=Notice('notice'), owner=None)
        with pytest.raises(AuthorityRefusal, match='invalid_params'):
            store.runtime.mutate(epoch, **args)
        raw_holder(store)._execute_write(lambda c: c.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON state_meta "
            "WHEN NEW.key LIKE 'gateway.mutation.%' BEGIN SELECT RAISE(ABORT, 'receipt failed'); END"))
        with pytest.raises((sqlite3.IntegrityError, Exception), match='receipt failed'):
            store.runtime.mutate(epoch, expected_generation=0, **args)
        assert _messages(store, 's') == []
        assert session_row(store, 's')['runtime_revision'] == 0
    finally:
        store.close()
