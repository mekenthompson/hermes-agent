from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from gateway.session_ingress import _approval_task_scope
from tools.approval_context import current_approval_task_context


def _args(*, internal=False, is_api=False, marker="native_text_v1"):
    row = {"admission_id": "admit-1", "generation": 8,
           "payload": {marker: {}} if marker else {}}
    source = SimpleNamespace(user_id="user-1")
    event = SimpleNamespace(internal=internal, text="Only update app.py; do not edit tests.", source=source)
    authority = SimpleNamespace(profile_id="profile-1")
    live = SimpleNamespace(route="route-1")
    return authority, SimpleNamespace(session_id="session-1"), row, live, event, is_api


def test_authenticated_native_admission_binds_raw_scope_with_identity_and_run():
    authority, ref, row, live, event, is_api = _args()
    scope = _approval_task_scope(authority, ref, row, live, event, is_api=is_api)
    with scope:
        context = current_approval_task_context()
        assert context is not None
        assert context.text == event.text
        assert (context.identity, context.profile, context.session) == ("user-1", "profile-1", "route-1")
        assert (context.task_id, context.run_id) == ("admit-1", "8")
    assert current_approval_task_context() is None


@pytest.mark.parametrize("internal,is_api,marker", [(True, False, "native_text_v1"), (False, True, "native_text_v1"),
                                                        (False, False, None)])
def test_internal_api_and_unprovenanced_rows_do_not_bind_task_scope(internal, is_api, marker):
    authority, ref, row, live, event, _ = _args(internal=internal, is_api=is_api, marker=marker)
    scope = _approval_task_scope(authority, ref, row, live, event, is_api=is_api)
    assert isinstance(scope, type(nullcontext()))
    with scope:
        assert current_approval_task_context() is None
