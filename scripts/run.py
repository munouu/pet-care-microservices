"""Run independent service processes and stream their logs; Ctrl-C stops them."""
import argparse, os, signal, subprocess, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SERVICES={'broker':('mocks.broker.main',8010),'psp':('mocks.psp.main',8011),'fcm':('mocks.fcm.main',8012),'pet':('services.pet.main',8001),'schedule':('services.schedule.main',8002),'appointment':('services.appointment.main',8003),'payment':('services.payment.main',8004),'health':('services.health.main',8005),'notification':('services.notification.main',8006),'gateway':('gateway.main',8080)}

def main():
    def stop_signal(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGINT,stop_signal)
    signal.signal(signal.SIGTERM,stop_signal)
    p=argparse.ArgumentParser();p.add_argument('--data-dir',default=str(ROOT/'.data'));p.add_argument('--log-dir',default=str(ROOT/'.logs'));p.add_argument('--quiet',action='store_true');p.add_argument('--port-offset',type=int,default=0);args=p.parse_args()
    logs=Path(args.log_dir);logs.mkdir(parents=True,exist_ok=True);procs=[];handles=[]
    env={**os.environ,'DATA_DIR':args.data_dir,'PYTHONUNBUFFERED':'1',**{name.upper()+'_URL':f'http://127.0.0.1:{port+args.port_offset}' for name,(_,port) in SERVICES.items()},'PUBLIC_BASE_URL':f'http://localhost:{8080+args.port_offset}'}
    try:
        for name,(module,port) in SERVICES.items():
            h=open(logs/(name+'.log'),'w',encoding='utf-8');handles.append(h)
            proc=subprocess.Popen([sys.executable,'-m','uvicorn',module+':app','--host','127.0.0.1','--port',str(port+args.port_offset),'--no-access-log'],cwd=ROOT,env=env,stdout=h,stderr=subprocess.STDOUT);procs.append(proc)
        print(f'Gateway: http://localhost:{8080+args.port_offset} | logs:',logs,flush=True)
        positions={n:0 for n in SERVICES}
        while all(p.poll() is None for p in procs):
            if not args.quiet:
                for name in SERVICES:
                    with open(logs/(name+'.log'),encoding='utf-8',errors='replace') as f:
                        f.seek(positions[name]);txt=f.read();positions[name]=f.tell()
                        if txt:print(txt,end='',flush=True)
            time.sleep(.25)
        raise SystemExit('A service stopped. See '+str(logs))
    except KeyboardInterrupt:pass
    finally:
        for p in procs:p.terminate()
        for p in procs:
            try:p.wait(timeout=8)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        for h in handles:h.close()
if __name__=='__main__':main()
