"""Common HTTP, SQLite, validation and messaging primitives."""
import contextvars, hashlib, json, logging, os, sqlite3, threading, time
from contextlib import contextmanager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
import httpx, yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.concurrency import run_in_threadpool
from jsonschema import Draft7Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
PORTS = dict(pet=8001, schedule=8002, appointment=8003, payment=8004, health=8005, notification=8006, gateway=8080, broker=8010, psp=8011, fcm=8012)
OWNER='11111111-1111-4111-8111-111111111111'; VET='44444444-4444-4444-8444-444444444444'; ADMIN='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
SECRET = os.getenv('MOCK_PSP_SECRET','local-psp-key')
CORR = contextvars.ContextVar('correlation', default=None)
ACTOR = contextvars.ContextVar('actor', default='')
KEY = contextvars.ContextVar('idempotency', default='')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)

def uid(): return str(uuid4())
def stable(label): return str(uuid5(NAMESPACE_URL, label))
def now(): return datetime.now(timezone.utc)
def dt(s):
    try:
        d=datetime.fromisoformat(s.replace('Z','+00:00'))
        if d.tzinfo is None: raise ValueError()
        return d.astimezone(timezone.utc)
    except (ValueError,TypeError,AttributeError): raise Fault(400,'INVALID_DATE','Нужна дата с часовым поясом')
def iso(d=None): return (d or now()).isoformat(timespec='milliseconds').replace('+00:00','Z')
def log(service,msg): logging.info('[%s] correlationId=%s %s',service,CORR.get() or '-',msg)
def address(name): return os.getenv(name.upper()+'_URL',f'http://127.0.0.1:{PORTS[name]}')
class Fault(Exception):
    def __init__(self,status,code,detail): self.status=status;self.code=code;self.detail=detail
    def body(self): return dict(type='https://pet-care.example/problems/'+self.code.lower(),title=self.code,status=self.status,detail=self.detail,code=self.code,correlationId=CORR.get() or uid())
def fail(status,code,detail): raise Fault(status,code,detail)
def page(items,p=1,s=20): return dict(items=items[(p-1)*s:p*s],page=p,size=s,total=len(items))
def public(obj,*hidden): return {k:v for k,v in obj.items() if k not in hidden}

def call(target,method,path,body=None,key=None,caller='internal',params=None):
    headers={'X-Correlation-Id':CORR.get() or uid(),'X-Service':caller}
    if key: headers['Idempotency-Key']=key
    quiet=target=='broker' and path.startswith('/queues/')
    if not quiet:log(caller,f'REST {method} {target}{path}')
    try:
        r=httpx.request(method,address(target)+path,json=body,params=params,headers=headers,timeout=4.0)
    except httpx.RequestError as e: raise Fault(503,'DEPENDENCY_UNAVAILABLE',f'{target}: {type(e).__name__}') from e
    if not quiet:log(caller,f'REST <- {target} {r.status_code}')
    if r.is_error:
        try: d=r.json()
        except ValueError: d={}
        raise Fault(r.status_code if r.status_code<500 else 503,d.get('code','DEPENDENCY_ERROR'),d.get('detail',f'{target} {r.status_code}'))
    return r.json()

class Store:
    def __init__(self,name,root=None):
        base=Path(root or os.getenv('DATA_DIR',ROOT/'.data'));base.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(base/(name+'.sqlite'),check_same_thread=False,isolation_level=None)
        self.conn.execute('PRAGMA journal_mode=WAL');self.lock=threading.RLock();self.depth=0
        self.conn.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))')
    @contextmanager
    def atomic(self):
        with self.lock:
            outer=self.depth==0
            if outer:self.conn.execute('BEGIN IMMEDIATE')
            self.depth+=1
            try:
                yield
                if outer:self.conn.execute('COMMIT')
            except BaseException:
                if outer:self.conn.execute('ROLLBACK')
                raise
            finally:self.depth-=1
    def get(self,kind,id):
        with self.lock:
            row=self.conn.execute('SELECT body FROM records WHERE kind=? AND id=?',(kind,str(id))).fetchone()
            return json.loads(row[0]) if row else None
    def require(self,kind,id):
        x=self.get(kind,id)
        if x is None:fail(404,'NOT_FOUND',f'{kind} не найден')
        return x
    def put(self,kind,id,data):
        with self.lock:self.conn.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body',(kind,str(id),json.dumps(data,ensure_ascii=False)))
        return data
    def all(self,kind):
        with self.lock:return [json.loads(x[0]) for x in self.conn.execute('SELECT body FROM records WHERE kind=? ORDER BY id',(kind,)).fetchall()]
    def remove(self,kind,id):
        with self.lock:self.conn.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,str(id)))
    def emit(self,event,payload):
        e=dict(eventId=uid(),eventType=event,occurredAt=iso(),schemaVersion=1,correlationId=CORR.get() or uid(),payload=payload)
        self.put('outbox',e['eventId'],e);return e

