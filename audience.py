"""CaseLens V7 audience training scenario.
The estimated breakdowns are *not* claims about real followers. Only
verified API/imported demographic distributions should be labelled as such.
"""
import hashlib
import random

MARKETS = {
    'china': [('广东',24),('上海',17),('北京',15),('浙江',14),('江苏',12),('其他',18)],
    'global': [('美国',28),('印度',20),('英国',13),('印度尼西亚',12),('菲律宾',9),('其他',18)],
    'north_america':[('美国',62),('加拿大',22),('墨西哥',9),('其他',7)],
    'southeast_asia':[('印度尼西亚',25),('马来西亚',23),('新加坡',19),('泰国',14),('菲律宾',11),('其他',8)],
    'europe':[('英国',24),('德国',20),('法国',17),('意大利',13),('西班牙',11),('其他',15)],
    'hong_kong':[('中国香港',65),('中国内地',15),('中国台湾',10),('新加坡',5),('其他',5)],
    'japan':[('日本',75),('中国台湾',8),('韩国',6),('美国',5),('其他',6)],
    'australia':[('澳大利亚',67),('新西兰',13),('英国',8),('美国',6),('其他',6)],
    'africa':[('尼日利亚',30),('南非',20),('肯尼亚',16),('加纳',14),('埃及',10),('其他',10)],
    'denmark':[('丹麦',47),('瑞典',15),('德国',12),('挪威',10),('荷兰',8),('其他',8)],
    'germany':[('德国',54),('奥地利',13),('瑞士',10),('荷兰',10),('法国',6),('其他',7)],
    'united_kingdom':[('英国',57),('爱尔兰',11),('美国',11),('德国',10),('其他',11)],
    'united_states':[('美国',67),('加拿大',11),('英国',9),('墨西哥',6),('其他',7)],
    'canada':[('加拿大',60),('美国',24),('英国',8),('其他',8)],
    'netherlands':[('荷兰',48),('比利时',17),('德国',15),('英国',10),('其他',10)],
    'uae':[('阿联酋',48),('沙特阿拉伯',20),('卡塔尔',12),('科威特',8),('其他',12)],
    'malaysia':[('马来西亚',58),('新加坡',17),('印度尼西亚',12),('泰国',6),('其他',7)],
    'singapore':[('新加坡',60),('马来西亚',19),('印度尼西亚',9),('其他',12)]
}
AGE_KEYS=['13–17','18–24','25–34','35–44','45–54','55–64','65+']
AGE_BASE={
    'youtube':[5,20,29,23,12,7,4], 'tiktok':[12,33,29,15,7,3,1],
    'instagram':[8,30,32,18,8,3,1], 'xiaohongshu':[5,28,37,20,7,2,1], 'facebook':[3,15,25,26,17,9,5],
    'linkedin':[0,13,37,30,15,4,1]
}
FEMALE={'youtube':44,'tiktok':55,'instagram':56,'xiaohongshu':68,'facebook':52,'linkedin':46}

def _scale(items):
    vals=[max(0.0,float(v)) for _,v in items]
    total=sum(vals) or 1
    raw=[v*100/total for v in vals]
    ints=[int(x) for x in raw]
    remainder=100-sum(ints)
    order=sorted(range(len(ints)),key=lambda i:raw[i]-ints[i],reverse=True)
    for i in order[:remainder]:ints[i]+=1
    return [{'name':str(items[i][0]),'percent':ints[i]} for i in range(len(items))]

# Explicitly verified company origin, not claimed follower location. Add new entries only
# when a credible public company source establishes it (not from an unverified handle).
KNOWN_ORIGINS={
    'instagram:arturel.studio': ('丹麦','denmark','品牌官网 arturel.com/pages/about（奥胡斯，丹麦）'),
}
COUNTRY_MARKETS={
    'denmark':'denmark','dk':'denmark','丹麦':'denmark',
    'germany':'germany','de':'germany','德国':'germany',
    'united kingdom':'united_kingdom','uk':'united_kingdom','gb':'united_kingdom','英国':'united_kingdom',
    'united states':'united_states','us':'united_states','usa':'united_states','美国':'united_states',
    'canada':'canada','ca':'canada','加拿大':'canada',
    'netherlands':'netherlands','nl':'netherlands','荷兰':'netherlands',
    'united arab emirates':'uae','uae':'uae','阿联酋':'uae',
    'malaysia':'malaysia','my':'malaysia','马来西亚':'malaysia',
    'singapore':'singapore','sg':'singapore','新加坡':'singapore',
    'japan':'japan','jp':'japan','日本':'japan',
    'australia':'australia','au':'australia','澳大利亚':'australia',
    'hong kong':'hong_kong','hk':'hong_kong','香港':'hong_kong',
}

