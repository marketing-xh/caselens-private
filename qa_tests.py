"""Offline tests with representative provider fixtures; do not call live APIs."""
from pathlib import Path
import sys
from datetime import datetime, timezone
sys.path.insert(0,str(Path(__file__).parent))
import server
from estimator import estimate

passed=[]
def check(name,fn):
    fn();passed.append(name)


def tiktok_profile():
    a=server.parse_account('https://www.tiktok.com/@pydlifeblanks')
    raw=[{'account_id':'pydlifeblanks','nickname':'PYD Life Blanks','followers':89900,'following':8373,'likes':583900,'videos_count':214,'url':a['url']}]
    p=server.bd_process(a,raw)
    assert p['stats']['followers']==89900
    assert p['stats']['total_likes']==583900
    assert p['stats']['video_count']==214
    assert p['quality']=='public-third-party'
    assert server.same_account(a,raw[0])
    assert not server.same_account(a,dict(raw[0],account_id='other',url='https://www.tiktok.com/@other'))


def instagram_profile():
    a=server.parse_account('https://www.instagram.com/petfedindia/')
    p=server.bd_process(a,[{'account':'petfedindia','followers':40000,'posts_count':300,'account_based_in':'India'}])
    assert p['account_country']=='India'
    assert p['stats']['followers']==40000


def cross_account_profile():
    a=server.parse_account('https://www.instagram.com/petfedindia/')
    try:server.bd_process(a,[{'account':'xianfoods','followers':66000}]);assert False
    except ValueError as e:assert '与输入链接一致' in str(e)


def posts_date_and_authorship():
    a=server.parse_account('https://www.instagram.com/petfedindia/')
    records=[{'user_posted':'petfedindia','url':'https://www.instagram.com/p/Real2026/','description':'真实营养测评','date_posted':'2026-11-04T10:00:00Z','likes':1500,'num_comments':81,'video_view_count':30000},
             {'user_posted':'xianfoods','url':'https://www.instagram.com/p/Other/','likes':990000},
             {'user_posted':'petfedindia','url':'https://evil.example/p/Fake/','likes':999999}]
    result=server.extract_post_records(records,a)
    assert len(result)==1,result
    assert result[0]['date']=='2026-11-04'
    assert result[0]['likes']==1500 and result[0]['views']==30000


def facebook_posts():
    a=server.parse_account('https://www.facebook.com/SmartStyle')
    data=[{'profile_handle':'SmartStyle','post_url':'https://www.facebook.com/SmartStyle/posts/123', 'content':'项目案例', 'date_posted':'2026-11-08','likes':43,'num_comments':11,'shares':5}]
    p=server.extract_post_records(data,a)
    assert len(p)==1 and p[0]['comments']==11 and p[0]['shares']==5


def tiktok_posts():
    a=server.parse_account('https://www.tiktok.com/@pydlifeblanks')
    row={'account_id':'pydlifeblanks','url':'https://www.tiktok.com/@pydlifeblanks/video/76543210','description':'衬衣测评', 'create_time':1794258000,'digg_count':5200,'play_count':130000,'comment_count':92,'share_count':100,'collect_count':230}
    x=server.extract_post_records([row],a)
    assert len(x)==1 and x[0]['likes']==5200 and x[0]['saves']==230
    assert x[0]['date'].startswith('2026-')


def unique_cadence():
    a1=server.parse_account('https://www.instagram.com/petfedindia/')
    a2=server.parse_account('https://www.instagram.com/xianfoods/')
    p1={'stats':{},'posts':[],'quality':'scenario'}
    p2={'stats':{},'posts':[],'quality':'scenario'}
    d1=estimate(p1,a1)['estimate_model']
    d2=estimate(p2,a2)['estimate_model']
    assert d1['posts_frequency_basis'].endswith('非历史发布数量')
    assert d2['posts_frequency_basis'].endswith('非历史发布数量')
    assert d1['posts_per_28d']!=d2['posts_per_28d']
    assert d1['public_recent_posts_28d']==0


def posts_cadence_from_dates():
    a=server.parse_account('https://www.instagram.com/petfedindia/')
    today=datetime.now(timezone.utc).date().isoformat()
    posts=[{'date':today,'url':f'https://www.instagram.com/p/{x}/','likes':100} for x in ('one','two','three','four')]
    r=estimate({'stats':{'followers':20000},'posts':posts,'quality':'public-third-party'},a)
    assert r['estimate_model']['public_recent_posts_28d']==4
    assert r['estimate_model']['posts_per_28d']==4


def no_made_up_posts():
    a=server.parse_account('https://www.tiktok.com/@pydlifeblanks')
    x=estimate({'stats':{'followers':89900,'total_likes':583900},'posts':[],'quality':'public-third-party'},a)
    assert x['estimate_model']['public_recent_posts']==0
    assert x['estimate_model']['historical_posts_completeness']=='unknown'


def origins():
    from audience import estimate_audience
    a=server.parse_account('https://www.instagram.com/arturel.studio/')
    z=estimate_audience(a)
    assert z['account_origin']['name']=='丹麦'
    assert z['source']=='training-scenario'
    a=server.parse_account('https://www.instagram.com/petfedindia/')
    z=estimate_audience(a)
    assert z['account_origin'] is None

for label,fx in [('TikTok account likes',tiktok_profile),('Instagram about country',instagram_profile),('Prevent wrong account',cross_account_profile),('Instagram works filter',posts_date_and_authorship),('Facebook works',facebook_posts),('TikTok works',tiktok_posts),('No fixed eight posts',unique_cadence),('28-day actual sample count',posts_cadence_from_dates),('No fabricated works',no_made_up_posts),('Origin vs audience',origins)]:check(label,fx)
print('PASS',len(passed),'\n'+'\n'.join('✓ '+x for x in passed))
