from shared.core import *
s=Service('notification',('appointment.confirmed','appointment.cancelled','health.course.created','health.occurrence.completed'));app=s.app;db=s.db
@s.route('PUT','/api/v1/device',roles=('OWNER',))
def register(b,p,u,r,req,raw):
    if any(x['ownerId']!=u and x['token']==b['pushToken'] for x in db.all('device')):fail(409,'TOKEN_IN_USE','Токен уже принадлежит другому владельцу')
    db.put('device',u,dict(ownerId=u,token=b['pushToken']));return {'ok':True}
@s.route('GET','/api/v1/notifications',roles=('OWNER',))
def notifications(b,p,u,r,req,raw):return page(sorted([public(x,'ownerId','reference','attempts','correlationId') for x in db.all('notification') if x['ownerId']==u],key=lambda x:(x['scheduledAt'],x['id'])),p.get('page',1),p.get('size',20))
def add(owner,kind,ref,at,cid):
    id=stable(f'notification:{owner}:{kind}:{ref}')
    if not db.get('notification',id):db.put('notification',id,dict(id=id,ownerId=owner,kind=kind,reference=ref,scheduledAt=at,status='PENDING',attempts=0,correlationId=cid))
def event(e):
    p=e['payload'];k=e['eventType']
    with db.atomic():
        if k=='appointment.confirmed':
            if not db.get('cancelled-appointment',p['appointmentId']):add(p['ownerId'],'APPOINTMENT_CONFIRMED',p['appointmentId'],e['occurredAt'],e['correlationId'])
        elif k=='appointment.cancelled':
            db.put('cancelled-appointment',p['appointmentId'],{'done':True})
            for x in db.all('notification'):
                if x['reference']==p['appointmentId'] and x['kind']=='APPOINTMENT_CONFIRMED' and x['status']=='PENDING':x['status']='CANCELLED';db.put('notification',x['id'],x)
            add(p['ownerId'],'APPOINTMENT_CANCELLED',p['appointmentId'],e['occurredAt'],e['correlationId'])
        elif k=='health.course.created':
            for x in p['occurrences']:
                if not db.get('completed-occurrence',x['occurrenceId']):add(p['ownerId'],'MEDICATION',x['occurrenceId'],x['scheduledAt'],e['correlationId'])
        elif k=='health.occurrence.completed':
            db.put('completed-occurrence',p['occurrenceId'],{'done':True})
            for x in db.all('notification'):
                if x['kind']=='MEDICATION' and x['reference']==p['occurrenceId'] and x['status']=='PENDING':x['status']='CANCELLED';db.put('notification',x['id'],x)
s.on_event=event

def tick():
    for x in db.all('notification'):
        if x['status']!='PENDING' or dt(x['scheduledAt'])>now():continue
        t=CORR.set(x['correlationId'])
        try:
            if x['kind']=='MEDICATION':
                if (now()-dt(x['scheduledAt'])).total_seconds()>3600:
                    x['status']='CANCELLED';db.put('notification',x['id'],x);continue
                if call('health','GET',f'/internal/v1/health/occurrences/{x["reference"]}',caller='notification')['status']!='PENDING':
                    x['status']='CANCELLED';db.put('notification',x['id'],x);continue
            device=db.get('device',x['ownerId'])
            if not device:x['status']='FAILED'
            else:
                call('fcm','POST','/messages:send',{'token':device['token'],'kind':x['kind'],'reference':x['reference'],'notificationId':x['id']},caller='notification');x['status']='SENT'
                log('notification',f'SENT {x["kind"]} reference={x["reference"]}')
            db.put('notification',x['id'],x)
        except Fault as e:
            x['attempts']+=1
            if x['attempts']>=3:x['status']='FAILED'
            db.put('notification',x['id'],x);log('notification',f'PUSH RETRY {e.code}')
        finally:CORR.reset(t)
s.tick=tick
