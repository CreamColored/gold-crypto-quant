"""只读币安行情的独立研究，不导入交易执行器、不修改账户。

冻结参数后一次运行：动态三轨走平、固定箱体、固定箱体加换色暂停。
这是受控策略原型，并非运行中策略完整复现；不含多周期结构或账户熔断。
"""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from numba import njit
from sqlalchemy import create_engine, text
from gold_crypto_quant.config import get_settings
from gold_crypto_quant.risk.indicators import average_directional_index

OUT = Path('var/research-anchor-minute-20260906')

def features(b):
    c=b.close; m=c.rolling(20).mean(); sd=c.rolling(20).std(ddof=0)
    u=m+2*sd; l=m-2*sd; w=u-l
    dif=c.ewm(span=12,adjust=False).mean()-c.ewm(span=26,adjust=False).mean()
    hist=dif-dif.ewm(span=9,adjust=False).mean()
    flat=((u.rolling(3).max()-u.rolling(3).min()<=2.5)
          &(m.rolling(3).max()-m.rolling(3).min()<=2.5)
          &(l.rolling(3).max()-l.rolling(3).min()<=2.5)&(w>=6))
    # 调用过去窗口计算：收缩、中心漂移和往返均不依赖未来价格。
    crossings=((c>m)!=(c.shift()>m.shift())).rolling(12).sum()
    candidate=(w>=6)&(w<=w.rolling(20).max()*.8)&((m-m.shift(6)).abs()<=w*.15)&(crossings>=2)
    flip=(np.sign(hist)!=np.sign(hist.shift())).rolling(2).max().fillna(1)
    # TradingView 的 CHOP(14)：高值偏横盘；ADX(14)低值表示趋势较弱。
    previous_close=c.shift(1)
    tr=pd.concat([(b.high-b.low),(b.high-previous_close).abs(),(b.low-previous_close).abs()],axis=1).max(axis=1)
    chop=100*np.log10(tr.rolling(14).sum()/(b.high.rolling(14).max()-b.low.rolling(14).min()).replace(0,np.nan))/np.log10(14)
    adx=average_directional_index(b,14)
    bbw=w/m*100
    # 252根仅作同品种同周期的相对宽度参照；不使用全历史分位数，避免未来数据。
    bbw_percentile=bbw.rolling(252).rank(pct=True)*100
    bbw_growth3=bbw/bbw.shift(3)-1
    return np.column_stack([u,m,l,flat.fillna(False),candidate.fillna(False),flip,c,
                            chop,adx,bbw_percentile,bbw_growth3])

@njit
def replay(a,t,f,ft,mode,start):
    # 每分钟最多持有一笔；记录完整交易（含减仓），不是把两次平仓算两单。
    out=np.zeros((len(a),8)); n=0; k=-1; prevk=-2
    live=False; upper=0.; lower=0.; born=0; blocked=-1
    direction=0; entry=0.; stop=0.; target=0.; middle=0.; qty=0.; remaining=0.; pnl=0.; entered=0; partial=False
    gaps=0; active=0
    for i in range(len(a)):
        while k+1<len(ft) and ft[k+1]<=t[i]: k+=1
        if k<0 or t[i]<start: continue
        gap=i>0 and t[i]-t[i-1]!=60
        if gap: gaps+=1;live=False;blocked=k+1
        if k!=prevk:
            if live and (f[k,6]>upper+3 or f[k,6]<lower-3 or k-born>=96):
                live=False; blocked=k+1
            if mode>0 and not live and k>=blocked and f[k,4]>0:
                upper=f[k,0];lower=f[k,2];born=k;live=True
            prevk=k
        eligible=(f[k,3]>0) if mode==0 else live
        if mode==2 and f[k,5]>0: eligible=False
        if mode in (3,5,6,7) and not f[k,7]>=61.8: eligible=False
        if mode in (4,5,6,7) and not f[k,8]<20: eligible=False
        # 极低BBW可能是突破前挤压；单独测试排除过去252根最低10%的情形。
        if mode in (6,7) and not f[k,9]>10: eligible=False
        # 带宽最近3根扩张超过20%时暂停，避免把已开始开口仍当作旧箱体。
        if mode==7 and not f[k,10]<=.20: eligible=False
        if eligible: active+=1
        o,h,lo,c=a[i]
        if direction!=0:
            hit=(lo<=stop) if direction==1 else (h>=stop)
            reason=0
            if gap or hit:
                ref=o if gap else (min(o,stop) if direction==1 else max(o,stop))
                price=ref*(1-direction*.0002)
                pnl+=remaining*(direction*(price-entry)-price*.0005)
                reason=4 if gap else (2 if partial else 1)
                blocked=k+1
            else:
                # 入场当分钟不猜测高低点顺序；其余分钟仍优先检查原止损。
                if not partial and ((h>=middle) if direction==1 else (lo<=middle)):
                    price=middle*(1-direction*.0002)
                    pnl+=qty*.5*(direction*(price-entry)-price*.0005)
                    remaining=qty*.5;partial=True;stop=entry
                    # 同一分钟减仓与保本均可能触发时，采取保本退出的保守路径。
                    if (lo<=entry) if direction==1 else (h>=entry):
                        price=entry*(1-direction*.0002)
                        pnl+=remaining*(direction*(price-entry)-price*.0005);reason=2
                if reason==0 and ((h>=target) if direction==1 else (lo<=target)):
                    price=target*(1-direction*.0002)
                    pnl+=remaining*(direction*(price-entry)-price*.0005);reason=3
                if reason==0 and (i==len(a)-1 or t[i]-t[entered]>=86400):
                    price=c*(1-direction*.0002)
                    pnl+=remaining*(direction*(price-entry)-price*.0005);reason=5
            if reason:
                out[n]=np.array([t[entered],t[i],direction,entry,pnl,reason,int(partial),qty]);n+=1;direction=0
            continue
        if not eligible or k<blocked or gap or i==len(a)-1: continue
        u,m,l=f[k,0],f[k,1],f[k,2]
        if not np.isfinite(u) or not l<o<u: continue
        up=h>=u;down=lo<=l
        if up==down: continue
        d=-1 if up else 1; ref=u if up else l
        if mode>0 and (ref<lower-3 or ref>upper+3 or (ref>lower+.2*(upper-lower) if d==1 else ref<upper-.2*(upper-lower))): continue
        protective=(l-3 if d==1 else u+3) if mode==0 else (lower-3 if d==1 else upper+3)
        fill=ref*(1+d*.0002);distance=d*(fill-protective)
        if distance<=0 or distance>10: continue
        direction=d;entry=fill;stop=protective;target=u if d==1 else l
        middle=m-d*2 if d*(m-fill)>10 else m
        if d*(middle-fill)<=0: direction=0;continue
        # 25U为每次计划风险，包含开平手续费与止损滑点；不是杠杆收益。
        qty=25/(distance+fill*.0005+protective*.0007);remaining=qty;pnl=-qty*fill*.0005;entered=i;partial=False
        if (lo<=stop) if d==1 else (h>=stop):
            price=stop*(1-d*.0002);pnl+=qty*(d*(price-entry)-price*.0005)
            out[n]=np.array([t[i],t[i],d,entry,pnl,1,0,qty]);n+=1;direction=0;blocked=k+1
    return out[:n],gaps,active

