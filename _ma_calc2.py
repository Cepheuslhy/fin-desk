import json, urllib.request, time

IDX = [
    ("上证指数", "1.000001"),
    ("深证成指", "0.399001"),
    ("创业板指", "0.399006"),
    ("科创50",   "1.000688"),
    ("沪深300",  "1.000300"),
    ("中证1000", "1.000852"),
    ("国证2000", "0.399303"),
    ("上证50",   "1.000016"),
    ("中证500",  "1.000905"),
    ("上证180",  "1.000010"),
    ("北证50",   "0.899050"),
    ("红利指数", "1.000015"),
]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def snap(secid):
    url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
           "&fields=f43,f44,f45,f46,f47,f48,f60,f57,f58,f169,f170" % secid)
    d = get(url)["data"]
    return d


def kline(secid, lmt=80):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           "&klt=101&fqt=1&end=20500101&lmt=%d" % (secid, lmt))
    return get(url)["data"]["klines"]


out = {}
for name, secid in IDX:
    rec = {}
    for attempt in range(3):
        try:
            s = snap(secid)
            rec["close"] = round(s["f43"] / 100.0, 2)
            rec["prev"] = round(s["f60"] / 100.0, 2)
            rec["high"] = round(s["f44"] / 100.0, 2)
            rec["low"] = round(s["f45"] / 100.0, 2)
            rec["chgPts"] = round(s["f169"] / 100.0, 2)
            rec["chg"] = round(s["f170"] / 100.0, 2)
            rec["amount"] = round(s["f48"] / 1e8, 0)  # 亿
            rec["ampl"] = round((rec["high"] - rec["low"]) / rec["prev"] * 100, 2) if rec["prev"] else None
            break
        except Exception as e:
            rec["snapErr"] = str(e)
            time.sleep(1)
    for attempt in range(3):
        try:
            kl = kline(secid)
            closes = [float(x.split(",")[2]) for x in kl]
            last = closes[-1]
            rec["lastDate"] = kl[-1].split(",")[0]
            rec["kclose"] = round(last, 2)
            for n in (5, 10, 20, 30, 60):
                if len(closes) >= n:
                    ma = sum(closes[-n:]) / n
                    rec["ma%d" % n] = int(round(ma, 0))
                    rec["ma%dDev" % n] = round((last / ma - 1) * 100, 2)
            rec.pop("klErr", None)
            break
        except Exception as e:
            rec["klErr"] = str(e)
            time.sleep(1)
    out[name] = rec
    time.sleep(0.3)

print(json.dumps(out, ensure_ascii=False, indent=1))
