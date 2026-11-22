"""Immutable, GUI-thread download naming; network discovery never assigns numbers."""
from pathlib import Path
import re
from urllib.parse import unquote,urljoin,urlparse,parse_qs

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QThread,Signal

from download_journal import original_url
from resumable_worker import ATTACHMENT_FOLDER
from subscriptions import parse_site_datetime


def clean_component(value,fallback='file',limit=100):
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f\ud800-\udfff]','_',str(value or '')).strip(' .') or fallback
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:[ .]|$)',value,re.I):value='_'+value
    suffix=Path(value).suffix
    return value[:max(1,limit-len(suffix))].rstrip(' .')+suffix if len(value)>limit else value


def extract_assets(html,post_url):
    soup=BeautifulSoup(html,'html.parser');assets=[];seen=set()
    for anchor in soup.select('.post__files a.fileThumb[href],a.post__attachment-link[href]'):
        url=urljoin(post_url,str(anchor.get('href') or '').strip())
        if not original_url(url) or url in seen:continue
        seen.add(url);direct='post__attachment-link' in (anchor.get('class') or [])
        fallback=f'attachment_{len(assets)+1}.bin' if direct else f'image_{len(assets)+1}.jpg'
        choices=[anchor.get('download'),(parse_qs(urlparse(url).query).get('f') or [''])[0],
                 re.sub(r'^Download\s+','',anchor.get_text(' ',strip=True),flags=re.I),Path(unquote(urlparse(url).path)).name]
        name=next((str(value).strip() for value in choices if str(value or '').strip()),fallback)
        assets.append(dict(url=url,name=clean_component(unquote(name),fallback,180),direct=direct,index=len(assets)))
    return assets


def assign_plans(entries,config):
    """Pure bounded CPU work, invoked once by the GUI before journaling/start."""
    result=[dict(entry) for entry in entries]
    selected=[]
    for index,entry in enumerate(result):
        mode=entry.get('content_mode') or config.get('content_mode','all')
        if not isinstance(entry.get('assets'),list):raise ValueError('Collection file manifest is incomplete.')
        assets=[];seen=set()
        for asset in entry['assets']:
            if not original_url(asset.get('url')):raise ValueError('Invalid original file URL.')
            if asset['url'] in seen:continue
            seen.add(asset['url'])
            if mode=='all' or bool(asset.get('direct'))==(mode=='attachments'):assets.append(dict(asset))
        selected.append(assets)
    images_only=not any(asset.get('direct') for assets in selected for asset in assets)
    numbered=bool(config.get('queue_rename_order',False));by_title=bool(config.get('by_title',True))
    sequence=0;used_targets=set();used_titles={};used_title_dirs=set()
    order=sorted(range(len(result)),key=lambda i:(parse_site_datetime(result[i].get('date')) or 0,i))
    for index in order:
        entry=result[index];title=clean_component(entry.get('title'),'Album',100)
        if by_title:
            key=title.casefold();occurrence=used_titles.get(key,0)+1;used_titles[key]=occurrence
            original_title=title
            if occurrence>1:title=original_title[:88]+f' ({occurrence})'
            # A real title such as "Album (2)" can collide with a suffix
            # reserved for another post titled "Album". Reserve the final
            # directory too, so distinct albums never share their numbering.
            while title.casefold() in used_title_dirs:
                occurrence+=1;title=original_title[:88]+f' ({occurrence})'
            used_titles[key]=occurrence;used_title_dirs.add(title.casefold())
        album_sequence=0;plan=[]
        for asset in sorted(selected[index],key=lambda a:int(a.get('index',0))):
            direct=bool(asset.get('direct'));name=clean_component(asset.get('name'),'file',180)
            suffix=Path(name).suffix.lower();number=None
            if not direct and numbered:
                sequence+=1;album_sequence+=1;number=album_sequence if by_title else sequence
                name=f'{number}{suffix or ".jpg"}'
            if by_title:relative=Path(title)/(ATTACHMENT_FOLDER if direct else '')
            else:relative=Path('')
            target=str(relative/name)
            # Reserve every name on the main thread, including original-name
            # collisions; workers must never make their own suffix decisions.
            original=name;collision=1
            while target.casefold() in used_targets:
                collision+=1;name=clean_component(f'{Path(original).stem} ({collision}){Path(original).suffix}',limit=180)
                target=str(relative/name)
            used_targets.add(target.casefold())
            plan.append(dict(url=str(asset['url']),name=str(asset.get('name') or name),save_name=name,
                relative_dir=str(relative),direct=direct,number=number,index=int(asset.get('index',0)),
                date=str(entry.get('date') or ''),post_url=str(entry['url']),title=str(entry.get('title') or title)))
        entry.update(static_plan=plan,static_numbering=numbered,image_only_layout=images_only,
                     file_count=len(plan),direct=any(a['direct'] for a in plan))
    return result


class ManifestFetchWorker(QThread):
    result=Signal(object)
    failed=Signal(str)
    progress=Signal(int,int)

    def __init__(self,entries,parent=None):
        super().__init__(parent);self.entries=[dict(item) for item in entries]

    def run(self):
        try:
            from link_utils import parse_url
            from popular_features import _read_bytes, MAX_HTML_BYTES
            with requests.Session() as session:
                session.headers['User-Agent']='PawchiveDownloader/1.3'
                for index,entry in enumerate(self.entries):
                    if self.isInterruptionRequested():raise OSError('cancelled')
                    if not isinstance(entry.get('assets'),list):
                        link = parse_url(entry.get('url'))
                        if link is None or not link.is_post:raise ValueError('Invalid collection URL.')
                        payload,encoding=_read_bytes(session,link.post_url,self,MAX_HTML_BYTES,subdomains=False)
                        html=payload.decode(encoding,'replace')
                        entry['assets']=extract_assets(html,link.post_url)
                        soup=BeautifulSoup(html,'html.parser')
                        title=soup.select_one('.post__title')
                        stamp=soup.select_one('time[datetime]')
                        if not entry.get('title'):entry['title']=title.get_text(' ',strip=True) if title else entry['url'].rstrip('/').rsplit('/',1)[-1]
                        if not entry.get('date') and stamp:entry['date']=stamp.get('datetime','')
                    self.progress.emit(index+1,len(self.entries))
            self.result.emit(self.entries)
        except Exception as error:self.failed.emit(str(error))
