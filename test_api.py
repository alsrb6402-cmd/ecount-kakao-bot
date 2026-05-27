import requests
import json
from datetime import datetime

COM_CODE = "684252"
USER_ID  = "곽민규"
API_KEY  = "323157954dbea4ffd829f178119be1e452"
BASE     = "https://sboapiad.ecount.com"

# 로그인
res = requests.post(f"{BASE}/OAPI/V2/OAPILogin", json={
    "COM_CODE": COM_CODE, "USER_ID": USER_ID,
    "API_CERT_KEY": API_KEY, "LAN_TYPE": "ko-KR", "ZONE": "AD"
}, timeout=10)
data = res.json()
if str(data.get("Status")) != "200":
    print("로그인 실패:", data)
    exit()
sid = data["Data"]["Datas"]["SESSION_ID"]
print("[로그인 성공]")

def api(path, body=None):
    r = requests.post(f"{BASE}{path}?SESSION_ID={sid}", json=body or {}, timeout=10)
    return r.json()

# 1. 품목 목록 조회
print("\n=== 품목 목록 조회 ===")
r = api("/OAPI/V2/InventoryBasic/GetBasicProductsList")
print("Status:", r.get("Status"))
items = r.get("Data", {}).get("Result", [])
print(f"품목 수: {len(items)}개")
for item in items[:5]:
    print(f"  [{item.get('PROD_CD')}] {item.get('PROD_DES')} / 단가: {item.get('SALE_PRICE')}")

# 2. 재고현황 조회
print("\n=== 재고현황 조회 ===")
today = datetime.today().strftime("%Y%m%d")
r = api("/OAPI/V2/InventoryBalance/GetListInventoryBalanceStatus", {
    "PROD_CD": "", "WH_CD": "", "BASE_DATE": today, "ZERO_FLAG": "N"
})
print("Status:", r.get("Status"))
stocks = r.get("Data", {}).get("Result", [])
print(f"재고 항목 수: {len(stocks)}개")
for s in stocks[:5]:
    print(f"  [{s.get('PROD_CD')}] {s.get('PROD_DES')} - {s.get('BAL_QTY')}개")

print("\n[모든 테스트 완료]")
