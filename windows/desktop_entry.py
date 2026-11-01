"""Production entry point. Test-only window construction never touches user state."""
import argparse
from collections import deque
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from activation import is_activated
from app_state import AppState
from desktop_runtime import DesktopRuntime, SingleInstance
from feature_dialogs import ActivationDialog
from feature_text import tr


def run(window_class,argv=None):
    parser=argparse.ArgumentParser(description='Pawchive Downloader for Windows')
    parser.add_argument('--scheduled',type=lambda value:str(uuid.UUID(value)))
    args=parser.parse_args(argv)
    app=QApplication(sys.argv[:1]); app.setApplicationName('PawchiveDownloader')
    app.setOrganizationName('PawchiveTools'); app.setQuitOnLastWindowClosed(False)
    icon_path=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'1.ico'
    app.setWindowIcon(QIcon(str(icon_path)))
    store=None; instance=None; runtime=None; window=None; language='简体中文'
    try:
        store=AppState(); language=store.get_setting('language','简体中文')
        instance=SingleInstance(store.data_dir)
        if not instance.acquire():
            sent=instance.forward({'command':'scheduled','id':args.scheduled} if args.scheduled else {'command':'show'})
            if not sent and not args.scheduled:QMessageBox.information(None,'Pawchive',tr(language,'starting'))
            return 0 if sent else 1
        pending=deque(maxlen=16)
        instance.message.connect(pending.append)
        language=store.get_setting('language','简体中文')
        if not is_activated(store):
            gate=ActivationDialog(store,language)
            if gate.exec()!=gate.DialogCode.Accepted:return 0
            gate.deleteLater()
        runtime=DesktopRuntime(app,store=store,instance=instance)
        runtime.startup_schedule_ids.update(command['id'] for command in pending if command.get('command')=='scheduled')
        if args.scheduled:runtime.startup_schedule_ids.add(args.scheduled)
        instance.message.disconnect(pending.append)
        window=window_class(services=runtime)
        runtime.attach(window)
        for command in pending:runtime.handle_command(command)
        if args.scheduled:
            if runtime.tray:window.hide()
            else:window.show()
            QTimer.singleShot(0,lambda:runtime.handle_command({'command':'scheduled','id':args.scheduled}))
        else:
            window.show(); window.raise_(); window.activateWindow()
            QTimer.singleShot(200,runtime.offer_resume)
        app.commitDataRequest.connect(lambda *_:runtime.request_exit())
        return app.exec()
    except Exception as error:
        if window is not None and runtime:
            runtime.prepare_exit()
            window.close()
        message=tr(language,'launch_error',error=str(error))
        if args.scheduled and store:
            try:(store.data_dir/'last-scheduler-error.txt').write_text(message,encoding='utf-8')
            except OSError:pass
        else:QMessageBox.critical(None,'Pawchive',message)
        return 1
    finally:
        if runtime:runtime.close()
        else:
            if instance:instance.close()
            if store:store.close()
