"""CaseLens V7 audience training scenario.
The estimated breakdowns are *not* claims about real followers. Only
verified API/imported demographic distributions should be labelled as such.
"""
import hashlib
import random

MARKETS = {
    'global': [('美国',28),('印度',20),('英国',13),('印度尼西亚',12),('菲律宾',9),('其他',18)],
    'north_america':[('美国',62),('加拿大',22),('墨西哥',9),('其他',7)],
    'southeast_asia':[('印度尼西亚',25),('马来西亚',23),('新加坡',19),('泰国',14),('菲律宾',11),('其他',8)],
    'europe':[('英国',24),('德国',20),('法国',17),('意大利',13),('西班牙',11),('其他',15)],
    'hong_kong':[('中国香港',65),('中国内地',15),('中国台湾',10),('新加坡',5),('其他',5)],
    'japan':[('日本',75),('中国台湾',8),('韩国',6),('美国',5),('其他',6)],
    'australia':[('澳大利亚',67),('新西兰',13),('英国',8),('美国',6),('其他',6)],
    'africa':[('尼日利亚',30),('南非',20),('肯尼亚',16),('加纳',14),('埃及',10),('其他',10)]
}
AGE_KEYS=['13–17','18–24','25–34','35–44','45–54','55–64','65+']
AGE_BASE={
    'youtube':[5,20,29,23,12,7,4], 'tiktok':[12,33,29,15,7,3,1],
    'instagram':[8,30,32,18,8,3,1], 'facebook':[3,15,25,26,17,9,5],
    'linkedin':[0,13,37,30,15,4,1]
}
FEMALE={'youtube':44,'tiktok':55,'instagram':56,'facebook':52,'linkedin':46}

def _scale(items):
    vals=[max(0.0,float(v)) for _,v in items]
    total=sum(vals) or 1
    raw=[v*100/total for v in vals]
    ints=[int(x) for x in raw]
    remainder=100-sum(ints)
    order=sorted(range(len(ints)),key=lambda i:raw[i]-ints[i],reverse=True)
    for i in order[:remainder]:ints[i]+=1
    return [{'name':str(items[i][0]),'percent':ints[i]} for i in range(len(items))]

def estimate_audience(account, assumptions=None):
    """Stable default scenario. Parameters describe *intended training market*, not geo detection."""
    assumptions=assumptions or {}
    market=assumptions.get('market','global')
    if market not in MARKETS:market='global'
    platform=account['platform']
    seed=int(hashlib.sha256((account['url']+'|audience-v7').encode()).hexdigest()[:12],16)
    rng=random.Random(seed)
    countries=[(name,pct*(.94+.12*rng.random())) for name,pct in MARKETS[market]]
    countries=_scale(countries)
    female=assumptions.get('female_pct')
    try: female=float(female) if female is not None else FEMALE[platform]+rng.randint(-4,4)
    except (TypeError,ValueError):female=FEMALE[platform]
    female=max(0,min(100,round(female)))
    gender=[{'name':'女性','percent':female},{'name':'男性','percent':100-female}]
    ages=_scale([(age,pct*(.92+.16*rng.random())) for age,pct in zip(AGE_KEYS,AGE_BASE[platform])])
    return {'source':'training-scenario','countries':countries,'genders':gender,'ages':ages,
            'market':market,'note':'用于公司内部培训的情景分布。国家、性别、年龄不是该账号的实测粉丝数据。'}

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
