"""Offline startup check shared by source runs and the frozen release."""
import json
import os
from pathlib import Path
import sys
import tempfile

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QSettings, QThread, QTimer, qVersion
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from PySide6.QtGui import QImage,QColor
import requests
from activation import load_public_key
from app_state import AppState
from download_journal import DownloadJournal
from desktop_runtime import DesktopRuntime
from feature_dialogs import ActivationDialog, CheckInDialog, ScheduleDialog, ArtistCardsDialog
from subscription_ui import SubscriptionDialog
from artist_lookup import input_profiles
from batch_artists import BatchArtistDialog, split_batch_inputs
from static_download_plan import assign_plans
from subscription_preview import UpdatePreviewDialog


def check_static_plans(app, directory):
    """Exercise immutable naming on the GUI thread, without files or HTTP."""
    if QThread.currentThread() != app.thread():
        raise RuntimeError('Static filenames must be assigned on the GUI thread.')
    profile='https://pawchive.pw/fanbox/user/offline-check'
    def asset(name,index,direct=False):
        return dict(url='https://file.pawchive.pw/data/offline/'+name,
                    name=name,index=index,direct=direct)
    entries=[dict(url=profile+'/post/new',title='New',date='2026-11-20',
                  assets=[asset('second.webp',1),asset('first.png',0),asset('archive.zip',2,True)]),
             dict(url=profile+'/post/old',title='Old',date='2026-10-9',assets=[asset('old.jpg',0)])]
    original=json.dumps(entries,sort_keys=True)
    folder=Path(directory)/'static-plan-downloads'
    config=dict(folder=str(folder),order='newest',by_title=False,
                queue_rename_order=True,content_mode='images')
    flat=assign_plans(entries,config)
    names=[[file['save_name'] for file in item['static_plan']] for item in flat]
    if names != [['2.png','3.webp'],['1.jpg']]:
        raise RuntimeError('Static image numbers must follow oldest-to-newest collection dates.')
    if any(not item['image_only_layout'] or any(file['relative_dir'] not in ('','.')
               or file['direct'] for file in item['static_plan']) for item in flat):
        raise RuntimeError('Image-only downloads without title folders must use the selected folder.')
    titled=assign_plans(entries,{**config,'by_title':True})
    if [[file['save_name'] for file in item['static_plan']] for item in titled] != [['1.png','2.webp'],['1.jpg']]:
        raise RuntimeError('Static image numbers must restart inside each collection folder.')
    mixed=assign_plans(entries,{**config,'content_mode':'all'})
    if any(file['relative_dir'] not in ('','.') for item in mixed for file in item['static_plan']):
        raise RuntimeError('All files must use the selected folder when title folders are disabled.')
    attachment=next(file for file in mixed[0]['static_plan'] if file['direct'])
    if attachment['save_name'] != 'archive.zip' or attachment['number'] is not None:
        raise RuntimeError('Direct attachments must retain their original names outside image numbering.')
    if folder.exists() or json.dumps(entries,sort_keys=True) != original:
        raise RuntimeError('Assigning static filenames must not mutate input manifests or create files.')


