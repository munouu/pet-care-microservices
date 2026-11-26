from shared.core import *
app=FastAPI(title='Mock FCM');db=Store('mock-fcm')
@app.get('/health')
def health():return {'status':'UP'}
@app.post('/messages:send')
def send(b:dict,request:Request):
    if not b.get('token'):return JSONResponse({'detail':'token missing'},400)
    db.put('message',b['notificationId'],dict(**b,acceptedAt=iso()));log('fcm',f'ACCEPTED {b["kind"]} reference={b["reference"]} correlationId={request.headers.get("X-Correlation-Id")}');return {'name':'messages/'+b['notificationId']}
