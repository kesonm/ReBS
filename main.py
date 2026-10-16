from datetime import date
from pathlib import Path

import akshare as ak

symbol = "sz002027"  # 深圳用 sz，上海用 sh，例如 sh600519

df = ak.stock_zh_a_hist_tx(
    symbol=symbol,
    start_date="19000101",
    end_date=date.today().strftime("%Y%m%d"),
    adjust="",  # 不复权；AKShare 使用空字符串，bfq 仅作为保存文件的标签
    timeout=15,
)

if df.empty:
    raise RuntimeError("没有获取到行情，请检查股票代码和数据源")

# 按日期去重、排序
df = (
    df.drop_duplicates(subset=["date"], keep="last")
    .sort_values("date")
    .reset_index(drop=True)
)

print(f"共获取 {len(df)} 条日线")
print(f"日期范围：{df['date'].iloc[0]} 至 {df['date'].iloc[-1]}")
print(df.head())
print(df.tail())

# 保存到当前脚本所在目录
output = Path(__file__).resolve().parent / f"{symbol}_daily_bfq.csv"
df.to_csv(output, index=False, encoding="utf-8-sig")
print(f"已保存：{output}")
