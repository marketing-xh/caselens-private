"""CaseLens local dashboard service. Stdlib only. Binds to 127.0.0.1.
Data comes only from authorized official APIs; no invented network metrics.
"""
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv('CASELENS_DB_PATH', str(ROOT / 'history.sqlite3')))
CONF = ROOT / 'settings.json'
PORT = int(os.getenv('PORT') or os.getenv('CASELENS_PORT') or ('3000' if os.getenv('REPL_ID') or os.getenv('CASELENS_CLOUD') == '1' else '8765'))
HOST = '0.0.0.0' if os.getenv('REPL_ID') or os.getenv('CASELENS_CLOUD') == '1' else '127.0.0.1'
DATABASE_URL = os.getenv('DATABASE_URL', '')
JOBS = {}
# Bright Data dataset IDs from provider's public official docs; may evolve.
BD_DATASETS = {'youtube': 'gd_lk538t2k2p1k3oos71', 'tiktok': 'gd_l1villgoiiidt09ci', 'facebook': 'gd_mf124a0511bauquyow'}


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
    raise ValueError('当前仅支持 YouTube / TikTok / Facebook 账号主页链接')


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
    if not entry:
        raise ValueError('Facebook 只能读取配置过授权的 Page。请在 settings.json 的 facebook_pages 中添加该账号映射及 Page Access Token。')
    pid = str(entry.get('page_id') or '')
    token = entry.get('access_token') or ''
    if not re.fullmatch(r'[0-9]{5,40}', pid) or not token:
        raise ValueError('Facebook 配置缺少合法的 Page ID 或 Access Token')
    ver = 'v23.0'  # Endpoint version may need updating as Meta deprecates old versions.
    fields = 'id,name,fan_count,followers_count,link,picture'
    url = f'https://graph.facebook.com/{ver}/{pid}?' + urlencode({'fields': fields})
    data = get_json(url, token)
    if str(data.get('id')) != pid:
        raise ValueError('Facebook 返回了不匹配的 Page ID')
    return {'platform': 'facebook', 'name': data.get('name', a['id']), 'url': data.get('link', a['url']),
            'avatar': (data.get('picture') or {}).get('data', {}).get('url'),
            'stats': {'followers': data.get('followers_count'), 'fans': data.get('fan_count')}, 'posts': [],
            'source': 'Meta Graph API（Page 授权数据）', 'quality': 'authorized', 'notes': ['Page Insights / 受众与广告指标需要额外页面权限。']}


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


def bd_trigger(a):
    ds=os.getenv('BRIGHTDATA_'+a['platform'].upper()+'_DATASET_ID', BD_DATASETS[a['platform']])
    endpoint='https://api.brightdata.com/datasets/v3/trigger?'+urlencode({'dataset_id':ds,'format':'json'})
    bd_url=(a['url'].rstrip('/')+'/about') if a['platform']=='youtube' else a['url']
    obj=bd_call(endpoint, [{'url':bd_url}])
    sid=obj.get('snapshot_id') if isinstance(obj,dict) else None
    if not sid or not re.fullmatch(r's_[a-zA-Z0-9_\-]{5,120}', sid):
        raise ValueError('公开数据服务未返回采集任务编号，请检查账号权限和余额。')
    jid=secrets.token_urlsafe(18)
    JOBS[jid]={'snapshot_id':sid, 'a':a, 'at':time.time()}
    # Avoid keeping old jobs indefinitely
    for k, v in list(JOBS.items()):
        if time.time()-v['at'] > 3600:
            del JOBS[k]
    return {'pending':True,'job_id':jid,'message':'正在采集当前公开资料，结果取决于平台页面可访问性。'}


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
    d=next((x for x in raw if isinstance(x,dict) and not x.get('error')),None)
    if not d:
        raise ValueError('第三方采集没有返回账号记录，可能是区域限制、页面保护或不支持该账号。')
    if a['platform']=='youtube':
        subscribers=numeric_field(d,'subscribers','subscriber_count','subscribers_count','number_of_subscribers','subscribers_number')
        total_views=numeric_field(d,'total_views','views','views_count','total_view_count')
        count=numeric_field(d,'video_count','videos_count','videos','number_of_videos')
        name=get_first(d,'name','channel_name','title','handle') or a['id']
        stats={'followers':subscribers,'total_views':total_views,'video_count':count}
    elif a['platform']=='tiktok':
        # TikTok profile dataset fields vary between versions. Only use fields actually present.
        followers=numeric_field(d,'followers','follower_count','followers_count','num_followers','fans','fans_count')
        total_likes=numeric_field(d,'likes','total_likes','hearts','likes_count','total_hearts')
        count=numeric_field(d,'video_count','videos','posts_count','number_of_posts','post_count')
        name=get_first(d,'nickname','account_name','username','user_name') or '@'+a['id']
        stats={'followers':followers,'total_likes':total_likes,'video_count':count}
    else:
        followers=numeric_field(d,'followers','follower_count','followers_count','page_followers')
        fans=numeric_field(d,'likes','page_likes','fan_count','fans')
        name=get_first(d,'page_name','name','title','username') or a['id']
        stats={'followers':followers,'fans':fans}
    if not any(v is not None for v in stats.values()):
        raise ValueError('公开记录已返回，但未获得可核验的数值指标；请检查 Bright Data 的字段和采集结果。')
    # Avoid implying official verification or exactly live telemetry
    return {'platform':a['platform'],'name':name,'url':a['url'], 'avatar':get_first(d,'avatar','profile_picture','profile_pic','profile_image'),
        'stats':stats,'posts':[], 'quality':'public-third-party',
        'source':'Bright Data · 公开页面采集（非平台官方后台）',
        'references':[a['url']], 'provider_data_date':get_first(d,'timestamp','updated_at','scraped_at'),
        'notes':['取自可访问的公开页面，可能存在平台缓存或采集延迟。',
                 '不能据此取得广告费用、后台触达、受众地区或询盘转化。',
                 '指标根据提供商实际返回字段显示，未返回的字段保持空值。',
                 ('提供商记录日期：'+str(get_first(d,'timestamp','updated_at','scraped_at')) if get_first(d,'timestamp','updated_at','scraped_at') else '数据记录日期未提供，不能保证即时更新。')]}


