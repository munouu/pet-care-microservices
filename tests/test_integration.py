"""End-to-end tests across independent processes. No Docker or external accounts needed."""
import os,re,signal,subprocess,sys,time,itertools,socket
from datetime import timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx,pytest,yaml
from shared.core import OWNER,VET,ADMIN,ROOT,uid,now,iso,validate,resolve
from scripts.run import SERVICES

OFFSET=int(os.getenv('TEST_PORT_OFFSET','12000')); BASE=f'http://127.0.0.1:{8080+OFFSET}'
SPECS={n:yaml.safe_load((ROOT/'api'/f'{n}-service.yaml').read_text()) for n in ('pet','schedule','appointment','payment','health','notification')}
ROUTES={}
for n,spec in SPECS.items():
    for p,methods in spec['paths'].items():
        for m,op in methods.items():ROUTES[(m.upper(),p)]=(spec,op)

@pytest.fixture(scope='module',autouse=True)
def cluster(tmp_path_factory):
    global OFFSET,BASE
    # Select an unused block of ports so a previous local run cannot contaminate the tests.
    def available(offset):
        sockets=[]
        try:
            for _,port in SERVICES.values():
                sock=socket.socket();sockets.append(sock);sock.bind(('127.0.0.1',port+offset))
            return True
        except OSError:return False
        finally:
            for sock in sockets:sock.close()
    for candidate in range(OFFSET,40000,100):
        if available(candidate):OFFSET=candidate;break
    else:pytest.fail('No free block of ports')
    BASE=f'http://127.0.0.1:{8080+OFFSET}'
    root=tmp_path_factory.mktemp('pet-cluster');logs=root/'logs';data=root/'data'
    proc=subprocess.Popen([sys.executable,'scripts/run.py','--quiet','--port-offset',str(OFFSET),'--data-dir',str(data),'--log-dir',str(logs)],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT)
    try:
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            if proc.poll() is not None:pytest.fail(f'Stack stopped; logs {logs}')
            try:
                if all(httpx.get(f'http://127.0.0.1:{port+OFFSET}/health',timeout=.5).status_code==200 for _,port in SERVICES.values()):break
            except httpx.RequestError:pass
            time.sleep(.25)
        else:pytest.fail(f'Stack did not start; logs {logs}')
        yield
    finally:
        if proc.poll() is None:proc.terminate()
        try:proc.wait(timeout=15)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()

N=itertools.count(1)
def request(method,path,body=None,role='OWNER',key=None,expect=200,user=None,params=None):
    headers={'X-User-Id':user or {'OWNER':OWNER,'VET':VET,'CLINIC_ADMIN':ADMIN}[role],'X-Role':role,'Idempotency-Key':key or uid()}
    r=httpx.request(method,BASE+path,json=body,headers=headers,params=params,timeout=20)
    assert r.status_code==expect,(method,path,r.status_code,r.text)
    if expect>=400:assert 'application/problem+json' in r.headers['content-type']
    # Check actual response against the LR3 contract, if it is a business endpoint.
    for (m,p),(spec,op) in ROUTES.items():
        if m==method and re.fullmatch(re.sub(r'\{[^}]+\}',r'[^/]+',p),path) and str(expect) in op['responses']:
            response=resolve(spec,op['responses'][str(expect)]);schema=next(iter(response['content'].values()))['schema'];validate(spec,schema,r.json());break
    return r.json()
def until(fn,check,timeout=12):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        data=fn()
        if check(data):return data
        time.sleep(.25)
    pytest.fail(f'Expected state not reached: {data}')
def pet():return request('POST','/api/v1/pets',{'name':'Барсик','species':'CAT'},expect=201)['id']
def slot(start=None,duration=15):
    start=start or now()+timedelta(hours=next(N)*2+1)
    return request('POST','/api/v1/admin/slots',dict(vetId=VET,startAt=iso(start),endAt=iso(start+timedelta(minutes=duration)),feeKopecks=150000),role='CLINIC_ADMIN',expect=201)
def create(pet_id,slot_id,key=None):return request('POST','/api/v1/appointments',{'petId':pet_id,'slotId':slot_id},key=key,expect=201)
def pay(aid,status):return request('POST',f'/mock/payments/{aid}/result',{'status':status})
def appointment(aid):return request('GET',f'/api/v1/appointments/{aid}')
def wait_state(aid,state):return until(lambda:appointment(aid),lambda x:x['status']==state)
def free(slot_data):
    start=slot_data['startAt'];end=slot_data['endAt'];x=request('GET','/api/v1/slots',params={'from':start,'to':end});return any(s['id']==slot_data['id'] for s in x['items'])

@pytest.fixture(scope='module')
def owner_device():request('PUT','/api/v1/device',{'pushToken':'integration-token'})

def test_happy_path_and_idempotency(owner_device):
    p=pet();sl=slot();k=uid();a=create(p,sl['id'],k);aid=a['id']
    assert a['status']=='PENDING_PAYMENT' and a['paymentUrl'] and not free(sl)
    assert create(p,sl['id'],k)['id']==aid
    request('POST','/api/v1/appointments',{'petId':p,'slotId':uid()},key=k,expect=409)
    pay(aid,'SUCCEEDED');x=wait_state(aid,'CONFIRMED');assert x['paymentStatus']=='PAID' and not free(sl)
    items=lambda:request('GET','/api/v1/notifications')['items']
    # The first success notification is accepted by the mock provider.
    until(items,lambda items:any(x['kind']=='APPOINTMENT_CONFIRMED' and x['status']=='SENT' for x in items))
    before=len(items());pay(aid,'SUCCEEDED');time.sleep(1.0);assert len(items())==before

