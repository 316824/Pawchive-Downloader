"""Small native dialogs for the Windows lifecycle and local plans."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, QLocale, Qt
from PySide6.QtGui import QColor, QPalette, QTextCharFormat
from PySide6.QtWidgets import (QApplication, QCalendarWidget, QCheckBox, QComboBox,
    QDateTimeEdit, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QTextEdit,
    QTabWidget, QToolButton, QVBoxLayout)

from feature_text import LANGUAGES, tr


def locale_for(language):
    return QLocale({'简体中文':'zh_CN','繁體中文':'zh_TW','English':'en_US','日本語':'ja_JP'}.get(language,'zh_CN'))


def label(text,muted=False):
    value=QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(True)
    if muted: value.setObjectName('muted')
    return value


class CloseChoice(QDialog):
    def __init__(self,parent,tray_available):
        super().__init__(parent)
        self.choice='cancel'
        lang=parent.lang
        self.setWindowTitle(tr(lang,'close_title'))
        self.setMinimumWidth(440)
        layout=QVBoxLayout(self); layout.setContentsMargins(28,26,28,24); layout.setSpacing(18)
        title=label(tr(lang,'close_title')); title.setStyleSheet('font-size:21px;font-weight:600')
        layout.addWidget(title)
        layout.addWidget(label(tr(lang,'close_detail'),True))
        if not tray_available: layout.addWidget(label(tr(lang,'no_tray'),True))
        row=QHBoxLayout()
        self.background_button=QPushButton(tr(lang,'background'))
        self.background_button.setEnabled(tray_available)
        self.exit_button=QPushButton(tr(lang,'quit' if parent.busy else 'quit_idle'))
        self.cancel_button=QPushButton(tr(lang,'cancel'))
        self.cancel_button.clicked.connect(self.reject)
        self.background_button.clicked.connect(lambda:self.finish('background'))
        self.exit_button.clicked.connect(lambda:self.finish('exit'))
        for button in (self.cancel_button,self.exit_button,self.background_button):
            button.setAutoDefault(False); row.addWidget(button)
        layout.addLayout(row)

    def finish(self,choice):
        self.choice=choice; self.accept()


class ResumeChoice(QDialog):
    def __init__(self,parent,job):
        super().__init__(parent)
        self.choice='later'; lang=parent.lang
        self.setWindowTitle(tr(lang,'resume_title'))
        self.setMinimumWidth(480)
        layout=QVBoxLayout(self); layout.setContentsMargins(28,26,28,24); layout.setSpacing(16)
        title=label(tr(lang,'resume_title')); title.setStyleSheet('font-size:21px;font-weight:600')
        layout.addWidget(title); layout.addWidget(label(tr(lang,'resume_detail'),True))
        config=job.get('config',{})
        layout.addWidget(label(str(config.get('profile',''))))
        layout.addWidget(label(str(config.get('folder','')),True))
        layout.addWidget(label(tr(lang,'discard_detail'),True))
        row=QHBoxLayout()
        for key in ('discard','later','resume'):
            button=QPushButton(tr(lang,key)); button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False,k=key:self.finish(k)); row.addWidget(button)
        layout.addLayout(row)

    def finish(self,choice):
        self.choice=choice; self.accept()


class ActivationDialog(QDialog):
    def __init__(self,store,language='简体中文',parent=None):
        super().__init__(parent)
        import activation
        self.api=activation; self.store=store; self.lang=language
        self._localized=[]
        self.setWindowTitle(tr(language,'activation'))
        self.setMinimumWidth(550)
        self.setStyleSheet('QDialog{background:#10141c;color:#e9edf6} QLabel{color:#e9edf6;background:transparent;font-size:14px} '
            'QLineEdit,QTextEdit{background:#192130;color:#e9edf6;border:1px solid #354158;border-radius:10px;padding:10px;font-size:13px}'
            'QPushButton{background:#242b3c;color:#e9edf6;border:1px solid #47516c;border-radius:10px;padding:11px 15px}'
            'QPushButton:hover{border-color:#b89af4} QPushButton:disabled{color:#737d91}'
            'QComboBox{background:#192130;color:#e9edf6;border:1px solid #354158;border-radius:8px;padding:7px}'
            'QComboBox QAbstractItemView{background:#192130;color:#e9edf6;selection-background-color:#354158}')
        layout=QVBoxLayout(self); layout.setContentsMargins(30,28,30,26); layout.setSpacing(16)
        header=QHBoxLayout()
        title=self.translated_label('activation'); title.setStyleSheet('font-size:23px;font-weight:600')
        header.addWidget(title,1)
        self.languages=QComboBox(); self.languages.addItems(LANGUAGES)
        self.languages.setCurrentText(language); header.addWidget(self.languages)
        layout.addLayout(header); layout.addWidget(self.translated_label('activation_info'))
        layout.addWidget(self.translated_label('device'))
        row=QHBoxLayout(); self.device=QLineEdit(activation.device_code()); self.device.setReadOnly(True)
        copy=QPushButton(tr(language,'copy_device')); copy.clicked.connect(lambda:QApplication.clipboard().setText(self.device.text()))
        self._localized.append((copy,'copy_device'))
        row.addWidget(self.device,1); row.addWidget(copy); layout.addLayout(row)
        layout.addWidget(self.translated_label('invitation'))
        self.token=QTextEdit(); self.token.setAcceptRichText(False); self.token.setMaximumHeight(110)
        layout.addWidget(self.token)
        self.counter=label(''); layout.addWidget(self.counter)
        self.error=label(''); self.error.setStyleSheet('color:#ef9eaf'); layout.addWidget(self.error)
        layout.addWidget(self.translated_label('activation_notice'))
        buttons=QHBoxLayout(); self.cancel_button=QPushButton(tr(language,'cancel'))
        self.unlock_button=QPushButton(tr(language,'unlock')); self.submit=QPushButton(tr(language,'activate'))
        self.cancel_button.clicked.connect(self.reject); self.submit.clicked.connect(self.activate)
        self.unlock_button.clicked.connect(self.unlock)
        for button,key in ((self.cancel_button,'cancel'),(self.unlock_button,'unlock'),(self.submit,'activate')):
            self._localized.append((button,key))
            button.setAutoDefault(False); buttons.addWidget(button)
        layout.addLayout(buttons)
        self.languages.currentTextChanged.connect(self.change_language)
        self.token.textChanged.connect(self.update_limit)
        self.update_limit()

    def translated_label(self,key):
        value=label(tr(self.lang,key)); self._localized.append((value,key)); return value

    def change_language(self,language):
        self.lang=language; self.store.set_setting('language',language)
        self.setWindowTitle(tr(language,'activation'))
        for widget,key in self._localized:widget.setText(tr(language,key))
        self.error.clear(); self.update_limit()

    def update_limit(self):
        remaining=self.api.remaining_attempts(self.store)
        self.counter.setText(tr(self.lang,'remaining',count=remaining))
        self.submit.setEnabled(remaining>0 and bool(self.token.toPlainText().strip())); self.token.setReadOnly(remaining<=0)
        if remaining<=0: self.error.setText(tr(self.lang,'activation_locked'))

    def activate(self):
        try:
            self.api.activate(self.store,self.token.toPlainText().strip())
        except self.api.ActivationError as error:
            self.error.setText(tr(self.lang,'activation_unavailable' if error.code in {'public_key_unavailable','device_unavailable'} else 'activation_invalid'))
            self.update_limit(); return
        self.accept()

    def unlock(self):
        token,accepted=QInputDialog.getMultiLineText(self,tr(self.lang,'unlock_title'),tr(self.lang,'unlock_title'))
        if not accepted or not token.strip(): return
        try: self.api.accept_unlock(self.store,token.strip())
        except self.api.ActivationError:
            self.error.setText(tr(self.lang,'unlock_invalid')); return
        self.error.clear(); self.token.setReadOnly(False); self.update_limit()


class ScheduleDialog(QDialog):
    def __init__(self,parent,runtime):
        super().__init__(parent)
        self.runtime=runtime; self.lang=parent.lang
        self.setWindowTitle(tr(self.lang,'schedules')); self.resize(760,700)
        self.setMinimumSize(620,550)
        layout=QVBoxLayout(self); layout.setContentsMargins(26,24,26,24); layout.setSpacing(14)
        heading=label(tr(self.lang,'schedules')); heading.setStyleSheet('font-size:22px;font-weight:600')
        layout.addWidget(heading); layout.addWidget(label(tr(self.lang,'schedule_intro'),True))
        layout.addWidget(label(tr(self.lang,'schedule_rules'),True))
        layout.addWidget(label(tr(self.lang,'creator')))
        self.profile=QLineEdit(parent.profile.text()); layout.addWidget(self.profile)
        layout.addWidget(label(tr(self.lang,'folder')))
        pathrow=QHBoxLayout(); self.folder=QLineEdit(parent.path.text()); self.folder.setReadOnly(True)
        browse=QPushButton(tr(self.lang,'browse')); browse.clicked.connect(self.browse)
        pathrow.addWidget(self.folder,1); pathrow.addWidget(browse); layout.addLayout(pathrow)
        controls=QHBoxLayout(); controls.addWidget(label(tr(self.lang,'when')))
        self.when=QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600)); self.when.setCalendarPopup(True)
        self.when.setLocale(locale_for(self.lang))
        self.when.setDisplayFormat('yyyy-MM-dd HH:mm'); controls.addWidget(self.when,1)
        self.order=QComboBox(); self.order.addItems([parent.order_combo.itemText(0),parent.order_combo.itemText(1)])
        self.order.setCurrentIndex(parent.order_combo.currentIndex()); controls.addWidget(self.order)
        layout.addLayout(controls)
        self.by_title=type(parent.title_check)(parent.title_check.text()); self.by_title.setProperty('lightTheme',not parent._dark); self.by_title.setChecked(parent.title_check.isChecked())
        layout.addWidget(self.by_title)
        numbering=QHBoxLayout(); self.rename=type(parent.rename_check)(parent.rename_check.text()); self.rename.setProperty('lightTheme',not parent._dark); self.rename.setChecked(parent.rename_check.isChecked())
        self.start_number=QSpinBox(); self.start_number.setRange(0,999999999); self.start_number.setValue(parent.start_number.value())
        numbering.addWidget(self.rename,1); numbering.addWidget(self.start_number); layout.addLayout(numbering)
        self.rename.setEnabled(not self.by_title.isChecked()); self.start_number.setEnabled(not self.by_title.isChecked())
        self.by_title.toggled.connect(lambda state:(self.rename.setEnabled(not state),self.start_number.setEnabled(not state)))
        add=QPushButton(tr(self.lang,'add')); add.clicked.connect(self.add_schedule); layout.addWidget(add)
        self.notice=label('',True); layout.addWidget(self.notice)
        self.items=QListWidget(); self.items.setMinimumHeight(120); layout.addWidget(self.items,1)
        buttons=QHBoxLayout(); remove=QPushButton(tr(self.lang,'remove')); remove.clicked.connect(self.remove_selected)
        close=QPushButton(tr(self.lang,'ok')); close.clicked.connect(self.accept)
        buttons.addWidget(remove); buttons.addStretch(1); buttons.addWidget(close); layout.addLayout(buttons)
        self.refresh()

    def browse(self):
        folder=QFileDialog.getExistingDirectory(self,tr(self.lang,'folder'),self.folder.text())
        if folder:self.folder.setText(folder)

    def add_schedule(self):
        from app_state import normalize_snapshot
        due=self.when.dateTime().toSecsSinceEpoch()
        if not self.folder.text().strip() or due<=QDateTime.currentSecsSinceEpoch():
            self.notice.setText(tr(self.lang,'schedule_invalid')); return
        snapshot=dict(profile=self.profile.text().strip(),folder=str(Path(self.folder.text()).resolve()),
            order='newest' if self.order.currentIndex()==0 else 'oldest',by_title=self.by_title.isChecked(),
            rename_order=not self.by_title.isChecked() and self.rename.isChecked(),start_number=self.start_number.value())
        try:self.runtime.add_schedule(normalize_snapshot(snapshot),due)
        except Exception as error:
            self.notice.setText(tr(self.lang,'schedule_error',error=str(error))); return
        self.notice.setText(tr(self.lang,'schedule_done')); self.refresh()

    def refresh(self):
        self.items.clear()
        plans=self.runtime.store.list_schedules()
        for plan in reversed(plans[-100:]):
            when=datetime.fromtimestamp(plan.due_at).strftime('%Y-%m-%d %H:%M')
            text=f'{when}  ·  {tr(self.lang,plan.status)}\n{plan.snapshot["profile"]}\n{plan.snapshot["folder"]}'
            item=QListWidgetItem(text); item.setData(Qt.ItemDataRole.UserRole,plan.id)
            item.setToolTip(plan.error or text); self.items.addItem(item)
        if not plans:self.notice.setText(tr(self.lang,'schedule_empty'))

    def remove_selected(self):
        item=self.items.currentItem()
        if not item:return
        try:self.runtime.cancel_schedule(item.data(Qt.ItemDataRole.UserRole))
        except Exception as error:self.notice.setText(tr(self.lang,'schedule_error',error=str(error))); return
        self.refresh()


class CheckInDialog(QDialog):
    def __init__(self,parent,store):
        super().__init__(parent)
        self.parent_window=parent; self.store=store; self.lang=parent.lang
        self.setWindowTitle(tr(self.lang,'calendar')); self.resize(700,760)
        layout=QVBoxLayout(self); layout.setContentsMargins(26,24,26,24); layout.setSpacing(12)
        title=label(tr(self.lang,'calendar')); title.setStyleSheet('font-size:22px;font-weight:600')
        layout.addWidget(title); layout.addWidget(label(tr(self.lang,'calendar_intro'),True))
        self.stats=label(''); self.stats.setStyleSheet('font-size:17px;font-weight:600'); layout.addWidget(self.stats)
        self.level=label(''); self.level.setStyleSheet('font-size:14px;font-weight:600'); layout.addWidget(self.level)
        self.calendar=QCalendarWidget(); self.calendar.setGridVisible(False); self.calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.calendar.setLocale(locale_for(self.lang))
        tokens=parent.palette_tokens
        palette=self.calendar.palette()
        for role,value in ((QPalette.ColorRole.Base,tokens['field']),(QPalette.ColorRole.AlternateBase,tokens['card']),
                           (QPalette.ColorRole.Text,tokens['text']),(QPalette.ColorRole.WindowText,tokens['text']),
                           (QPalette.ColorRole.Highlight,tokens['glow']),(QPalette.ColorRole.HighlightedText,'#ffffff')):
            palette.setColor(role,QColor(value))
        self.calendar.setPalette(palette)
        weekday=QTextCharFormat(); weekday.setBackground(QColor(tokens['card'])); weekday.setForeground(QColor(tokens['muted']))
        for day in (Qt.DayOfWeek.Monday,Qt.DayOfWeek.Tuesday,Qt.DayOfWeek.Wednesday,Qt.DayOfWeek.Thursday,Qt.DayOfWeek.Friday,Qt.DayOfWeek.Saturday,Qt.DayOfWeek.Sunday):
            self.calendar.setWeekdayTextFormat(day,weekday)
        from desktop_visuals import icon
        for name,kind in (('qt_calendar_prevmonth','left'),('qt_calendar_nextmonth','right')):
            arrow=self.calendar.findChild(QToolButton,name)
            if arrow:arrow.setIcon(icon(kind,tokens['muted']))
        self.calendar.currentPageChanged.connect(self.paint_month); layout.addWidget(self.calendar,1)
        self.message=label(''); layout.addWidget(self.message)
        self.button=QPushButton(tr(self.lang,'checkin')); self.button.clicked.connect(self.check_in); layout.addWidget(self.button)
        theme_row=QHBoxLayout()
        self.theme_label=label(''); theme_row.addWidget(self.theme_label)
        self.skin_combo=QComboBox(); self.skin_combo.currentIndexChanged.connect(self.change_skin); theme_row.addWidget(self.skin_combo,1)
        layout.addLayout(theme_row)
        self.skin_notice=label('',True); layout.addWidget(self.skin_notice)
        self.tabs=QTabWidget()
        # Keep the reward cabinet visually in the same language as the rest of
        # the application.  The stock tab style varies across Windows themes
        # (and made the achievements page look like a separate, unthemed
        # panel), so the tab strip and its three list panes use the current
        # palette tokens explicitly.
        self.tabs.setObjectName('rewardTabs')
        self.badge_list=QListWidget(); self.gallery_list=QListWidget(); self.achievement_list=QListWidget()
        self.badge_list.setObjectName('rewardBadges')
        self.gallery_list.setObjectName('rewardGallery')
        self.achievement_list.setObjectName('rewardAchievements')
        self.tabs.addTab(self.badge_list,''); self.tabs.addTab(self.gallery_list,''); self.tabs.addTab(self.achievement_list,'')
        self._apply_reward_styles(tokens)
        layout.addWidget(self.tabs,1)
        layout.addWidget(label(tr(self.lang,'calendar_locked'),True))
        close=QPushButton(tr(self.lang,'ok')); close.clicked.connect(self.accept); layout.addWidget(close)
        self.refresh()

    def _apply_reward_styles(self, tokens):
        """Style the reward tabs/lists with stable hover and selected states.

        In particular, ``:selected:hover`` repeats the selected treatment so
        moving over the active tab or achievement row never produces a second
        (brighter or differently coloured) outline.  This also avoids the
        platform-dependent native tab background that previously made the
        achievements pane look unlike the badges and gallery panes.
        """
        glow=tokens['glow']; line=tokens['line']; field=tokens['field']
        card=tokens['card']; hover=tokens['hover']; text=tokens['text']; muted=tokens['muted']
        tab_bar=self.tabs.tabBar()
        tab_bar.setObjectName('rewardTabBar')
        self.tabs.setStyleSheet(f'''
            QTabWidget#rewardTabs {{
                background: transparent;
            }}
            QTabWidget#rewardTabs::pane {{
                background: {field};
                border: 1px solid {line};
                border-radius: 12px;
                top: -1px;
            }}
            QTabBar#rewardTabBar {{
                background: transparent;
            }}
            QTabBar#rewardTabBar::tab {{
                background: {card};
                color: {muted};
                border: 1px solid {line};
                border-radius: 8px;
                padding: 8px 14px;
                margin-right: 6px;
            }}
            QTabBar#rewardTabBar::tab:hover,
            QTabBar#rewardTabBar::tab:selected,
            QTabBar#rewardTabBar::tab:selected:hover {{
                background: {hover};
                color: {text};
                border-color: {glow};
            }}
            QListWidget#rewardBadges,
            QListWidget#rewardGallery,
            QListWidget#rewardAchievements {{
                background: {field};
                color: {text};
                border: 0;
                border-radius: 10px;
                padding: 7px;
                outline: 0;
            }}
            QListWidget#rewardBadges::item,
            QListWidget#rewardGallery::item,
            QListWidget#rewardAchievements::item {{
                background: {card};
                color: {text};
                border: 1px solid transparent;
                border-radius: 7px;
                padding: 9px;
                margin: 1px 0;
            }}
            QListWidget#rewardBadges::item:hover,
            QListWidget#rewardGallery::item:hover,
            QListWidget#rewardAchievements::item:selected,
            QListWidget#rewardAchievements::item:selected:hover,
            QListWidget#rewardBadges::item:selected,
            QListWidget#rewardBadges::item:selected:hover,
            QListWidget#rewardGallery::item:selected,
            QListWidget#rewardGallery::item:selected:hover {{
                background: {hover};
                color: {text};
                border-color: {glow};
            }}
        ''')
        # Re-polish after assigning the object name: Qt may have already
        # cached the selector during QTabWidget construction.
        tab_bar.style().unpolish(tab_bar)
        tab_bar.style().polish(tab_bar)

    def refresh(self):
        stats=self.store.checkin_stats()
        total=stats['total']; streak=stats['streak']
        self.stats.setText(tr(self.lang,'total_days',total=total)+'  ·  '+tr(self.lang,'streak_days',streak=streak))
        today=QDate.currentDate().toString('yyyy-MM-dd')
        dates=self.store.checkin_dates(QDate.currentDate().year(),QDate.currentDate().month())
        checked=today in dates
        self.button.setEnabled(not checked); self.button.setText(tr(self.lang,'checked' if checked else 'checkin'))
        milestone=total in (7,14,30,60,100,180,365) or (total>365 and total%100==0)
        self.message.setText(tr(self.lang,'milestone',total=total) if milestone else tr(self.lang,'affirmation'+str(total%3+1)))
        self.refresh_rewards()
        self.paint_month(self.calendar.yearShown(),self.calendar.monthShown())

    def _fill_reward_list(self, widget, definitions, profile, prefix):
        from feature_text import reward_tr
        widget.clear()
        unlocked=set(profile[prefix])
        key_prefix={'badges':'badge_','gallery':'gallery_','achievements':'achievement_'}[prefix]
        for identifier, metric, threshold in definitions:
            is_unlocked=identifier in unlocked
            name=reward_tr(self.lang,key_prefix+identifier)
            status=reward_tr(self.lang,'unlocked' if is_unlocked else 'locked')
            current=int(profile.get(metric,0))
            detail='' if is_unlocked else f' · {current}/{threshold}'
            item=QListWidgetItem(('✓ ' if is_unlocked else '🔒 ')+name+' · '+status+detail)
            item.setToolTip(name)
            widget.addItem(item)

    def refresh_rewards(self):
        from app_state import REWARD_BADGES, REWARD_GALLERY, REWARD_ACHIEVEMENTS, REWARD_SKINS
        from feature_text import reward_tr
        profile=self.store.reward_profile()
        level_name=reward_tr(self.lang,'level_'+profile['level'])
        next_text='' if profile['next_level'] is None else reward_tr(self.lang,'progress',count=max(0,profile['next_level']-profile['total']))
        self.level.setText(f"{reward_tr(self.lang,'level')} · {level_name}  |  {reward_tr(self.lang,'downloads',files=profile['downloaded_files'],sessions=profile['download_sessions'])}  {next_text}")
        self.theme_label.setText(reward_tr(self.lang,'themes'))
        self.tabs.setTabText(0,reward_tr(self.lang,'badges'))
        self.tabs.setTabText(1,reward_tr(self.lang,'gallery'))
        self.tabs.setTabText(2,reward_tr(self.lang,'achievements'))
        self._fill_reward_list(self.badge_list,REWARD_BADGES,profile,'badges')
        self._fill_reward_list(self.gallery_list,REWARD_GALLERY,profile,'gallery')
        self._fill_reward_list(self.achievement_list,REWARD_ACHIEVEMENTS,profile,'achievements')
        selected=profile['selected_skin']; unlocked=set(profile['skins'])
        self.skin_combo.blockSignals(True); self.skin_combo.clear()
        for identifier,_threshold in REWARD_SKINS:
            if identifier in unlocked:
                self.skin_combo.addItem(reward_tr(self.lang,'skin_'+identifier),identifier)
        index=self.skin_combo.findData(selected)
        if index>=0:self.skin_combo.setCurrentIndex(index)
        self.skin_combo.blockSignals(False)
        if len(unlocked)==1:
            self.skin_notice.setText(reward_tr(self.lang,'theme_locked'))
        else:
            self.skin_notice.setText(reward_tr(self.lang,'theme_saved')+' · '+reward_tr(self.lang,'skin_'+selected))

    def change_skin(self,index):
        if index<0:return
        skin=self.skin_combo.itemData(index)
        if not skin:return
        apply_skin=getattr(self.parent_window,'apply_reward_skin',None)
        if callable(apply_skin) and not apply_skin(skin):
            self.refresh_rewards(); return
        try:self.store.set_reward_skin(skin)
        except (ValueError,TypeError):return
        self.refresh_rewards()

    def paint_month(self,year,month):
        self.calendar.setDateTextFormat(QDate(),QTextCharFormat())
        mark=QTextCharFormat(); mark.setBackground(QColor('#8963cf')); mark.setForeground(QColor('#ffffff'))
        for day in self.store.checkin_dates(year,month):
            date=QDate.fromString(str(day),'yyyy-MM-dd')
            if date.isValid():self.calendar.setDateTextFormat(date,mark)

    def check_in(self):
        self.store.check_in(); self.refresh()
        refresh=getattr(self.parent_window,'refresh_reward_badge',None)
        if callable(refresh):refresh()
