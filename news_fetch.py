import json, urllib.request, os, sys
from datetime import datetime, timezone, timedelta

today = "2026-08-07"
news_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news")
news_file = os.path.join(news_dir, f"{today}.json")
index_file = os.path.join(news_dir, "index.json")

CHANNELS = [
    ("global-channel", "宏观"),
    ("a-stock-channel", "A股"),
    ("us-stock-channel", "美股"),
    ("xgb-channel", "港股"),
    ("commodity-channel", "大宗商品"),
    ("forex-channel", "外汇"),
    ("bond-channel", "债券"),
    ("financing-channel", "金融"),
    ("tmt-channel", "TMT"),
]

def fetch_channel(channel):
    url = f"https://api-one.wallstcn.com/apiv1/content/lives?channel={channel}&limit=200&first=0"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("data", {}).get("items", [])
        return [(it["id"], it["display_time"], it.get("content_text",""), it.get("channels",[channel])[0]) for it in items if it.get("content_text","").strip()]
    except Exception as e:
        print(f"  FAIL {channel}: {e}", file=sys.stderr)
        return []

print("Fetching 9 channels...")
all_items = {}
for ch, label in CHANNELS:
    items = fetch_channel(ch)
    print(f"  {label} ({ch}): {len(items)} items")
    for (iid, ts, txt, cl) in items:
        if iid not in all_items:
            tz = timezone(timedelta(hours=8))
            dt = datetime.fromtimestamp(ts, tz=tz)
            t_str = dt.strftime("%Y-%m-%dT%H:%M")
            all_items[iid] = {
                "i": iid,
                "t": t_str,
                "c": txt,
                "ch": label,
                "cl": cl,
                "u": f"/livenews/{iid}"
            }

all_new = sorted(all_items.values(), key=lambda x: x["i"], reverse=True)
print(f"\nTotal after dedup: {len(all_new)} items")

# Read existing
existing_ids = set()
existing_items = []
if os.path.exists(news_file):
    with open(news_file, "r", encoding="utf-8") as f:
        old = json.load(f)
    existing_ids = {it["i"] for it in old.get("items", [])}
    existing_items = old.get("items", [])
    print(f"Existing: {len(existing_ids)} items")

# Find new
truly_new = [it for it in all_new if it["i"] not in existing_ids]
print(f"New: {len(truly_new)} items")

# Merge: prepend new items
merged = truly_new + existing_items
# Truncate at 1MB
data = {"date": today, "count": len(merged), "items": merged}
raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
while len(raw.encode("utf-8")) > 1024 * 1024:
    merged = merged[:len(merged)-50]
    data = {"date": today, "count": len(merged), "items": merged}
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

with open(news_file, "w", encoding="utf-8") as f:
    f.write(raw)
print(f"Saved: {len(merged)} items, {len(raw.encode('utf-8'))} bytes")

# Update index
if os.path.exists(index_file):
    with open(index_file, "r", encoding="utf-8") as f:
        idx = json.load(f)
else:
    idx = {"dates": [], "updatedAt": ""}

dates = idx.get("dates", [])
if today not in dates:
    dates.insert(0, today)
# Keep last 30 days
dates = dates[:30]
idx["dates"] = dates
idx["updatedAt"] = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")

with open(index_file, "w", encoding="utf-8") as f:
    json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
print(f"Index updated: {len(dates)} dates")

# Output final stats
print(f"\n__STATS__:{len(truly_new)}:{len(merged)}")