def origin_context(account,profile=None):
    platform=account.get('platform','')
    ident=account.get('id','').lower()
    known=KNOWN_ORIGINS.get(f'{platform}:{ident}')
    if known:
        return {'name':known[0],'market':known[1],'basis':known[2],'type':'verified-company-origin'}
    profile=profile or {}
    # Only explicit profile fields, never infer from an account's arbitrary username.
    for key in ('account_country','country','location_country'):
        raw=profile.get(key)
        if isinstance(raw,str) and raw.strip().lower() in COUNTRY_MARKETS:
            return {'name':raw.strip(),'market':COUNTRY_MARKETS[raw.strip().lower()],
                    'basis':'公开资料明确标注的账号所在地（不是粉丝所在地）','type':'public-profile-location'}
    return None

def estimate_audience(account,assumptions=None,profile=None):
    """Account-specific training-market hypothesis, never observed follower geography."""
    assumptions=assumptions or {}
    origin=origin_context(account,profile)
    manual_market=assumptions.get('market')
    if manual_market and manual_market not in ('auto',):
        market=manual_market if manual_market in MARKETS else 'global'
        basis='手动指定的培训目标市场'
    elif origin:
        market=origin['market'];basis='已核实品牌所在地：'+origin['basis']+'；仅用作培训市场假设'
    elif account['platform']=='xiaohongshu':
        market='china';basis='小红书内地市场训练基准（非真实受众）'
    else:
        market='global';basis='未核实账号位置，使用账号专属全球培训情景'
    platform=account['platform']
    seed=int(hashlib.sha256((account['url']+'|audience-v9').encode()).hexdigest()[:12],16)
    rng=random.Random(seed)
    if market=='global':
        # Unlike V8 the same US-led breakdown is not copied into every account.
        pool=['美国','英国','德国','法国','印度','巴西','日本','印度尼西亚','加拿大','澳大利亚','丹麦','新加坡','墨西哥']
        rng.shuffle(pool)
        picked=pool[:5]; weights=[35,22,16,11,8]
        countries=[(n,w*(.8+.4*rng.random())) for n,w in zip(picked,weights)]+[('其他',8)]
    else:
        countries=[(name,pct*(.94+.12*rng.random())) for name,pct in MARKETS[market]]
    countries=_scale(countries)
    female=assumptions.get('female_pct')
    try: female=float(female) if female is not None else FEMALE[platform]+rng.randint(-4,4)
    except (TypeError,ValueError):female=FEMALE[platform]
    female=max(0,min(100,round(female)))
    gender=[{'name':'女性','percent':female},{'name':'男性','percent':100-female}]
    ages=_scale([(age,pct*(.92+.16*rng.random())) for age,pct in zip(AGE_KEYS,AGE_BASE[platform])])
    return {'version':9,'source':'training-scenario','countries':countries,'genders':gender,'ages':ages,
            'market':market,'market_basis':basis,'account_origin':origin,
            'note':'此分布是针对该账号背景设定的内部培训情景，不是账号实测粉丝地理/性别/年龄结构。'}

def normalized_audience(raw):
    """Sanitize imported/authorized demographics: counts or percents, return normalized percent sets."""
    if not isinstance(raw,dict):return None
    cleaned={}
    for key in ('countries','genders','ages'):
        x=raw.get(key)
        if isinstance(x,dict): x=[{'name':k,'value':v} for k,v in x.items()]
        if not isinstance(x,list):continue
        pairs=[]
        for item in x[:60]:
            if isinstance(item,dict):
                name=str(item.get('name',''))[:55]
                value=item.get('value',item.get('percent'))
            else:continue
            try: value=float(value)
            except (ValueError,TypeError):continue
            if name and value>=0:pairs.append((name,value))
        if pairs:cleaned[key]=_scale(pairs)
    return cleaned if cleaned else None
