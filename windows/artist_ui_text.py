"""Shared copy for the two artist browser dialogs."""
from feature_text import LANGUAGES
from ui_i18n import t


COPY = {
    '画师更新订阅': ('畫師更新訂閱', 'Artist subscriptions', '作家の更新購読'),
    '后台检查最多同时访问 3 位画师；新画集会显示红点提醒。': ('背景檢查最多同時存取 3 位畫師；新圖集以紅點提醒。', 'Checks visit at most 3 artists at a time. New collections show a red dot.', '最大3名の作家を同時に確認し、新しいコレクションを赤い点でお知らせします。'),
    '输入 Pawchive 画师主页或数字 ID': ('輸入 Pawchive 畫師首頁或數字 ID', 'Enter a Pawchive artist URL or numeric ID', 'Pawchiveの作家URLまたは数字のIDを入力'),
    '添加': ('新增', 'Add', '追加'),
    '检查更新': ('檢查更新', 'Check updates', '更新を確認'),
    '删除选中': ('刪除選取項目', 'Remove selected', '選択した作家を削除'),
    '还没有订阅画师。添加一个主页后会在后台检查更新。': ('還沒有訂閱畫師。新增首頁後會在背景檢查更新。', 'No artist subscriptions yet. Add a profile to check for updates in the background.', '購読している作家はいません。プロフィールを追加すると、バックグラウンドで更新を確認します。'),
    '有更新': ('有更新', 'New collections', '更新あり'),
    '未知': ('未知', 'Unknown', '不明'),
    '最新记录：{date}': ('最新紀錄：{date}', 'Latest record: {date}', '最新の記録：{date}'),
    '无法添加': ('無法新增', 'Could not add artist', '作家を追加できません'),
    '请输入完整的 Pawchive 画师主页或数字 ID。': ('請輸入完整的 Pawchive 畫師首頁或數字 ID。', 'Enter a complete Pawchive artist URL or numeric ID.', 'Pawchiveの作家URL全体または数字のIDを入力してください。'),
    '正在后台检查订阅…': ('正在背景檢查訂閱…', 'Checking subscriptions in the background…', '購読の更新をバックグラウンドで確認中…'),
    '检查完成。': ('檢查完成。', 'Check complete.', '確認が完了しました。'),
    '{count} 位画师暂时无法访问。': ('{count} 位畫師暫時無法存取。', '{count} artists are temporarily unavailable.', '{count} 名の作家を一時的に確認できません。'),
    '添加画师': ('新增畫師', 'Add artist', '作家を追加'),
    '上一页': ('上一頁', 'Previous', '前へ'),
    '下一页': ('下一頁', 'Next', '次へ'),
    '数据迁移': ('資料遷移', 'Transfer data', 'データ移行'),
    '已记录': ('已記錄', 'Recorded', '記録済み'),
    '下载画师图集后会生成档案卡，也可以手动添加画师。': ('下載畫師圖集後會建立檔案卡，也可以手動新增畫師。', 'Downloading an artist’s collections creates a profile card. You can also add an artist manually.', '作家のコレクションをダウンロードするとカードが作成されます。手動で追加することもできます。'),
    '正在检查画师更新…': ('正在檢查畫師更新…', 'Checking artist updates…', '作家の更新を確認中…'),
    '有新图集，点击查看更新': ('有新圖集，點擊查看更新', 'New collections — click to view', '新しいコレクションがあります。クリックして確認'),
    '下载画师图集': ('下載畫師圖集', 'Download artist collections', '作家のコレクションをダウンロード'),
    '是否扫描该画师的全部图集并选择下载？': ('要掃描這位畫師的全部圖集並選擇下載嗎？', 'Scan this artist’s collections and choose what to download?', 'この作家のコレクションをスキャンし、ダウンロードする作品を選択しますか？'),
}


def artist_text(language, source, **values):
    index = LANGUAGES.index(language) if language in LANGUAGES else 0
    text = source if index == 0 else COPY.get(source, (source, source, source))[index - 1]
    if source not in COPY:
        return t(language, source, **values)
    return text.format(**values)
