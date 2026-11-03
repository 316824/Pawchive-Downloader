"""Durable download engine used by Worker when a checkpoint journal is supplied."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import random
import re
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from download_journal import fingerprint, inside, matches, original_url


def move_exclusive(source,target):
    """Move on the same filesystem without replacing an unrelated existing file."""
    source,target=Path(source),Path(target)
    if os.name=='nt': source.rename(target)
    else:
        os.link(source,target)
        source.unlink()


class DownloadEngine:
    def __init__(self,worker,helpers):
        self.w=worker; self.h=helpers; self.journal=worker.journal
        self.job=self.journal.load(worker.job_id)
        self.session=None; self.current=None

    def save(self,record=None,full=False):
        if full: self.job['saved_count']=sum(r.get('status')=='complete' for r in self.job['files'])
        self.journal.save(self.job,dirty_records=None if full else [record] if record is not None else [])

    def interrupted(self):
        if self.w.isInterruptionRequested(): raise RuntimeError(self.w.text['cancelled'])

    def get(self,url,**kwargs):
        # Redirects may select another original-file host, never another domain.
        for _ in range(6):
            self.interrupted()
            parsed=urlparse(url)
            metadata=(parsed.scheme=='https' and parsed.hostname=='pawchive.pw'
                      and not parsed.username and not parsed.password and parsed.port in (None,443)
                      and (parsed.path.rstrip('/')==urlparse(self.w.profile).path.rstrip('/') or url.startswith(self.w.profile+'/post/')))
            if not metadata and not original_url(url): raise ValueError('Unsupported download redirect.')
            response=self.session.get(url,allow_redirects=False,**kwargs)
            if response.status_code in (301,302,303,307,308):
                location=response.headers.get('Location','')
                response.close()
                if not location: raise ValueError('The response redirects without a location.')
                url=urljoin(url,location)
            else: return response
        raise ValueError('Too many download redirects.')

    def posts(self):
        if self.job.get('posts') is not None: return self.job['posts']
        posts=[]; seen=set(); offset=0
        while True:
            page=f'{self.w.profile}?o={offset}' if offset else self.w.profile
            with self.get(page,timeout=(15,20)) as response:
                response.raise_for_status(); soup=BeautifulSoup(response.text,'html.parser')
            cards=soup.select('article.post-card')
            if not cards: break
            before=len(posts)
            for card in cards:
                link=card.select_one('a[href*="/post/"]'); date=card.select_one('time.timestamp[datetime]')
                if not link or not date: continue
                url=self.h['absolute_post_url'](link.get('href'))
                if not url.startswith(self.w.profile+'/post/') or url in seen: continue
                seen.add(url)
                title=card.select_one('.post-card__header')
                posts.append([date.get('datetime',''),url,title.get_text(' ',strip=True) if title else 'post'])
            if len(cards)<self.h['PAGE_SIZE'] or len(posts)==before: break
            offset+=self.h['PAGE_SIZE']
        if not posts: raise RuntimeError('EMPTY')
        if self.w.selected_post_urls: posts=[post for post in posts if post[1] in self.w.selected_post_urls]
        if not posts: raise RuntimeError('NO_SELECTED_POSTS')
        posts.sort(key=lambda p:p[0],reverse=self.w.order=='newest')
        self.job['posts']=posts; self.job['done_posts']=[]; self.save()
        return posts

    def normalize(self):
        for record in self.job['files']:
            if record.get('move_to'):
                self.move_record(record,Path(record['move_to']))
            if record.get('status') in ('ready','complete'):
                if matches(record):
                    record['status']='complete'
                elif record.get('status')=='ready' and matches(record,record['part']):
                    self.finalize(record)
                else:
                    record['status']='pending'
                    done=self.job.setdefault('done_posts',[])
                    if record['post_url'] in done: done.remove(record['post_url'])
        self.save(full=True)

    def move_record(self,record,destination):
        source=inside(self.w.folder,record['target'])
        destination=inside(self.w.folder,destination)
        if source==destination:
            record.pop('move_to',None); return
        if not source.exists():
            if matches(record,destination):
                record['target']=str(destination); record.pop('move_to',None); self.save(record); return
            record['status']='pending'; record.pop('move_to',None); self.save(record); return
        if not matches(record,source):
            raise ValueError('A saved file changed; it will not be renamed or removed.')
        if destination.exists(): destination=self.h['unique_path'](destination.parent,destination.name)
        record['move_to']=str(destination); self.save(record)
        try: move_exclusive(source,destination)
        except FileExistsError:
            return self.move_record(record,self.h['unique_path'](destination.parent,destination.name))
        record['target']=str(destination); record.pop('move_to',None); self.save(record)

    def record(self,date,post_url,title,url,name,direct,dest):
        records=self.job['files']
        for record in records:
            if record['post_url']==post_url and record['url']==url: return record
        target=dest/name
        if not self.w.by_title:
            other=[r for r in records if r['name']==name and r['post_url']!=post_url and r.get('status')=='complete']
            collision=any(r['name']==name and r.get('prefixed') for r in records)
            if direct and other or collision:
                for old in other:
                    if not old.get('prefixed'):
                        previous=Path(old['target'])
                        prefixed=self.h['safe_path_component'](f'({old["title"]}) - {previous.name}',limit=240)
                        self.move_record(old,self.h['unique_path'](dest,prefixed)); old['prefixed']=True; self.save(old)
                name_on_disk=self.h['safe_path_component'](f'({title}) - {name}',limit=240)
                target=self.h['unique_path'](dest,name_on_disk)
            elif direct and target.exists() and not any(r['target']==str(target) for r in records):
                target=self.h['unique_path'](dest,self.h['safe_path_component'](f'({title}) - {name}',limit=240))
            elif target.exists(): target=self.h['unique_path'](dest,name)
        elif target.exists(): target=self.h['unique_path'](dest,name)
        key=hashlib.sha256((post_url+'\0'+url).encode()).hexdigest()[:20]
        part=dest/f'.pawchive-{self.job["id"]}-{key}.part'
        record=dict(url=url,name=name,target=str(target),part=str(part),post_url=post_url,
                    date=date,title=title,direct=bool(direct),status='pending',sequence=len(records),prefixed=target.name.startswith('('+title+') - '))
        records.append(record); self.save(record)
        return record

    def finalize(self,record):
        target=inside(self.w.folder,record['target']); part=inside(self.w.folder,record['part'])
        if target.exists():
            if matches(record,target):
                part.unlink(missing_ok=True); record['status']='complete'; self.job['saved_count']+=1; self.save(record); return
            target=self.h['unique_path'](target.parent,target.name)
            record['target']=str(target); self.save(record)
        try: move_exclusive(part,target)
        except FileExistsError: return self.finalize(record)
        record['status']='complete'; self.job['saved_count']+=1; self.save(record)

    def transfer(self,record,label):
        part=inside(self.w.folder,record['part']); target=inside(self.w.folder,record['target'])
        self.current=record
        error=None
        attachment=Path(urlparse(record['url']).path).suffix.lower() in {'.zip','.rar','.7z','.tar','.gz','.psd','.clip','.mp4','.mov','.webm','.mkv','.pdf'}
        for attempt in range(1,5):
            self.interrupted()
            try:
                if attempt>1:
                    cooldown=min(18,3*(2**(attempt-2))+random.uniform(.5,2.5)) if attachment else 0
                    self.w.wait_interruptibly(1.5*(attempt-1)+cooldown)
                offset=part.stat().st_size if part.exists() else 0
                headers={'Referer':record['post_url'],'Accept':'*/*','Accept-Encoding':'identity'}
                if offset:
                    headers['Range']=f'bytes={offset}-'
                    validator=record.get('etag') or record.get('last_modified')
                    if validator: headers['If-Range']=validator
                with self.get(record['url'],stream=True,timeout=(15,20),headers=headers) as response:
                    if response.status_code==416:
                        # An unknown/inconsistent length cannot prove that the partial is complete.
                        part.unlink(missing_ok=True); record.pop('etag',None); record.pop('last_modified',None)
                        self.save(record); raise ValueError('The saved range no longer matches; restarting safely.')
                    response.raise_for_status()
                    ctype=response.headers.get('content-type','')
                    if 'text/html' in ctype.lower() or 'application/json' in ctype.lower():
                        raise ValueError(self.w.text['bad_content'].format(content_type=ctype))
                    received=0; mode='wb'; segment_size=None; total=int(response.headers.get('content-length') or 0)
                    if response.status_code==206:
                        content_range=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('content-range',''))
                        if not content_range or int(content_range[1])!=offset or int(content_range[2])<offset:
                            raise ValueError('The server returned an inconsistent byte range.')
                        total=int(content_range[3])
                        if int(content_range[2])>=total or response.headers.get('content-encoding','identity').lower()!='identity':
                            raise ValueError('The byte range cannot be appended safely.')
                        if offset and (record.get('etag') and response.headers.get('etag')!=record['etag']
                            or not record.get('etag') and record.get('last_modified') and response.headers.get('last-modified')!=record['last_modified']):
                            raise ValueError('The remote file changed; restarting safely.')
                        segment_size=int(content_range[2])-offset+1
                        if response.headers.get('content-length') and int(response.headers['content-length'])!=segment_size:
                            raise ValueError('The response length does not match its byte range.')
                        received=offset; mode='ab'
                    elif response.status_code!=200: raise ValueError('Unexpected original-file response.')
                    elif response.headers.get('content-encoding','identity').lower()!='identity': total=0
                    etag=response.headers.get('etag','')
                    record['etag']=etag if etag and not etag.startswith('W/') else ''
                    record['last_modified']=response.headers.get('last-modified','')
                    record['expected_size']=total; record['content_type']=ctype; self.save(record)
                    self.w.progress.emit(label,received,total); last_progress=0.
                    with part.open(mode) as output:
                        for chunk in response.iter_content(131072):
                            self.interrupted()
                            if not chunk: continue
                            output.write(chunk); received+=len(chunk)
                            now=time.monotonic()
                            if now-last_progress>=.08:
                                self.w.progress.emit(label,received,total); last_progress=now
                    if total and received!=total: raise requests.ConnectionError('The original-file response was truncated.')
                    if segment_size is not None and received-offset!=segment_size:
                        raise ValueError('The server sent data outside the declared byte range.')
                    if not self.h['valid_download'](part,target.suffix,ctype):
                        raise ValueError(self.w.text['bad_content'].format(content_type=ctype or self.w.text['unknown']))
                    record.update(status='ready',size=received,sha256=fingerprint(part)); self.save(record)
                self.interrupted()
                self.finalize(record)
                self.w.progress.emit(label,received,total)
                return
            except (requests.RequestException,OSError,ValueError) as exc:
                self.interrupted()
                error=exc
                if isinstance(exc,ValueError):
                    part.unlink(missing_ok=True); record.pop('etag',None); record.pop('last_modified',None); self.save(record)
        self.w.progress.emit(self.w.text['download_failed'].format(title=record['title'],filename=target.name,error=error),0,0)
        raise RuntimeError(self.w.text['album_failed'].format(title=record['title'])) from error

    def cleanup_album(self):
        album=self.job.get('album')
        if not album or not album.get('created'): return
        folder=inside(self.w.folder,album['path'])
        kept=[]
        for record in self.job['files']:
            if record['post_url']!=album['post_url']:
                kept.append(record); continue
            part=inside(self.w.folder,record['part']); part.unlink(missing_ok=True)
            target=inside(self.w.folder,record['target'])
            if record.get('status') in ('ready','complete') and matches(record,target): target.unlink()
        self.job['files']=kept
        try: folder.rmdir()
        except OSError: pass  # A user-added file or an existing directory is never recursively removed.

    def run(self):
        try:
            self.w.folder.mkdir(parents=True,exist_ok=True)
            self.session=requests.Session(); self.session.headers['User-Agent']=self.h['USER_AGENT']
            self.job['status']='running'; self.normalize()
            self.w.completed_records=[dict(record) for record in self.job.get('files',[])
                                      if record.get('status') in ('complete','ready')]
            posts=self.posts()
            done=self.job.setdefault('done_posts',[])
            for index,(date,post_url,title) in enumerate(posts,1):
                self.interrupted()
                if post_url in done: continue
                with self.get(post_url,timeout=(15,20)) as response:
                    response.raise_for_status(); soup=BeautifulSoup(response.text,'html.parser')
                heading=soup.select_one('h1.post__title span')
                if heading: title=heading.get_text(' ',strip=True)
                title=self.h['safe_path_component'](title,limit=100)
                dest=self.w.folder/title if self.w.by_title else self.w.folder
                previous=self.job.get('album')
                created=(previous['created'] if previous and previous['post_url']==post_url else self.w.by_title and not dest.exists())
                dest=inside(self.w.folder,dest,child=self.w.by_title); dest.mkdir(parents=True,exist_ok=True)
                self.w.current_album=dest if self.w.by_title else None; self.w.created_album=created
                self.job['album']=dict(path=str(dest),created=created,post_url=post_url) if self.w.by_title else None
                self.save(); seen=set()
                for number,anchor in enumerate(soup.select('.post__files a.fileThumb[href],a.post__attachment-link[href]'),1):
                    url=anchor.get('href','').strip()
                    if not original_url(url) or url in seen: continue
                    seen.add(url); direct='post__attachment-link' in (anchor.get('class') or [])
                    name=anchor.get('download','').strip() or (parse_qs(urlparse(url).query).get('f') or [''])[0]
                    name=name or re.sub(r'^Download\s+','',anchor.get_text(' ',strip=True),flags=re.I)
                    name=self.h['safe_path_component'](unquote(name or Path(urlparse(url).path).name),fallback=f'image_{number}.bin')
                    record=self.record(date,post_url,title,url,name,direct,dest)
                    if record['status']=='complete': continue
                    if Path(urlparse(url).path).suffix.lower() in {'.zip','.rar','.7z','.tar','.gz','.psd','.clip','.mp4','.mov','.webm','.mkv','.pdf'} and self.job['saved_count']:
                        self.w.wait_interruptibly(random.uniform(1.8,3.8))
                    label=f'({index}/{len(posts)}) {title} / {Path(record["target"]).name}'
                    self.transfer(record,label); self.current=None
                    if record.get('status') in ('complete','ready'):
                        self.w.completed_records=[dict(item) for item in self.job.get('files',[])
                                                  if item.get('status') in ('complete','ready')]
                    self.w.progress.emit('✓  '+label,1,1)
                done.append(post_url); self.journal.mark_post_done(self.job['id'],post_url)
                self.job['album']=None; self.w.current_album=None; self.w.created_album=False; self.save()
            if self.w.rename_order:
                for number,record in enumerate(sorted(self.job['files'],key=lambda r:r['date']),self.w.start_number):
                    self.interrupted()
                    if record.get('number')==number: continue
                    filename=Path(record['target']).name
                    extension=next((filename[-len(suffix):] for suffix in ('.tar.gz','.tar.xz','.tar.bz2')
                                    if filename.lower().endswith(suffix)),Path(filename).suffix)
                    target=Path(record['target']).with_name(f'{number}{extension}')
                    self.move_record(record,target); record['number']=number; self.save(record)
            self.w.completed_records=[dict(record) for record in self.job.get('files',[])
                                      if record.get('status') in ('complete','ready')]
            count=sum(r['status']=='complete' for r in self.job['files'])
            self.job.update(status='completed',album=None,saved_count=count,error='',posts=None,files=[],done_posts=[])
            self.journal.save(self.job); self.w.finished_ok.emit(count)
        except Exception as exc:
            self.w.completed_records=[dict(record) for record in self.job.get('files',[])
                                      if record.get('status') in ('complete','ready')]
            paused=self.w.isInterruptionRequested() and self.w.preserve_on_interrupt
            try:
                if not paused:
                    if self.current: inside(self.w.folder,self.current['part']).unlink(missing_ok=True)
                    self.cleanup_album(); self.job['album']=None
                self.job['status']='paused' if paused else 'cancelled' if self.w.isInterruptionRequested() else 'failed'
                self.job['error']=str(exc)[:1000]; self.save(full=True)
            except (OSError,ValueError): pass
            self.w.failed.emit(str(exc))
        finally:
            if self.session is not None: self.session.close()
