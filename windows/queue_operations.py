"""Disk work for queue preparation and recoverable final numbering, off the UI thread."""
from pathlib import Path
from types import SimpleNamespace
import uuid

from PySide6.QtCore import QThread, Signal

from download_journal import inside, matches, fingerprint
from resumable_worker import DownloadEngine, IMAGE_EXTENSIONS, IMAGE_FOLDER, move_exclusive
from download_scheduler import has_direct_attachment


class QueuePreparationWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, journal, entries, config, parent=None):
        super().__init__(parent)
        self.journal, self.entries, self.config = journal, entries, dict(config)

    def run(self):
        created=[]
        try:
            entries=[]
            batch_id=self.config.get('batch_id') or uuid.uuid4().hex
            for sequence, entry in enumerate(self.entries):
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
                if self.journal:
                    if job is None:
                        job_id=self.journal.create(config)
                        created.append(job_id)
                        job=self.journal.load(job_id)
                        job['status']='paused'
                        meta['job_id']=job_id
                    job['config']=config
                    self.journal.save(job,dirty_records=[])
                    meta['_journal_records']=job['files']
                    meta['completed_records']=[dict(r) for r in job['files']
                                               if r.get('status') in ('complete','ready')]
                    meta['_worker_config']={**config,'_saved_count':job.get('saved_count',0)}
                meta.update(profile_url=config['profile'], ordinal=sequence,
                    by_title=config.get('by_title',True), queue_rename_order=config['queue_rename_order'],
                    content_mode=mode, batch_id=config.get('batch_id',batch_id))
                item['content_mode']=mode
                item['metadata']=meta
                entries.append(item)
            self.result.emit(entries)
        except Exception as error:
            for job_id in created:
                try: self.journal.discard(job_id)
                except (KeyError,ValueError,OSError): pass
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