def bd_check_job(jid):
    item=JOBS.get(jid)
    if not item:
        raise ValueError('任务已过期，请重新输入链接并同步。')
    if 'done' in item:
        return item['done']
    sid=item['snapshot_id']
    result=bd_call('https://api.brightdata.com/datasets/v3/progress/'+sid)
    status=result.get('status') if isinstance(result,dict) else None
    if status in ('starting','running'):
        return {'pending':True,'message':'正在获取公开资料…'}
    if status!='ready':
        raise ValueError('公开数据采集失败；页面可能受限制或不支持此账号。')
    raw=bd_call('https://api.brightdata.com/datasets/v3/snapshot/'+sid+'?format=json')
    a=item['a']; payload=bd_process(a,raw)
    payload['account']=a
    payload['fetched_at']=now()
    payload['history']=store_and_history(a,payload['stats'])
    save_case(a,payload)
    item['done']=payload
    return payload


def finish_direct(a, result):
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
        if u.path=='/api/health':
            return self.send_json(200, {'ok':True,'service':'CaseLens V3','time':now()})
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
                if a['platform']=='youtube':
                    if os.getenv('YOUTUBE_API_KEY') or cfg.get('youtube_api_key'):
                        return self.send_json(200,finish_direct(a,yt_fetch(a,cfg)))
                    return self.send_json(202,bd_trigger(a))
                if a['platform']=='tiktok':
                    tok=os.getenv('TIKTOK_ACCESS_TOKEN') or cfg.get('tiktok',{}).get('access_token')
                    if tok:
                        return self.send_json(200,finish_direct(a,tk_fetch(a,cfg)))
                    return self.send_json(202,bd_trigger(a))
                if a['platform']=='facebook':
                    entry=cfg.get('facebook_pages',{}).get(a['id']) or cfg.get('facebook_pages',{}).get(a['url'])
                    if entry:
                        return self.send_json(200,finish_direct(a,fb_fetch(a,cfg)))
                    return self.send_json(202,bd_trigger(a))
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
        if u.path!='/api/cases':
            return self.send_json(404, {'error':'Not found'})
        try:
            n=int(self.headers.get('Content-Length','0'))
            if n<=0 or n>200_000:
                raise ValueError('导入文件内容不合法或超过大小限制')
            obj=json.loads(self.rfile.read(n))
            a=parse_account(obj['url'])
            d=obj['data']
            if not isinstance(d,dict) or not isinstance(d.get('stats'),dict) or d.get('platform')!=a['platform']:
                raise ValueError('数据内容或平台与链接不一致')
            d['quality']='imported'
            d['source']='内部导入（非平台 API 自动验证）'
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
    print(f'CaseLens V3: http://{HOST}:{PORT} (cloud={HOST!="127.0.0.1"})')
    if not DATABASE_URL:
        print('NOTE: SQLite is used; configure Replit SQL DATABASE_URL for persistent cloud history.')
    if not os.getenv('CASELENS_PASSWORD'):
        print('WARNING: CASELENS_PASSWORD is not configured; the app will refuse public access.')
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
