import json, urllib.request

IDX = [
    ("上证指数", "1.000001"),
    ("深证成指", "0.399001"),
    ("创业板指", "0.399006"),
    ("科创50",   "1.000688"),
    ("沪深300",  "1.000300"),
    ("中证1000", "1.000852"),
    ("国证2000", "0.399303"),
]

def fetch(secid):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           "&klt=101&fqt=1&end=20500101&lmt=80" % secid)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d["data"]["klines"]

out = {}
for name, secid in IDX:
    try:
        kl = fetch(secid)
        closes = [float(x.split(",")[2]) for x in kl]
        dates = [x.split(",")[0] for x in kl]
        last = closes[-1]
        row = {"lastDate": dates[-1], "close": round(last, 2)}
        for n in (5, 10, 20, 30, 60):
            if len(closes) >= n:
                ma = sum(closes[-n:]) / n
                row["ma%d" % n] = round(ma, 0)
                row["ma%dDev" % n] = round((last / ma - 1) * 100, 2)
            else:
                row["ma%d" % n] = None
        out[name] = row
    except Exception as e:
        out[name] = {"error": str(e)}

print(json.dumps(out, ensure_ascii=False, indent=1))
