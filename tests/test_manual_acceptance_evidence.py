from pathlib import Path
import runpy

import pytest

HARNESS = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/accept_agent_manual.py'))
ASSESS = HARNESS['assess']


def evidence(decision):
    return dict(decision=decision, workspace='/synthetic/workspace',
                response={'decision': decision, 'status': 'submitted', 'approval_id': 'fixture'},
                request={'approval_id': 'fixture', 'method': 'item/commandExecution/requestApproval',
                         'details': {'command': HARNESS['COMMAND'], 'cwd': '/synthetic/workspace'}},
                complete_events=True, session_closed=True,
                state='succeeded' if decision == 'accept' else 'cancelled',
                commands=[{'exit_code': 0, 'status': 'completed'}] if decision == 'accept' else
                         [{'exit_code': None, 'status': 'unknown'}],
                marker_absent=True, independent_mcp_content=HARNESS['CONTENT'])


@pytest.mark.parametrize('decision', ['cancel', 'accept'])
def test_complete_approval_evidence_passes(decision):
    assert ASSESS(evidence(decision))


@pytest.mark.parametrize('missing', ['response', 'binding', 'command', 'cwd', 'events', 'closed', 'content', 'exit'])
def test_accept_requires_ack_real_execution_independent_read_and_closed_session(missing):
    report = evidence('accept')
    if missing == 'response':
        report['response']['decision'] = 'cancel'
    elif missing == 'binding':
        report['response']['approval_id'] = 'other-request'
    elif missing == 'command':
        report['request']['details']['command'] = 'echo different'
    elif missing == 'cwd':
        report['request']['details']['cwd'] = '/different/workspace'
    elif missing == 'events':
        report['complete_events'] = False
    elif missing == 'closed':
        report['session_closed'] = False
    elif missing == 'content':
        report['independent_mcp_content'] = 'wrong'
    else:
        report['commands'][0]['exit_code'] = None
    assert not ASSESS(report)


def test_cancel_does_not_accept_executed_commands_or_absent_marker_alone():
    report = evidence('cancel')
    report['commands'][0] = {'exit_code': 0, 'status': 'completed'}
    assert not ASSESS(report)
    report['commands'] = []
    report['state'] = 'succeeded'
    assert not ASSESS(report)
