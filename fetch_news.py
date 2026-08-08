import json, os, time, requests
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ).strftime("%Y-%m-%d")
BASE = r"D:\AI-openwork-aion\workbuddyfiles\fin-desk-deploy"
NEWS_DIR = os.path.join(BASE, "news")
DAILY_FILE = os.path.join(NEWS_DIR, f"{TODAY}.json")
INDEX_FILE = os.path.join(NEWS_DIR, "index.json")

CHANNEL_MAP = {
    "a-stock-channel": "A股",
    "us-stock-channel": "美股",
    "xgb-channel": "港股",
    "hk-stock-channel": "港股",
    "commodity-channel": "大宗商品",
    "oil-channel": "能源",
    "global-channel": "宏观",
    "forex-channel": "外汇",
    "bond-channel": "债券",
    "financing-channel": "金融",
    "tmt-channel": "TMT",
    "ai-channel": "AI",
}

CHANNELS = [
    "global-channel", "a-stock-channel", "us-stock-channel",
    "xgb-channel", "commodity-channel", "forex-channel",
    "bond-channel", "financing-channel", "tmt-channel"
]

def fetch_channel(chan):
    url = f"https://api-one.wallstcn.com/apiv1/content/lives?channel={chan}&limit=200&first=0"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json()
            items = data.get("data", {}).get("items", [])
            return items
        else:
            print(f"  [{chan}] HTTP {r.status_code}")
            return []
    except Exception as e:
        print(f"  [{chan}] Error: {e}")
        return []

def get_channel_label(ch):
    return CHANNEL_MAP.get(ch, ch)

# Fetch all channels in sequence (keep it simple)
all_raw = []
for chan in CHANNELS:
    items = fetch_channel(chan)
    print(f"  [{chan}]: {len(items)} items")
    all_raw.extend(items)

print(f"\nTotal raw: {len(all_raw)} items")

# Deduplicate by id, skip empty content
seen_ids = set()
unique_items = []
for item in all_raw:
    iid = item.get("id")
    if not iid or iid in seen_ids:
        continue
    content = (item.get("content_text") or "").strip()
    if not content:
        continue
    seen_ids.add(iid)
    unique_items.append(item)

print(f"After dedup + skip empty: {len(unique_items)} items")

# Read existing file
existing_ids = set()
if os.path.exists(DAILY_FILE):
    with open(DAILY_FILE, "r", encoding="utf-8") as f:
        existing = json.load(f)
    for eitem in existing.get("items", []):
        existing_ids.add(eitem.get("i"))
    print(f"Existing items: {existing['count']}")
else:
    existing = {"date": TODAY, "count": 0, "items": []}
    print("No existing file, creating new")

# Build new items
new_items = []
for item in unique_items:
    iid = item["id"]
    if iid in existing_ids:
        continue
    ts = item.get("display_time", 0)
    dt = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%dT%H:%M")
    ch_raw = item.get("channels", [])[0] if item.get("channels") else "global-channel"
    new_items.append({
        "i": iid,
        "t": dt,
        "c": (item.get("content_text") or "").strip(),
        "ch": get_channel_label(ch_raw),
        "cl": ch_raw,
        "u": f"/livenews/{iid}"
    })

print(f"New items to add: {len(new_items)}")

# Prepend new items (keep newest first by display_time desc)
new_items.sort(key=lambda x: x["i"], reverse=True)
all_items = new_items + existing.get("items", [])

# Truncate if > 1MB (keep latest)
total_count = len(all_items)
result = {"date": TODAY, "count": total_count, "items": all_items}
raw_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
while len(raw_json.encode("utf-8")) > 1048576 and len(all_items) > 0:
    all_items.pop()
    result["count"] = len(all_items)
    result["items"] = all_items
    raw_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

os.makedirs(NEWS_DIR, exist_ok=True)
with open(DAILY_FILE, "w", encoding="utf-8") as f:
    f.write(raw_json)

file_size_kb = os.path.getsize(DAILY_FILE) / 1024
print(f"Written: {DAILY_FILE}")
print(f"Count: {result['count']}, Size: {file_size_kb:.0f}KB")

# Update index
if os.path.exists(INDEX_FILE):
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        idx = json.load(f)
else:
    idx = {"dates": [], "updatedAt": ""}

dates = idx.get("dates", [])
if TODAY not in dates:
    dates.insert(0, TODAY)
else:
    dates.remove(TODAY)
    dates.insert(0, TODAY)

# Keep only 30 days
dates = dates[:30]

idx["dates"] = dates
idx["updatedAt"] = datetime.now(TZ).isoformat()
with open(INDEX_FILE, "w", encoding="utf-8") as f:
    f.write(json.dumps(idx, ensure_ascii=False, separators=(",", ":")))

print(f"Index updated: {len(dates)} dates")

# Output summary for the calling script
print(f"\nSUMMARY:{len(new_items)}:{result['count']}")
