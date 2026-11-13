"""Disk work for queue preparation and recoverable final numbering, off the UI thread."""
from pathlib import Path
from types import SimpleNamespace
import uuid

from PySide6.QtCore import QThread, Signal

from download_journal import inside, matches
from resumable_worker import DownloadEngine, IMAGE_EXTENSIONS
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


class QueueFinalizeWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self,journal,job_ids,helpers,parent=None):
        super().__init__(parent)
        self.journal,self.job_ids,self.helpers=journal,list(job_ids),helpers

    def run(self):
        try:
            jobs={}
            for job_id in self.job_ids:
                job=self.journal.load(job_id)
                jobs[job_id]=job
                for sibling in self.journal.list_related(job['config']):
                    jobs[sibling['id']]=sibling
            groups={}; engines={}; seen=set()
            for job_id,job in jobs.items():
                config=job['config']
                if job['status'] not in ('completed','downloaded'):
                    continue
                worker=SimpleNamespace(journal=self.journal,job_id=job_id,
                    folder=Path(config['folder']),by_title=config.get('by_title',True))
                engine=DownloadEngine(worker,self.helpers)
                engines[job_id]=engine
                if not config.get('queue_rename_order',config.get('rename_order',False)):
                    continue
                # Finish an interrupted atomic move before looking at target.
                # A process can stop after the OS rename but before the record
                # switches from target -> move_to, so target alone may be absent.
                for record in engine.job['files']:
                    if record.get('move_to'):
                        engine.move_record(record,Path(record['move_to']))
                    if record.get('status')!='complete': continue
                    path=inside(worker.folder,record['target'])
                    if path.suffix.lower() not in IMAGE_EXTENSIONS: continue
                    if not path.is_file():
                        raise ValueError('待编号图片已丢失，请恢复下载：'+path.name)
                    key=str(path).casefold()
                    if key in seen: continue
                    if not matches(record):
                        raise ValueError('图片已被外部修改，保留原文件并停止编号：'+path.name)
                    seen.add(key)
                    groups.setdefault(path.parent,[]).append((path.stat().st_mtime_ns,
                        str(record.get('date','')),record.get('sequence',0),engine,record))
            # Stage the complete group before assigning numbers; each move is
            # journaled so an interrupted finalization can resume safely.
            for directory,items in groups.items():
                items.sort(key=lambda item:(item[0],item[1],item[2],item[4]['target']))
                staged=[]
                for _,_,_,engine,record in items:
                    source=Path(record['target'])
                    extension=source.suffix
                    engine.move_record(record,directory/f'.pawchive-rename-{uuid.uuid4().hex}{extension}')
                    staged.append((engine,record,extension))
                number=1
                for engine,record,extension in staged:
                    target=directory/f'{number}{extension}'
                    while target.exists():
                        number+=1; target=directory/f'{number}{extension}'
                    engine.move_record(record,target)
                    record['number']=number
                    engine.save(record)
                    number+=1
            for engine in engines.values():
                engine.job['status']='completed'
                engine.save()
            self.result.emit({job_id:engine.job['files'] for job_id,engine in engines.items()})
        except Exception as error:
            self.failed.emit(str(error))
