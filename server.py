"""CaseLens V10: validated public profiles + per-post Highlights + training estimates."""
import json
import base64
import hmac
import secrets
import time
import os
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from estimator import estimate, scenario_profile
from audience import estimate_audience, normalized_audience, MARKETS, origin_context

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv('CASELENS_DB_PATH', str(ROOT / 'history.sqlite3')))
CONF = ROOT / 'settings.json'
PORT = int(os.getenv('PORT') or os.getenv('CASELENS_PORT') or ('3000' if os.getenv('REPL_ID') or os.getenv('CASELENS_CLOUD') == '1' else '8765'))
HOST = '0.0.0.0' if os.getenv('REPL_ID') or os.getenv('CASELENS_CLOUD') == '1' else '127.0.0.1'
DATABASE_URL = os.getenv('DATABASE_URL', '')
JOBS = {}
# Bright Data dataset IDs from provider's public official docs; may evolve.
BD_POST_DATASETS = {'instagram':'gd_lk5ns7kz21pck8jpis', 'facebook':'gd_lkaxegm826bjpoo9m5', 'tiktok':'gd_lu702nij2f790tmv9h', 'linkedin':'gd_lyy3tktm25m4avu764'}
BD_DATASETS = {'youtube': 'gd_lk538t2k2p1k3oos71', 'tiktok': 'gd_l1villgoiiidt09ci', 'facebook': 'gd_mf124a0511bauquyow', 'instagram':'gd_l1vikfch901nx3by4', 'linkedin':'gd_l1vikfnt1wgvvqz95w'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def config():
    if not CONF.exists():
        return {}
    with open(CONF, encoding='utf-8') as f:
        return json.load(f)


def parse_account(raw):
    raw = raw.strip()
    if not raw:
        raise ValueError('请输入账号主页链接')
    if not re.match(r'^https?://', raw, re.I):
        raw = 'https://' + raw
    u = urlparse(raw)
    host = u.hostname.lower().removeprefix('www.').removeprefix('m.') if u.hostname else ''
    bits = [p for p in u.path.split('/') if p]
    if host in ('youtube.com', 'youtube-nocookie.com'):
        if len(bits) >= 2 and bits[0] in ('channel', 'user'):
            ident = bits[1]
            kind = bits[0]
        elif bits and bits[0].startswith('@'):
            ident = bits[0].lstrip('@')
            kind = 'handle'
        else:
            raise ValueError('YouTube 请使用 /@账号、/channel/UC... 或 /user/... 的频道主页链接')
        if not re.fullmatch(r'[\w.\-]{2,100}', ident):
            raise ValueError('YouTube 账号格式不正确')
        return {'platform': 'youtube', 'id': ident, 'kind': kind, 'url': f'https://www.youtube.com/{"@" + ident if kind == "handle" else kind + "/" + ident}'}
    if host in ('xiaohongshu.com', 'xiaohongshu.cn'):
        # User profiles only; /explore/ and /discovery/item/ are posts, not account URLs.
        if len(bits) >= 3 and bits[0].lower() == 'user' and bits[1].lower() == 'profile' and re.fullmatch(r'[A-Za-z0-9_-]{8,80}', bits[2]):
            ident=bits[2]
            return {'platform':'xiaohongshu','id':ident,'kind':'user_profile','url':'https://www.xiaohongshu.com/user/profile/'+ident}
        raise ValueError('小红书请使用账号主页完整链接，例如 https://www.xiaohongshu.com/user/profile/账号ID；笔记链接和短链暂不支持。')
    if host in ('linkedin.com', 'cn.linkedin.com'):
        if len(bits) >= 2 and bits[0] in ('company', 'showcase') and re.fullmatch(r'[\w.\-]{2,100}', bits[1]):
            return {'platform': 'linkedin', 'id': bits[1], 'kind': 'company', 'url': 'https://www.linkedin.com/company/' + bits[1] + '/'}
        raise ValueError('LinkedIn 请使用 /company/公司名称 的公司主页链接；个人账号的后台指标需单独授权。')
    if host in ('instagram.com','instagr.am'):
        if not bits or bits[0].lower() in ('p','reel','reels','stories','explore','accounts','direct'):
            raise ValueError('Instagram 请使用 https://www.instagram.com/账号/ 的主页链接')
        ident=bits[0].lstrip('@')
        if not re.fullmatch(r'[A-Za-z0-9._]{2,30}', ident):
            raise ValueError('Instagram 用户名格式不正确')
        return {'platform':'instagram','id':ident,'kind':'handle','url':'https://www.instagram.com/'+ident.lower()+'/'}
    if host == 'tiktok.com':
        if not bits or not bits[0].startswith('@'):
            raise ValueError('TikTok 请使用 /@账号 的主页链接')
        ident = bits[0][1:]
        if not re.fullmatch(r'[\w.]{2,50}', ident):
            raise ValueError('TikTok 用户名格式不正确')
        return {'platform': 'tiktok', 'id': ident, 'kind': 'handle', 'url': 'https://www.tiktok.com/@' + ident}
    if host in ('facebook.com', 'fb.com', 'web.facebook.com'):
        ident = parse_qs(u.query).get('id', [''])[0] if bits and bits[0] == 'profile.php' else bits[0] if bits else ''
        if not re.fullmatch(r'[\w.\-]{2,140}', ident) or ident in ('watch', 'reel', 'share', 'groups'):
            raise ValueError('Facebook 请使用 Page 主页链接')
        return {'platform': 'facebook', 'id': ident, 'kind': 'page', 'url': ('https://www.facebook.com/profile.php?id='+ident if bits and bits[0]=='profile.php' else f'https://www.facebook.com/{ident}')}
    raise ValueError('请使用 YouTube / TikTok / Instagram / Facebook / LinkedIn / 小红书 账号主页链接')


def get_json(url, token=None):
    headers = {'User-Agent': 'CaseLens/2.0', 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=14) as res:
            return json.loads(res.read(2_000_000))
    except HTTPError as e:
        try:
            error_text = e.read(1000).decode('utf-8', 'replace')
        except Exception:
            error_text = ''
        raise ValueError(f'平台接口返回 {e.code}：请检查 API 凭据、权限及调用配额。{error_text[:160]}') from e
    except URLError as e:
        raise ValueError('无法连接平台 API，请确认网络能访问对应平台：' + str(e.reason)) from e


def yt_fetch(a, cfg):
    key = os.getenv('YOUTUBE_API_KEY') or cfg.get('youtube_api_key', '')
    if not key:
        raise ValueError('YouTube 公开统计需要配置 API Key。请复制 settings.example.json 为 settings.json 并填写 youtube_api_key。')
    filt = {'handle': 'forHandle', 'channel': 'id', 'user': 'forUsername'}[a['kind']]
    base = 'https://www.googleapis.com/youtube/v3/'
    q = {'part': 'snippet,statistics,contentDetails', filt: ('@' + a['id'] if filt == 'forHandle' else a['id']), 'key': key}
    items = get_json(base + 'channels?' + urlencode(q)).get('items', [])
    if not items:
        raise ValueError('YouTube 未找到此频道；请检查账号链接是否正确')
    c = items[0]
    s = c.get('statistics', {})
    snippet = c.get('snippet', {})
    stats = {'followers': int(s['subscriberCount']) if not s.get('hiddenSubscriberCount') and s.get('subscriberCount') is not None else None,
             'total_views': int(s['viewCount']) if s.get('viewCount') else None,
             'video_count': int(s['videoCount']) if s.get('videoCount') else 0}
    posts = []
    playlist = c.get('contentDetails', {}).get('relatedPlaylists', {}).get('uploads')
    if playlist:
        v = get_json(base + 'playlistItems?' + urlencode({'part': 'snippet', 'playlistId': playlist, 'maxResults': 25, 'key': key}))
        ids = [it.get('snippet', {}).get('resourceId', {}).get('videoId') for it in v.get('items', [])]
        ids = [i for i in ids if i]
        if ids:
            vv = get_json(base + 'videos?' + urlencode({'part': 'snippet,statistics', 'id': ','.join(ids), 'key': key}))
            for item in vv.get('items', []):
                st = item.get('statistics', {})
                sn = item.get('snippet', {})
                posts.append({'title': sn.get('title',''), 'date': sn.get('publishedAt', '')[:10], 'views': int(st['viewCount']) if st.get('viewCount') else None, 'likes': int(st['likeCount']) if st.get('likeCount') else None,
                              'comments': int(st['commentCount']) if st.get('commentCount') else None, 'url': 'https://www.youtube.com/watch?v=' + item['id']})
    return {'platform': 'youtube', 'name': snippet.get('title', a['id']), 'url': 'https://www.youtube.com/channel/' + c['id'],
            'avatar': snippet.get('thumbnails', {}).get('default', {}).get('url'), 'stats': stats, 'posts': posts,
            'source': 'YouTube Data API（公开频道指标）', 'quality': 'public', 'notes': ['订阅量可能经过平台四舍五入。', '累计播放量不是近28天播放量；近28天实际播放量需频道 Analytics 授权。']}


def tk_fetch(a, cfg):
    # Display API supports a creator who authorized this app, not arbitrary third-party handles.
    tk = cfg.get('tiktok', {})
    token = os.getenv('TIKTOK_ACCESS_TOKEN') or tk.get('access_token', '')
    if not token:
        raise ValueError('TikTok 官方 Display API 只能读取已授权账号。请配置 tiktok.access_token（需要 user.info.stats 等授权范围）。')
    url = 'https://open.tiktokapis.com/v2/user/info/?fields=username,display_name,avatar_url,follower_count,likes_count,video_count'
    data = get_json(url, token).get('data', {}).get('user', {})
    if not data:
        raise ValueError('TikTok API 未返回账号数据，请检查授权范围')
    handle = str(data.get('username') or '').lstrip('@')
    if not handle or handle.lower() != a['id'].lower():
        raise ValueError(f'当前 TikTok Token 属于 @{handle or "未知"}，与输入的 @{a["id"]} 不匹配；不能用另一个账号的数据替代')
    return {'platform': 'tiktok', 'name': data.get('display_name') or handle, 'url': a['url'], 'avatar': data.get('avatar_url'),
            'stats': {'followers': data.get('follower_count'), 'total_likes': data.get('likes_count'), 'video_count': data.get('video_count')},
            'posts': [], 'source': 'TikTok Display API（账号授权数据）', 'quality': 'authorized', 'notes': ['仅提供当前授权可取得的账号统计；历史增长、受众等指标需要对应额外权限或报表。']}


def fb_fetch(a, cfg):
    pages = cfg.get('facebook_pages', {})
    entry = pages.get(a['id']) or pages.get(a['url'])
    if not entry and os.getenv('FACEBOOK_PAGE_ID') and os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN'):
        entry={'page_id':os.getenv('FACEBOOK_PAGE_ID'), 'access_token':os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN')}
    if not entry:
        raise ValueError('Facebook 只能读取配置过授权的 Page。请在 settings.json 的 facebook_pages 中添加该账号映射及 Page Access Token。')
    pid = str(entry.get('page_id') or '')
    token = entry.get('access_token') or ''
    if not re.fullmatch(r'[0-9]{5,40}', pid) or not token:
        raise ValueError('Facebook 配置缺少合法的 Page ID 或 Access Token')
    ver = 'v23.0'  # Endpoint version may need updating as Meta deprecates old versions.
    fields = 'id,name,username,fan_count,followers_count,link,picture'
    url = f'https://graph.facebook.com/{ver}/{pid}?' + urlencode({'fields': fields})
    data = get_json(url, token)
    if str(data.get('id')) != pid:
        raise ValueError('Facebook 返回了不匹配的 Page ID')
    ident=a['id'].lower()
    allowed={pid.lower(),str(data.get('username') or '').lower()}
    link=str(data.get('link') or '')
    if link:
        try:allowed.add(urlparse(link).path.strip('/').split('/')[0].lower())
        except Exception:pass
    if ident not in allowed:
        raise ValueError('Facebook Page Token 对应的主页与输入链接不一致。')
    return {'platform': 'facebook', 'name': data.get('name', a['id']), 'url': data.get('link', a['url']),
            'avatar': (data.get('picture') or {}).get('data', {}).get('url'),
            'stats': {'followers': data.get('followers_count'), 'fans': data.get('fan_count')}, 'posts': [],
            'source': 'Meta Graph API（Page 授权数据）', 'quality': 'authorized', 'notes': ['Page Insights / 受众与广告指标需要额外页面权限。']}


def _ig_audience_from_insights(ig_user_id, token, api_base):
    """Read optional aggregate demographics on the authorized account only."""
    groups={}
    for breakdown, dest in [('country','countries'),('gender','genders'),('age','ages')]:
        url=api_base+f'/{ig_user_id}/insights?'+urlencode({
            'metric':'follower_demographics','period':'lifetime','metric_type':'total_value',
            'breakdown':breakdown,'timeframe':'last_30_days'})
        try:
            obj=get_json(url, token)
            arr=obj.get('data',[])
            if not arr:continue
            breakdowns=arr[0].get('total_value',{}).get('breakdowns',[])
            if not breakdowns:continue
            rows=breakdowns[0].get('results',[])
            converted=[]
            for row in rows:
                label=(row.get('dimension_values') or [''])[0]
                value=row.get('value')
                try:value=int(value)
                except (TypeError,ValueError):continue
                if label and value>=0:
                    if dest=='genders':label={'F':'女性','M':'男性','U':'其他'}.get(str(label).upper(),str(label))
                    converted.append({'name':str(label),'value':value})
            if converted:groups[dest]=converted
        except (ValueError,KeyError,IndexError):
            pass
    return normalized_audience(groups)


def ig_fetch(a,cfg):
    token=os.getenv('INSTAGRAM_ACCESS_TOKEN','').strip()
    uid=os.getenv('INSTAGRAM_IG_USER_ID','').strip()
    if not token or not uid:
        raise ValueError('Instagram 官方读取需要专业账号授权、INSTAGRAM_ACCESS_TOKEN 和 INSTAGRAM_IG_USER_ID。')
    if not re.fullmatch(r'[0-9]{5,30}',uid):
        raise ValueError('INSTAGRAM_IG_USER_ID 必须为数字用户 ID')
    # Instagram Login access token: do not confuse with a Facebook Page token.
    base='https://graph.instagram.com/v26.0'
    url=base+'/'+uid+'?'+urlencode({'fields':'id,username,name,followers_count,media_count,profile_picture_url'})
    d=get_json(url, token)
    username=str(d.get('username') or '').lstrip('@')
    if username.lower()!=a['id'].lower():
        raise ValueError('已授权的 Instagram 账号与输入链接不匹配，不能用其他账号的数据替代。')
    audience=_ig_audience_from_insights(uid,token,base)
    return {'platform':'instagram','name':d.get('name') or username, 'url':a['url'],
            'avatar':d.get('profile_picture_url'),
            'stats':{'followers':d.get('followers_count'),'video_count':d.get('media_count')},
            'posts':[],'quality':'authorized','source':'Instagram API（授权专业账号）',
            'audience':dict(audience,source='authorized') if audience else None,
            'notes':['粉丝画像若由 Meta 返回，则为授权账号的聚合 Insights；无法返回的维度会使用培训情景补齐。']}


def integration_status(cfg):
    bd=bool(os.getenv('BRIGHTDATA_API_TOKEN'))
    return {'xiaohongshu':{'official':False,'public':False,'import':True,'reason':'创作者分析尚无通用开放接口；支持后台报表导入与培训情景'},
            'youtube':{'official':bool(os.getenv('YOUTUBE_API_KEY') or cfg.get('youtube_api_key')),'public':False},
            'instagram':{'official':bool(os.getenv('INSTAGRAM_ACCESS_TOKEN') and os.getenv('INSTAGRAM_IG_USER_ID')),'public':bd},
            'facebook':{'official':bool(cfg.get('facebook_pages') or (os.getenv('FACEBOOK_PAGE_ID') and os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN'))),'public':bd},
            'tiktok':{'official':bool(os.getenv('TIKTOK_ACCESS_TOKEN') or cfg.get('tiktok',{}).get('access_token')),'public':bd},
            'linkedin':{'official':False,'public':bd}}

def db_conn():
    if DATABASE_URL:
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError('PostgreSQL requires psycopg. Please install requirements.txt') from e
        conn = psycopg.connect(DATABASE_URL, connect_timeout=8)
        conn.execute('CREATE TABLE IF NOT EXISTS snapshots (platform TEXT NOT NULL, account TEXT NOT NULL, fetched_at TEXT NOT NULL, stats TEXT NOT NULL, PRIMARY KEY(platform,account,fetched_at))')
        conn.execute('CREATE TABLE IF NOT EXISTS cases (url TEXT PRIMARY KEY, platform TEXT NOT NULL, account TEXT NOT NULL, name TEXT, payload TEXT NOT NULL, updated_at TEXT NOT NULL)')
        conn.commit()
    else:
        DB.parent.mkdir(exist_ok=True, parents=True)
        conn = sqlite3.connect(str(DB), timeout=6)
        conn.execute('CREATE TABLE IF NOT EXISTS snapshots (platform TEXT NOT NULL, account TEXT NOT NULL, fetched_at TEXT NOT NULL, stats TEXT NOT NULL, PRIMARY KEY(platform,account,fetched_at))')
        conn.execute('CREATE TABLE IF NOT EXISTS cases (url TEXT PRIMARY KEY, platform TEXT NOT NULL, account TEXT NOT NULL, name TEXT, payload TEXT NOT NULL, updated_at TEXT NOT NULL)')
    return conn


def sql(conn, stmt, args=()):
    if DATABASE_URL:
        stmt = stmt.replace('?', '%s')
    return conn.execute(stmt, args)


def retain_verified_public(a,result):
    """Keep a user's source-checked public facts, without overwriting fresh API facts."""
    try:
        with db_conn() as c:
            row=sql(c,'SELECT payload FROM cases WHERE platform=? AND account=? ORDER BY updated_at DESC LIMIT 1',(a['platform'],a['id'].lower())).fetchone()
        if not row:return result
        old=json.loads(row[0]); facts=old.get('manual_verified') or {}
        if not facts:return result
        current=result.setdefault('stats',{})
        for k in ('followers','total_likes','video_count'):
            if current.get(k) is None and facts.get(k) is not None:current[k]=facts[k]
        if not result.get('account_country') and facts.get('account_country'):
            result['account_country']=facts['account_country']
        result['manual_verified']=facts
        result['manual_verified_at']=old.get('manual_verified_at')
    except (OSError,ValueError,TypeError):pass
    return result

def retain_imported_report(a, result):
    # Preserve manually imported time-bound analytics when refreshing public profiles.
    try:
        with db_conn() as conn:
            row=sql(conn, 'SELECT payload FROM cases WHERE platform=? AND account=? ORDER BY updated_at DESC LIMIT 1', (a['platform'], a['id'].lower())).fetchone()
        if row:
            previous=json.loads(row[0])
            if previous.get('report_source') or previous.get('analytics') or (previous.get('series') and not previous.get('estimate_model')):
                for k in ('analytics','series','audience','report_start','report_end','report_source','report_notes'):
                    if k in previous:
                        result[k]=previous[k]
    except (OSError,ValueError,TypeError):
        pass
    return result


def save_case(a, result):
    saved = dict(result)
    if saved.get('quality')!='imported':
        saved.pop('history', None)  # verified API history stored separately
    with db_conn() as c:
        sql(c, 'INSERT INTO cases(url,platform,account,name,payload,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET name=excluded.name,payload=excluded.payload,updated_at=excluded.updated_at',
            (a['url'], a['platform'], a['id'].lower(), saved.get('name') or a['id'], json.dumps(saved, ensure_ascii=False), now()))


def list_cases():
    with db_conn() as c:
        rows = sql(c, 'SELECT url,platform,account,name,payload,updated_at FROM cases ORDER BY updated_at DESC LIMIT 100').fetchall()
    res=[]
    for url, plat, acct, name, payload, updated_at in rows:
        obj=json.loads(payload)
        aobj=obj.get('audience')
        if (not aobj or (aobj.get('source')=='training-scenario' and aobj.get('version',0)<9)) and not obj.get('report_source'):
            try:obj['audience']=estimate_audience(parse_account(url),(obj.get('estimate_model') or {}).get('assumptions'),obj)
            except ValueError:pass
        obj['history']=read_history(plat, acct) or obj.get('history', [])
        res.append({'url':url, 'platform':plat, 'name':name, 'data':obj})
    return res


def read_history(plat, acct):
    with db_conn() as c:
        rows = sql(c, 'SELECT fetched_at,stats FROM snapshots WHERE platform=? AND account=? ORDER BY fetched_at DESC LIMIT 180', (plat,acct.lower())).fetchall()
    return [{'date':stamp, 'stats':json.loads(st)} for stamp,st in reversed(rows)]


def store_and_history(a, stats):
    with db_conn() as c:
        sql(c, 'INSERT INTO snapshots (platform,account,fetched_at,stats) VALUES (?,?,?,?) ON CONFLICT(platform,account,fetched_at) DO UPDATE SET stats=excluded.stats',
            (a['platform'], a['id'].lower(), now(), json.dumps(stats)))
        rows = sql(c, 'SELECT fetched_at, stats FROM snapshots WHERE platform=? AND account=? ORDER BY fetched_at DESC LIMIT 180', (a['platform'], a['id'].lower())).fetchall()
    return [{'date': stamp, 'stats': json.loads(s)} for stamp, s in reversed(rows)]



def safe_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (float, int)) and value >= 0:
        return int(value)
    if isinstance(value, str):
        t = value.strip().lower().replace(',', '')
        mt = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([kmb万亿]?)(?:\s+(?:followers|subscribers|likes|views|粉丝|订阅|播放))?', t)
        if mt:
            mul = {'': 1, 'k': 1_000, 'm': 1_000_000, 'b': 1_000_000_000, '万': 10_000, '亿': 100_000_000}
            return int(float(mt.group(1)) * mul[mt.group(2)])
    return None


def get_first(d, *names):
    for key in names:
        val=d.get(key)
        if val not in (None, ''):
            return val
    return None


def numeric_field(d, *names):
    for key in names:
        val=safe_number(d.get(key))
        if val is not None:
            return val
    return None


def bd_call(url, data=None):
    tok=os.getenv('BRIGHTDATA_API_TOKEN', '').strip()
    if not tok:
        raise ValueError('未配置公开数据采集凭据 BRIGHTDATA_API_TOKEN。只输入陌生 TikTok / Facebook 链接无法直接获得官方后台数据。')
    hdr={'Authorization': 'Bearer '+tok, 'Accept': 'application/json', 'User-Agent': 'CaseLens/3.0'}
    raw = None
    if data is not None:
        raw=json.dumps(data).encode('utf-8')
        hdr['Content-Type']='application/json'
    req=Request(url, data=raw, headers=hdr, method='POST' if data is not None else 'GET')
    try:
        with urlopen(req, timeout=25) as r:
            return json.loads(r.read(5_000_000))
    except HTTPError as ex:
        raise ValueError('公开数据提供商接口错误 HTTP '+str(ex.code)+'；检查账号配额或数据服务权限。') from ex
    except URLError as ex:
        raise ValueError('公开数据服务暂时无法连接：'+str(ex.reason)) from ex


def _bd_start(ds, inputs, discovery=None, limit=18):
    """One bounded Bright Data job. Discover doors require input+limit_per_input body."""
    query={'dataset_id':ds,'format':'json'}
    if discovery:
        query.update({'type':'discover_new','discover_by':discovery})
    endpoint='https://api.brightdata.com/datasets/v3/trigger?'+urlencode(query)
    body={'input':inputs,'limit_per_input':limit} if discovery else {'input':inputs}
    result=bd_call(endpoint,body)
    sid=result.get('snapshot_id') if isinstance(result,dict) else None
    if not isinstance(sid,str) or not re.fullmatch(r'sd?_[a-zA-Z0-9_\-]{5,120}', sid):
        raise ValueError('采集服务未返回有效任务编号；核对数据集、调用额度与输入字段。')
    return sid


def bd_trigger(a, base_result=None):
    jobs={}
    if base_result is None:
        ds=os.getenv('BRIGHTDATA_'+a['platform'].upper()+'_DATASET_ID', BD_DATASETS[a['platform']])
        url=(a['url'].rstrip('/')+'/about') if a['platform']=='youtube' else a['url']
        inp={'url':url}
        if a['platform']=='tiktok':inp['country']=''
        profile_sid=_bd_start(ds,[inp])
        jobs['profile']=profile_sid
    warning=None
    if a['platform'] in BD_POST_DATASETS and os.getenv('CASELENS_FETCH_POSTS','1')!='0':
        try:
            ds_posts=os.getenv('BRIGHTDATA_'+a['platform'].upper()+'_POSTS_DATASET_ID',BD_POST_DATASETS[a['platform']])
            posts_input={'url':a['url']}
            if a['platform']=='facebook':
                posts_input.update({'num_of_posts':18, 'include_profile_data':True})
                posts_sid=_bd_start(ds_posts,[posts_input])
            elif a['platform']=='tiktok':
                posts_input.update({'country':''})
                posts_sid=_bd_start(ds_posts,[posts_input],discovery='profile_url',limit=18)
            elif a['platform']=='linkedin':
                posts_sid=_bd_start(ds_posts,[posts_input],discovery='company_url',limit=18)
            else:  # Instagram: discover recent posts by public profile URL
                posts_sid=_bd_start(ds_posts,[posts_input],discovery='url',limit=18)
            jobs['posts']=posts_sid
        except ValueError as ex:
            warning='作品采集未启动：'+str(ex)[:120]
    if not jobs and base_result is not None:
        return finish_direct(a,base_result)
    jid=secrets.token_urlsafe(18)
    JOBS[jid]={'snapshots':jobs,'a':a,'at':time.time(),'warning':warning,'base_result':base_result}
    for k,v in list(JOBS.items()):
        if time.time()-v['at']>3600:JOBS.pop(k,None)
    return {'pending':True,'job_id':jid,'message':'正在分别采集账号资料及公开内容；作品数据仅使用真实返回值。'}


def account_token(a):
    return str(a.get('id','')).lower().lstrip('@')


def same_account(a,d):
    """Block cross-account contamination even if provider returns a stale/wrong record."""
    expected=account_token(a)
    fields={
        'instagram':('account','username','user_name','profile_url'),
        'tiktok':('account_id','username','user_name','unique_id','url'),
        'facebook':('username','profile_handle','page_username','page_url','url'),
        'linkedin':('company_url','url','company_slug'),
        'youtube':('url','handle','channel_url'),
    }.get(a['platform'],('url',))
    values=[]
    for k in fields:
        v=d.get(k)
        if isinstance(v,dict):continue
        if isinstance(v,str) and v.strip():values.append(v.strip())
    if not values:return False
    for val in values:
        vv=val.lower().strip().lstrip('@')
        if vv==expected:return True
        if vv.startswith('http'):
            try:
                u=parse_account(vv)
                if u['platform']==a['platform'] and u['id'].lower()==expected:return True
            except ValueError:pass
        if a['platform']=='facebook' and vv.endswith('/'+expected):return True
    return False


def post_matches_account(a,d):
    candidates=[]
    keys={
        'instagram':('user_posted','username','profile_url','author_username'),
        'tiktok':('account_id','author_username','author','username','unique_id','author_url'),
        'facebook':('profile_handle','user_username_raw','user_url','page_url'),
        'linkedin':('company_url','company_slug','profile_url'),
        'youtube':('channel_url','channel_id'),
        'xiaohongshu':('author_id','user_id'),
    }[a['platform']]
    for k in keys:
        v=d.get(k)
        if isinstance(v,dict):v=v.get('unique_id') or v.get('username') or v.get('url')
        if isinstance(v,str) and v.strip():candidates.append(v)
    if not candidates:
        inp=d.get('discovery_input')
        if isinstance(inp,dict):inp=inp.get('url')
        if isinstance(inp,str) and inp:
            candidates.append(inp)
    return bool(candidates) and any(same_account(a,{k:v}) for v in candidates for k in ('account_id','account','username','profile_url','url','company_url','profile_handle'))


def normalize_date(v):
    if isinstance(v,(int,float)):
        try:return datetime.fromtimestamp(v/(1000 if v>1e11 else 1),timezone.utc).date().isoformat()
        except (OverflowError,OSError,ValueError):return None
    if isinstance(v,str) and v:
        t=v.strip()
        if re.match(r'^\d{4}-\d\d-\d\d',t):return t[:10]
        if re.match(r'^\d{10,13}$',t):return normalize_date(int(t))
    return None


def extract_post_records(records,a):
    """Normalize genuine individual posts. Missing fields stay None, never invented."""
    if not isinstance(records,list):return []
    out=[];used=set()
    for d in records:
        if not isinstance(d,dict) or d.get('error') or not post_matches_account(a,d):continue
        link=get_first(d,'post_url','url','permalink','video_url','link','post_link')
        if not isinstance(link,str) or not link.startswith('https://'):continue
        host=urlparse(link).hostname or ''
        domain={'instagram':'instagram.com','tiktok':'tiktok.com','facebook':'facebook.com','linkedin':'linkedin.com'}[a['platform']]
        if not (host==domain or host.endswith('.'+domain)):continue
        if link in used:continue
        used.add(link)
        label=get_first(d,'title','description','caption','content','text','message','post_text')
        when=normalize_date(get_first(d,'date_posted','created_at','date','timestamp','create_time','published_at'))
        out.append({'title':str(label or '公开作品（无文字描述）')[:145],
                    'date':when,'url':link,
                    'views':numeric_field(d,'views','view_count','view_count_video','video_play_count','video_view_count','play_count','views_count','impressions'),
                    'likes':numeric_field(d,'likes','like_count','digg_count','reactions','num_likes'),
                    'comments':numeric_field(d,'comments','num_comments','comments_count','comment_count'),
                    'shares':numeric_field(d,'shares','share_count','share_count_video','reposts','num_shares'),
                    'saves':numeric_field(d,'collect_count','collects','save_count','saves','favorites'),
                    'type':get_first(d,'content_type','post_type','type','media_type'),
                    'source':'公开作品采集'})
        if len(out)>=50:break
    return out


def merge_post_records(a,payload,raw):
    if isinstance(raw,dict):raw=raw.get('data',raw.get('records',[raw]))
    posts=extract_post_records(raw,a)
    payload['posts']=posts
    payload['posts_data_basis']='公开作品采集' if posts else '未取得账号的可核实作品'
    payload['posts_sample_size']=len(posts)
    return payload

def extract_public_posts(record, platform):
    """Keep only provider-returned post details; never fabricate URLs or metrics."""
    if not isinstance(record,dict): return []
    candidates=[]
    for key in ('posts','recent_posts','latest_posts','media','publications','videos','recent_videos'):
        val=record.get(key)
        if isinstance(val,dict): val=val.get('items') or val.get('data')
        if isinstance(val,list): candidates.extend(x for x in val if isinstance(x,dict))
    result=[]
    for post in candidates[:50]:
        text=get_first(post,'title','caption','description','text','message')
        src=get_first(post,'url','link','permalink','post_url','video_url')
        if not isinstance(src,str) or not src.startswith('https://'):src=None
        item={'title':str(text or '公开内容').strip()[:110],
              'views':numeric_field(post,'views','view_count','plays','video_views','play_count','impressions','reach'),
              'likes':numeric_field(post,'likes','like_count','reactions'),
              'comments':numeric_field(post,'comments','comments_count','comment_count'),
              'shares':numeric_field(post,'shares','share_count','reposts'),
              'saves':numeric_field(post,'saves','saves_count','saved','favorites'),
              'date':get_first(post,'date','timestamp','taken_at','create_time','created_at')}
        if src:item['url']=src
        if any(item.get(k) is not None for k in ('views','likes','comments','shares','saves')):
            result.append(item)
        if len(result)>=20:break
    return result

def bd_process(a, raw):
    if isinstance(raw,dict):
        if 'data' in raw and isinstance(raw['data'],list):
            raw=raw['data']
        elif 'records' in raw and isinstance(raw['records'],list):
            raw=raw['records']
        else:
            raw=[raw]
    if not isinstance(raw,list) or not raw:
        raise ValueError('该账号没有返回可验证的公开数据；没有生成假指标。')
    d=next((x for x in raw if isinstance(x,dict) and not x.get('error') and same_account(a,x)),None)
    if not d:
        raise ValueError('公开采集没有返回与输入链接一致的账号记录；为防止串号，不采用无法确认身份的数据。')
    if a['platform']=='youtube':
        subscribers=numeric_field(d,'subscribers','subscriber_count','subscribers_count','number_of_subscribers','subscribers_number')
        total_views=numeric_field(d,'total_views','views','views_count','total_view_count')
        count=numeric_field(d,'video_count','videos_count','videos','number_of_videos')
        name=get_first(d,'name','channel_name','title','handle') or a['id']
        stats={'followers':subscribers,'total_views':total_views,'video_count':count}
    elif a['platform']=='instagram':
        followers=numeric_field(d,'followers','follower_count','followers_count','num_followers')
        count=numeric_field(d,'posts_count','post_count','posts','media_count','posts_count_total')
        name=get_first(d,'full_name','name','account','username') or '@'+a['id']
        stats={'followers':followers,'video_count':count,'following':numeric_field(d,'following','following_count')}
    elif a['platform']=='linkedin':
        followers=numeric_field(d,'followers','follower_count','followers_count','num_followers')
        name=get_first(d,'name','company_name','title') or a['id']
        stats={'followers':followers}
    elif a['platform']=='tiktok':
        # TikTok profile dataset fields vary between versions. Only use fields actually present.
        followers=numeric_field(d,'followers','follower_count','followers_count','num_followers','fans','fans_count')
        total_likes=numeric_field(d,'likes','total_likes','hearts','likes_count','total_hearts','heart_count','heartCount')
        count=numeric_field(d,'video_count','videos_count','videos','posts_count','number_of_posts','post_count')
        name=get_first(d,'nickname','account_name','username','user_name') or '@'+a['id']
        stats={'followers':followers,'total_likes':total_likes,'video_count':count}
    else:
        followers=numeric_field(d,'followers','follower_count','followers_count','page_followers')
        fans=numeric_field(d,'page_likes','fan_count','fans')
        name=get_first(d,'page_name','name','title','username') or a['id']
        stats={'followers':followers,'fans':fans}
    if not any(v is not None for v in stats.values()):
        raise ValueError('公开记录已返回，但未获得可核验的数值指标；请检查 Bright Data 的字段和采集结果。')
    # Location metadata only reflects account origin; it does not expose audience insights.
    origin=get_first(d,'account_based_in','account_country','country','country_name','location_country')
    if not isinstance(origin,str):origin=None
    # Avoid implying official verification or exactly live telemetry
    return {'account_country':origin,'platform':a['platform'],'name':name,'url':a['url'], 'avatar':get_first(d,'avatar','profile_picture','profile_pic','profile_image','profile_image_url','profile_pic_url'),
        'stats':stats,'posts':extract_public_posts(d,a['platform']), 'quality':'public-third-party',
        'source':'Bright Data · 公开页面采集（非平台官方后台）',
        'references':[a['url']], 'provider_data_date':get_first(d,'timestamp','updated_at','scraped_at'),
        'notes':['取自可访问的公开页面，可能存在平台缓存或采集延迟。',
                 '不能据此取得广告费用、后台触达、受众地区或询盘转化。',
                 '指标根据提供商实际返回字段显示，未返回的字段保持空值。',
                 ('提供商记录日期：'+str(get_first(d,'timestamp','updated_at','scraped_at')) if get_first(d,'timestamp','updated_at','scraped_at') else '数据记录日期未提供，不能保证即时更新。')]}


def bd_check_job(jid):
    item=JOBS.get(jid)
    if not item:raise ValueError('任务已过期，请重新同步。')
    if 'done' in item:return item['done']
    snapshots=item.get('snapshots') or {'profile':item['snapshot_id']}
    statuses={}
    for kind,sid in snapshots.items():
        response=bd_call('https://api.brightdata.com/datasets/v3/progress/'+sid)
        statuses[kind]=response.get('status') if isinstance(response,dict) else 'failed'
    if any(v in ('starting','running') for v in statuses.values()):
        return {'pending':True,'message':'正在读取账号资料与作品内容…'}
    a=item['a']
    if item.get('base_result') is not None:
        payload=item['base_result']
    else:
        if statuses.get('profile')!='ready':
            raise ValueError('账号资料采集失败，请检查公开采集服务权限及账号可访问性。')
        raw=bd_call('https://api.brightdata.com/datasets/v3/snapshot/'+snapshots['profile']+'?format=json')
        payload=bd_process(a,raw)
    if snapshots.get('posts'):
        if statuses.get('posts')=='ready':
            posts_raw=bd_call('https://api.brightdata.com/datasets/v3/snapshot/'+snapshots['posts']+'?format=json')
            payload=merge_post_records(a,payload,posts_raw)
        else:
            payload['posts_warning']='作品采集未成功，本次只更新账号资料'
    if item.get('warning'):payload['posts_warning']=item['warning']
    payload=finish_direct(a,payload)
    item['done']=payload
    return payload

def finish_direct(a, result):
    result=retain_verified_public(a,result)
    result=retain_imported_report(a,result)
    original_audience=normalized_audience(result.get('audience')) if result.get('audience') else None
    audience_source=(result.get('audience') or {}).get('source','authorized') if isinstance(result.get('audience'),dict) else 'authorized'
    if not result.get('report_source') and not result.get('series'):
        # Respect scenario tuning when an existing case is refreshed.
        assumptions={}
        try:
            with db_conn() as c:
                row=sql(c,'SELECT payload FROM cases WHERE platform=? AND account=? ORDER BY updated_at DESC LIMIT 1',(a['platform'],a['id'].lower())).fetchone()
            if row:assumptions=(json.loads(row[0]).get('estimate_model') or {}).get('assumptions') or {}
        except (ValueError,TypeError,sqlite3.Error):pass
        result.update(estimate(result,a,assumptions))
    if original_audience:
        # Some authorized Insights breakdowns may be unavailable (e.g. <100 followers).
        # Fill missing charts with transparent training assumptions, never fake their source.
        scenario=result.get('audience') or estimate_audience(a)
        mixed={k:original_audience.get(k) or scenario.get(k,[]) for k in ('countries','genders','ages')}
        mixed['source']='authorized' if all(original_audience.get(k) for k in ('countries','genders','ages')) else 'mixed-authorized-and-training'
        result['audience']=mixed
    elif not result.get('audience'):
        result['audience']=estimate_audience(a,(result.get('estimate_model') or {}).get('assumptions'),result)
    result['account']=a
    result['fetched_at']=now()
    result['history']=store_and_history(a,result['stats'])
    save_case(a,result)
    return result


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, obj):
        raw=json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authenticated(self):
        user=os.getenv('CASELENS_USER','team')
        passwd=os.getenv('CASELENS_PASSWORD','')
        if not passwd:
            self.send_json(503, {'error':'管理员尚未设置 CASELENS_PASSWORD。为了防止案例数据泄露，网站暂不对外开放。'})
            return False
        auth=self.headers.get('Authorization','')
        good='Basic '+base64.b64encode((user+':'+passwd).encode()).decode()
        if not hmac.compare_digest(auth,good):
            self.send_response(401)
            self.send_header('WWW-Authenticate','Basic realm="CaseLens internal workspace", charset="UTF-8"')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length','0')
            self.end_headers()
            return False
        return True

    def do_HEAD(self):
        """Render uptime probes should not receive 501 unsupported method."""
        path=urlparse(self.path).path
        if path=='/healthz':
            self.send_response(200)
            self.send_header('Content-Length','0');self.end_headers();return
        if path=='/':
            if not self.authenticated():return
            self.send_response(200)
            self.send_header('Content-Length','0');self.end_headers();return
        self.send_response(404);self.end_headers()

    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/healthz':
            return self.send_json(200, {'ok':True})
        if not self.authenticated():
            return
        if u.path=='/':
            b=(ROOT/'后台.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if u.path=='/api/integrations':
            return self.send_json(200,{'items':integration_status(config())})
        if u.path=='/api/health':
            return self.send_json(200, {'ok':True,'service':'CaseLens V10','time':now()})
        try:
            if u.path=='/api/cases':
                return self.send_json(200, {'items':list_cases()})
            if u.path=='/api/job':
                jid=parse_qs(u.query).get('id',[''])[0]
                if not re.fullmatch(r'[A-Za-z0-9_\-]{10,50}', jid):
                    raise ValueError('无效的采集任务编号')
                return self.send_json(200, bd_check_job(jid))
            if u.path=='/api/profile':
                a=parse_account(parse_qs(u.query).get('url',[''])[0]); cfg=config()
                if a['platform']=='xiaohongshu':
                    # Never treat an OAuth login token as access to private creator metrics.
                    # Use training scenario; imported analytics is preserved on refresh.
                    return self.send_json(200, finish_direct(a,scenario_profile(a,'小红书账号的周期阅读、曝光、粉丝画像不提供任意链接公开读取；可导入本人后台报表。')))
                if a['platform']=='linkedin':
                    if os.getenv('BRIGHTDATA_API_TOKEN'):
                        return self.send_json(202,bd_trigger(a))
                    return self.send_json(200,finish_direct(a,scenario_profile(a,'LinkedIn 公开采集未配置')))
                if a['platform']=='instagram':
                    if os.getenv('INSTAGRAM_ACCESS_TOKEN') and os.getenv('INSTAGRAM_IG_USER_ID'):
                        try:
                            own=ig_fetch(a,cfg)
                            if os.getenv('BRIGHTDATA_API_TOKEN') and os.getenv('CASELENS_FETCH_POSTS','1')!='0':
                                response=bd_trigger(a,base_result=own)
                                return self.send_json(202 if response.get('pending') else 200,response)
                            return self.send_json(200,finish_direct(a,own))
                        except ValueError as ex:
                            if not os.getenv('BRIGHTDATA_API_TOKEN'):
                                return self.send_json(200,finish_direct(a,scenario_profile(a,ex)))
                    if os.getenv('BRIGHTDATA_API_TOKEN'):
                        return self.send_json(202,bd_trigger(a))
                    return self.send_json(200,finish_direct(a,scenario_profile(a,'Instagram 公开采集及授权接口未配置')))
                if a['platform']=='youtube':
                    if os.getenv('YOUTUBE_API_KEY') or cfg.get('youtube_api_key'):
                        try:
                            return self.send_json(200,finish_direct(a,yt_fetch(a,cfg)))
                        except ValueError as ex:
                            return self.send_json(200,finish_direct(a,scenario_profile(a,ex)))
                    return self.send_json(200,finish_direct(a,scenario_profile(a,'未配置YouTube公开API Key')))
                if a['platform']=='tiktok':
                    tok=os.getenv('TIKTOK_ACCESS_TOKEN') or cfg.get('tiktok',{}).get('access_token')
                    if tok:
                        try:
                            own=tk_fetch(a,cfg)
                        except ValueError:
                            if not os.getenv('BRIGHTDATA_API_TOKEN'):raise
                        else:
                            if os.getenv('BRIGHTDATA_API_TOKEN') and os.getenv('CASELENS_FETCH_POSTS','1')!='0':
                                response=bd_trigger(a,base_result=own)
                                return self.send_json(202 if response.get('pending') else 200,response)
                            return self.send_json(200,finish_direct(a,own))
                    if os.getenv('BRIGHTDATA_API_TOKEN'):
                        return self.send_json(202,bd_trigger(a))
                    return self.send_json(200,finish_direct(a,scenario_profile(a,'未配置TikTok公开采集服务')))
                if a['platform']=='facebook':
                    entry=cfg.get('facebook_pages',{}).get(a['id']) or cfg.get('facebook_pages',{}).get(a['url']) or (os.getenv('FACEBOOK_PAGE_ID') and os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN'))
                    if entry:
                        try:
                            own=fb_fetch(a,cfg)
                        except ValueError:
                            if not os.getenv('BRIGHTDATA_API_TOKEN'):raise
                        else:
                            if os.getenv('BRIGHTDATA_API_TOKEN') and os.getenv('CASELENS_FETCH_POSTS','1')!='0':
                                response=bd_trigger(a,base_result=own)
                                return self.send_json(202 if response.get('pending') else 200,response)
                            return self.send_json(200,finish_direct(a,own))
                    if os.getenv('BRIGHTDATA_API_TOKEN'):
                        return self.send_json(202,bd_trigger(a))
                    return self.send_json(200,finish_direct(a,scenario_profile(a,'未配置Facebook公开采集服务')))
            return self.send_json(404, {'error':'Not found'})
        except ValueError as ex:
            return self.send_json(400, {'error':str(ex)})
        except Exception:
            # API keys, connection strings, and internal paths must never appear on the client
            return self.send_json(500, {'error':'服务端处理出错。请检查部署日志与数据服务设置。'})

    def do_POST(self):
        if not self.authenticated():
            return
        u=urlparse(self.path)
        if u.path not in ('/api/cases','/api/estimate','/api/verify-public'):
            return self.send_json(404, {'error':'Not found'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>200_000:
                raise ValueError('导入文件内容不合法或超过大小限制')
            obj=json.loads(self.rfile.read(n))
            a=parse_account(obj['url'])
            if u.path=='/api/verify-public':
                fields=obj.get('fields')
                if not isinstance(fields,dict):raise ValueError('核实资料格式错误')
                if not any(str(v).strip() for v in fields.values() if v is not None):raise ValueError('至少填写一项已核实的数据')
                with db_conn() as c:
                    row=sql(c,'SELECT payload FROM cases WHERE platform=? AND account=? ORDER BY updated_at DESC LIMIT 1',(a['platform'],a['id'].lower())).fetchone()
                d=json.loads(row[0]) if row else scenario_profile(a)
                facts=dict(d.get('manual_verified') or {})
                for k in ('followers','total_likes','video_count'):
                    v=fields.get(k)
                    if v in ('',None):continue
                    parsed=safe_number(v)
                    if parsed is None or parsed>10_000_000_000:raise ValueError(k+' 必须是非负数字')
                    if k=='total_likes' and a['platform']!='tiktok':raise ValueError('累计获赞字段仅供TikTok账号使用')
                    facts[k]=parsed
                    d.setdefault('stats',{})[k]=parsed
                country=str(fields.get('account_country') or '').strip()
                if country:
                    if len(country)>60 or any(x in country for x in '<>{}'):
                        raise ValueError('账号所在地格式错误')
                    facts['account_country']=country
                    d['account_country']=country
                d['manual_verified']=facts
                d['manual_verified_at']=now()
                # Recompute scenario with updated verified baseline. Do not touch imported true analytics.
                if not d.get('report_source'):
                    assumptions=(d.get('estimate_model') or {}).get('assumptions') or {}
                    d.update(estimate(d,a,assumptions))
                d['fetched_at']=now()
                save_case(a,d)
                return self.send_json(200,{'ok':True,'verified_fields':list(facts)})
            if u.path=='/api/estimate':
                raw_assumptions=obj.get('assumptions',{})
                if not isinstance(raw_assumptions,dict):
                    raise ValueError('假设参数格式不合法')
                allowed=('followers','posts_per_month','views_per_post','views_28d','female_pct')
                asum={}
                for k in allowed:
                    val=raw_assumptions.get(k)
                    if val not in ('',None):
                        try: val=float(val)
                        except (ValueError,TypeError): raise ValueError('参数必须是有效的非负数字')
                        if not (0<=val<=10000000000): raise ValueError('参数必须在允许范围内')
                        asum[k]=val
                with db_conn() as conn:
                    row=sql(conn,'SELECT payload FROM cases WHERE url=?',(a['url'],)).fetchone()
                d=json.loads(row[0]) if row else scenario_profile(a)
                if d.get('report_source') and (d.get('analytics') or d.get('series')):
                    raise ValueError('当前账号已有导入的真实分析报表，不能用预估覆盖。')
                market=raw_assumptions.get('market')
                if market is not None:
                    if not isinstance(market,str) or market not in MARKETS and market!='auto':raise ValueError('目标市场选择无效')
                    asum['market']=market
                if 'female_pct' in asum and asum['female_pct']>100:raise ValueError('女性比例不能超过100%')
                d.update(estimate(d,a,asum))
                d['fetched_at']=now()
                save_case(a,d)
                return self.send_json(200,d)
            d=obj['data']
            if not isinstance(d,dict) or not isinstance(d.get('stats'),dict) or d.get('platform')!=a['platform']:
                raise ValueError('数据内容或平台与链接不一致')
            d['quality']='imported'
            d['source']='内部导入（非平台 API 自动验证）'
            if d.get('audience') is not None:
                cleaned=normalized_audience(d['audience'])
                if cleaned:d['audience']=dict(cleaned,source='imported')
                else:d.pop('audience',None)
            d['fetched_at']=d.get('fetched_at') or now()
            d['url']=a['url']
            d['account']=a
            save_case(a,d)
            return self.send_json(200, {'ok':True})
        except (ValueError,KeyError,TypeError,UnicodeDecodeError,json.JSONDecodeError) as ex:
            return self.send_json(400, {'error':str(ex)})
        except Exception:
            return self.send_json(500, {'error':'无法保存数据，请检查数据库是否连接。'})

    def log_message(self, fmt, *args):
        sys.stderr.write('[CaseLens] %s\n' % (fmt % args))


if __name__=='__main__':
    print(f'CaseLens V10: http://{HOST}:{PORT} (cloud={HOST!="127.0.0.1"})')
    if not DATABASE_URL:
        print('NOTE: SQLite on Render ephemeral disk is not persistent across redeploy/restart. Configure DATABASE_URL for durable history.')
    if not os.getenv('CASELENS_PASSWORD'):
        print('WARNING: CASELENS_PASSWORD is not configured; the app will refuse public access.')
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
