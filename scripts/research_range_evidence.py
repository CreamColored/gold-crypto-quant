"""独立震荡识别实验；只读行情，不修改线上状态。

冻结方案：20根窗口；方向效率<=0.25；窗口前后半部价格中位数漂移
<=全窗口宽度20%；跨越中央40%-60%区域至少两次。
比较全触轨、V5.9、上述价格结构条件、结构条件加ADX<20。
所有识别使用上一根已收盘数据。简化交易不是V5.9复现：触轨入场，
固定当时中轨全平，止损距离等于目标距离，最多持有16根；同根先止损。
各币种周期独立，单仓不重叠，25U固定风险，费用maker 2bp/taker 5bp，
止损和到期出场滑点2bp；没有复利、账户熔断、减仓或反手。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from gold_crypto_quant.config import get_settings
from gold_crypto_quant.strategy.bollinger_range import (
    build_rotation_box_context, parameters_for_same_timeframe,
)


def masks(b, interval, symbol):
    # 全历史批算时，BTC价格比例必须逐行取历史中位数，不能取数据末尾价格。
    params = parameters_for_same_timeframe(interval)
    ctx = build_rotation_box_context(b, params)
    scale = (b.close.rolling(20).median()/2500).clip(lower=1) if symbol=='BTC_USDT' else 1
    flat = pd.Series(True,index=b.index)
    for column in ('bb_upper','bb_middle','bb_lower'):
        flat &= ctx[column].rolling(3).max()-ctx[column].rolling(3).min() <= params.maximum_band_drift*scale
    ctx['box_candidate'] = (flat & (ctx.bb_width>=params.minimum_bandwidth*scale)
        & (ctx.volume_ratio<=2) & ~ctx.breakout
        & (ctx.relative_width<=params.maximum_relative_width)
        & (ctx.middle_crossings>=3) & (ctx.width_growth<=50)
        & (ctx.histogram_scale<=params.maximum_macd_histogram)).fillna(False)
    c = b.close
    lo, hi = b.low.rolling(20).min(), b.high.rolling(20).max()
    width = (hi-lo).replace(0, np.nan)
    er = c.diff(20).abs()/c.diff().abs().rolling(20).sum().replace(0, np.nan)
    drift = (c.rolling(10).median()-c.shift(10).rolling(10).median()).abs()/width
    # 逐窗口固定边界，跨越中央缓冲区域才记一次有效往返。
    crossings = np.zeros(len(b))
    cv, lv, wv = c.to_numpy(), lo.to_numpy(), width.to_numpy()
    for i in range(20, len(b)):
        last, count = 0, 0
        for v in cv[i-19:i+1]:
            side = -1 if v < lv[i]+0.4*wv[i] else (1 if v > lv[i]+0.6*wv[i] else 0)
            if side:
                if last and side != last:
                    count += 1
                last = side
        crossings[i] = count
    structure = (er <= .25) & (drift <= .20) & (crossings >= 2)
    return ctx, {'all': pd.Series(True,index=b.index),
                 'v59': ctx.box_candidate,
                 'structure': structure,
                 'structure_adx': structure & (ctx.adx < 20)}


def replay(b, ctx, allowed, minutes):
    a=b[['open','high','low','close']].to_numpy()
    bands=ctx[['bb_upper','bb_middle','bb_lower']].to_numpy()
    times=b.index
    records=[]
    i=60
    while i < len(b)-16:
        if not allowed.iloc[i-1] or not np.isfinite(bands[i-1]).all():
            i+=1; continue
        upper,middle,lower=bands[i-1]
        # 跳空越轨不假设获得轨道价；双轨同触无法确定方向也跳过。
        if not lower < a[i,0] < upper:
            i+=1; continue
        up,down=a[i,1]>=upper,a[i,2]<=lower
        if up==down:
            i+=1; continue
        direction=-1 if up else 1
        entry=upper if up else lower
        distance=abs(middle-entry)
        stop=entry-direction*distance
        qty=25/(distance+entry*.0002+stop*.0005+stop*.0002)
        # 不跨时间分组边界，也不穿越缺失K线。
        segment=lambda t: 'train' if t.year<=2022 else ('validation' if t < pd.Timestamp('2024-09-01',tz='UTC') else 'historical_check')
        if segment(times[i])!=segment(times[i+16]):
            i+=1; continue
        if ((times[i:i+17].to_series().diff().dropna()/pd.Timedelta(minutes=minutes))!=1).any():
            i+=1; continue
        exit_price=a[i+16,3]*(1-direction*.0002); fee=.0005; reason='timeout'; end=i+16
        for j in range(i,i+17):
            stop_hit=a[j,2]<=stop if direction==1 else a[j,1]>=stop
            target_hit=a[j,1]>=middle if direction==1 else a[j,2]<=middle
            if stop_hit:
                ref=(min(stop,a[j,0]) if direction==1 else max(stop,a[j,0])) if j>i else stop
                exit_price=ref*(1-direction*.0002);reason='stop';end=j;break
            # 入场当根的中轨触碰可能发生在入场前，因此不记同根止盈。
            if j>i and target_hit:
                exit_price=middle;fee=.0002;reason='target';end=j;break
        net=qty*(direction*(exit_price-entry)-entry*.0002-exit_price*fee)
        records.append({'time':str(times[i]),'split':segment(times[i]),'net':net,'reason':reason})
        i=end+1
    return records


def stats(records):
    p=np.array([x['net'] for x in records])
    # 25U标准化单笔结果转成0.25%风险复利，避免固定风险账本破产后仍计回撤。
    curve=np.r_[10000,10000*np.cumprod(1+p/10000)]
    peak=np.maximum.accumulate(curve)
    return dict(trades=len(p),net=round(float(p.sum()),2),win_pct=round(float((p>0).mean()*100),2) if len(p) else None,
                pf=round(float(p[p>0].sum()/-p[p<0].sum()),3) if (p<0).any() else None,
                return_pct=round(float((curve[-1]/10000-1)*100),3),dd_pct=round(float(((peak-curve)/peak).max()*100),3),
                target_pct=round(100*sum(x['reason']=='target' for x in records)/len(records),2) if records else None)


def main():
    out=Path('var/research-range-20260906');out.mkdir(parents=True,exist_ok=True)
    engine=create_engine(get_settings().database_url)
    results=[]; coverage=[]
    for symbol in ('BTC_USDT','ETH_USDT'):
        for interval,minutes in (('15m',15),('30m',30),('1h',60)):
            query=text('SELECT b.open_time,b.open_price AS open,b.high_price AS high,b.low_price AS low,b.close_price AS close,b.volume FROM market_bars b JOIN instruments i ON i.id=b.instrument_id WHERE i.venue=:v AND i.symbol=:s AND b.interval_code=:iv ORDER BY b.open_time')
            b=pd.read_sql(query,engine,params={'v':'BINANCE_LIVE_PUBLIC','s':symbol,'iv':interval}).set_index('open_time').astype(float)
            b.index=pd.DatetimeIndex(b.index).tz_localize('UTC')
            coverage.append(dict(symbol=symbol,interval=interval,rows=len(b),start=str(b.index[0]),end=str(b.index[-1]),gaps=int((b.index.to_series().diff().dropna()!=pd.Timedelta(minutes=minutes)).sum())))
            ctx,candidates=masks(b,interval,symbol)
            for name,allowed in candidates.items():
                records=replay(b,ctx,allowed,minutes)
                pd.DataFrame(records).to_csv(out/f'{symbol}-{interval}-{name}-trades.csv',index=False)
                for split in ('train','validation','historical_check'):
                    row=dict(symbol=symbol,interval=interval,method=name,split=split,**stats([r for r in records if r['split']==split]))
                    results.append(row)
                print(symbol,interval,name,stats(records),flush=True)
    pd.DataFrame(results).to_csv(out/'summary.csv',index=False)
    (out/'coverage.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
