from shared.core import *
from fastapi.responses import Response
app=FastAPI(title='Pet Care Gateway')
ROUTES={'pets':'pet','care':'pet','calendar':'pet','slots':'schedule','admin':'schedule','appointments':'appointment','health':'health','device':'notification','notifications':'notification'}
@app.get('/health')
def health():return {'status':'UP'}
@app.api_route('/{path:path}',methods=['GET','POST','PUT','DELETE','PATCH'])
async def proxy(path:str,request:Request):
    # Only public routes and explicitly allowed mock payment page/callback.
    parts=path.split('/');target=None;upstream='/'+path
    if len(parts)>=3 and parts[:2]==['api','v1']:target=ROUTES.get(parts[2])
    elif path=='webhooks/payments' and request.method=='POST':target='payment'
    elif len(parts) in (3,4) and parts[:2]==['mock','payments'] and (len(parts)==3 and request.method=='GET' or len(parts)==4 and parts[3]=='result' and request.method=='POST'):
        target='psp';upstream='/'+('/'.join(parts[1:]))
    try:cid=str(UUID(request.headers.get('X-Correlation-Id','')))
    except ValueError:cid=uid()
    tok=CORR.set(cid)
    try:
        if not target:raise Fault(404,'NOT_FOUND','Маршрут недоступен через Gateway')
        headers={k:v for k,v in request.headers.items() if k.lower() not in ('host','content-length','connection','x-service','x-correlation-id')};headers['X-Correlation-Id']=cid
        log('gateway',f'PROXY {request.method} /{path} -> {target}')
        async with httpx.AsyncClient(timeout=20) as client:
            r=await client.request(request.method,address(target)+upstream,params=list(request.query_params.multi_items()),content=await request.body(),headers=headers)
        if r.status_code>=400 and 'application/problem+json' not in r.headers.get('content-type',''):
            raise Fault(r.status_code,'UPSTREAM_ERROR',f'{target}: {r.status_code}')
        return Response(r.content,status_code=r.status_code,media_type=r.headers.get('content-type','application/json'),headers={'X-Correlation-Id':cid})
    except httpx.RequestError:
        e=Fault(503,'DEPENDENCY_UNAVAILABLE',f'{target} недоступен');return JSONResponse(e.body(),503,media_type='application/problem+json',headers={'X-Correlation-Id':cid})
    except Fault as e:return JSONResponse(e.body(),e.status,media_type='application/problem+json',headers={'X-Correlation-Id':cid})
    finally:CORR.reset(tok)
