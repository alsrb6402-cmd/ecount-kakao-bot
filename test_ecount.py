import requests
import json

COM_CODE = "684252"
API_KEY  = "323157954dbea4ffd829f178119be1e452"
USER_ID  = "곽민규"

# ZONE을 도메인에 포함하는 방식으로 시도
ZONE     = "ad"
BASE_URL = f"https://sboapi{ZONE}.ecount.com"

def login():
    res = requests.post(f"{BASE_URL}/OAPI/V2/OAPILogin", json={
        "COM_CODE": COM_CODE, "USER_ID": USER_ID,
        "API_CERT_KEY": API_KEY, "LAN_TYPE": "ko-KR", "ZONE": ZONE
    }, timeout=10)
    data = res.json()
    if str(data.get("Status")) == "200":
        session_id = data["Data"]["Datas"]["SESSION_ID"]
        print(f"✅ 로그인 성공\n")
        return session_id
    print(f"❌ 로그인 실패: {data}")
    return None

def login_full():
    res = requests.post(f"{BASE_URL}/OAPI/V2/OAPILogin", json={
        "COM_CODE": COM_CODE, "USER_ID": USER_ID,
        "API_CERT_KEY": API_KEY, "LAN_TYPE": "ko-KR", "ZONE": ZONE
    }, timeout=10)
    data = res.json()
    if str(data.get("Status")) == "200":
        datas = data["Data"]["Datas"]
        session_id = datas["SESSION_ID"]
        cookie_str = datas["SET_COOKIE"]
        print(f"✅ 로그인 성공\n")
        return session_id, cookie_str
    print(f"❌ 로그인 실패: {data}")
    return None, None

def api_call(session_id, path, body=None):
    # SESSION_ID는 URL 파라미터로 전달
    url = f"{BASE_URL}{path}?SESSION_ID={session_id}"
    res = requests.post(url, json=body or {}, timeout=10)
    return res.json()

if __name__ == "__main__":
    session_id, cookie_str = login_full()
    if not session_id:
        exit()

    # 테스트할 API 경로들
    test_apis = [
        ("/OAPI/V2/InventoryBasic/GetBasicProductsList", "품목 목록 조회"),
        ("/OAPI/V2/InventoryBalance/GetListInventoryBalanceStatus", "재고현황"),
        ("/OAPI/V2/InventoryBalance/GetListInventoryBalanceStatusByLocation", "창고별 재고현황"),
        ("/OAPI/V2/Purchases/GetPurchasesOrderList", "매입 발주 목록"),
        ("/OAPI/V2/Sale/SaveSale", "매출 입력 테스트"),
    ]

    for path, label in test_apis:
        print(f"=== {label} ===")
        result = api_call(session_id, path)
        status = result.get("Status", "?")
        print(f"Status: {status}")
        print(json.dumps(result, ensure_ascii=False, indent=2)[:600])
        print()
