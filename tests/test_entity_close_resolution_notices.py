"""Close-time resolution notices come from completed reflection metadata only."""
import pytest
from abstractgateway.entity_visits import close_notices

pytestmark = pytest.mark.basic


def reflection(entries, status='completed', turn='t-reflect'):
    return {'effect': {'type': 'llm_call', 'payload': {'turn_id': turn}}, 'status': status, 'result': {'diary_entries': entries}}


def test_close_resolution_notice_is_confirmed_private_safe_and_deduplicated():
    public = {'resolves_status': 'resolved_open_question', 'resolved_record_id': 'ex:q1', 'resolved_title': 'A public question'}
    private = {**public, 'resolved_record_id': 'ex:q2', 'resolved_visibility': 'private', 'resolved_title': 'PRIVATE WORDS'}
    rows = [reflection([public], status='started'), reflection([public, private, public]), reflection([public])]
    assert close_notices(rows) == [
        {'kind': 'question_resolved', 'record_id': 'ex:q1', 'title': 'A public question'},
        {'kind': 'question_resolved', 'record_id': 'ex:q2', 'title': '', 'visibility': 'private'},
    ]


def test_close_rejects_unconfirmed_other_turn_and_unprojected_claims():
    confirmed = {'resolves_status': 'resolved_open_question', 'resolved_record_id': 'ex:q'}
    assert close_notices([
        reflection([confirmed], turn='t-main'), reflection([confirmed], status='failed'),
        reflection([{**confirmed, 'resolves_status': 'target_not_found'}, {'resolves_status': 'resolved_open_question'}]),
    ]) == []


def test_close_notices_interleave_completed_formations_and_reflections_in_ledger_order():
    lesson = {'effect': {'type':'memory_form','payload':{'turn_id':'t-reflect-lesson-1','records':[{'title':'Lesson','digest':'Learned'}]}},'status':'started'}
    completed = {'effect':{'type':'memory_form','payload':{'turn_id':'t-reflect-lesson-1'}},'status':'completed','result':{'record_ids':['ex:lesson']}}
    q1 = {'resolves_status':'resolved_open_question','resolved_record_id':'ex:q1','resolved_title':'First question'}
    q2 = {**q1, 'resolved_record_id':'ex:q2','resolved_title':'Second question'}
    result = close_notices([lesson,reflection([q1]),completed,reflection([q2,q1])])
    assert [(notice['kind'],notice['record_id']) for notice in result] == [
        ('question_resolved','ex:q1'),('lesson','ex:lesson'),('question_resolved','ex:q2')]
    assert result[1]['title'] == 'Lesson' and result[1]['text'] == 'Learned'
