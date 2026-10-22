"""Frozen stage deadlines; legacy sessions keep their original 30-minute policy."""
from __future__ import annotations

from kiroclops.visual.schema import VisualError

LEGACY_ID = 'phase3d-execution-v1'
REVISED_ID = 'phase3d-execution-v2'
STAGES = ('jump', 'ground', 'panel', 'svg')
BUDGET_FIELDS = ('max_candidates', 'max_seconds', 'max_validation_seconds')


def resolve_policy(policy_id=None, stage=None):
    policy_id = LEGACY_ID if policy_id is None else policy_id
    if policy_id not in (LEGACY_ID, REVISED_ID) or stage not in (*STAGES, None):
        raise VisualError('invalid_execution_policy', 'Unknown frozen execution policy or stage.')
    if policy_id == REVISED_ID and stage is None:
        raise VisualError('invalid_execution_policy', 'The revised policy requires an explicit stage.')
    revised = policy_id == REVISED_ID
    return {'policy_id': policy_id, 'stage': stage, 'max_candidates': 3,
            'max_seconds': 3600 if revised else 1800,
            'max_validation_seconds': (5400 if stage == 'ground' else 3600) if revised else 1800,
            'max_initial_measurement_seconds': 3600 if revised else 1800}


def validate_policy(value, *, stage=None):
    if not isinstance(value, dict):
        raise VisualError('invalid_execution_policy', 'Execution policy must be the complete frozen object.')
    expected = resolve_policy(value.get('policy_id'), value.get('stage'))
    if value != expected or (stage is not None and value['stage'] != stage):
        raise VisualError('invalid_execution_policy', 'Execution policy differs from the frozen stage policy.')
    # bool compares equal to int in Python; reject that wire ambiguity explicitly.
    if any(type(value[field]) is not int for field in (*BUDGET_FIELDS, 'max_initial_measurement_seconds')):
        raise VisualError('invalid_execution_policy', 'Execution limits must be integer seconds/counts.')
    return dict(expected)


def budget_from_policy(value):
    policy = validate_policy(value)
    return {key: policy[key] for key in BUDGET_FIELDS}


def policy_from_state(state):
    declared = state.get('experiment', {}).get('execution_policy')
    if declared is None:
        if state.get('schema_version') != 1:
            raise VisualError('invalid_execution_policy', 'Revised sessions require their frozen execution policy.')
        return resolve_policy()
    policy = validate_policy(declared, stage=state.get('experiment', {}).get('stage'))
    expected_version = 2 if policy['policy_id'] == REVISED_ID else 1
    if state.get('schema_version') != expected_version:
        raise VisualError('invalid_execution_policy', 'Session schema and execution policy versions differ.')
    if state.get('budget') != budget_from_policy(policy):
        raise VisualError('invalid_execution_policy', 'Session budget does not match its declared execution policy.')
    return policy


def execution_job_limit(state, operation):
    """The job watchdog uses the same clock as its authoring/validation phase."""
    policy = policy_from_state(state)
    return policy['max_validation_seconds'] if operation == 'finalize' else policy['max_seconds']


class PassiveNativeActivity:
    """Observe native progress only; never send a message or change a deadline."""
    def __init__(self):
        self.events = 0
        self.thought_updates = 0
        self.pause_notifications = 0
        self.last_event_at = self.last_thought_at = None
        self.last_tool_started_at = self.last_tool_finished_at = None
        self.last_pause_at = None

    def observe(self, event, timestamp):
        if not isinstance(event, dict):
            return
        self.events += 1
        self.last_event_at = timestamp
        params = event.get('params', {})
        if not isinstance(params, dict):
            return
        update = params.get('update', {})
        if event.get('method') == 'session/update' and isinstance(update, dict):
            kind = update.get('sessionUpdate')
            if kind == 'agent_thought_chunk':
                self.thought_updates += 1
                self.last_thought_at = timestamp
            elif kind == 'tool_call':
                self.last_tool_started_at = timestamp
            elif kind == 'tool_call_update' and update.get('status') in ('completed', 'failed'):
                self.last_tool_finished_at = timestamp
        message = params.get('message', '')
        if (event.get('method') == '_kiro/system/notify' and params.get('level') == 'warning'
                and isinstance(message, str) and 'model response paused unexpectedly' in message.lower()):
            self.pause_notifications += 1
            self.last_pause_at = timestamp

    def summary(self):
        return {**vars(self), 'upstream_pause_observed': bool(self.pause_notifications),
                'provider_root_cause': 'unknown', 'automatic_resume_sent': False,
                'prompt_retried': False, 'deadline_extended': False,
                'scope': 'Passive native notifications; not a diagnosis of provider or network failure.'}
