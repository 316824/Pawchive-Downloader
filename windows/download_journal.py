"""Small local download checkpoints; user files are never recursively discarded."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlparse
import uuid


PROFILE = re.compile(r'https://pawchive\.pw/(?:fanbox|patreon|subscribestar|discord|boosty)/user/[^/?#\s]+/?')
JOB_ID = re.compile(r'[a-f0-9]{32}')


def original_url(value):
    parsed=urlparse(str(value))
    host=parsed.hostname or ''
    return (parsed.scheme=='https' and (host=='file.pawchive.pw' or re.fullmatch(r'n\d+\.pawchive\.pw',host))
            and not parsed.username and not parsed.password and parsed.port in (None,443)
            and parsed.path.startswith('/data/'))


def inside(folder, path, *, child=True):
    root=Path(folder).resolve()
    candidate=Path(path)
    if not candidate.is_absolute(): raise ValueError('Checkpoint path is not absolute.')
    current=candidate
    while current!=root and current.parent!=current:
        if current.is_symlink() or getattr(current,'is_junction',lambda:False)():
            raise ValueError('Checkpoint paths cannot traverse symbolic links or junctions.')
        current=current.parent
    resolved=candidate.resolve()
    if not resolved.is_relative_to(root) or (child and resolved==root):
        raise ValueError('Checkpoint path is outside the download folder.')
    return resolved


def fingerprint(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def matches(record, path=None):
    path=Path(path or record['target'])
    try:
        return (path.is_file() and not path.is_symlink() and path.stat().st_size==record.get('size')
                and fingerprint(path)==record.get('sha256'))
    except OSError:
        return False


class DownloadJournal:
    def __init__(self,data_dir=None):
        if data_dir is None:
            base=Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local'/'share'))
            data_dir=base/'PawchiveDownloader'
        self.data_dir=Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True,exist_ok=True)
        self.db_path=self.data_dir/'download-journal.sqlite3'
        self._lock=threading.RLock()
        self._db=sqlite3.connect(self.db_path,timeout=15,check_same_thread=False)
        self._db.execute('PRAGMA auto_vacuum=FULL')
        self._db.execute('PRAGMA journal_mode=DELETE')
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, updated REAL NOT NULL, document TEXT NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS file_records (job_id TEXT NOT NULL, key TEXT NOT NULL, ordinal INTEGER NOT NULL, document TEXT NOT NULL, PRIMARY KEY(job_id,key)) WITHOUT ROWID')
        self._db.execute('CREATE TABLE IF NOT EXISTS post_lists (job_id TEXT PRIMARY KEY, document TEXT NOT NULL)')
        self._db.execute('CREATE TABLE IF NOT EXISTS done_posts (job_id TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY(job_id,url)) WITHOUT ROWID')
        self._db.commit()
        self._post_objects={}

    def create(self,config):
        config=dict(config)
        config['folder']=str(Path(config['folder']).resolve())
        if not PROFILE.fullmatch(str(config.get('profile',''))): raise ValueError('Invalid checkpoint profile.')
        job={'id':uuid.uuid4().hex,'status':'running','config':config,'posts':None,'files':[],
             'album':None,'updated':time.time(),'created':time.time(),'saved_count':0}
        self.save(job)
        return job['id']

    def _validate(self,job,records=None,check_posts=True):
        if not isinstance(job,dict) or not JOB_ID.fullmatch(str(job.get('id',''))):
            raise ValueError('Invalid download checkpoint identifier.')
        config=job.get('config')
        if not isinstance(config,dict) or not PROFILE.fullmatch(str(config.get('profile',''))):
            raise ValueError('Invalid download checkpoint configuration.')
        if config.get('lang','简体中文') not in ('简体中文','繁體中文','English','日本語'):
            raise ValueError('Invalid checkpoint language.')
        for option in ('by_title','rename_order'):
            if option in config and type(config[option]) is not bool: raise ValueError('Invalid checkpoint option.')
        folder=Path(config.get('folder',''))
        if not folder.is_absolute(): raise ValueError('Invalid checkpoint folder.')
        if config.get('order','newest') not in ('newest','oldest'): raise ValueError('Invalid checkpoint order.')
        if type(config.get('start_number',1)) is not int or not 0<=config.get('start_number',1)<=999999999: raise ValueError('Invalid checkpoint numbering.')
        if check_posts and job.get('posts') is not None:
            if not isinstance(job['posts'],list): raise ValueError('Invalid checkpoint posts.')
            for post in job['posts']:
                if not isinstance(post,(list,tuple)) or len(post)!=3 or not str(post[1]).startswith(config['profile'].rstrip('/')+'/post/'):
                    raise ValueError('Invalid checkpoint post URL.')
        if not isinstance(job.get('files'),list): raise ValueError('Invalid checkpoint files.')
        for record in job['files'] if records is None else records:
            if not isinstance(record,dict) or not original_url(record.get('url')):
                raise ValueError('Invalid checkpoint original URL.')
            if not str(record.get('post_url','')).startswith(config['profile'].rstrip('/')+'/post/'):
                raise ValueError('Invalid checkpoint owner post.')
            for key in ('target','part','move_to'):
                if record.get(key): inside(folder,record[key])
            if record.get('part'):
                name=Path(record['part']).name
                if not re.fullmatch(r'\.pawchive-'+job['id']+r'-[a-f0-9]{20}\.part',name):
                    raise ValueError('Unowned checkpoint temporary file.')
        album=job.get('album')
        if album: inside(folder,album['path'])
        return job

    def save(self,job,dirty_records=None):
        # Records live in separate rows: saving a 100 MB file does not rewrite
        # the URL/path manifest for every previously completed file.
        posts_changed=self._post_objects.get(job['id'],object()) is not job.get('posts')
        self._validate(job,dirty_records,posts_changed)
        job['updated']=time.time()
        metadata={k:v for k,v in job.items() if k not in ('files','posts','done_posts')}
        metadata['schema']=2
        payload=json.dumps(metadata,ensure_ascii=False,separators=(',',':'))
        if len(payload)>8*1024*1024: raise ValueError('Download checkpoint metadata is too large.')
        with self._lock,self._db:
            self._db.execute('INSERT INTO jobs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated=excluded.updated,document=excluded.document',
                             (job['id'],job['status'],job['updated'],payload))
            if posts_changed:
                posts=json.dumps(job.get('posts'),ensure_ascii=False,separators=(',',':'))
                if len(posts)>32*1024*1024: raise ValueError('Download post list is too large.')
                self._db.execute('INSERT INTO post_lists VALUES (?,?) ON CONFLICT(job_id) DO UPDATE SET document=excluded.document',(job['id'],posts))
            records=job['files'] if dirty_records is None else dirty_records
            if dirty_records is None:
                self._db.execute('DELETE FROM file_records WHERE job_id=?',(job['id'],))
                self._db.execute('DELETE FROM done_posts WHERE job_id=?',(job['id'],))
                self._db.executemany('INSERT INTO done_posts VALUES (?,?)',[(job['id'],url) for url in job.get('done_posts',[])])
            for ordinal,record in enumerate(records):
                record.setdefault('sequence',ordinal)
                key=hashlib.sha256((record['post_url']+'\0'+record['url']).encode()).hexdigest()
                self._db.execute('INSERT INTO file_records VALUES (?,?,?,?) ON CONFLICT(job_id,key) DO UPDATE SET ordinal=excluded.ordinal,document=excluded.document',
                                 (job['id'],key,record['sequence'],json.dumps(record,ensure_ascii=False,separators=(',',':'))))
        self._post_objects[job['id']]=job.get('posts')

    def mark_post_done(self,job_id,url):
        with self._lock,self._db:
            self._db.execute('INSERT OR IGNORE INTO done_posts VALUES (?,?)',(job_id,url))

    def load(self,job_id):
        if not JOB_ID.fullmatch(str(job_id)): raise ValueError('Invalid download checkpoint identifier.')
        with self._lock:
            row=self._db.execute('SELECT document FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row is None: raise KeyError('Download checkpoint was not found.')
        try:
            job=json.loads(row[0])
            if job.get('schema')==2:
                with self._lock:
                    files=self._db.execute('SELECT document FROM file_records WHERE job_id=? ORDER BY ordinal,key',(job_id,)).fetchall()
                    posts=self._db.execute('SELECT document FROM post_lists WHERE job_id=?',(job_id,)).fetchone()
                    done=self._db.execute('SELECT url FROM done_posts WHERE job_id=?',(job_id,)).fetchall()
                job['files']=[json.loads(value[0]) for value in files]
                job['posts']=json.loads(posts[0]) if posts else None
                job['done_posts']=[value[0] for value in done]
        except (ValueError,TypeError,AttributeError) as error: raise ValueError('Download checkpoint is damaged.') from error
        self._validate(job)
        for ordinal,record in enumerate(job['files']): record.setdefault('sequence',ordinal)
        self._post_objects[job_id]=job.get('posts')
        return job

    def list_recoverable(self):
        with self._lock:
            rows=self._db.execute("SELECT id FROM jobs WHERE status IN ('running','paused','failed') ORDER BY updated DESC").fetchall()
        result=[]
        for (job_id,) in rows:
            try: result.append(self.load(job_id))
            except (ValueError,KeyError,OSError): continue
        return result

    def discard(self,job_id):
        # Completed originals belong to the user, even when this checkpoint is removed.
        try:
            job=self.load(job_id)
        except (ValueError,KeyError,OSError): job=None
        if job:
            for record in job['files']:
                if record.get('part'):
                    part=inside(job['config']['folder'],record['part'])
                    if not part.is_symlink(): part.unlink(missing_ok=True)
        with self._lock,self._db:
            self._db.execute('DELETE FROM jobs WHERE id=?',(str(job_id),))
            for table in ('file_records','post_lists','done_posts'):
                self._db.execute('DELETE FROM '+table+' WHERE job_id=?',(str(job_id),))
        self._post_objects.pop(str(job_id),None)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close(); self._db=None
