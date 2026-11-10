from pathlib import Path
import sys,json
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).parent))
from playwright.sync_api import sync_playwright
from server import parse_account
from estimator import estimate
root=Path(__file__).parent
html=(root/'后台.html').read_text()
urls={
 'youtube':'https://www.youtube.com/@dlw2023',
 'instagram':'https://www.instagram.com/petfedindia/',
 'facebook':'https://www.facebook.com/SmartStyle',
 'tiktok':'https://www.tiktok.com/@pydlifeblanks',
 'linkedin':'https://www.linkedin.com/company/example-company/',
 'xiaohongshu':'https://www.xiaohongshu.com/user/profile/615db356000000000100763b'}
profiles=[]
for platform,url in urls.items():
 a=parse_account(url)
 posts=[]
 if platform=='instagram':
  posts=[{'title':'营养知识系列｜实测','url':'https://www.instagram.com/p/real123/','date':'2026-11-06','views':48800,'likes':2500,'comments':132,'shares':78,'saves':352},
         {'title':'产品实拍细节','url':'https://www.instagram.com/p/real456/','date':'2026-11-04','views':20000,'likes':1200,'comments':86,'shares':32,'saves':110}]
 if platform=='tiktok':
  posts=[{'title':'生产流程实拍','url':'https://www.tiktok.com/@pydlifeblanks/video/76543210','date':'2026-11-06','views':128000,'likes':8500,'comments':82,'shares':630,'saves':44}]
 st={'followers':89900,'total_likes':583900,'video_count':300} if platform=='tiktok' else {'followers':21500} if platform=='instagram' else {}
 d={'platform':platform,'name':a['id'],'url':url,'stats':st,'posts':posts,'quality':'public-third-party','source':'Test fixture','fetched_at':datetime.now(timezone.utc).isoformat()}
 d.update(estimate(d,a));profiles.append(d)
with sync_playwright() as pw:
 b=pw.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-dev-shm-usage'])
 page=b.new_page(viewport={'width':1550,'height':980},device_scale_factor=1)
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.set_content(html,wait_until='domcontentloaded')
 page.evaluate('''p => {window.fetch = async (url,opts) => {let text=String(url);let payload={};if(text.includes('/api/integrations'))payload={items:{}};else if(text.includes('/api/cases'))payload={items:p.map(d=>({name:d.name,platform:d.platform,url:d.url,data:d}))};else if(text.includes('/api/profile'))payload=p.find(x=>text.includes(encodeURIComponent(x.url)))||p[0];else if(text.includes('/api/verify-public'))payload={ok:true};return {ok:true,status:200,json:async()=>payload}}}''',profiles)
 for plat,url in urls.items():
  page.locator('#url').fill(url)
  page.locator('#sync').click()
  page.wait_for_function('(name) => document.querySelector("#accountname").textContent===name',arg=parse_account(url)['id'],timeout=8000)
  print('platform',plat,'tabs=',page.locator('#tabs .tab').all_text_contents())
  assert page.locator('#tabs').get_by_text('优秀内容').count()==1,plat
  page.locator('#tabs').get_by_text('优秀内容').click()
  assert page.locator('#highlightSection').is_visible()
  assert not page.locator('.metrics').is_visible()
  if plat in ('instagram','tiktok'):
   assert page.locator('.highlightCard').count()==len(next(x for x in profiles if x['platform']==plat)['posts'])
   assert page.locator('.highlightCard .highlightTitle').first.get_attribute('href').startswith('https://')
  else:
   assert '尚未获取到' in page.locator('#highlightCards').inner_text(),plat
  page.locator('#tabs').get_by_text('内容',exact=(plat!='xiaohongshu')).first.click() if plat!='xiaohongshu' else page.locator('#tabs').get_by_text('笔记分析').click()
  assert page.locator('.below').is_visible()
  if plat=='tiktok':
   page.locator('#disclosureDetails').evaluate('(el)=>el.open=true')
   page.locator('#manualCheck').evaluate('(el)=>el.open=true')
   page.locator('#manualFollowers').fill('89900')
   page.locator('#manualLikes').fill('583900')
   page.locator('#manualSave').click()
   page.wait_for_function('() => document.querySelector("#manualStatus").textContent.includes("已保存")',timeout=5000)
   val=page.locator('#detailstats').inner_text()
   assert '583.9K' in val,val
   page.screenshot(path=str(root/'V10_TikTok_Highlights.png'),full_page=True)
  if plat=='instagram':
   page.locator('#tabs').get_by_text('优秀内容').click()
   page.screenshot(path=str(root/'V10_Instagram_Highlights.png'),full_page=True)
   page.locator('#tabs').get_by_text('内容',exact=True).click()
  if plat=='facebook':
   val=page.locator('#detailstats').inner_text()
   assert '28天内容计划量' not in val
  print(' pass')
 assert not errors,errors
 b.close()
 print('Browser QA PASS six platforms, highlight, links, sidebars')