def owner_check(pet,owner,caller):
    if not call('pet','POST','/internal/v1/access/check',{'petId':pet,'ownerId':owner},caller=caller)['allowed']:fail(403,'FORBIDDEN','Питомец не принадлежит пользователю')

# Runtime validation uses the same OpenAPI schema supplied for LR3.
def resolve(spec,node):
    if isinstance(node,list):return [resolve(spec,x) for x in node]
    if not isinstance(node,dict):return node
    if '$ref' in node:
        cur=spec
        for k in node['$ref'].split('/')[1:]:cur=cur[k]
        return resolve(spec,cur)
    res={k:resolve(spec,v) for k,v in node.items() if k!='nullable'}
    if node.get('nullable') and isinstance(res.get('type'),str):res['type']=[res['type'],'null']
    return res

def validate(spec,schema,data):
    err=next(Draft7Validator(resolve(spec,schema),format_checker=FormatChecker()).iter_errors(data),None)
    if err:fail(400,'INVALID_INPUT',f'{".".join(map(str,err.path)) or "request"}: {err.message}')

class Service:
    def __init__(self,name,subscriptions=()):
        self.name=name;self.db=Store(name);self.lock=threading.RLock();self.subscriptions=list(subscriptions);self.on_event=lambda e:None;self.tick=lambda:None
        self.spec=yaml.safe_load((ROOT/'api'/f'{name}-service.yaml').read_text())
        @asynccontextmanager
        async def life(app):
            stop=threading.Event(); t=threading.Thread(target=self.worker,args=(stop,),daemon=True);t.start()
            yield
            stop.set();t.join(timeout=5)
        self.app=FastAPI(title=name,lifespan=life);self.app.openapi=lambda:self.spec
        @self.app.exception_handler(StarletteHTTPException)
        async def unknown(request,exc):
            e=Fault(exc.status_code,'HTTP_ERROR',str(exc.detail));return JSONResponse(e.body(),exc.status_code,media_type='application/problem+json')
        @self.app.get('/health',include_in_schema=False)
        def health():return {'service':name,'status':'UP'}
    def route(self,method,path,roles=(),callers=(),replay_current=False):
        spec=self.spec;op=spec['paths'][path][method.lower()];success=int(next(k for k in op['responses'] if str(k).startswith('2')))
        def decorate(fn):
            async def endpoint(request:Request):
                raw=await request.body()
                return await run_in_threadpool(self.handle,fn,request,raw,op,success,roles,callers,replay_current)
            self.app.add_api_route(path,endpoint,methods=[method],include_in_schema=False);return fn
        return decorate
    def handle(self,fn,req,raw,op,success,roles,callers,replay_current):
        try:cid=str(UUID(req.headers.get('X-Correlation-Id','')))
        except ValueError:cid=uid()
        ct=CORR.set(cid);at=ACTOR.set('');kt=KEY.set('')
        try:
            log(self.name,f'RECEIVED {req.method} {req.url.path}')
            role=req.headers.get('X-Role','');actor=req.headers.get('X-User-Id','');caller=req.headers.get('X-Service','')
            if roles:
                try:actor=str(UUID(actor))
                except ValueError:fail(401,'UNAUTHENTICATED','Нужен X-User-Id')
                if role not in roles:fail(403,'FORBIDDEN','Недостаточно прав')
                # Mock identity registry: known staff roles; all other UUIDs are owners.
                expected={VET:'VET',ADMIN:'CLINIC_ADMIN'}.get(actor,'OWNER')
                if role!=expected:fail(403,'FORBIDDEN','Роль не соответствует учётной записи')
            if callers and caller not in callers:fail(403,'FORBIDDEN','Внутренний API')
            ACTOR.set(actor or caller or 'webhook')
            try:body=json.loads(raw) if raw else {}
            except ValueError:fail(400,'INVALID_INPUT','Некорректный JSON')
            params={};idem=None
            for par in op.get('parameters',[]):
                p=resolve(self.spec,par);n=p['name'];where=p['in'];schema=p['schema']
                val=(req.path_params.get(n) if where=='path' else req.query_params.get(n) if where=='query' else req.headers.get(n))
                if val is None:val=schema.get('default')
                if val is None:
                    if p.get('required'):fail(400,'INVALID_INPUT',f'Отсутствует {n}')
                    continue
                if schema.get('type')=='integer':
                    try:val=int(val)
                    except ValueError:fail(400,'INVALID_INPUT',f'{n}: требуется целое число')
                validate(self.spec,schema,val);params[n]=val
                if n=='Idempotency-Key':idem=val
            if 'requestBody' in op:validate(self.spec,op['requestBody']['content']['application/json']['schema'],body)
            KEY.set(idem or '')
            with self.lock:
                cache_id=stable(f'{ACTOR.get()}:{req.method}:{req.url.path}:{idem}') if idem else None
                digest=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                if cache_id:
                    old=self.db.get('idempotency',cache_id)
                    if old:
                        if old['hash']!=digest:fail(409,'IDEMPOTENCY_CONFLICT','Ключ уже использован для другого запроса')
                        if not replay_current:
                            log(self.name,'IDEMPOTENT REPLAY');return JSONResponse(old['body'],old['status'],headers={'X-Correlation-Id':cid})
                    # Bind key before business operation, including failed attempts.
                    binding=self.db.get('idem-binding',cache_id)
                    if binding and binding['hash']!=digest:fail(409,'IDEMPOTENCY_CONFLICT','Ключ уже использован для другого запроса')
                    self.db.put('idem-binding',cache_id,{'hash':digest})
                result=fn(body,params,actor,role,req,raw)
                if cache_id:self.db.put('idempotency',cache_id,{'hash':digest,'body':result,'status':success})
            return JSONResponse(result,success,headers={'X-Correlation-Id':cid})
        except Fault as e:
            log(self.name,f'ERROR {e.status} {e.code} {e.detail}');return JSONResponse(e.body(),e.status,media_type='application/problem+json',headers={'X-Correlation-Id':cid})
        except Exception:
            logging.exception('[%s] unexpected error',self.name);e=Fault(503,'INTERNAL_ERROR','Временная ошибка обработки');return JSONResponse(e.body(),503,media_type='application/problem+json',headers={'X-Correlation-Id':cid})
        finally:CORR.reset(ct);ACTOR.reset(at);KEY.reset(kt)
    def worker(self,stop):
        while not stop.is_set():
            try:
                with self.lock:
                    self.tick()
                    for e in self.db.all('outbox'):
                        token=CORR.set(e['correlationId'])
                        try:
                            call('broker','POST','/publish',e,caller=self.name);self.db.remove('outbox',e['eventId']);log(self.name,'PUBLISH '+e['eventType'])
                        finally:CORR.reset(token)
                if self.subscriptions:
                    data=call('broker','GET','/queues/'+self.name,caller=self.name)
                    e=data.get('event')
                    if e:
                        token=CORR.set(e['correlationId'])
                        try:
                            with self.lock:
                                if not self.db.get('inbox',e['eventId']):
                                    if e['schemaVersion']!=1:raise ValueError('unsupported event version')
                                    log(self.name,'CONSUME '+e['eventType'])
                                    if self.name in ('pet','notification'):
                                        with self.db.atomic():self.on_event(e);self.db.put('inbox',e['eventId'],{'done':True})
                                    else:
                                        self.on_event(e);self.db.put('inbox',e['eventId'],{'done':True})
                            call('broker','POST','/queues/'+self.name+'/ack',{'eventId':e['eventId']},caller=self.name)
                        except Exception as err:
                            log(self.name,f'CONSUMER ERROR {err}');call('broker','POST','/queues/'+self.name+'/nack',{'eventId':e['eventId']},caller=self.name)
                        finally:CORR.reset(token)
            except Exception as e:log(self.name,f'WORKER RETRY {e}')
            stop.wait(0.45)
