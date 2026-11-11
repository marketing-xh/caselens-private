"""CaseLens access requests and short-lived approval gates.

All user access tokens are random, kept on the client in sessionStorage and
stored on the server as SHA-256 hashes only. Admin code lives in environment.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque


class AccessControl:
    def __init__(self, db_conn, sql):
        self.db_conn = db_conn
        self.sql = sql
        self.lock = threading.RLock()
        self.admin_tokens = {}  # token_hash => expiration; lost on restart by design
        self.attempts = defaultdict(deque)
        self.requests = defaultdict(deque)
        self._setup()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def _setup(self):
        with self.db_conn() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS access_requests (
              request_id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
              token_hash TEXT NOT NULL, status TEXT NOT NULL, created_at BIGINT NOT NULL,
              decided_at BIGINT, expires_at BIGINT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS content_plans (
              platform TEXT NOT NULL, account TEXT NOT NULL, payload TEXT NOT NULL,
              updated_at BIGINT NOT NULL, PRIMARY KEY(platform,account))''')
            c.commit()

    def throttle(self, table, ip, limit, window):
        with self.lock:
            arr = table[ip]
            t = time.time()
            while arr and arr[0] < t-window:
                arr.popleft()
            if len(arr) >= limit:
                return False
            arr.append(t)
            return True

    def admin_login(self, code, ip):
        # Configured in Render Environment. No hardcoded or default master ID.
        master = os.getenv('CASELENS_MASTER_CODE', '')
        if len(master) < 20:
            raise ValueError('管理员通行码未配置或长度不足；请在 Render 设置 CASELENS_MASTER_CODE（至少20位）')
        if not self.throttle(self.attempts, 'login-'+ip, 5, 15*60):
            raise ValueError('尝试次数过多，请15分钟后再试')
        if not hmac.compare_digest(code.encode(), master.encode()):
            raise ValueError('管理员通行码不正确')
        tok = secrets.token_urlsafe(36)
        with self.lock:
            self.admin_tokens[self.digest(tok)] = int(time.time()) + 8*3600
        return tok

    def role(self, token):
        if not isinstance(token, str) or len(token) < 35 or len(token) > 160:
            return None
        hsh = self.digest(token)
        t = int(time.time())
        with self.lock:
            expire = self.admin_tokens.get(hsh)
            if expire and expire > t:
                return 'admin'
            if expire:
                self.admin_tokens.pop(hsh, None)
        with self.db_conn() as c:
            row = self.sql(c, "SELECT status, expires_at FROM access_requests WHERE token_hash=?", (hsh,)).fetchone()
        if row and row[0] == 'approved' and int(row[1]) > t:
            return 'user'
        return None

    def apply(self, name, purpose, ip):
        name = str(name or '').strip()
        purpose = str(purpose or '').strip()
        if not (2 <= len(name) <= 55 and 2 <= len(purpose) <= 200):
            raise ValueError('请填写姓名或称呼（2–55字）及申请用途（2–200字）')
        if not self.throttle(self.requests, 'apply-'+ip, 5, 10*60):
            raise ValueError('申请过于频繁，请稍后重试')
        # Expiration resets to approval time when approved; request pending max 24h.
        req_id = secrets.token_urlsafe(16)
        secret = secrets.token_urlsafe(36)
        t = int(time.time())
        with self.db_conn() as c:
            self.sql(c, '''INSERT INTO access_requests
                (request_id,name,purpose,token_hash,status,created_at,expires_at)
                VALUES (?,?,?,?,?,?,?)''', (req_id,name,purpose,self.digest(secret),'pending',t,t+24*3600))
        return {'request_id': req_id, 'secret': secret, 'status': 'pending'}

    def status(self, req_id, secret):
        if not isinstance(req_id, str) or not isinstance(secret,str):
            return {'status':'not_found'}
        with self.db_conn() as c:
            row=self.sql(c,'SELECT status,expires_at,token_hash FROM access_requests WHERE request_id=?',(req_id,)).fetchone()
        if not row or not hmac.compare_digest(self.digest(secret),row[2]):
            return {'status':'not_found'}
        if int(row[1]) <= int(time.time()):
            return {'status':'expired'}
        return {'status':row[0]}

    def list_requests(self):
        with self.db_conn() as c:
            rows=self.sql(c,'''SELECT request_id,name,purpose,status,created_at,decided_at,expires_at
               FROM access_requests ORDER BY created_at DESC LIMIT 100''').fetchall()
        return [{'request_id':x[0], 'name':x[1], 'purpose':x[2], 'status':x[3],
                 'created_at':x[4], 'decided_at':x[5], 'expires_at':x[6]}
                for x in rows]

    def decide(self, req_id, action):
        if action not in ('approved','rejected','revoked'):
            raise ValueError('无效的审批操作')
        with self.db_conn() as c:
            row=self.sql(c,'SELECT status FROM access_requests WHERE request_id=?',(req_id,)).fetchone()
            if not row:
                raise ValueError('找不到访问申请')
            if action=='revoked' and row[0]!='approved':
                raise ValueError('只能撤销已经批准的访问')
            if action in ('approved','rejected') and row[0]!='pending':
                raise ValueError('只能审批待处理申请')
            # 8 h max from approval; if browser is closed earlier, sessionStorage is lost.
            t=int(time.time())
            self.sql(c, '''UPDATE access_requests SET status=?,decided_at=?,expires_at=?
              WHERE request_id=?''', (action,t,t+8*3600 if action=='approved' else t,req_id))
        return {'ok': True, 'status':action}

    def get_plan(self, platform, account):
        with self.db_conn() as c:
            row=self.sql(c,'SELECT payload FROM content_plans WHERE platform=? AND account=?',
                         (platform,account.lower())).fetchone()
        if not row:
            return None
        import json
        return json.loads(row[0])

    def save_plan(self, platform, account, items):
        import json
        if not isinstance(items,list) or not (1 <= len(items) <= 8):
            raise ValueError('内容类型必须在1到8项之间')
        cleaned=[]
        for item in items:
            if not isinstance(item,dict):
                raise ValueError('内容类型格式错误')
            name=item.get('name'); desc=item.get('description'); percentage=item.get('pct')
            if not isinstance(name,str) or not 1 <= len(name.strip()) <= 45:
                raise ValueError('内容名称长度需要1–45字')
            if not isinstance(desc,str) or len(desc.strip())>150:
                raise ValueError('内容说明最多150字')
            if not isinstance(percentage,int) or isinstance(percentage,bool) or not 0<=percentage<=100:
                raise ValueError('内容比例必须是0–100之间的整数')
            cleaned.append({'name':name.strip(),'description':desc.strip(),'pct':percentage})
        if sum(i['pct'] for i in cleaned)!=100:
            raise ValueError('全部内容比例相加必须为100%')
        if len({i['name'].lower() for i in cleaned})!=len(cleaned):
            raise ValueError('内容名称不能重复')
        with self.db_conn() as c:
            self.sql(c,'''INSERT INTO content_plans(platform,account,payload,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(platform,account) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at''',
                     (platform,account.lower(),json.dumps(cleaned,ensure_ascii=False),int(time.time())))
        return cleaned