def test_failed_payment_frees_slot(owner_device):
    p=pet();sl=slot();a=create(p,sl['id']);assert not free(sl)
    pay(a['id'],'FAILED');x=wait_state(a['id'],'CANCELLED');assert x['paymentStatus']=='FAILED' and free(sl)
    a2=create(p,sl['id']);assert a2['status']=='PENDING_PAYMENT' and a2['id']!=a['id']
    request('PUT',f'/api/v1/appointments/{a2["id"]}/cancellation',expect=202);wait_state(a2['id'],'CANCELLED')
    until(lambda:request('GET','/api/v1/notifications')['items'],lambda items:any(x['kind']=='APPOINTMENT_CANCELLED' and x['status']=='SENT' for x in items))

def test_permissions_and_validation():
    p=pet();sl=slot();other=uid()
    request('POST','/api/v1/appointments',{'petId':p,'slotId':sl['id']},user=other,expect=403)
    assert free(sl)
    request('POST','/api/v1/appointments',{'petId':'bad','slotId':sl['id']},expect=400)
    request('GET','/api/v1/pets/'+p,user=other,expect=403)
    request('POST','/internal/v1/access/check',{'petId':p,'ownerId':OWNER},expect=404)

def test_cancellation_after_payment_refunds(owner_device):
    p=pet();sl=slot();a=create(p,sl['id']);pay(a['id'],'SUCCEEDED');wait_state(a['id'],'CONFIRMED')
    request('PUT',f'/api/v1/appointments/{a["id"]}/cancellation',expect=202)
    x=until(lambda:appointment(a['id']),lambda x:x['status']=='CANCELLED' and x['paymentStatus']=='REFUNDED');assert free(sl)

def test_late_payment_after_expiry_is_refunded(owner_device):
    p=pet();sl=slot(now()+timedelta(seconds=5),duration=0.01);a=create(p,sl['id']);wait_state(a['id'],'CANCELLED');pay(a['id'],'SUCCEEDED')
    x=until(lambda:appointment(a['id']),lambda x:x['paymentStatus']=='REFUNDED');assert x['status']=='CANCELLED'

def test_two_clients_cannot_book_same_slot():
    p=pet();sl=slot()
    def do():return httpx.post(BASE+'/api/v1/appointments',json={'petId':p,'slotId':sl['id']},headers={'X-User-Id':OWNER,'X-Role':'OWNER','Idempotency-Key':uid()},timeout=20)
    with ThreadPoolExecutor(max_workers=2) as pool:r1,r2=list(pool.map(lambda _:do(),range(2)))
    assert sorted([r1.status_code,r2.status_code])==[201,409]
    winner=(r1 if r1.status_code==201 else r2).json();assert not free(sl)
    request('PUT',f'/api/v1/appointments/{winner["id"]}/cancellation',expect=202);wait_state(winner['id'],'CANCELLED')

def test_care_and_health_projection(owner_device):
    p=pet();future=now()+timedelta(minutes=3)
    task=request('POST','/api/v1/care/tasks',{'petId':p,'title':'Кормление','scheduledAt':iso(future)},expect=201)
    assert request('PUT',f'/api/v1/care/tasks/{task["id"]}/completion')['status']=='DONE'
    # Separate short slot in a different interval from the expiration test.
    sl=slot(now()+timedelta(seconds=7)+timedelta(minutes=0))
    a=create(p,sl['id']);pay(a['id'],'SUCCEEDED');wait_state(a['id'],'CONFIRMED')
    # Wait until visit start. (The schedule enforces the real time rule.)
    from shared.core import dt
    time.sleep(max(0,(dt(sl['startAt'])-now()).total_seconds())+.2)
    assert request('PUT',f'/api/v1/appointments/{a["id"]}/completion',role='VET')['status']=='COMPLETED'
    c=request('POST','/api/v1/health/courses',dict(appointmentId=a['id'],medication='Препарат по назначению',dose='По назначению врача',firstDoseAt=iso(now()+timedelta(minutes=2)),intervalHours=24,doseCount=2),role='VET',expect=201)
    occ=request('GET',f'/api/v1/health/courses/{c["id"]}/occurrences')['items'];assert len(occ)==2
    start,end=iso(now()-timedelta(minutes=1)),iso(now()+timedelta(days=2))
    cal=lambda:request('GET','/api/v1/calendar',params={'petId':p,'from':start,'to':end})['items']
    until(cal,lambda items:len(items)==3)
    request('PUT',f'/api/v1/health/occurrences/{occ[0]["id"]}/completion',{'status':'TAKEN'})
    until(cal,lambda items:any(x['id']==occ[0]['id'] and x['status']=='TAKEN' for x in items))
    assert request('GET',f'/api/v1/health/visits/{a["id"]}/courses',role='VET')['total']==1
