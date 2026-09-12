"""Repair explicit existing records' body/media only; never rescreen or redeliver."""
import argparse, hashlib, json, os, pathlib, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from article_extractor import extract_article_text
from rss_ingest import download_image_for_attachment
import config

def main():
    p=argparse.ArgumentParser()
    for name in ['base-token','table-id','lark-config-dir','output']:p.add_argument('--'+name,required=True)
    p.add_argument('--record-id',action='append',required=True);p.add_argument('--apply',action='store_true')
    a=p.parse_args();out=pathlib.Path(a.output).resolve();out.mkdir(parents=True,exist_ok=True)
    env={k:v for k,v in os.environ.items() if not k.startswith(('OPENCLAW_','HERMES_'))}
    env['LARKSUITE_CLI_CONFIG_DIR']=a.lark_config_dir
    def lark(args):
        r=subprocess.run(['lark-cli',*args],capture_output=True,text=True,env=env,cwd=out,timeout=120)
        if r.returncode:raise RuntimeError((r.stderr or r.stdout)[-800:])
        d=json.loads(r.stdout)
        if d.get('ok') is False or d.get('code',0)!=0:raise RuntimeError(str(d)[:500])
        return d.get('data',d)
    def get(rid):return lark(['api','GET',f'/open-apis/bitable/v1/apps/{a.base_token}/tables/{a.table_id}/records/{rid}','--as','bot'])['record']
    def fetch(rid):
        record=get(rid);f=record['fields'];url=f['标题']['link']
        if not url.startswith('https://linux.do/t/'):raise RuntimeError('Not a Linux DO record: '+rid)
        body=extract_article_text(url,'LINUX DO','',{'summary':f['标题']['text'],'_content_incomplete':True},timeout=40)
        (out/(rid+'.before.json')).write_text(json.dumps(record,ensure_ascii=False,indent=2))
        (out/(rid+'.extraction.json')).write_text(json.dumps(body,ensure_ascii=False,indent=2))
        print(json.dumps({'record_id':rid,'status':body['status'],'chars':len(body['text']),'images':len(body.get('image_urls',[]))}),flush=True)
        return rid,record,body
    config.IMAGE_ATTACHMENT_PROXY_FAKE_IP_HOSTS=config.IMAGE_ATTACHMENT_PROXY_FAKE_IP_HOSTS|{'cdn3.ldstatic.com','cdn.ldstatic.com'}
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(fetch,a.record_id))
    report=[]
    for rid,record,body in results:
        item={'record_id':rid,'status':body['status'],'chars':len(body['text']),'images':len(body.get('image_urls',[])),'applied':False}
        if body['status']!='ok':item['error']=body['error'];report.append(item);continue
        assets=[]
        for url in body['image_urls'][:config.IMAGE_ATTACHMENT_MAX_PER_RECORD]:
            name,data,mime=download_image_for_attachment(url)
            filename=hashlib.sha256(url.encode()).hexdigest()[:20]+pathlib.Path(name).suffix
            (out/filename).write_bytes(data);assets.append(filename)
        if a.apply:
            current=get(rid)
            if current['fields'].get('全文')!=record['fields'].get('全文') or current['fields'].get('图片')!=record['fields'].get('图片'):
                raise RuntimeError('Record changed since read; do not overwrite: '+rid)
            lark(['base','+record-upsert','--as','bot','--base-token',a.base_token,'--table-id',a.table_id,'--record-id',rid,'--json',json.dumps({'全文':body['text']},ensure_ascii=False)])
            # Preserve any preexisting attachments. This repair sample originally had none.
            if assets and not current['fields'].get('图片'):
                args=['base','+record-upload-attachment','--as','bot','--base-token',a.base_token,'--table-id',a.table_id,'--record-id',rid,'--field-id','图片']
                for filename in assets:args+=['--file',filename]
                lark(args)
            after=get(rid)['fields'];full=after.get('全文') or ''
            if isinstance(full,list):full='\n'.join(x.get('text','') for x in full)
            assert full==body['text'],rid
            assert len(after.get('图片') or [])>=len(assets),rid
            for key in ['QA总结','已巡检','skip_reason','delivered_reply','obsidian_action']:
                assert after.get(key)==record['fields'].get(key),(rid,key)
            item.update(applied=True,readback_verified=True)
        report.append(item)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(item),flush=True)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if all(x['status']=='ok' for x in report) else 1

if __name__=='__main__':sys.exit(main())
