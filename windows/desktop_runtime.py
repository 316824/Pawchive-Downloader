"""Windows lifecycle coordinator: one instance, a tray and a durable serial queue."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

from PySide6.QtCore import QObject, QLockFile, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from app_state import AppState
from download_journal import DownloadJournal
from feature_text import tr
from link_utils import extract_profile_url
from windows_tasks import TaskAction, WindowsTaskScheduler, boot_time


class SingleInstance(QObject):
    message=Signal(dict)

    def __init__(self,data_dir,parent=None):
        super().__init__(parent)
        self.root=Path(data_dir); self.root.mkdir(parents=True,exist_ok=True)
        self.name='PawchiveDownloader-'+hashlib.sha256(str(self.root.resolve()).casefold().encode()).hexdigest()[:24]
        self.lock=QLockFile(str(self.root/'instance.lock'))
        self.lock.setStaleLockTime(0)
        self.server=QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.accept_connections)
        self.buffers={}
        self.primary=False

    def acquire(self):
        if not self.lock.tryLock(0):return False
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            self.lock.unlock(); raise RuntimeError(self.server.errorString())
        self.primary=True; return True

    def forward(self,payload):
        wire=json.dumps(payload,separators=(',',':')).encode()+b'\n'
        for _ in range(8):
            socket=QLocalSocket()
            socket.connectToServer(self.name)
            if socket.waitForConnected(350):
                socket.write(wire); socket.flush(); socket.waitForBytesWritten(700)
                socket.waitForReadyRead(700)
                ok=bytes(socket.readAll()).startswith(b'OK')
                socket.disconnectFromServer()
                if ok:return True
            socket.abort()
        return False

    def accept_connections(self):
        while self.server.hasPendingConnections():
            socket=self.server.nextPendingConnection()
            if len(self.buffers)>=16:socket.abort(); socket.deleteLater(); continue
            socket.setReadBufferSize(8193); self.buffers[socket]=b''
            timeout=QTimer(socket); timeout.setSingleShot(True)
            timeout.timeout.connect(socket.abort); timeout.start(3000)
            socket.readyRead.connect(lambda s=socket:self.read_message(s))
            socket.disconnected.connect(lambda s=socket:self.drop(s))
            self.read_message(socket)

    def drop(self,socket):
        self.buffers.pop(socket,None); socket.deleteLater()

    def read_message(self,socket):
        data=self.buffers.get(socket,b'')+bytes(socket.readAll())
        if len(data)>8192:socket.abort(); return
        self.buffers[socket]=data
        if b'\n' not in data:return
        try:
            payload=json.loads(data.split(b'\n',1)[0])
            if not isinstance(payload,dict) or payload.get('command') not in {'show','scheduled'}:
                raise ValueError('Unknown local command')
            if payload['command']=='scheduled':
                payload={'command':'scheduled','id':str(uuid.UUID(payload['id']))}
            else:payload={'command':'show'}
        except (ValueError,KeyError,TypeError):socket.abort(); return
        self.message.emit(payload)
        socket.write(b'OK\n'); socket.flush(); socket.disconnectFromServer()

    def close(self):
        if self.primary:
            self.server.close()
            for socket in list(self.buffers):socket.abort()
            self.buffers.clear(); self.lock.unlock(); self.primary=False


class DesktopRuntime(QObject):
    CLIPBOARD_POLL_INTERVAL_MS = 1500
    CLIPBOARD_NOTICE_COOLDOWN_SECONDS = 12.0
    CLIPBOARD_NOTICE_TIMEOUT_MS = 9000

    def __init__(self,app,store=None,journal=None,task_scheduler=None,instance=None,*,tray_enabled=True):
        super().__init__()
        self.app=app; self.store=store if store is not None else AppState()
        self.journal=journal if journal is not None else DownloadJournal(self.store.data_dir)
        self.task_scheduler=task_scheduler if task_scheduler is not None else WindowsTaskScheduler()
        self.instance=instance; self.window=None; self.tray=None; self.tray_enabled=tray_enabled
        self.active_schedule=None; self.resume_job_id=None; self.exit_requested=False
        self.closed=False; self._old_selection=None; self._runner_path=None
        self.commands=deque(maxlen=16)
        self.startup_schedule_ids=set()
        self._last_poll_error=''; self._last_poll_error_at=0.
        self.timer=QTimer(self); self.timer.setInterval(2000); self.timer.timeout.connect(self.poll)
        self.clipboard_timer=QTimer(self)
        self.clipboard_timer.setInterval(self.CLIPBOARD_POLL_INTERVAL_MS)
        self.clipboard_timer.timeout.connect(self.poll_clipboard)
        self._clipboard_last_text=''
        self._clipboard_seen=set()
        self._clipboard_last_notice_at=0.0
        self._clipboard_pending=None
        self._clipboard_deferred=None
        self.app.setQuitOnLastWindowClosed(False)
        if instance:instance.message.connect(self.handle_command)

    @property
    def language(self):
        return self.window.lang if self.window else self.store.get_setting('language','简体中文')

    def attach(self,window):
        self.window=window
        if self.tray_enabled and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray=QSystemTrayIcon(window.windowIcon(),self)
            self.tray.activated.connect(self.tray_activated)
            self.tray.messageClicked.connect(self.accept_clipboard_download)
            self.update_language(); self.tray.show()
            self._clipboard_last_text=self._clipboard_text()
            self.clipboard_timer.start()
        self.reconcile_schedules()
        self.timer.start()
        if self.tray is not None:
            QTimer.singleShot(0,self.poll_clipboard)
        while self.commands:self.handle_command(self.commands.popleft())

    def update_language(self):
        if self.tray is None or self.window is None:return
        menu=QMenu(self.window)
        menu.addAction(tr(self.language,'show'),self.show_window)
        menu.addAction(tr(self.language,'schedules'),self.show_schedules)
        menu.addAction(tr(self.language,'calendar'),self.show_calendar)
        menu.addSeparator(); menu.addAction(tr(self.language,'quit_idle'),self.request_exit)
        previous=getattr(self,'tray_menu',None)
        self.tray_menu=menu; self.tray.setContextMenu(menu)
        self.tray.setToolTip('Pawchive Downloader')
        if previous:previous.deleteLater()

    def tray_activated(self,reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,QSystemTrayIcon.ActivationReason.DoubleClick):self.show_window()

    def show_window(self):
        if self.window:self.window.showNormal(); self.window.raise_(); self.window.activateWindow()

    @staticmethod
    def _clipboard_text():
        try:
            value=QApplication.clipboard().text()
        except (RuntimeError, AttributeError):
            return ''
        return value[:8192] if isinstance(value,str) else ''

    def poll_clipboard(self):
        """Notice a newly copied Pawchive creator link once per process run."""
        if self.closed or self.exit_requested or self.tray is None or self.window is None:
            return
        text=self._clipboard_text()
        if text!=self._clipboard_last_text:
            self._clipboard_last_text=text
            link=extract_profile_url(text)
            if not link or link in self._clipboard_seen:
                self._clipboard_deferred=None
                return
            self._clipboard_deferred=(text,link)
        elif self._clipboard_deferred:
            deferred_text,link=self._clipboard_deferred
            if deferred_text!=text:
                self._clipboard_deferred=None
                return
        else:
            return
        if self.window.busy or self.app.activeModalWidget() is not None:
            return
        now=time.monotonic()
        if now-self._clipboard_last_notice_at < self.CLIPBOARD_NOTICE_COOLDOWN_SECONDS:
            return
        self._clipboard_seen.add(link)
        if len(self._clipboard_seen)>256:
            self._clipboard_seen.clear(); self._clipboard_seen.add(link)
        self._clipboard_last_notice_at=now
        self._clipboard_deferred=None
        self._clipboard_pending=link
        self.tray.showMessage(
            tr(self.language,'clipboard_title'),
            tr(self.language,'clipboard_prompt'),
            QSystemTrayIcon.MessageIcon.Information,
            self.CLIPBOARD_NOTICE_TIMEOUT_MS)
        QTimer.singleShot(self.CLIPBOARD_NOTICE_TIMEOUT_MS,self._clear_clipboard_pending)

    def _clear_clipboard_pending(self):
        self._clipboard_pending=None

    def suppress_clipboard_text(self,text):
        """Mark text handled by the visible UI so the sentinel stays quiet."""
        if not isinstance(text,str):
            return
        value=text[:8192]
        self._clipboard_last_text=value
        self._clipboard_deferred=None
        link=extract_profile_url(value)
        if link:
            self._clipboard_seen.add(link)
            if len(self._clipboard_seen)>256:
                self._clipboard_seen.clear(); self._clipboard_seen.add(link)

    def accept_clipboard_download(self):
        """Treat a click on the clipboard balloon as the affirmative action."""
        link=self._clipboard_pending
        self._clipboard_pending=None
        if not link or self.window is None or self.exit_requested:
            return
        self.window.profile.setText(link)
        self.window.profile.setFocus()
        self.show_window()
        if self.window.busy:
            return
        self.window.download()

    def show_schedules(self):
        from feature_dialogs import ScheduleDialog
        self.show_window(); dialog=ScheduleDialog(self.window,self)
        try:dialog.exec()
        finally:
            dialog.deleteLater()
            refresh=getattr(self.window,'refresh_background',None)
            if callable(refresh):refresh()

    def show_calendar(self):
        from feature_dialogs import CheckInDialog
        self.show_window(); dialog=CheckInDialog(self.window,self.store)
        try:dialog.exec()
        finally:
            dialog.deleteLater()
            refresh=getattr(self.window,'refresh_background',None)
            if callable(refresh):refresh()

    def request_close(self):
        if self.exit_requested:return True
        # Closing the main window is a reversible hide action.  A complete
        # shutdown is deliberately available only from the tray menu, which
        # prevents an accidental X click from interrupting a long download.
        if self.tray is not None:
            self.keep_background()
            return False
        self.prepare_exit()
        return True

    def keep_background(self):
        if self.tray is None:
            QMessageBox.information(self.window,'Pawchive',tr(self.language,'no_tray')); return
        self.window.hide()
        self.tray.showMessage('Pawchive',tr(self.language,'tray_message'),QSystemTrayIcon.MessageIcon.Information,3500)

    def prepare_exit(self):
        self.exit_requested=True; self.timer.stop(); self.clipboard_timer.stop(); self._clipboard_pending=None; self._clipboard_deferred=None
        worker=getattr(self.window,'worker',None)
        if worker and worker.isRunning():worker.preserve_on_interrupt=True

    def request_exit(self):
        if self.window is not None and self.window.busy:
            from ui_i18n import t
            choice=QMessageBox.question(self.window,t(self.language,'退出 Pawchive'),
                t(self.language,'检测到当前下载任务正在进行，是否继续退出？'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes: return
        self.prepare_exit(); self.window.close()

    def finish_exit(self):
        self.app.quit()

    def ensure_runner(self):
        if not getattr(sys,'frozen',False):return None
        if self._runner_path:return self._runner_path
        root=self.store.data_dir.resolve()
        folder=(root/'bin').resolve()
        if not folder.is_relative_to(root):raise RuntimeError('The scheduler runtime directory is outside the application data folder.')
        folder.mkdir(parents=True,exist_ok=True)
        target=folder/'PawchiveDownloader.exe'; source=Path(sys.executable).resolve()
        if source!=target.resolve():
            def digest(path):
                with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').digest()
            if not target.is_file() or digest(target)!=digest(source):
                temporary=folder/'PawchiveDownloader.update.tmp'
                try:
                    shutil.copyfile(source,temporary)
                    if digest(temporary)!=digest(source):raise RuntimeError('The scheduler runtime copy could not be verified.')
                    temporary.replace(target)
                finally:temporary.unlink(missing_ok=True)
        self._runner_path=target; return target

    def task_action(self,identifier):
        runner=self.ensure_runner()
        return TaskAction.for_application(identifier,executable=runner) if runner else TaskAction.for_application(identifier)

    def add_schedule(self,snapshot,due):
        plan=self.store.add_schedule(snapshot,due,timezone=datetime_timezone())
        try:self.task_scheduler.install(plan,self.task_action(plan.id))
        except Exception:
            try:self.task_scheduler.remove(plan.id)
            except Exception:pass
            self.store.delete_schedule(plan.id)
            raise
        return plan

    def cancel_schedule(self,identifier):
        plan=self.store.get_schedule(identifier)
        if plan is None:return
        if plan.status=='running':raise RuntimeError(tr(self.language,'schedule_running'))
        if plan.status not in {'pending','queued','paused'}:return
        self.task_scheduler.remove(identifier)
        self.store.set_schedule_status(identifier,'cancelled')

    def reconcile_schedules(self):
        # A checkpoint is independent of the appointment's original start time.
        recoverable={job['id']:job for job in self.journal.list_recoverable()}
        plans=self.store.list_schedules()
        if any(plan.status=='pending' and plan.due_at>time.time() for plan in plans):
            self.ensure_runner()
        for plan in plans:
            if plan.status=='running':
                job_id=self.store.get_setting('journal_for_schedule:'+plan.id)
                job_ids=self.store.get_setting('journals_for_schedule:'+plan.id,[]) or ([job_id] if job_id else [])
                state='paused' if any(value in recoverable for value in job_ids) else 'failed'
                if job_ids:
                    try:
                        if all(self.journal.load(value).get('status')=='completed' for value in job_ids):state='completed'
                    except (KeyError,ValueError,OSError):pass
                self.store.set_schedule_status(plan.id,state)
            elif plan.status=='queued' or (plan.status=='pending' and plan.due_at<=time.time()):
                from app_state import due_eligible
                eligible=plan.id in self.startup_schedule_ids and due_eligible(plan.due_at,time.time(),boot_time())
                self.store.set_schedule_status(plan.id,'queued' if eligible else 'missed')

    def handle_command(self,payload):
        if self.window is None:
            self.commands.append(payload)
            if payload.get('command')=='scheduled':self.startup_schedule_ids.add(payload['id'])
            return
        if payload.get('command')=='show':self.show_window()
        elif payload.get('command')=='scheduled':
            from app_state import due_eligible
            plan=self.store.get_schedule(payload['id'])
            if plan and plan.status in {'pending','missed'} and due_eligible(plan.due_at,time.time(),boot_time()):
                self.store.set_schedule_status(plan.id,'queued',expected=plan.status)
            self.poll()

    def poll(self):
        if self.closed or self.exit_requested or self.window is None:return
        try:
            self.store.enqueue_due(time.time(),boot_time())
            if self.window.busy or self.active_schedule is not None or self.app.activeModalWidget() is not None:return
            plan=self.store.claim_next_schedule()
            if plan is None:return
            self.active_schedule=plan.id
            try:self.run_snapshot(plan.snapshot)
            except Exception as error:
                self.store.set_schedule_status(plan.id,'failed',error=str(error))
                self.active_schedule=None
                self.restore_controls()
                self.window.log.append(str(error))
        except Exception as error:
            message=str(error)
            if message!=self._last_poll_error or time.monotonic()-self._last_poll_error_at>=60:
                self.window.log.append(tr(self.language,'schedule_error',error=message))
                self._last_poll_error=message; self._last_poll_error_at=time.monotonic()

    def run_snapshot(self,config,resume_job_id=None):
        w=self.window
        if w.busy:raise RuntimeError('Another download is still running.')
        w.profile.setText(config['profile']); w.path.setText(config['folder'])
        w.order_combo.setCurrentIndex(0 if config['order']=='newest' else 1)
        w.title_check.setChecked(bool(config['by_title']))
        if hasattr(w,'artist_folder_check'):
            w.artist_folder_check.setChecked(bool(config.get('artist_folder',False)))
        w.rename_check.setChecked(bool(config['rename_order'])); w.start_number.setValue(int(config['start_number']))
        self._old_selection=w.select_posts_check.isChecked()
        w.select_posts_check.setChecked(False)
        self.resume_job_id=resume_job_id
        if resume_job_id:
            w.launch_worker(config['profile'],None)
        else:
            # New scheduled work shares the GUI-thread static manifest plan.
            w.download()

    def restore_controls(self):
        if self._old_selection is not None:
            self.window.select_posts_check.setChecked(self._old_selection); self._old_selection=None
        self.resume_job_id=None
        if not getattr(getattr(self.window,'worker',None),'isRunning',lambda:False)():
            self.window.set_busy_state(False)

    def worker_created(self,worker):
        if self.active_schedule and worker.job_id:
            self.store.set_setting('journal_for_schedule:'+self.active_schedule,worker.job_id)
            self.store.set_setting('schedule_for_journal:'+worker.job_id,self.active_schedule)

    def task_finished(self,success,error='',paused=False):
        if self.active_schedule:
            identifier=self.active_schedule; self.active_schedule=None
            state='completed' if success else 'paused' if paused else 'cancelled' if self.window.stop_requested else 'failed'
            try:self.store.set_schedule_status(identifier,state,error=str(error)[:2000])
            except Exception as exception:self.window.log.append(str(exception))
            try:self.task_scheduler.remove(identifier)
            except Exception as exception:self.window.log.append(str(exception))
        self.restore_controls()
        # A minimized window remains visible to Qt, but the user cannot see
        # the workspace. Treat both minimized and tray-hidden states as
        # background completion so the balloon notification is delivered.
        minimized = bool(getattr(self.window, 'isMinimized', lambda: False)())
        if self.tray and (minimized or not self.window.isVisible()):
            message='download_complete' if success else 'paused' if paused else 'failed'
            self.tray.showMessage('Pawchive',tr(self.language,message),QSystemTrayIcon.MessageIcon.Information,3500)
        if not self.exit_requested:QTimer.singleShot(0,self.poll)

    def offer_resume(self):
        if self.window.busy or self.exit_requested:return
        jobs=self.journal.list_recoverable()
        if not jobs:return
        from feature_dialogs import ResumeChoice
        jobs=sorted(jobs,key=lambda value:value.get('updated',0),reverse=True)
        # Restore one plan at a time; unrelated roots/artists can carry
        # different folder and numbering choices and must never be merged.
        first=jobs[0].get('config',{})
        if first.get('queue_task'):
            keys=('folder','profile','order','by_title','queue_rename_order')
            batch=first.get('batch_id')
            jobs=[job for job in jobs if job.get('config',{}).get('queue_task') and
                  (job['config'].get('batch_id')==batch if batch else
                   all(job['config'].get(key)==first.get(key) for key in keys))]
        dialog=ResumeChoice(self.window,jobs)
        try:dialog.exec(); choice=dialog.choice
        finally:
            dialog.deleteLater()
            refresh=getattr(self.window,'refresh_background',None)
            if callable(refresh):refresh()
        if self.window.busy or self.active_schedule is not None or self.exit_requested:return
        if choice=='resume':
            try:
                queue_jobs=[job for job in jobs if self._is_queue_checkpoint(job)]
                if queue_jobs and len(queue_jobs)==len(jobs):
                    self._resume_queue_jobs(queue_jobs)
                else:
                    job=jobs[0]
                    schedule=self.store.get_setting('schedule_for_journal:'+job['id'])
                    plan=self.store.get_schedule(schedule) if schedule else None
                    # Cancelled reservations stay cancelled. Their checkpoint can
                    # still be resumed manually without occupying a new plan slot.
                    if plan and plan.status=='paused':
                        self.store.set_schedule_status(schedule,'running'); self.active_schedule=schedule
                    self.run_snapshot(job['config'],job['id'])
            except Exception as error:
                identifier=self.active_schedule; self.active_schedule=None
                if identifier:
                    try:self.store.set_schedule_status(identifier,'paused',error=str(error))
                    except Exception:pass
                self.restore_controls()
                QMessageBox.warning(self.window,'Pawchive',str(error))
        elif choice=='discard':
            for job in jobs:
                self.journal.discard(job['id'])
                schedule=self.store.get_setting('schedule_for_journal:'+job['id'])
                if schedule and self.store.get_schedule(schedule):self.store.set_schedule_status(schedule,'cancelled')

    @staticmethod
    def _is_queue_checkpoint(job):
        config=job.get('config',{}) if isinstance(job,dict) else {}
        selected=config.get('selected_post_urls')
        return bool(config.get('queue_task')) and isinstance(selected,list) and len(selected)==1

    def _resume_queue_jobs(self,jobs):
        """Rescan and rebuild the visible queue from durable checkpoints."""
        if not jobs or self.window is None:
            return
        resume_scan=getattr(self.window,'resume_recoverable_jobs',None)
        if callable(resume_scan) and resume_scan(jobs):
            return
        first=jobs[0].get('config',{})
        self.window.profile.setText(str(first.get('profile','')))
        self.window.path.setText(str(first.get('folder','')))
        self.window.order_combo.setCurrentIndex(0 if first.get('order','newest')=='newest' else 1)
        self.window.title_check.setChecked(bool(first.get('by_title',True)))
        self.window.rename_check.setChecked(bool(first.get('static_numbering',first.get('queue_rename_order',False))))
        self.window.start_number.setValue(int(first.get('queue_start_number',first.get('start_number',1))))
        entries=[]
        for job in jobs:
            config=job.get('config',{})
            selected=config.get('selected_post_urls') or []
            if not selected: continue
            post_url=str(selected[0]).rstrip('/')
            entries.append({
                'title':str(config.get('task_title') or post_url),
                'url':post_url,
                'image':str(config.get('task_thumbnail') or ''),
                'metadata':{
                    'job_id':job['id'],
                    'queue_rename_order':bool(config.get('queue_rename_order',False)),
                    'queue_start_number':int(config.get('queue_start_number',1)),
                    'by_title':bool(config.get('by_title',True)),
                },
            })
        if not entries or not self.window._start_queue(entries,str(first.get('profile',''))):
            raise RuntimeError('无法恢复未完成的图集队列。')

    def close(self):
        if self.closed:return
        self.closed=True; self.timer.stop()
        self.clipboard_timer.stop(); self._clipboard_pending=None; self._clipboard_deferred=None
        if self.tray:self.tray.hide()
        if self.instance:self.instance.close()
        self.journal.close(); self.store.close()


def datetime_timezone():
    from datetime import datetime
    return str(datetime.now().astimezone().tzinfo)
