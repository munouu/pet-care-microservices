import hmac
from shared.core import *

s=Service('payment');app=s.app;db=s.db

def view(x):return public(x,'providerPaymentId','_cancelled')
def signature(raw):return hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()
def cancel_provider(aid):
    x=db.get('payment',aid)
    if not x:return
    reply=call('psp','POST',f'/payments/{aid}/cancel',caller='payment')
    with db.atomic():
        if reply['status']=='REFUNDED' and x['status']!='REFUNDED':
            x.update(status='REFUNDED',paymentUrl=None);db.put('payment',aid,x);db.emit('payment.refunded',dict(appointmentId=aid,paymentId=x['id'],amountKopecks=x['amountKopecks']))
        elif reply['status']!='REFUNDED' and x['status'] not in ('REFUNDED','FAILED'):
            x.update(status='CANCELLED',paymentUrl=None);db.put('payment',aid,x)
        db.remove('cancel-pending',aid)
@s.route('POST','/internal/v1/payments',callers=('appointment',),replay_current=True)
def create(b,p,u,r,req,raw):
    aid=b['appointmentId']
    if db.get('cancelled',aid):fail(409,'PAYMENT_CANCELLED','Платёж отменён')
    x=db.get('payment',aid)
    if x and (x['amountKopecks']!=b['amountKopecks'] or x['currency']!=b['currency']):fail(409,'PAYMENT_CONFLICT','Сумма уже зафиксирована')
    if x and x.get('providerPaymentId'):return view(x)
    if not x:
        x=dict(id=stable('payment:'+aid),appointmentId=aid,amountKopecks=b['amountKopecks'],currency=b['currency'],status='INITIATED',paymentUrl=None,providerPaymentId=None);db.put('payment',aid,x)
    resp=call('psp','POST','/payments',b,caller='payment',key=aid)
    x.update(providerPaymentId=resp['providerPaymentId'],paymentUrl=resp['paymentUrl']);db.put('payment',aid,x);return view(x)
@s.route('GET','/internal/v1/payments/{appointmentId}',callers=('appointment',))
def get_payment(b,p,u,r,req,raw):return view(db.require('payment',p['appointmentId']))
@s.route('PUT','/internal/v1/payments/{appointmentId}/cancellation',callers=('appointment',))
def cancel(b,p,u,r,req,raw):
    aid=p['appointmentId']
    with db.atomic():
        db.put('cancelled',aid,{'cancelled':True});db.put('cancel-pending',aid,{'appointmentId':aid,'correlationId':CORR.get()})
        x=db.get('payment',aid)
        if x and x['status']=='COMPLETED':x.update(status='REFUND_PENDING',paymentUrl=None);db.put('payment',aid,x)
    # Provider retry is asynchronous; the intent is already saved.
    try:cancel_provider(aid)
    except Fault:pass
    return {'ok':True}
@s.route('POST','/webhooks/payments')
def callback(b,p,u,r,req,raw):
    if not hmac.compare_digest(req.headers.get('X-Provider-Signature',''),signature(raw)):fail(403,'BAD_SIGNATURE','Подпись PSP неверна')
    aid=b['appointmentId'];x=db.require('payment',aid)
    if x['providerPaymentId']!=b['providerPaymentId'] or x['amountKopecks']!=b['amountKopecks'] or x['currency']!=b['currency']:fail(409,'PAYMENT_MISMATCH','Идентификатор или сумма не совпадает')
    with db.atomic():
        if db.get('provider-event',b['providerEventId']):return {'ok':True}
        db.put('provider-event',b['providerEventId'],{'done':True})
        if b['status']=='SUCCEEDED' and x['status'] not in ('COMPLETED','REFUND_PENDING','REFUNDED'):
            x.update(status='COMPLETED',paymentUrl=None);db.put('payment',aid,x)
            db.emit('payment.completed',dict(appointmentId=aid,paymentId=x['id'],amountKopecks=x['amountKopecks']))
            if db.get('cancelled',aid):
                x['status']='REFUND_PENDING';db.put('payment',aid,x);db.put('cancel-pending',aid,{'appointmentId':aid,'correlationId':CORR.get()})
        elif b['status']=='FAILED' and x['status']=='INITIATED':
            x.update(status='FAILED',paymentUrl=None);db.put('payment',aid,x);db.emit('payment.failed',dict(appointmentId=aid,paymentId=x['id'],reason='DECLINED'))
        elif b['status']=='REFUNDED' and x['status']!='REFUNDED':
            x.update(status='REFUNDED',paymentUrl=None);db.put('payment',aid,x);db.emit('payment.refunded',dict(appointmentId=aid,paymentId=x['id'],amountKopecks=x['amountKopecks']))
    log('payment',f'CALLBACK {b["status"]} appointment={aid}')
    return {'ok':True}
def tick():
    for x in db.all('cancel-pending'):
        t=CORR.set(x.get('correlationId'))
        try:cancel_provider(x['appointmentId']);db.remove('cancel-pending',x['appointmentId'])
        except Fault:pass
        finally:CORR.reset(t)
s.tick=tick
