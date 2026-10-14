"""Pure and fake-runtime tests for the unattended interaction boundary."""
import pytest
from kanpilot import plus_interaction, util


def receipt(mode='unattended', version=1, sid='s'):
    return {'session': {'id': sid, 'interactionMode': mode, 'interactionPolicyVersion': version}}


@pytest.mark.parametrize('value', [{}, receipt('interactive'), receipt(version=None), receipt(version=True), receipt(version=2), receipt(sid='other')])
def test_policy_requires_exact_host_receipt(value):
    with pytest.raises(util.KanpilotError) as exc:
        plus_interaction.require_unattended(value, 's')
    assert exc.value.code == 'interaction_policy_unverified'


def interaction(questions=None, **extra):
    return {'status': 'error', 'errorCode': 'INTERACTION_REQUIRED', 'errorDetails': {
        'kind': 'interaction_required', 'toolName': 'asktool', 'toolCallId': 'ask-1',
        'questions': questions if questions is not None else [{'question': '范围？', 'options': ['A', {'label': 'B', 'description': '说明'}]}], **extra}}


def test_valid_questions_map_without_inventing_recommendation():
    raw = interaction()
    questions = plus_interaction.planner_questions(plus_interaction.required_interaction(raw))
    assert questions[0]['recommended_option_id'] is None
    assert questions[0]['options'][1] == {'id': 'option-1', 'label': 'B', 'description': '说明'}
    assert plus_interaction.planner_questions(raw['errorDetails']) == questions


def test_multiselect_preserves_options_in_text_question():
    details = interaction([{'question': '范围？', 'options': ['A', {'label': 'B', 'description': '说明'}], 'multiSelect': True}])['errorDetails']
    question = plus_interaction.planner_questions(details)[0]
    assert question['options'] == []
    assert question['allow_custom'] is True
    assert question['required'] is True
    assert '可多选' in question['description']
    assert 'A' in question['description'] and 'B：说明' in question['description']


@pytest.mark.parametrize('value', [interaction([]), interaction(invalidArguments=True),
    interaction([{'question': '', 'options': ['A']}]),
    interaction([{'question': '问题', 'options': [None]}]),
    interaction([{'question': '问题', 'options': [], 'multiSelect': 'yes'}]),
    {'status': 'completed', **{k:v for k,v in interaction().items() if k != 'status'}},
    {'status': 'error', 'errorCode': 'INTERACTION_REQUIRED', 'errorDetails': {}}])
def test_invalid_interceptions_do_not_make_questions(value):
    with pytest.raises(util.KanpilotError) as exc:
        plus_interaction.required_interaction(value)
    assert exc.value.code == 'plan_interaction_invalid'


@pytest.mark.parametrize('details', [
    {'questions': [{'question': '问题', 'options': []}]},
    {'questions': [{'question': '问题', 'options': ['A']}] * 21},
    {'questions': [{'question': '问题', 'options': ['A'] * 101}]},
    {'questions': [{'question': '长' * 22000, 'options': ['A']}]},
    {'toolCallId': '号' * 171},
])
def test_oversized_and_empty_host_questions_are_invalid(details):
    value = interaction()
    value['errorDetails'].update(details)
    with pytest.raises(util.KanpilotError) as exc:
        plus_interaction.required_interaction(value)
    assert exc.value.code == 'plan_interaction_invalid'


def test_whole_details_json_utf8_limit_matches_host():
    import json
    value = interaction([{'question': 'x', 'options': ['A']}])
    size = len(json.dumps(value['errorDetails'], ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    value['errorDetails']['questions'][0]['question'] = 'x' * (65536 - size + 1)
    assert plus_interaction.required_interaction(value) is value['errorDetails']
    value['errorDetails']['questions'][0]['question'] += 'x'
    with pytest.raises(util.KanpilotError):
        plus_interaction.required_interaction(value)


def test_host_bounds_accept_maximum_counts_and_unicode_call_id():
    value = interaction([{'question': '问题', 'options': ['A'] * 100}] * 20)
    value['errorDetails']['toolCallId'] = '号' * 170 + 'ab'
    assert plus_interaction.required_interaction(value) is value['errorDetails']
