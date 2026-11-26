"""Payment provider stand-in. Postman selects success/failure explicitly."""
import hmac
from shared.core import *
app=FastAPI(title='Mock PSP'); db=Store('mock-psp');lock=threading.RLock()
@app.get('/health')
def health():return {'status':'UP'}
@app.post('/payments',status_code=201)
def create(b:dict,request:Request):
    aid=b['appointmentId']
    with lock:
        x=db.get('payment',aid)
        if not x:
            if db.get('cancelled',aid):return JSONResponse({'detail':'payment cancelled'},409)
            x=dict(appointmentId=aid,providerPaymentId=stable('psp:'+aid),paymentUrl=os.getenv('PUBLIC_BASE_URL','http://localhost:8080')+'/mock/payments/'+aid,amountKopecks=b['amountKopecks'],currency=b['currency'],status='PENDING',correlationId=request.headers.get('X-Correlation-Id') or uid());db.put('payment',aid,x)
        elif x['amountKopecks']!=b['amountKopecks']:return JSONResponse({'detail':'amount mismatch'},409)
        return x
@app.get('/payments/{aid}')
def get(aid:str):
    x=db.get('payment',aid);return x if x else JSONResponse({'detail':'unknown payment'},404)
@app.post('/payments/{aid}/cancel')
def cancel(aid:str):
    with lock:
        db.put('cancelled',aid,{'done':True});x=db.get('payment',aid)
        if not x:return {'status':'CANCELLED'}
        if x['status']=='SUCCEEDED':x['status']='REFUNDED'
        elif x['status']!='REFUNDED' and x['status']!='FAILED':x['status']='CANCELLED'
        db.put('payment',aid,x);return {'status':x['status']}
@app.post('/payments/{aid}/result')
def result(aid:str,b:dict):
    status=b.get('status')
    if status not in ('SUCCEEDED','FAILED'):return JSONResponse({'detail':'expected SUCCEEDED or FAILED'},400)
    with lock:
        x=db.get('payment',aid)
        if not x:return JSONResponse({'detail':'unknown payment'},404)
        # Late SUCCEEDED after CANCELLED simulates a delayed settlement.
        if x['status'] in ('REFUNDED','SUCCEEDED','FAILED') and x['status']!=status:return JSONResponse({'detail':'terminal payment'},409)
        x['status']=status;db.put('payment',aid,x)
        e=dict(providerEventId=stable(f'psp-event:{aid}:{status}'),providerPaymentId=x['providerPaymentId'],appointmentId=aid,status=status,amountKopecks=x['amountKopecks'],currency=x['currency']);raw=json.dumps(e,separators=(',',':')).encode();sig=hmac.new(SECRET.encode(),raw,hashlib.sha256).hexdigest()
    try:
        resp=httpx.post(address('gateway')+'/webhooks/payments',content=raw,headers={'Content-Type':'application/json','X-Provider-Signature':sig,'X-Correlation-Id':x['correlationId']},timeout=8)
    except httpx.RequestError:return JSONResponse({'detail':'callback unavailable; repeat same result'},503)
    log('psp',f'WEBHOOK {status} appointment={aid} -> {resp.status_code}')
    return JSONResponse({'appointmentId':aid,'status':status,'callbackStatus':resp.status_code},200 if resp.status_code==200 else 503)
