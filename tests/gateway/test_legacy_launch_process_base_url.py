"""A process-backed provider's ``process://`` billing stamp is not a resumable endpoint."""

import json

import pytest

from gateway.session_local_migration import legacy_launch_params


def _row(**overrides):
    row = {
        'id': '20261110_172401_4b2de2', 'source': 'desktop', 'cwd': None, 'chat_id': None,
        'model': 'claude-opus-5-5[1m]',
        'model_config': json.dumps({'reasoning_config': {'enabled': True, 'effort': 'medium'},
                                    'service_tier': 'normal'}),
        'billing_provider': 'claude-subscription-directsdk-experimental',
        'billing_base_url': 'process://claude-subscription-directsdk-experimental',
    }
    row.update(overrides)
    return row


def test_process_marker_is_not_carried_as_a_launch_base_url():
    params, _ = legacy_launch_params(_row())
    assert 'base_url' not in params
    assert params['provider'] == 'claude-subscription-directsdk-experimental'
    assert params['model'] == 'claude-opus-5-5[1m]'


def test_process_marker_params_pass_resume_launch_validation():
    from gateway.session_policy import _apply_launch_overrides
    params, _ = legacy_launch_params(_row())
    config = {'model': {}}
    _apply_launch_overrides(params, config)
    assert 'base_url' not in config['model']


@pytest.mark.parametrize('url', ['http://127.0.0.1:8080/v1', 'https://api.example.test/v1'])
def test_http_billing_endpoint_still_resumes_on_its_endpoint(url):
    params, _ = legacy_launch_params(_row(billing_provider='custom', billing_base_url=url))
    assert params['base_url'] == url
    assert params['provider'] == 'custom'
