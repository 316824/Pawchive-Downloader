"""Render local UI examples; no requests, artist data or downloads are used."""
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
import download_pawchive_images as source
from app_state import AppState
from download_journal import DownloadJournal
from desktop_runtime import DesktopRuntime
from feature_dialogs import ActivationDialog,CheckInDialog,CloseChoice,ScheduleDialog


def main():
    app=QApplication([])
    for name in ('segoeui.ttf','seguisb.ttf','seguisym.ttf','msyh.ttc','msyhbd.ttc'):
        path=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
        if path.is_file(): QFontDatabase.addApplicationFont(str(path))
    output=Path(__file__).resolve().parents[1]/'build'/'ui-preview'
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pawchive_design_example_') as folder:
        settings=QSettings(str(Path(folder)/'settings.ini'),QSettings.Format.IniFormat)
        class NoSystemTasks:
            def install(self,*args):raise RuntimeError('Rendering must not register Windows tasks.')
            def remove(self,*args):raise RuntimeError('Rendering must not remove Windows tasks.')
        state=AppState(Path(folder)/'state'); journal=DownloadJournal(Path(folder)/'state')
        runtime=DesktopRuntime(app,state,journal,NoSystemTasks(),tray_enabled=False)
        w=source.App(settings=settings,services=runtime); runtime.attach(w)
        w.show(); app.processEvents()
        w.grab().save(str(output/'dark.png'))
        w.theme.click(); app.processEvents()
        w.grab().save(str(output/'light.png'))
        w.theme.click(); app.processEvents()
        w.profile.setText('https://pawchive.pw/fanbox/user/demo')
        w.path.setText('D:/Artworks/Pawchive')
        w.reset_dashboard(18)
        w.set_busy_state(True); w.set_workflow('download')
        w.update_progress('(3/18) Sketchbook / sketch_014.png',5872025,13631488)
        w.speed_value.setText('5.8 MB/s')
        w.files_saved=12; w.files_value.setText('12')
        w.log.append('界面示例 · 下方为模拟记录，未进行网络下载。')
        w.log.append('✓  (1/18) Sketchbook / study_001.png')
        w.log.append('✓  (2/18) Sketchbook / study_002.png')
        w.footer_hint.setText('OFFLINE UI EXAMPLE · 离线界面示例')
        app.processEvents(); w.grab().save(str(output/'activity.png'))
        w.resize(900,740); app.processEvents()
        w.grab().save(str(output/'compact.png'))
        print('Compact geometry:',w.width(),w.height())
        w.set_busy_state(False)
        for name,dialog in [('activation',ActivationDialog(state)),('schedules',ScheduleDialog(w,runtime)),
                            ('calendar',CheckInDialog(w,state)),('close-choice',CloseChoice(w,True))]:
            dialog.show(); app.processEvents(); dialog.grab().save(str(output/(name+'.png')))
            dialog.close(); dialog.deleteLater()
        runtime.prepare_exit(); w.close(); runtime.close()
    print('Rendered local UI examples:',output)


if __name__=='__main__': main()
