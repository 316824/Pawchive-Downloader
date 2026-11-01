"""Offline startup check shared by source runs and the frozen release."""
import json
from pathlib import Path
import tempfile

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QSettings, QTimer, qVersion
from PySide6.QtWidgets import QApplication
import requests
from activation import load_public_key
from app_state import AppState
from download_journal import DownloadJournal
from desktop_runtime import DesktopRuntime
from feature_dialogs import ActivationDialog, CheckInDialog, ScheduleDialog


def run(window_class, output):
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
                window.profile.setText('https://pawchive.pw/fanbox/user/offline-check')
                if window.start.isEnabled():
                    raise RuntimeError('Download must be disabled without a folder.')
                window.path.setText(directory)
                window.validate_profile()
                if not window.start.isEnabled():
                    raise RuntimeError('Download validation did not accept the test inputs.')
                window.profile.clear()
                window.language.setCurrentIndex(2)
                window.language.setCurrentIndex(0)
                window.progress.setValue(45)
                window.grab()  # Exercise painting of controls, gradients and the icon.
                window.theme.click()
                app.processEvents()
                window.grab()
                load_public_key()
                for dialog in (ActivationDialog(state),ScheduleDialog(window,runtime),CheckInDialog(window,state)):
                    if isinstance(dialog,ActivationDialog):
                        if dialog.submit.isEnabled():raise RuntimeError('An empty invitation must not be submitted.')
                        dialog.languages.setCurrentText('English'); dialog.languages.setCurrentText('简体中文')
                    dialog.grab(); dialog.close(); dialog.deleteLater()
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
                    'pinned invitation public key','invitation, schedule and calendar dialogs'])
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
