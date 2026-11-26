from shared.core import *

s=Service('pet',('health.course.created','health.occurrence.completed')); app=s.app; db=s.db

def owned(id,owner):
    p=db.require('pet',id)
    if p['ownerId']!=owner:fail(403,'FORBIDDEN','Чужой питомец')
    return p

@s.route('POST','/api/v1/pets',roles=('OWNER',))
def create(b,p,u,r,req,raw):
    id=stable(f'pet:{u}:{KEY.get()}');return db.put('pet',id,dict(id=id,ownerId=u,**b))
@s.route('GET','/api/v1/pets',roles=('OWNER',))
def list_pets(b,p,u,r,req,raw):return page([x for x in db.all('pet') if x['ownerId']==u],p.get('page',1),p.get('size',20))
@s.route('GET','/api/v1/pets/{petId}',roles=('OWNER',))
def get_pet(b,p,u,r,req,raw):return owned(p['petId'],u)
@s.route('POST','/internal/v1/access/check',callers=('appointment','health'))
def access(b,p,u,r,req,raw):
    pet=db.get('pet',b['petId']);return {'allowed':bool(pet and pet['ownerId']==b['ownerId'])}
@s.route('POST','/api/v1/care/tasks',roles=('OWNER',))
def create_task(b,p,u,r,req,raw):
    owned(b['petId'],u)
    if dt(b['scheduledAt'])<=now():fail(400,'INVALID_DATE','Дело должно быть запланировано в будущем')
    b={**b,'scheduledAt':iso(dt(b['scheduledAt']))};id=stable(f'task:{u}:{KEY.get()}');return db.put('task',id,dict(id=id,**b,status='PENDING',completedAt=None))
@s.route('GET','/api/v1/care/tasks',roles=('OWNER',))
def tasks(b,p,u,r,req,raw):
    owned(p['petId'],u);return page(sorted([x for x in db.all('task') if x['petId']==p['petId']],key=lambda x:(x['scheduledAt'],x['id'])),p.get('page',1),p.get('size',20))
@s.route('PUT','/api/v1/care/tasks/{taskId}/completion',roles=('OWNER',))
def complete(b,p,u,r,req,raw):
    x=db.require('task',p['taskId']);owned(x['petId'],u)
    if x['status']!='DONE':x.update(status='DONE',completedAt=iso());db.put('task',x['id'],x)
    return x
@s.route('GET','/api/v1/calendar',roles=('OWNER',))
def calendar(b,p,u,r,req,raw):
    owned(p['petId'],u);start,end=dt(p['from']),dt(p['to'])
    if start>=end:fail(400,'INVALID_DATE','from должен быть раньше to')
    items=[dict(id=x['id'],kind='CARE',title=x['title'],scheduledAt=x['scheduledAt'],status=x['status']) for x in db.all('task') if x['petId']==p['petId']]
    items += [public(x,'petId') for x in db.all('projection') if x['petId']==p['petId']]
    return page(sorted([x for x in items if start<=dt(x['scheduledAt'])<end],key=lambda x:(x['scheduledAt'],x['id'])),p.get('page',1),p.get('size',20))
def event(e):
    p=e['payload']
    with db.atomic():
        if e['eventType']=='health.course.created':
            for x in p['occurrences']:
                id=x['occurrenceId'];mark=db.get('mark',id)
                db.put('projection',id,dict(id=id,petId=p['petId'],kind='MEDICATION',title=p['medication'],scheduledAt=x['scheduledAt'],status=mark['status'] if mark else 'PENDING'))
        elif e['eventType']=='health.occurrence.completed':
            id=p['occurrenceId'];db.put('mark',id,{'status':p['status']});x=db.get('projection',id)
            if x:x['status']=p['status'];db.put('projection',id,x)
s.on_event=event
