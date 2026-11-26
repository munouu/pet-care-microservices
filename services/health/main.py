from datetime import timedelta
from shared.core import *
from shared.rules import dose_times
s=Service('health');app=s.app;db=s.db

def vet_access(aid,vet):return call('appointment','GET',f'/internal/v1/appointments/{aid}/access',caller='health',params={'vetId':vet})
def course_view(x):return public(x,'ownerId')
@s.route('POST','/api/v1/health/courses',roles=('VET',))
def create(b,p,u,r,req,raw):
    info=vet_access(b['appointmentId'],u)
    if info['status']!='COMPLETED':fail(409,'VISIT_NOT_COMPLETED','Сначала завершите приём')
    if dt(b['firstDoseAt'])<=now():fail(400,'INVALID_DATE','Первый приём лекарства должен быть в будущем')
    b={**b,'firstDoseAt':iso(dt(b['firstDoseAt']))};id=stable(f'course:{u}:{KEY.get()}')
    with db.atomic():
        old=db.get('course',id)
        if old:return course_view(old)
        c=dict(id=id,petId=info['petId'],ownerId=info['ownerId'],vetId=u,**b);db.put('course',id,c);occ=[]
        for i,dose_at in enumerate(dose_times(dt(b['firstDoseAt']),b['intervalHours'],b['doseCount'])):
            oid=stable(f'occurrence:{id}:{i}');at=iso(dose_at);db.put('occurrence',oid,dict(id=oid,courseId=id,scheduledAt=at,status='PENDING',recordedAt=None));occ.append(dict(occurrenceId=oid,scheduledAt=at))
        db.emit('health.course.created',dict(courseId=id,petId=c['petId'],ownerId=c['ownerId'],medication=c['medication'],occurrences=occ));return course_view(c)
@s.route('GET','/api/v1/health/courses',roles=('OWNER',))
def courses(b,p,u,r,req,raw):
    owner_check(p['petId'],u,'health');return page([course_view(x) for x in db.all('course') if x['petId']==p['petId']],p.get('page',1),p.get('size',20))
@s.route('GET','/api/v1/health/visits/{appointmentId}/courses',roles=('VET',))
def history(b,p,u,r,req,raw):
    info=vet_access(p['appointmentId'],u);return page([course_view(x) for x in db.all('course') if x['petId']==info['petId']],p.get('page',1),p.get('size',20))
@s.route('GET','/api/v1/health/courses/{courseId}/occurrences',roles=('OWNER',))
def occurrences(b,p,u,r,req,raw):
    c=db.require('course',p['courseId']);owner_check(c['petId'],u,'health');return page(sorted([x for x in db.all('occurrence') if x['courseId']==c['id']],key=lambda x:(x['scheduledAt'],x['id'])),p.get('page',1),p.get('size',20))
@s.route('PUT','/api/v1/health/occurrences/{occurrenceId}/completion',roles=('OWNER',))
def complete(b,p,u,r,req,raw):
    x=db.require('occurrence',p['occurrenceId']);c=db.require('course',x['courseId']);owner_check(c['petId'],u,'health')
    if x['status']==b['status']:return x
    if x['status']!='PENDING':fail(409,'ALREADY_RECORDED','Факт уже зафиксирован')
    with db.atomic():
        x.update(status=b['status'],recordedAt=iso());db.put('occurrence',x['id'],x);db.emit('health.occurrence.completed',dict(courseId=c['id'],petId=c['petId'],occurrenceId=x['id'],status=x['status'],recordedAt=x['recordedAt']))
    return x
@s.route('GET','/internal/v1/health/occurrences/{occurrenceId}',callers=('notification',))
def get_occurrence(b,p,u,r,req,raw):return db.require('occurrence',p['occurrenceId'])