def load(engine,interval,start,end):
    with engine.connect() as conn:
        b=pd.read_sql(text('''SELECT b.open_time,b.open_price AS open,b.high_price AS high,
          b.low_price AS low,b.close_price AS close FROM market_bars b JOIN instruments i ON i.id=b.instrument_id
          WHERE i.symbol='ETH_USDT' AND i.venue='BINANCE_LIVE_PUBLIC' AND b.interval_code=:iv
          AND b.open_time>=:start AND b.open_time<:end ORDER BY b.open_time'''),conn,
          params={'iv':interval,'start':start.to_pydatetime().replace(tzinfo=None),'end':end.to_pydatetime().replace(tzinfo=None)})
    b.index=pd.DatetimeIndex(pd.to_datetime(b.pop('open_time'),utc=True));return b.astype(float)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    engine=create_engine(get_settings().database_url)
    end=pd.Timestamp('2026-09-06T05:00:00Z') # 冻结共同结束点，不追随正在新增的行情。
    windows=[(str(y),pd.Timestamp(f'{y}-01-01',tz='UTC'),min(pd.Timestamp(f'{y+1}-01-01',tz='UTC'),end)) for y in range(2020,2027)]
    windows.append(('last30',end-pd.Timedelta(days=30),end))
    summaries=[]
    for label,start,stop in windows:
        b=load(engine,'15m',start-pd.Timedelta(days=3),stop)
        a=load(engine,'1m',start,stop)
        f=features(b);t=a.index.as_unit('s').asi8;ft=b.index.as_unit('s').asi8+900
        # 对追加未来数据做不变性检查，避免指标批算时偷看未来。
        cut=min(200,len(b));assert np.allclose(features(b.iloc[:cut]),f[:cut],equal_nan=True)
        names=['dynamic_flat','anchor','anchor_pause','anchor_chop','anchor_adx',
               'anchor_chop_adx','anchor_chop_adx_no_squeeze','anchor_chop_adx_guard']
        for mode,name in enumerate(names):
            r,gaps,active=replay(a.to_numpy(),t,f,ft,mode,int(start.timestamp()))
            df=pd.DataFrame(r,columns=['entry_time','exit_time','direction','entry_price','net_usdt','reason','partial','quantity'])
            df.to_csv(OUT/f'{label}_{name}.csv',index=False)
            p=r[:,4];wins=float(p[p>0].sum());loss=float(-p[p<0].sum());cum=np.r_[0,np.cumsum(p)]
            row=dict(period=label,mode=name,trades=len(p),win_rate=float((p>0).mean()) if len(p) else 0,net=round(float(p.sum()),2),pf=wins/loss if loss else None,max_drawdown_usdt=float(np.max(np.maximum.accumulate(cum)-cum)),unreduced_stops=int((r[:,5]==1).sum()),partial_trades=int((r[:,6]==1).sum()),gaps=gaps,minutes=len(a),expected_minutes=int((stop-start).total_seconds()/60),eligible_share=active/len(a))
            summaries.append(row);print(json.dumps(row),flush=True)
        pd.DataFrame(summaries).to_csv(OUT/'summary.csv',index=False)

if __name__=='__main__':main()
