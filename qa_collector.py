from pathlib import Path
import sys,os
sys.path.insert(0,str(Path(__file__).parent))
import server
from unittest.mock import patch

account=server.parse_account('https://www.tiktok.com/@pydlifeblanks')
requests=[]
def fake_call(url,data=None):
    requests.append((url,data))
    if '/trigger?' in url:
        if 'gd_l1villgoiiidt09ci' in url:return {'snapshot_id':'s_profile1234'}
        if 'gd_lu702nij2f790tmv9h' in url:return {'snapshot_id':'sd_posts1234'}
    if '/progress/' in url:return {'status':'ready'}
    if 's_profile1234' in url and '/snapshot/' in url:
        return [{'account_id':'pydlifeblanks','url':account['url'],'followers':89900,'likes':583900,'videos_count':200,'nickname':'PYD Life Blanks'}]
    if 'sd_posts1234' in url and '/snapshot/' in url:
        return [{'account_id':'pydlifeblanks','url':'https://www.tiktok.com/@pydlifeblanks/video/123456789','description':'Video A','create_time':'2026-11-07','digg_count':12000,'play_count':250000,'share_count':400,'comment_count':300},
                {'account_id':'someone_else','url':'https://www.tiktok.com/@someone_else/video/8888','digg_count':999999}]
    raise AssertionError('unexpected request '+url)

from tempfile import TemporaryDirectory
with TemporaryDirectory() as temp:
 server.DB=Path(temp)/'tmp.sqlite'
 server.DATABASE_URL=''
 with patch.dict(os.environ,{'BRIGHTDATA_API_TOKEN':'TEST_IN_MEMORY','CASELENS_FETCH_POSTS':'1'}), patch.object(server,'bd_call',side_effect=fake_call):
    pending=server.bd_trigger(account)
    assert pending['pending']
    complete=server.bd_check_job(pending['job_id'])
    assert complete['stats']['total_likes']==583900
    assert len(complete['posts'])==1
    assert complete['posts'][0]['likes']==12000
    assert complete['estimate_model']['public_recent_posts_28d']==1
    assert 'discover_by=profile_url' in requests[1][0]
    assert requests[1][1].get('limit_per_input')==18
    assert server.bd_check_job(pending['job_id']) is complete
print('Collector integration QA PASS: profile+posts, async progress, 583900 likes, author validation, capped post requests')
