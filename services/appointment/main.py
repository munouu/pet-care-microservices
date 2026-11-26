from shared.core import *

s=Service('appointment',('payment.completed','payment.failed','payment.refunded','slot.expired'));app=s.app;db=s.db

def view(x,role='OWNER'):
    x=public(x,'_reason','_correlation','_createdAt')
    if role!='OWNER' or x['status']!='PENDING_PAYMENT':x['paymentUrl']=None
    return x
def access(x,u,r):
    if not (r=='CLINIC_ADMIN' or r=='OWNER' and x['ownerId']==u or r=='VET' and x['vetId']==u):fail(403,'FORBIDDEN','Запись недоступна')
def save(x):return db.put('appointment',x['id'],x)
def payment_status(x):
    try:p=call('payment','GET',f'/internal/v1/payments/{x["id"]}',caller='appointment')['status']
    except Fault as e:
        if e.status==404:return 'CANCELLED'
        return x['paymentStatus']
    return {'INITIATED':'PENDING','COMPLETED':'PAID','FAILED':'FAILED','CANCELLED':'CANCELLED','REFUND_PENDING':'REFUND_PENDING','REFUNDED':'REFUNDED'}[p]
def cancel(x,reason):
    aid=x['id']
    if x['status']=='CANCELLED':return x
    x.update(status='CANCELLING',_reason=reason,paymentUrl=None,paymentStatus='CANCELLATION_PENDING');save(x)
    log('appointment',f'CANCELLING {aid} reason={reason}')
    try:
        call('payment','PUT',f'/internal/v1/payments/{aid}/cancellation',key=stable('cancel-payment:'+aid),caller='appointment')
        call('schedule','DELETE',f'/internal/v1/reservations/{aid}',key=stable('release:'+aid),caller='appointment')
    except Fault: return x # worker retries the same idempotent compensation
    ps=payment_status(x)
    with db.atomic():
        x.update(status='CANCELLED',paymentStatus=ps);save(x)
        db.emit('appointment.cancelled',dict(appointmentId=aid,ownerId=x['ownerId'],petId=x['petId'],reason=reason))
    log('appointment',f'CANCELLED {aid}');return x
@s.route('POST','/api/v1/appointments',roles=('OWNER',))
def create(b,p,u,r,req,raw):
    owner_check(b['petId'],u,'appointment');aid=stable(f'appointment:{u}:{KEY.get()}');x=db.get('appointment',aid)
    if x:
        if x['status']=='PENDING_PAYMENT':return view(x)
        if x['status']!='PREPARING':fail(409,'APPOINTMENT_CONFLICT','Эта попытка уже завершена; используйте новый ключ')
    else:
        x=dict(id=aid,petId=b['petId'],slotId=b['slotId'],ownerId=u,vetId=None,startAt=None,amountKopecks=None,status='PREPARING',paymentStatus='NOT_STARTED',paymentUrl=None,expiresAt=None,_correlation=CORR.get(),_createdAt=iso());save(x)
    try:
        h=call('schedule','POST','/internal/v1/reservations',dict(slotId=b['slotId'],appointmentId=aid),key=stable('reserve:'+aid),caller='appointment')
        x.update(vetId=h['vetId'],startAt=h['startAt'],amountKopecks=h['feeKopecks'],expiresAt=h['expiresAt']);save(x)
        pay=call('payment','POST','/internal/v1/payments',dict(appointmentId=aid,amountKopecks=x['amountKopecks'],currency='RUB',expiresAt=x['expiresAt']),key=stable('payment:'+aid),caller='appointment')
        x.update(status='PENDING_PAYMENT',paymentStatus='PENDING',paymentUrl=pay['paymentUrl']);save(x);log('appointment',f'PENDING_PAYMENT {aid}');return view(x)
    except Fault as e:
        cancel(x,'PREPARATION_FAILED');raise e
@s.route('GET','/api/v1/appointments',roles=('OWNER','VET','CLINIC_ADMIN'))
def list_appointments(b,p,u,r,req,raw):
    items=[view(x,r) for x in db.all('appointment') if (r=='CLINIC_ADMIN' or (r=='OWNER' and x['ownerId']==u) or (r=='VET' and x['vetId']==u)) and (not p.get('status') or x['status']==p['status'])]
    return page(items,p.get('page',1),p.get('size',20))
