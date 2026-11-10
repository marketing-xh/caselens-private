"""CaseLens V7 - internal training forecasts, separate from verified Insights.

This module needs no API credentials. A profile with public metrics gets an
account-informed estimate; one without public data gets a *generic training
scenario* with explicitly disclosed, adjustable assumptions.
"""
from datetime import datetime, timedelta, timezone
from audience import estimate_audience, origin_context
import hashlib
import math
import random
import statistics


def _num(value):
    try:
        v = float(value)
        return v if math.isfinite(v) and v >= 0 else None
    except (TypeError, ValueError):
        return None


def _bound(v, lo, hi):
    return max(lo, min(hi, v))


def _round(v):
    return max(0, int(round(v)))


def _mix_values(platform, views, followers, p, observed=None):
    r = {'youtube': (0.028, 0.005, 0.0008), 'tiktok': (0.065, 0.008, 0.0013),
         'facebook': (0.028, 0.006, 0.0008), 'instagram': (0.057, 0.006, 0.0010), 'linkedin': (0.035, 0.012, 0.0006),
         'xiaohongshu': (0.063, 0.006, 0.0025)}
    l, c, f = r[platform]
    if observed:
        # Public video engagement is cumulative, so these ratios are only
        # calibration inputs for the hypothetical period model.
        l = _bound(observed.get('likes', l), 0.001, 0.30)
        c = _bound(observed.get('comments', c), 0.0001, 0.05)
    likes = _round(views * l)
    comments = _round(views * c)
    gained = _round(views * f)
    if platform == 'xiaohongshu':
        reads=_round(views)
        impressions=_round(views*3.2)
        saves=_round(views*.029)
        shares=_round(views*.009)
        return {'impressions':impressions,'reads':reads,'ctr':round(100*reads/max(1,impressions),1),
                'likes':likes,'favorites':saves,'comments':comments,'shares':shares,
                'interactions':likes+saves+comments+shares,'new_followers':gained,
                'unfollows':_round(gained*.21),'profile_visits':_round(views*.04),
                'search_reads':_round(views*.27), 'recommend_reads':_round(views*.61),
                'follow_reads':_round(views*.12), 'note_count':_round(p/28),
                'engagement_rate':round(100*(likes+saves+comments+shares)/max(1,reads),2)}
    if platform == 'youtube':
        return {'views': _round(views), 'engaged_views': _round(views*0.51),
                'likes':likes, 'comments':comments, 'shares':_round(views*.0035), 'subscribers_gained':gained,
                'subscribers_lost':_round(gained*.25),
                'impressions':_round(views*2.8), 'impressions_ctr':6.2,
                'watch_time_hours':_round(views*0.75/60), 'average_view_duration':45,
                'unique_viewers':_round(views*.76), 'returning_viewers':_round(views*.17)}
    if platform == 'tiktok':
        return {'video_views':_round(views),'profile_views':_round(views*.032),
                'likes':likes,'comments':comments,'shares':_round(views*.014),
                'new_followers':gained,'unfollows':_round(gained*.20),
                'reach':_round(views*.75),'average_watch_time':13.4,
                'completion_rate':32.0}
    if platform == 'instagram':
        return {'views':_round(views), 'reach':_round(views*.73),
                'impressions':_round(views*1.35),'profile_visits':_round(views*.042),
                'likes':likes,'comments':comments,'shares':_round(views*.008),
                'saves':_round(views*.014),'accounts_engaged':_round(views*.072),
                'new_followers':gained,'unfollows':_round(gained*.22),
                'engagement_rate':7.2}
    if platform == 'facebook':
        interactions=_round(views*.065)
        return {'views':_round(views), 'reach':_round(views*.68),'interactions':interactions,
                'reactions':likes,'comments':comments,'shares':_round(views*.007),
                'new_followers':gained,'unfollows':_round(gained*.24),
                'page_visits':_round(views*.036),'link_clicks':_round(views*.009),
                'engagement_rate':6.5}
    impr=_round(views*2.5)
    page_views=_round(impr*.026)
    leads=_round(page_views*.008)
    return {'impressions':impr,'reactions':_round(impr*.029),'comments':_round(impr*.004),
            'reposts':_round(impr*.002),'page_views':page_views,
            'unique_visitors':_round(page_views*.78),
            'custom_button_clicks':_round(page_views*.055), 'clicks':_round(impr*.038),
            'new_followers':_round(impr*.0017), 'organic_followers':_round(impr*.00155),
            'paid_followers':_round(impr*.00015),'search_appearances':_round(impr*.28),
            'competitor_followers':_round(followers*1.3), 'competitor_posts':p*.8/28,
            'competitor_engagement':_round(impr*.049), 'share_of_voice':29.8,
            'leads':leads, 'lead_form_opens':_round(page_views*.042),
            'lead_form_completions':leads, 'conversion_rate':_bound(.8,0,100)}


