"""Public-source request audit and an explicit ledger of evidence actually supplied."""
from contextlib import contextmanager
from datetime import datetime,timezone
import threading
from urllib.parse import urlparse,parse_qs

TRACE=threading.local()


@contextmanager
def capture():
    logs=[]
    stack=getattr(TRACE,'stack',[])
    TRACE.stack=stack
    stack.append(logs)
    try:yield logs
    finally:stack.pop()


def record(url,success,final_url=None):
    stamp=datetime.now(timezone.utc).isoformat()
    for log in getattr(TRACE,'stack',[]):
        log.append({'url':url,'retrieved_at':stamp,'request_succeeded':success})
        if final_url and final_url!=url:
            log.append({'url':final_url,'retrieved_at':stamp,'request_succeeded':success,'redirect_from':url})


class Ledger:
    def __init__(self,existing=()):
        self.rows=[dict(row) for row in existing]

    def add(self,url,title,kind,access,available=True,**extra):
        if not isinstance(url,str) or urlparse(url).scheme!='https':return None
        old=next((row for row in self.rows if row['url']==url and row['kind']==kind),None)
        if old:return old['id']
        identifier='S'+str(max([int(row['id'][1:]) for row in self.rows if row['id'][1:].isdigit()]+[0])+1)
        self.rows.append({'id':identifier,'url':url,'title':title,'kind':kind,'access':access,'available':bool(available),**extra})
        return identifier

    def fetch(self,result,title,kind='market_data',available=None):
        ids=[]
        if available is None:available=not result.get('error') and bool(result.get('points'))
        for request in result.get('request_log',[]):
            endpoint=urlparse(request['url']).path.rsplit('/',1)[-1]
            identifier=self.add(request['url'],title+' · '+endpoint,kind,
                                'cached_snapshot' if result.get('cached') else 'fetched',
                                bool(available and request['request_succeeded']),
                                retrieved_at=request['retrieved_at'],request_succeeded=request['request_succeeded'])
            if identifier and self.rows[next(i for i,r in enumerate(self.rows) if r['id']==identifier)]['available']:ids.append(identifier)
        return ids

    def headline(self,item):
        return self.add(item['url'],item['title'],'headline_metadata','article_not_fetched',True,
                        publisher=item['publisher'],published_at=item['published_at'])
