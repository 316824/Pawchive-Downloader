"""Disk work for queue preparation and recoverable final numbering, off the UI thread."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import sqlite3
import uuid

from PySide6.QtCore import QThread, Signal

from download_journal import inside, matches, fingerprint, local_file_snapshot
from resumable_worker import DownloadEngine, IMAGE_EXTENSIONS, IMAGE_FOLDER, move_exclusive
from download_scheduler import has_direct_attachment


class QueuePreparationWorker(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int,int)

    def __init__(self, journal, entries, config, parent=None):
        super().__init__(parent)
        self.journal, self.entries, self.config = journal, entries, dict(config)

    def run(self):
        prepared_jobs=[]
        try:
            entries=[]
            batch_id=self.config.get('batch_id') or uuid.uuid4().hex
            for sequence, entry in enumerate(self.entries):
                if self.isInterruptionRequested():raise RuntimeError('cancelled')
                item=dict(entry)
                meta=dict(item.get('metadata') or {})
                config=dict(self.config)
                config['content_mode']=item.get('content_mode') or meta.get('content_mode') or config.get('content_mode','all')
                config.update(task_title=str(item.get('title') or '未命名图集'),
                    selected_post_urls=[item['url']], task_post_url=item['url'],
                    task_thumbnail=str(item.get('image') or item.get('thumbnail') or ''),
                    task_thumbnail_url=str(item.get('image_url') or ''),
                    task_formats=list(item.get('formats') or []),
                    task_file_count=int(item.get('file_count') or 0), batch_id=batch_id)
                meta.setdefault('completed_records',[])
                meta.setdefault('_journal_records',[])
                job = None
                if self.journal and meta.get('job_id'):
                    job=self.journal.load(meta['job_id'])
                    # Saved choices win over the current UI during recovery.
                    config={**config, **job['config']}
                config['queue_rename_order']=bool(config.get('queue_rename_order',config.get('rename_order',False)))
                if job is not None and 'queue_rename_order' not in job['config']:
                    config['queue_rename_order']=bool(job['config'].get('rename_order',False))
                mode=config.get('content_mode','all')
                if mode not in {'all','images','attachments'}:
                    raise ValueError('Invalid download content selection.')
                def count(key, saved_key):
                    value=item.get(key, config.get(saved_key))
                    return max(0,int(value)) if value is not None else None
                image_count=count('image_count','task_image_count')
                attachment_count=count('attachment_count','task_attachment_count')
                image_formats=list(item.get('image_formats') or config.get('task_image_formats') or
                                   [ext for ext in item.get('formats',[]) if str(ext).lower() in IMAGE_EXTENSIONS])
                attachment_formats=list(item.get('attachment_formats') or config.get('task_attachment_formats') or
                                        [ext for ext in item.get('formats',[]) if str(ext).lower() not in IMAGE_EXTENSIONS])
                if mode=='images':
                    # Even an opaque image URL belongs to the image pool. An
                    # empty format set otherwise conservatively implies direct.
                    item['formats']=image_formats or ['.jpg']
                    item['file_count']=image_count if image_count is not None else config.get('task_file_count',item.get('file_count',0))
                    item['direct']=False
                elif mode=='attachments':
                    item['formats']=attachment_formats
                    item['file_count']=attachment_count if attachment_count is not None else config.get('task_file_count',item.get('file_count',0))
                    item['direct']=True
                else:
                    item['formats']=list(item.get('formats') or config.get('task_formats') or [])
                    item['file_count']=(image_count+attachment_count if image_count is not None and attachment_count is not None
                                        else item.get('file_count',config.get('task_file_count',0)))
                    item['direct']=bool(attachment_count) if attachment_count is not None else has_direct_attachment(item['formats'])
                config.update(task_file_count=max(0,int(item.get('file_count') or 0)),
                              task_formats=list(item['formats']), task_image_formats=image_formats,
                              task_attachment_formats=attachment_formats)
                if image_count is not None: config['task_image_count']=image_count
                if attachment_count is not None: config['task_attachment_count']=attachment_count
                if job is None and 'static_plan' in item:
                    config.update(static_plan=item['static_plan'],static_numbering=bool(item.get('static_numbering')),
                        image_only_layout=bool(item.get('image_only_layout')),layout_version=3,
                        queue_rename_order=False,incremental_existing_check=True)
                    item['file_count']=len(config['static_plan']);config['task_file_count']=item['file_count']
                if self.journal:
                    if job is None:
                        # Freeze/check the job without a database transaction.
                        # All albums commit together before any result can
                        # start a download, avoiding one fsync per album.
                        def initialize(candidate):
                            if isinstance(candidate['config'].get('static_plan'),list):
                                self._check_static_targets(candidate,persist=False)
                        job=self.journal.prepare_new(config,status='paused',initializer=initialize)
                        config=job['config']
                        meta['job_id']=job['id']
                    else:
                        job['config']=config
                        if isinstance(config.get('static_plan'),list):
                            self._check_static_targets(job,persist=False)
                    prepared_jobs.append(job)
                    meta['_journal_records']=job['files']
                    meta['completed_records']=[dict(r) for r in job['files']
                                               if r.get('status') in ('complete','ready')]
                    meta['_worker_config']={**config,'_saved_count':job.get('saved_count',0)}
                elif isinstance(config.get('static_plan'),list):
                    # Script/UI-only runs can still present the checked result;
                    # production transfers always have a persistent journal.
                    job=dict(id=uuid.uuid4().hex,config=config,files=[],done_posts=[],saved_count=0,status='paused')
                    self._check_static_targets(job)
                    meta['_journal_records']=job['files']
                    meta['completed_records']=[dict(record) for record in job['files'] if record.get('status')=='complete']
                    meta['_worker_config']={**config,'_saved_count':job['saved_count']}
                if job is not None and isinstance(config.get('static_plan'),list):
                    completed=sum(record.get('status')=='complete' for record in job['files'])
                    meta.update(existing_files=sum(record.get('status')=='complete' and bool(record.get('existing_local'))
                                                   for record in job['files']),
                                pending_files=max(0,len(config['static_plan'])-completed),
                                all_files_existing=bool(config['static_plan']) and completed==len(config['static_plan']))
                    item['file_count']=len(config['static_plan'])
                meta.update(profile_url=config['profile'], ordinal=sequence,
                    by_title=config.get('by_title',True), queue_rename_order=config['queue_rename_order'],
                    content_mode=mode, batch_id=config.get('batch_id',batch_id))
                item['content_mode']=mode
                item['metadata']=meta
                entries.append(item)
            if self.isInterruptionRequested():raise RuntimeError('cancelled')
            if self.journal:
                self.journal.save_many(prepared_jobs,cancelled=self.isInterruptionRequested)
            self.result.emit(entries)
        except Exception as error:
            # Nothing is published until save_many commits the complete batch.
            # Failure leaves existing checkpoints untouched and new jobs absent.
            self.failed.emit(str(error))

    def _check_static_targets(self,job,*,persist=True):
        """Build every absolute target and inspect disk only on this worker."""
        config=job['config'];root=Path(config['folder']);plan=config['static_plan']
        records={(record['post_url'],record['url']):record for record in job['files']}
        incremental=bool(config.get('incremental_existing_check'))
        for index,asset in enumerate(plan):
            if self.isInterruptionRequested():raise RuntimeError('cancelled')
            target=inside(root,root/asset['relative_dir']/asset['save_name'])
            key=(asset['post_url'],asset['url']);record=records.get(key)
            # A legacy checkpoint's already-assigned path and SHA256 win.
            if record is not None and Path(record['target'])!=target:
                raise ValueError('The saved download plan changed.')
            snapshot=local_file_snapshot(target)
            if record is None:
                token=hashlib.sha256((key[0]+'\0'+key[1]).encode()).hexdigest()[:20]
                record=dict(url=asset['url'],name=asset.get('name') or asset['save_name'],
                    target=str(target),part=str(target.parent/f'.pawchive-{job["id"]}-{token}.part'),
                    post_url=asset['post_url'],date=asset.get('date',''),title=asset.get('title',''),
                    direct=bool(asset.get('direct')),status='pending',sequence=int(asset.get('index',0)),
                    number=asset.get('number'),preassigned=True,prefixed=False)
                job['files'].append(record);records[key]=record
            if snapshot is not None:
                if incremental and (record.get('existing_local') or record.get('status') not in ('complete','ready')):
                    record.update(status='complete',existing_local=True,local_snapshot=snapshot,size=snapshot['size'])
                elif matches(record,target):record['status']='complete'
                elif record.get('status') in ('complete','ready'):record['status']='pending'
            elif record.get('status')=='complete' or record.get('existing_local'):
                record['status']='pending';record.pop('existing_local',None);record.pop('local_snapshot',None)
            if index%32==0 or index+1==len(plan):self.progress.emit(index+1,len(plan))
        job['saved_count']=sum(record.get('status')=='complete' for record in job['files'])
        if plan and job['saved_count']==len(plan):
            job.update(status='completed',error='',album=None,done_posts=list(dict.fromkeys(asset['post_url'] for asset in plan)))
        elif job.get('status')=='completed':job['status']='paused'
        # Recycled jobs remain owned by the recycle bin until the UI accepts
        # this result; cancellation before that must not create a second
        # recovery entry. The transfer engine marks them running on start.
        if self.journal and persist:self.journal.save(job)


class QueueRestoreAcceptWorker(QThread):
    """Durably accept restored jobs before consuming their recycle entries.

    This second stage starts only after the GUI accepts preparation. A crash
    before preparation is accepted leaves ownership in the bin; a crash after
    acceptance leaves a normal paused checkpoint, even before a worker starts.
    """
    result = Signal(object)
    failed = Signal(str)

    def __init__(self,journal,recycle_bin,entries,parent=None):
        super().__init__(parent)
        self.journal,self.recycle_bin,self.entries=journal,recycle_bin,entries

    def run(self):
        try:
            entries=[];restores=[];promoted=set()
            for entry in self.entries:
                if self.isInterruptionRequested():raise RuntimeError('cancelled')
                item=dict(entry);meta=dict(item.get('metadata') or {})
                item['metadata']=meta;entries.append(item)
                if not meta.get('recycled_from') or meta.get('_restore_accepted'):continue
                job_id=meta.get('job_id')
                if not self.journal or not job_id:
                    raise ValueError('Restored download checkpoint is missing.')
                if job_id not in promoted:
                    job=self.journal.load(job_id)
                    if job.get('status')=='recycled':
                        job['status']='paused'
                        self.journal.save(job,dirty_records=[])
                    elif job.get('status') not in ('paused','completed'):
                        raise ValueError('Restored download checkpoint is not ready.')
                    promoted.add(job_id)
                restores.append(meta)
            # No bin entry is consumed until the whole batch has a durable
            # ordinary recovery path. Earlier promotions stay recoverable if
            # a later save fails; their redundant bin entries remain intact.
            for meta in restores:
                if self.isInterruptionRequested():raise RuntimeError('cancelled')
                warning=''
                try:
                    entry=self.recycle_bin.get(meta['recycled_from']) if self.recycle_bin else None
                    # Older bins could own moved user payloads. Restore those
                    # through their collision-safe API before removing their
                    # record; take() is only safe for metadata-only new bins.
                    if entry is not None and entry.payload_path:
                        removed=self.recycle_bin.restore(meta['recycled_from'])
                    else:
                        removed=self.recycle_bin.take(meta['recycled_from']) if self.recycle_bin else None
                    if removed is None:warning='Recycle entry cleanup was deferred.'
                except (OSError,ValueError,RuntimeError,KeyError,sqlite3.Error) as error:
                    # Cleanup failure must never discard the checkpoint or
                    # roll it back to bin-only ownership after acceptance.
                    warning=str(error)
                meta['_restore_accepted']=True
                if warning:meta['_restore_cleanup_warning']=warning
            self.result.emit(entries)
        except Exception as error:
            self.failed.emit(str(error))


class _LocalNumberingOwner:
    """Journal local-only images without inventing a remote URL or download record."""
    def __init__(self, engine, helpers):
        self.engine, self.helpers = engine, helpers
        self.job, self.w = engine.job, engine.w

    def save(self, record=None):
        self.engine.journal.save(self.job,dirty_records=[])

    def move_record(self, record, destination):
        source=inside(self.w.folder,record['target'])
        destination=inside(self.w.folder,destination)
        if source==destination:
            record.pop('move_to',None)
            self.save(record)
            return
        if not source.exists():
            if not matches(record,destination):
                raise ValueError('待编号图片已丢失，请恢复下载：'+source.name)
        else:
            if not matches(record,source):
                raise ValueError('图片已被外部修改，保留原文件并停止编号：'+source.name)
            if destination.exists():
                destination=self.helpers['unique_path'](destination.parent,destination.name)
            record['move_to']=str(destination)
            self.save(record)
            try: move_exclusive(source,destination)
            except FileExistsError:
                return self.move_record(record,self.helpers['unique_path'](destination.parent,destination.name))
        record['target']=str(destination)
        record.pop('move_to',None)
        self.save(record)


class QueueFinalizeWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self,journal,job_ids,helpers,parent=None):
        super().__init__(parent)
        self.journal,self.job_ids,self.helpers=journal,list(job_ids),helpers

    @staticmethod
    def _rename_enabled(config):
        return bool(config.get('queue_rename_order',config.get('rename_order',False)))

    def _directory(self, engine):
        config=engine.job['config']
        if not config.get('by_title',True):
            return inside(engine.w.folder,engine.w.folder/IMAGE_FOLDER)
        title=str(config.get('task_title') or '')
        if not title: return None
        safe=self.helpers.get('safe_path_component')
        if safe is not None:
            title=safe(title,limit=100)
        elif title in ('.','..') or '/' in title or chr(92) in title:
            # Without the application's sanitizer, only an exact safe single
            # component is eligible for migration. Never guess a parent path.
            return None
        return inside(engine.w.folder,engine.w.folder/title)

    @staticmethod
    def _finish_pending_move(owner, record):
        destination=record.get('move_to')
        if not destination: return
        destination=inside(owner.w.folder,destination)
        if matches(record,destination):
            # The old target might already hold another staged image after a
            # crash. The recorded destination/hash proves this move finished;
            # do not touch the current contents of that old path.
            record['target']=str(destination)
            record.pop('move_to',None)
            owner.save(record)
        else:
            owner.move_record(record,destination)

    @staticmethod
    def _move_references(references, target):
        owner,record=references[0]
        # All aliases journal the intended move before any filesystem change.
        # Each can recover independently if the process stops after OS rename.
        for alias_owner,alias in references:
            alias['move_to']=str(target)
            alias_owner.save(alias)
        owner.move_record(record,target)
        actual=record['target']
        for alias_owner,alias in references[1:]:
            if not matches(alias,actual):
                raise ValueError('共享图片编号校验失败，保留文件并停止编号。')
            alias['target']=actual
            alias.pop('move_to',None)
            alias_owner.save(alias)

    def run(self):
        try:
            jobs={}
            for job_id in self.job_ids:
                job=self.journal.load(job_id)
                jobs[job_id]=job
                for sibling in self.journal.list_related(job['config'],include_legacy=True):
                    jobs[sibling['id']]=sibling
            engines={}; local_owners={}; references={}; directories={}; all_records=[]
            for job_id,job in jobs.items():
                config=job['config']
                if job['status'] not in ('completed','downloaded'):
                    continue
                worker=SimpleNamespace(journal=self.journal,job_id=job_id,
                    folder=Path(config['folder']),by_title=config.get('by_title',True))
                engine=DownloadEngine(worker,self.helpers)
                engines[job_id]=engine
                local_owners[job_id]=_LocalNumberingOwner(engine,self.helpers)
                if not self._rename_enabled(config): continue
                directory=self._directory(engine)
                if directory is not None: directories.setdefault(directory,engine)
                owners=[(engine,r) for r in engine.job['files'] if r.get('status')=='complete']
                owners.extend((local_owners[job_id],r) for r in engine.job.get('local_numbering',[]))
                all_records.extend(owners)
            # If the process exited while journaling a shared move (before
            # every alias committed), propagate its recorded transition to
            # the remaining aliases before physically completing that move.
            transitions={}
            for owner,record in all_records:
                if record.get('move_to'):
                    key=(str(record['target']).casefold(),record.get('size'),record.get('sha256'))
                    transitions[key]=record['move_to']
            for owner,record in all_records:
                key=(str(record['target']).casefold(),record.get('size'),record.get('sha256'))
                if not record.get('move_to') and key in transitions:
                    record['move_to']=transitions[key]
                    owner.save(record)
            for owner,record in all_records:
                self._finish_pending_move(owner,record)
            for owner,record in all_records:
                path=inside(owner.w.folder,record['target'])
                if path.suffix.lower() not in IMAGE_EXTENSIONS: continue
                if not path.is_file():
                    raise ValueError('待编号图片已丢失，请恢复下载：'+path.name)
                if not matches(record):
                    raise ValueError('图片已被外部修改，保留原文件并停止编号：'+path.name)
                references.setdefault(path,[]).append((owner,record))
            # Old releases erased their completed manifests. Include the
            # actual direct-child images in the exact image/title directories
            # and persist local hashes before moving any of them. Attachments,
            # subdirectories, symbolic links and untracked staging files stay out.
            for directory,engine in directories.items():
                if not directory.is_dir(): continue
                # Keep migration records on a current recoverable batch member
                # rather than turning an old empty manifest into a redownload.
                host=next((engines[i] for i in self.job_ids if i in engines and
                           engines[i].w.folder==engine.w.folder and
                           self._rename_enabled(engines[i].job['config'])),engine)
                owner=local_owners[host.job['id']]
                for candidate in directory.iterdir():
                    if candidate.suffix.lower() not in IMAGE_EXTENSIONS: continue
                    if candidate.name.startswith('.pawchive-rename-'): continue
                    if candidate.is_symlink() or getattr(candidate,'is_junction',lambda:False)(): continue
                    path=inside(host.w.folder,candidate)
                    if not path.is_file() or path in references: continue
                    record=dict(id=uuid.uuid4().hex,target=str(path),status='complete',
                        size=path.stat().st_size,sha256=fingerprint(path),sequence=0,date='')
                    host.job.setdefault('local_numbering',[]).append(record)
                    owner.save(record)
                    references[path]=[(owner,record)]
            groups={}
            for path,refs in references.items():
                record=refs[0][1]
                groups.setdefault(path.parent,[]).append((path.stat().st_mtime_ns,
                    str(record.get('date','')),record.get('sequence',0),str(path),refs))
            # Every participant is recoverable until all staged moves and
            # their journal references have committed.
            for engine in engines.values():
                if self._rename_enabled(engine.job['config']) and (engine.job['files'] or engine.job.get('local_numbering')):
                    engine.job['status']='downloaded'
                    engine.save()
            for directory,items in groups.items():
                items.sort(key=lambda item:item[:4])
                staged=[]
                for *_,refs in items:
                    extension=Path(refs[0][1]['target']).suffix
                    self._move_references(refs,directory/f'.pawchive-rename-{uuid.uuid4().hex}{extension}')
                    staged.append((refs,extension))
                number=1
                for refs,extension in staged:
                    target=directory/f'{number}{extension}'
                    while target.exists():
                        number+=1; target=directory/f'{number}{extension}'
                    self._move_references(refs,target)
                    for owner,record in refs:
                        record['number']=number
                        owner.save(record)
                    number+=1
            for engine in engines.values():
                engine.job['status']='completed'
                engine.save()
            self.result.emit({job_id:engine.job['files'] for job_id,engine in engines.items()})
        except Exception as error:
            self.failed.emit(str(error))