def estimate(profile, account, assumptions=None):
    """Build 90 deterministic scenario daily rows (never actual dated Insights)."""
    assumptions=assumptions or {}
    platform=account['platform']
    public=profile.get('stats') or {}
    source=profile.get('quality') not in ('scenario', 'estimate-only')
    public_followers=_num(public.get('followers'))
    manual_followers=_num(assumptions.get('followers'))
    if manual_followers is not None:
        followers=manual_followers
        follower_basis='内部手动假设'
    elif public_followers is not None:
        followers=public_followers
        follower_basis='当前公开/授权粉丝基准'
    else:
        # Stable but explicitly hypothetical account-specific base for internal training.
        # No inference of an actual person's followers is being made here.
        stable=int(hashlib.sha256((account['url']+'|followers-v8').encode('utf-8')).hexdigest()[:8],16)
        followers=float(2000 + 100 * (stable % 650))
        follower_basis='该账号无可核实粉丝数，按账号链接生成稳定的培训假设（非真实粉丝）'
    observed_posts=[x for x in profile.get('posts', []) if isinstance(x,dict) and x.get('url')]
    recents=[x for x in observed_posts if _num(x.get('views')) is not None]
    recent_views=[_num(x.get('views')) for x in recents]
    median_views=statistics.median(recent_views) if recent_views else None
    # When real per-video public likes/comments exist, let this account's
    # engagement mix influence training estimates rather than one fixed ratio.
    rates={}
    if platform=='youtube':
        usable=[v for v in recents if (_num(v.get('views')) or 0)>=100]
        for field in ('likes','comments'):
            samples=[_num(v.get(field))/_num(v.get('views'))
                     for v in usable if _num(v.get(field)) is not None]
            if len(samples)>=3:
                rates[field]=statistics.median(samples)
    # Video views are cumulative as of scrape, not views earned in a 28-day interval.
    today=datetime.now(timezone.utc).date()
    active28=sum(1 for x in observed_posts if x.get('date') and (today-timedelta(days=27)).isoformat() <= x['date'] <= today.isoformat())
    active90=sum(1 for x in observed_posts if x.get('date') and (today-timedelta(days=89)).isoformat() <= x['date'] <= today.isoformat())
    manually_posts=_num(assumptions.get('posts_per_month'))
    if manually_posts is not None:
        posts28=_bound(manually_posts,0,90)
        posts_basis='手动发布频率假设'
    elif active90:
        # A subset of public posts is not guaranteed to be exhaustive.
        # Use the observed cadence as a floor for a *training* estimate only.
        if active28>=3:
            posts28=float(active28)
            posts_basis='采样中近28天已确认发布作品数；这是下限，未核实采样完整性'
        else:
            posts28=_bound(active90*28/90,1,90)
            posts_basis='采样中近90天已确认作品的频率估算（样本不一定完整）'
    else:
        # No arbitrary 8-post constant. Stable account-specific *scenario*,
        # never displayed as observed 28-day posting activity.
        stamp=int(hashlib.sha256((account['url']+'|training-cadence-v10').encode()).hexdigest()[:10],16)
        options={'youtube':(2,10),'tiktok':(5,20),'instagram':(3,16),'facebook':(2,14),
                 'linkedin':(2,12),'xiaohongshu':(4,20)}
        mn,mx=options[platform]
        posts28=float(mn+stamp%(mx-mn+1))
        posts_basis='针对当前账号创建的培训发文频率假设，非历史发布数量'
    manual_avg=_num(assumptions.get('views_per_post'))
    if manual_avg is not None:
        average=max(1,manual_avg)
        video_basis='内部手动每条观看假设'
    elif median_views is not None:
        average=max(1,median_views)
        video_basis='最近可读取内容的累计观看中位数（不是周期内观看）'
    else:
        average=max(50,followers*({'youtube':.11,'tiktok':.44,'facebook':.16,'instagram':.30,'linkedin':.09,'xiaohongshu':.38}[platform]))
        video_basis='缺少公开内容，按粉丝规模使用通用经验假设'
    # Only a portion of each post's cumulative views plausibly arises in a given window.
    if platform=='linkedin':
        month_views=posts28*average*.75
    elif platform in ('facebook','instagram','xiaohongshu'):
        month_views=posts28*average*.85
    else:
        month_views=posts28*average*.63 + followers*({'youtube':.13,'tiktok':.22}[platform])
    manual28=_num(assumptions.get('views_28d'))
    if manual28 is not None:
        month_views=manual28
        month_basis='内部手动设定的近28天基准'
    else:
        month_basis='由公开视频、粉丝规模与发文频率进行情景推算'
    month_views=_bound(month_views,0,1e10)
    daily_base=month_views/28
    key=(account['url']+'|caselens-v5-training').encode('utf-8')
    seed=int(hashlib.sha256(key).hexdigest()[:15],16)
    rng=random.Random(seed)
    days=90
    factors=[]
    for index in range(days):
        j=days-1-index
        x=0.75 + .65*rng.random()
        # Exposure volatility: sporadic strong posts plus mild recent activity bias.
        if rng.random()<.08:x*=1.7+rng.random()*1.8
        x*=1.0+0.12*math.sin(index*.15)
        if j<=9:x*=1.10
        factors.append(x)
    last28sum=sum(factors[-28:]) or 1
    scale=month_views/last28sum
    series=[]
    for i,factor in enumerate(factors):
        date=(today-timedelta(days=days-i-1)).isoformat()
        row={'date':date}
        row.update(_mix_values(platform,factor*scale,followers,posts28,rates))
        series.append(row)
    if platform=='youtube' and source:
        level='低—中（只有公开频道及视频累计数据）' if recent_views else '低（没有近期视频样本）'
    elif source and public_followers is not None:
        level='低（有粉丝基准，但无私有后台数据）'
    else:
        level='情景假设（没有该账号可核实的公开基准）'
    note=[
        '所有按日曲线和周期指标均为模型生成的培训估算，不是平台真实 Insights；日期只是演示时间轴。',
        '公开视频累计观看与视频发布于何日，不代表这些观看全部发生在选定日期内。',
        '没有提供账号授权或后台原始报表，不能从链接反推出真正的触达、观看时长、涨粉或线索。',
        f'估算基础：{follower_basis}；{posts_basis}；{video_basis}。',
        '互动数据：有足够公开视频样本时以该账号点赞/评论相对播放量的中位比例校准，否则使用训练假设；分享量仍需估算。',
        '指标使用固定种子生成，允许比较7/28/90天，但不代表平台后台真实日波动。'
    ]
    model={'version':'5.0', 'type':'account-informed' if source and (public_followers is not None or median_views is not None) else 'generic-training-scenario',
           'confidence':level,'followers_basis':follower_basis,'followers_assumed':_round(followers),
           'posts_per_28d':round(posts28,1),'views_per_post':_round(average),
           'views_28d_baseline':_round(month_views), 'estimate_28d_low':_round(month_views*.5),
           'estimate_28d_high':_round(month_views*1.8), 'method':month_basis,
           'public_recent_posts':len(observed_posts), 'public_recent_posts_28d':active28, 'public_recent_posts_90d':active90,
           'historical_posts_completeness':'unknown', 'posts_frequency_basis':posts_basis,
           'generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
           'assumptions':{k:v for k,v in assumptions.items() if k in ('followers','posts_per_month','views_per_post','views_28d','market','female_pct')}}
    return {'series':series,'estimate_model':model, 'estimate_notes':note, 'audience':estimate_audience(account,assumptions,profile)}


def scenario_profile(account, reason=None):
    platform=account['platform']
    notes=['未取得该账号的当前公开数据，因此此页仅用于内部培训情景；不能作为该账号实际运营表现。']
    if reason:notes.append('公开采集状态：'+str(reason)[:180])
    return {'platform':platform,'name':account['id'],'url':account['url'],'avatar':None,'stats':{},
            'posts':[],'quality':'scenario','source':'内部培训情景（公开资料未取得）',
            'notes':notes}
