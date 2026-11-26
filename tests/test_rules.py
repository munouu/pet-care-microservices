from datetime import datetime, timedelta, timezone
import pytest
from shared.rules import overlaps,reservation_deadline,dose_times
from shared.core import Store, Fault, validate, stable, page

T=datetime(2027,1,1,10,tzinfo=timezone.utc)
def test_adjacent_slots_do_not_overlap():
    assert not overlaps(T,T+timedelta(minutes=30),T+timedelta(minutes=30),T+timedelta(hours=1))
def test_slots_that_share_time_overlap():
    assert overlaps(T,T+timedelta(minutes=30),T+timedelta(minutes=15),T+timedelta(hours=1))
def test_deadline_is_15_minutes_or_start():
    assert reservation_deadline(T,T+timedelta(hours=1))==T+timedelta(minutes=15)
    assert reservation_deadline(T,T+timedelta(minutes=5))==T+timedelta(minutes=5)
def test_course_doses():
    assert dose_times(T,8,3)==[T,T+timedelta(hours=8),T+timedelta(hours=16)]
def test_store_business_and_outbox_are_atomic(tmp_path):
    db=Store('test',tmp_path)
    with pytest.raises(RuntimeError):
        with db.atomic():
            db.put('appointment','a',{'status':'CONFIRMED'});db.emit('appointment.confirmed',{});raise RuntimeError('fail')
    assert db.get('appointment','a') is None and not db.all('outbox')
    with db.atomic():db.put('appointment','a',{'status':'CONFIRMED'});db.emit('appointment.confirmed',{})
    assert db.get('appointment','a')['status']=='CONFIRMED' and len(db.all('outbox'))==1
def test_key_is_stable_and_page_is_bounded():
    assert stable('appointment:owner:key')==stable('appointment:owner:key')
    assert stable('appointment:owner:key')!=stable('appointment:other:key')
    assert page([1,2,3],2,2)=={'items':[3],'page':2,'size':2,'total':3}
def test_request_schema_rejects_bad_uuid():
    with pytest.raises(Fault) as e:validate({}, {'type':'string','format':'uuid'},'bad')
    assert e.value.status==400