def run(window_class, output):
    if os.name == 'nt':
        import ctypes
        # A failed isolated build check must report an exit code rather than
        # leave a Windows Error Reporting prompt on the publisher's desktop.
        ctypes.windll.kernel32.SetErrorMode(0x8003)
    app = QApplication([])
    report = {'ok': False, 'pyside': pyside_version, 'qt': qVersion()}
    failures = []
    original_request = requests.sessions.Session.request

    def reject_network(*args, **kwargs):
        raise RuntimeError('The offline startup check must not access the network.')

    requests.sessions.Session.request = reject_network
    with tempfile.TemporaryDirectory(prefix='pawchive_startup_check_') as directory:
        settings=QSettings(str(Path(directory)/'settings.ini'),QSettings.Format.IniFormat)
        class NoSystemTasks:
            def install(self,*args):raise RuntimeError('The offline startup check must not register a task.')
            def remove(self,*args):raise RuntimeError('The offline startup check must not remove a task.')
        state=AppState(Path(directory)/'state')
        journal=DownloadJournal(Path(directory)/'state')
        runtime=DesktopRuntime(app,state,journal,NoSystemTasks(),tray_enabled=False)
        window = window_class(settings=settings,services=runtime)
        runtime.attach(window)
        cache = window.cache_dir

        def check():
            try:
                if window.windowIcon().isNull():
                    raise RuntimeError('The bundled application icon is missing.')
                if window.start.isEnabled():
                    raise RuntimeError('Download must be disabled without a profile and folder.')
                if window.logs_hidden or window.logs_card.isHidden() or window.focus_button.text() != '收起日志':
                    raise RuntimeError('Logs must be visible by default with the hide-logs control.')
                window.toggle_logs()
                if not window.logs_card.isHidden() or not window.focus_button.isVisible() or window.focus_button.text() != '显示日志':
                    raise RuntimeError('The show-logs control must remain visible when logs are hidden.')
                window.toggle_logs()
                check_static_plans(app,directory)
                ScanAnimation=sys.modules[window_class.__module__].ScanAnimation
                for expected in (False,True):
                    scan=ScanAnimation(window)
                    try:
                        scan.set_language(window.lang)
                        if scan.auto_queue_check.isChecked() != expected:
                            raise RuntimeError('The scan automatic-queue checkbox preference was not restored.')
                        scan.auto_queue_check.setChecked(True)
                        if not settings.value('download/scan_auto_confirm',False,type=bool):
                            raise RuntimeError('The scan automatic-queue checkbox preference was not saved.')
                        scan.grab()
                    finally:
                        scan._allow_close=True;scan.close();scan.deleteLater()
                settings.remove('download/scan_auto_confirm')
                candidates=input_profiles('123')
                if candidates != ('https://pawchive.pw/fanbox/user/123','https://pawchive.pw/patreon/user/123'):
                    raise RuntimeError('Numeric artist IDs must check both supported platforms.')
                batch_text='123, '+candidates[1]+'\n123 456'
                expected_batch=['123',candidates[1],'456']
                if split_batch_inputs(batch_text) != expected_batch:
                    raise RuntimeError('Batch artist input must preserve order and merge duplicate inputs.')
                batch=BatchArtistDialog(window)
                try:
                    batch.editor.setPlainText(batch_text)
                    if batch.inputs != expected_batch:
                        raise RuntimeError('The batch artist dialog did not read the pasted inputs.')
                    batch.grab()
                finally:
                    batch.close();batch.deleteLater()
                window.profile.setText('https://pawchive.pw/fanbox/user/offline-check')
                if window.start.isEnabled():
                    raise RuntimeError('Download must be disabled without a folder.')
                window.path.setText(directory)
                window.validate_profile()
                if not window.start.isEnabled():
                    raise RuntimeError('Download validation did not accept the test inputs.')
                from download_settings import sanitize_artist_name,browse_directory
                if not window.artist_folder_check.isChecked():
                    raise RuntimeError('Artist folders must be enabled on a fresh profile.')
                window.artist_folder_check.setChecked(False)
                if settings.value('download/artist_folder',True,type=bool):
                    raise RuntimeError('The download checkbox preference was not saved.')
                window.artist_folder_check.setChecked(True)
                settings.setValue('last_download_path',str(Path(directory)/'removed'/'child'))
                if Path(browse_directory(settings)) != Path(directory):
                    raise RuntimeError('Deleted browse history did not fall back to its parent.')
                settings.remove('last_download_path')
                if sanitize_artist_name('画师/ねこ:') != '画师_ねこ_':
                    raise RuntimeError('The Unicode artist-folder sanitizer is missing.')
                window.profile.clear()
                window.language.setCurrentIndex(2)
                window.language.setCurrentIndex(0)
                window.progress.setValue(45)
                window.grab()  # Exercise painting of controls, gradients and the icon.
                window.theme.click()
                app.processEvents()
                window.grab()
                for language, caption in (('简体中文','取消'),('繁體中文','取消'),
                                          ('English','Cancel'),('日本語','キャンセル')):
                    window.set_lang(language)
                    buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
                    if buttons.button(QDialogButtonBox.StandardButton.Cancel).text().replace('&','') != caption:
                        raise RuntimeError('The bundled Qt translation is missing: '+language)
                    buttons.deleteLater()
                window.set_lang('简体中文')
                load_public_key()
                from artist_cards import record_download,update_artist_metadata
                avatar_root=state.data_dir/'artist-cards'; avatar_root.mkdir(exist_ok=True)
                avatar=QImage(64,64,QImage.Format.Format_RGB32); avatar.fill(QColor('#44cc88'))
                avatar_path=avatar_root/'startup-avatar.png'; avatar.save(str(avatar_path))
                artist='https://pawchive.pw/fanbox/user/1234567890'
                record_download(state,artist,posts=30,files=2)
                update_artist_metadata(state,artist,name='Offline artist',avatar_path=str(avatar_path))
                window.subscription_store.add(artist,name='Offline artist',avatar_path=str(avatar_path),baseline_ts=1)
                pending=[dict(url=artist+'/post/update',title='Offline update',timestamp=2)]
                window.subscription_store.set_baseline(artist,2,posts=pending,unread=True)
                window.refresh_subscription_badge()
                archive=window.artist_cards_button
                if archive.text() != '✦ 1' or archive.unread_badge.isHidden():
                    raise RuntimeError('The artist archive must display its count and separate unread marker.')
                if not archive.rect().contains(archive.unread_badge.geometry()):
                    raise RuntimeError('The artist archive unread marker is clipped.')
                preview=UpdatePreviewDialog(window,artist)
                preview_root=preview.pool.root
                try:
                    if preview.picker is None or [entry['url'] for entry in preview.picker.titles] != [pending[0]['url']]:
                        raise RuntimeError('The subscription preview must contain only pending update collections.')
                    preview.grab()
                finally:
                    preview.reject();preview.deleteLater()
                if preview_root.exists() or window.subscription_store.get(artist)['update_count'] != 1:
                    raise RuntimeError('Closing the subscription preview must release its cache and retain unread updates.')
                for dialog in (ActivationDialog(state),ScheduleDialog(window,runtime),CheckInDialog(window,state),
                               ArtistCardsDialog(window,state),SubscriptionDialog(window,state)):
                    if isinstance(dialog,ActivationDialog):
                        if dialog.submit.isEnabled():raise RuntimeError('An empty invitation must not be submitted.')
                        dialog.languages.setCurrentText('English'); dialog.languages.setCurrentText('简体中文')
                    dialog.grab()
                    if isinstance(dialog,ArtistCardsDialog):
                        if dialog.total_label.text() != '共 1 位画师':
                            raise RuntimeError('The artist cabinet total count is missing.')
                        item=dialog.cards.item(0)
                        if item is None or item.text() or not item.icon().isNull():
                            raise RuntimeError('The artist card delegate duplicates its custom row.')
                    dialog.close(); dialog.deleteLater()
                window.theme.click()
                app.processEvents()
                window.grab()
                titles=[dict(title='Offline study one',url='https://pawchive.pw/fanbox/user/offline-check/post/one',image=None,formats=['.zip']),
                        dict(title='Offline study two',url='https://pawchive.pw/fanbox/user/offline-check/post/two',image=None,formats=[])]
                def inspect_picker():
                    dialog=app.activeModalWidget()
                    try:
                        if dialog is None: raise RuntimeError('The cover picker did not open.')
                        dialog.search.setText('two')
                        if len(dialog.selected)!=2: raise RuntimeError('Search changed the selected posts.')
                        dialog.none_button.click()
                        if dialog.confirm_button.isEnabled(): raise RuntimeError('An empty selection was accepted.')
                        dialog.all_button.click(); dialog.confirm_button.click()
                    except Exception as error:
                        failures.append(str(error))
                        if dialog is not None: dialog.reject()
                QTimer.singleShot(80,inspect_picker)
                chosen=window.choose_titles(titles)
                if chosen!={entry['url'] for entry in titles}:
                    raise RuntimeError('The frozen cover picker did not return the selected URLs.')
                report.update(ok=True, checks=[
                    'QtCore/QtGui/QtWidgets import', 'native GUI startup',
                    'bundled icon', 'input validation', 'language switching',
                    'theme switching', 'rainbow progress painting',
                    'cover picker and local title search','QtNetwork and local lifecycle',
                    'pinned invitation public key','invitation, schedule and calendar dialogs',
                    'artist cards and subscriptions dialogs','single-paint populated artist cards',
                    'download preferences, Unicode artist folders and deleted-path fallback',
                    'bundled four-language Qt controls',
                    'static oldest-to-newest image numbering on GUI thread',
                    'flat mixed-content layout and per-collection image numbering',
                    'unchanged direct attachment names',
                    'numeric artist platform candidates and ordered batch dialog',
                    'remembered automatic scan queue checkbox',
                    'visible-by-default logs and persistent show control',
                    'artist archive count and separate unread marker',
                    'pending subscription preview and disposable cache'])
            except Exception as error:
                failures.append(str(error))
            finally:
                runtime.prepare_exit(); window.close()
                app.quit()

        window.show()
        QTimer.singleShot(350, check)
        app.exec()
        runtime.close()
        report['cache_removed'] = not cache.exists()
        report['ok'] = report['ok'] and report['cache_removed'] and not failures
        if failures:
            report['errors'] = failures
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    requests.sessions.Session.request = original_request
    return 0 if report['ok'] else 1
