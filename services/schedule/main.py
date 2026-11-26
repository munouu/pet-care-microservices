from datetime import timedelta
from shared.core import *
from shared.rules import overlaps, reservation_deadline

s=Service('schedule');app=s.app;db=s.db

def release_reservation(r,status):
    with db.atomic():
        r['status']=status;db.put('reservation',r['appointmentId'],r)
        slot=db.require('slot',r['slotId'])
        if slot.get('_reservation')==r['appointmentId']:
            slot['status']='FREE';slot.pop('_reservation',None);db.put('slot',slot['id'],slot)
        if status=='EXPIRED':
            meta=db.get('reservation-meta',r['appointmentId']) or {};t=CORR.set(meta.get('correlationId') or CORR.get())
            try:db.emit('slot.expired',dict(appointmentId=r['appointmentId'],slotId=r['slotId']))
            finally:CORR.reset(t)
def expire():
    for r in db.all('reservation'):
        if r['status']=='HELD' and dt(r['expiresAt'])<=now():release_reservation(r,'EXPIRED')
s.tick=expire
@s.route('POST','/api/v1/admin/slots',roles=('CLINIC_ADMIN',))
def create(b,p,u,r,req,raw):
    if b['vetId']!=VET:fail(400,'INVALID_VET','Врач не зарегистрирован в справочнике авторизации')
    start,end=dt(b['startAt']),dt(b['endAt'])
    if start<=now() or end<=start:fail(400,'INVALID_DATE','Неверный интервал слота')
    b={**b,'startAt':iso(start),'endAt':iso(end)};id=stable(f'slot:{u}:{KEY.get()}')
    with db.atomic():
        old=db.get('slot',id)
        if old:return public(old,'_reservation')
        if any(x['vetId']==b['vetId'] and overlaps(dt(x['startAt']),dt(x['endAt']),start,end) for x in db.all('slot')):fail(409,'OVERLAPPING_SLOT','Слоты врача пересекаются')
        return db.put('slot',id,dict(id=id,**b,status='FREE'))
@s.route('GET','/api/v1/slots',roles=('OWNER',))
def slots(b,p,u,r,req,raw):
    expire();start,end=dt(p['from']),dt(p['to'])
    if start>=end:fail(400,'INVALID_DATE','from должен быть раньше to')
    items=[public(x,'_reservation') for x in db.all('slot') if x['status']=='FREE' and dt(x['startAt'])>now() and start<=dt(x['startAt'])<end and (not p.get('vetId') or x['vetId']==p['vetId'])]
    return page(sorted(items,key=lambda x:(x['startAt'],x['id'])),p.get('page',1),p.get('size',20))
@s.route('POST','/internal/v1/reservations',callers=('appointment',),replay_current=True)
def reserve(b,p,u,r,req,raw):
    expire();aid=b['appointmentId']
    with db.atomic():
        if db.get('cancelled',aid):fail(409,'RESERVATION_CANCELLED','Резерв уже отменён')
        old=db.get('reservation',aid)
        if old:
            if old['slotId']!=b['slotId'] or old['status'] not in ('HELD','BOOKED'):fail(409,'RESERVATION_CONFLICT','Резерв недействителен')
            return old
        x=db.require('slot',b['slotId'])
        if x['status']!='FREE' or dt(x['startAt'])<=now():fail(409,'SLOT_UNAVAILABLE','Слот недоступен')
        res=dict(slotId=x['id'],appointmentId=aid,vetId=x['vetId'],startAt=x['startAt'],feeKopecks=x['feeKopecks'],expiresAt=iso(reservation_deadline(now(),dt(x['startAt']))),status='HELD')
        x.update(status='HELD',_reservation=aid);db.put('slot',x['id'],x);db.put('reservation',aid,res);db.put('reservation-meta',aid,{'correlationId':CORR.get()});log('schedule',f'HELD slot={x["id"]}');return res
@s.route('PUT','/internal/v1/reservations/{appointmentId}/confirmation',callers=('appointment',),replay_current=True)
def confirm(b,p,u,r,req,raw):
    expire();aid=p['appointmentId']
    with db.atomic():
        x=db.require('reservation',aid)
        if x['status']=='BOOKED':return x
        if x['status']!='HELD':fail(409,'RESERVATION_EXPIRED','Резерв истёк или отменён')
        slot=db.require('slot',x['slotId']);slot['status']='BOOKED';db.put('slot',slot['id'],slot);x['status']='BOOKED';db.put('reservation',aid,x);log('schedule',f'BOOKED slot={slot["id"]}');return x
@s.route('DELETE','/internal/v1/reservations/{appointmentId}',callers=('appointment',))
def release(b,p,u,r,req,raw):
    aid=p['appointmentId']
    with db.atomic():
        db.put('cancelled',aid,{'cancelled':True});x=db.get('reservation',aid)
        if x and x['status'] in ('HELD','BOOKED'):release_reservation(x,'RELEASED');log('schedule',f'RELEASED slot={x["slotId"]}')
    return {'ok':True}
