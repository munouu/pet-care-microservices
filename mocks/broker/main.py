"""Separate queue broker with publish, consume, ack/nack and DLQ semantics."""
from collections import defaultdict, deque
from shared.core import *
app=FastAPI(title='Mock message broker')
SUBS={'appointment':{'payment.completed','payment.failed','payment.refunded','slot.expired'},'pet':{'health.course.created','health.occurrence.completed'},'notification':{'appointment.confirmed','appointment.cancelled','health.course.created','health.occurrence.completed'}}
QUEUES={k:deque() for k in SUBS};DLQ=defaultdict(list);IN_FLIGHT={};LOCK=threading.RLock()
EVENT_SPEC=yaml.safe_load((ROOT/'api/events.yaml').read_text())
@app.get('/health')
def health():return {'status':'UP'}
@app.post('/publish')
def publish(e:dict):
    try:
        channel=EVENT_SPEC['channels'][e['eventType']];schema=resolve(EVENT_SPEC,channel['publish']['message'])['payload'];validate(EVENT_SPEC,schema,e)
    except (Fault,KeyError) as err:return JSONResponse({'detail':str(err)},400)
    with LOCK:
        for q,subs in SUBS.items():
            if e['eventType'] in subs:QUEUES[q].append({'event':e,'attempts':0})
    log('broker',f'PUBLISH {e["eventType"]} eventId={e["eventId"]} correlationId={e["correlationId"]}');return {'ok':True}
@app.get('/queues/{name}')
def consume(name:str):
    with LOCK:
        if name not in QUEUES:return JSONResponse({'detail':'unknown queue'},404)
        old=IN_FLIGHT.get(name)
        if old:
            if old['until']>time.monotonic():return {'event':None}
            IN_FLIGHT.pop(name);retry(name,old)
        if not QUEUES[name]:return {'event':None}
        x=QUEUES[name].popleft();x['until']=time.monotonic()+30;IN_FLIGHT[name]=x;return {'event':x['event']}
def retry(name,x):
    x['attempts']+=1
    if x['attempts']>=3:DLQ[name].append(x);log('broker',f'DLQ {name} {x["event"]["eventType"]}')
    else:QUEUES[name].append(x)
def finish(name,eid,nack=False):
    with LOCK:
        x=IN_FLIGHT.get(name)
        if x and x['event']['eventId']==eid:
            IN_FLIGHT.pop(name)
            if nack:retry(name,x)
    return {'ok':True}
@app.post('/queues/{name}/ack')
def ack(name:str,b:dict):return finish(name,b['eventId'])
@app.post('/queues/{name}/nack')
def nack(name:str,b:dict):return finish(name,b['eventId'],True)
@app.get('/stats')
def stats():
    with LOCK:return {k:{'queued':len(v),'unacked':int(k in IN_FLIGHT),'deadLetters':len(DLQ[k])} for k,v in QUEUES.items()}