@s.route('GET','/api/v1/appointments/{appointmentId}',roles=('OWNER','VET','CLINIC_ADMIN'))
def get(b,p,u,r,req,raw):
    x=db.require('appointment',p['appointmentId']);access(x,u,r);return view(x,r)
@s.route('PUT','/api/v1/appointments/{appointmentId}/cancellation',roles=('OWNER','CLINIC_ADMIN'))
def cancel_public(b,p,u,r,req,raw):
    x=db.require('appointment',p['appointmentId']);access(x,u,r)
    if x['status']=='CANCELLED':return view(x,r)
    if x['status']=='COMPLETED' or x['startAt'] and dt(x['startAt'])<=now():fail(409,'APPOINTMENT_STARTED','Приём уже начался')
    return view(cancel(x,'USER_CANCELLED'),r)
@s.route('PUT','/api/v1/appointments/{appointmentId}/completion',roles=('VET',))
def complete(b,p,u,r,req,raw):
    x=db.require('appointment',p['appointmentId']);access(x,u,r)
    if x['status']=='COMPLETED':return view(x,r)
    if x['status']!='CONFIRMED' or dt(x['startAt'])>now():fail(409,'INVALID_STATE','Приём ещё не начался или не подтверждён')
    x['status']='COMPLETED';save(x);return view(x,r)
@s.route('GET','/internal/v1/appointments/{appointmentId}/access',callers=('health',))
def vet_access(b,p,u,r,req,raw):
    x=db.require('appointment',p['appointmentId'])
    if x['vetId']!=p['vetId'] or x['status'] not in ('CONFIRMED','COMPLETED'):fail(403,'FORBIDDEN','Нет доступа к истории пациента')
    return dict(petId=x['petId'],ownerId=x['ownerId'],status=x['status'])
def confirm(x):
    aid=x['id'];x.update(status='CONFIRMING',paymentStatus='PAID',paymentUrl=None);save(x)
    try:call('schedule','PUT',f'/internal/v1/reservations/{aid}/confirmation',key=stable('confirm:'+aid),caller='appointment')
    except Fault as e:
        if e.status in (404,409):cancel(x,'RESERVATION_EXPIRED');return
        raise
    with db.atomic():
        x['status']='CONFIRMED';save(x);db.emit('appointment.confirmed',dict(appointmentId=aid,ownerId=x['ownerId'],petId=x['petId'],vetId=x['vetId'],startAt=x['startAt']))
    log('appointment',f'CONFIRMED {aid}')
def event(e):
    p=e['payload'];x=db.get('appointment',p['appointmentId'])
    if not x:return
    k=e['eventType']
    if k=='payment.completed':
        if x['status'] in ('CANCELLED','CANCELLING'):
            call('payment','PUT',f'/internal/v1/payments/{x["id"]}/cancellation',key=stable('cancel-payment:'+x['id']),caller='appointment');x['paymentStatus']=payment_status(x);save(x)
        elif x['status'] in ('PENDING_PAYMENT','CONFIRMING'):confirm(x)
    elif k=='payment.failed' and x['status'] in ('PREPARING','PENDING_PAYMENT'):cancel(x,'PAYMENT_FAILED')
    elif k=='slot.expired' and x['status'] in ('PREPARING','PENDING_PAYMENT','CONFIRMING'):cancel(x,'RESERVATION_EXPIRED')
    elif k=='payment.refunded':x['paymentStatus']='REFUNDED';save(x)
s.on_event=event
def tick():
    for x in db.all('appointment'):
        t=CORR.set(x.get('_correlation'))
        try:
            if x['status']=='CANCELLING':cancel(x,x.get('_reason','CANCELLED'))
            elif x['status']=='PREPARING' and (now()-dt(x['_createdAt'])).total_seconds()>30:cancel(x,'PREPARATION_TIMEOUT')
            elif x['status']=='PENDING_PAYMENT' and x['expiresAt'] and dt(x['expiresAt'])<=now():cancel(x,'RESERVATION_EXPIRED')
            elif x['status']=='CONFIRMING':
                try:confirm(x)
                except Fault:
                    if dt(x['expiresAt'])<=now():cancel(x,'CONFIRMATION_TIMEOUT')
            elif x['status']=='CANCELLED' and x['paymentStatus'] in ('CANCELLATION_PENDING','PENDING','PAID','REFUND_PENDING'):
                x['paymentStatus']=payment_status(x);save(x)
        finally:CORR.reset(t)
s.tick=tick
