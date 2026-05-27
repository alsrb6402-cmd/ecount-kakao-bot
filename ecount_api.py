import requests
import json
from datetime import datetime

# ── 계정 정보 ─────────────────────────────────────────
COM_CODE = "684252"
USER_ID  = "곽민규"
API_KEY  = "4ba157290f23b4ee18e0814731b6af3fc5"  # 정식 인증키
WH_CD    = "00002"  # 기본 창고 (함평1공장[완제품]) - 필요시 변경
# 창고 목록:
# 00001 함평1공장[생산]     00002 함평1공장[완제품]  00003 함평2공장[원재료]
# 00004 함평2공장[완제품]   00005 씨레인보우[인천]   00006 우련평택[평택]
# 00007 CJ대한통운[군산]    00008 백제글로벌[인천]   00009 리움로직스[인천]
# 00010 서림물류[김포]      00011 서림물류[화성]     00012 채움로지스[인천]
# 00013 대신택배[3PL]       00014 CJ대한통운[함평]

# ── 내부 상태 ─────────────────────────────────────────
_zone       = None
_session_id = None
_use_test   = False  # True: sboapi(테스트), False: oapi(정식)

def _base_url():
    prefix = "sboapi" if _use_test else "oapi"
    return f"https://{prefix}{_zone}.ecount.com"

# ── 로그인 ────────────────────────────────────────────
def login(use_test=True):
    global _zone, _session_id, _use_test
    _use_test = use_test

    # ZONE 자동 조회
    res = requests.post("https://oapi.ecount.com/OAPI/V2/Zone",
                        json={"COM_CODE": COM_CODE}, timeout=10)
    _zone = res.json()["Data"]["ZONE"]

    # 로그인
    prefix = "sboapi" if use_test else "oapi"
    res = requests.post(
        f"https://{prefix}{_zone}.ecount.com/OAPI/V2/OAPILogin",
        json={"COM_CODE": COM_CODE, "USER_ID": USER_ID,
              "API_CERT_KEY": API_KEY, "LAN_TYPE": "ko-KR", "ZONE": _zone},
        timeout=10
    )
    data = res.json()
    if str(data.get("Status")) == "200":
        _session_id = data["Data"]["Datas"]["SESSION_ID"]
        return _session_id
    raise Exception(f"Login failed: {data}")

def _api(path, body=None, _retry=True):
    url = f"{_base_url()}{path}?SESSION_ID={_session_id}"
    res = requests.post(url, json=body or {}, timeout=10)
    res.encoding = 'utf-8'  # 이카운트 응답 한글 인코딩 강제 지정
    data = res.json()
    if data is None:
        return {}
    # 세션 만료(999) 또는 인증 오류 → 자동 재로그인 후 1회 재시도
    status = str(data.get("Status", ""))
    if _retry and status in ("999", "401", "403"):
        login(use_test=_use_test)
        return _api(path, body, _retry=False)
    return data

# ── 품목 조회 ─────────────────────────────────────────
def get_products(prod_cd="", prod_nm="", prod_type=""):
    """품목 목록 조회. prod_type: 0=원재료 1=제품 2=반제품 3=상품 4=부재료"""
    return _api("/OAPI/V2/InventoryBasic/GetBasicProductsList", {
        "PROD_CD": prod_cd,
        "PROD_DES": prod_nm,
        "PROD_TYPE": prod_type
    })

def search_products_by_name(keyword: str) -> list:
    """품목명 키워드로 검색 후 매칭 목록 반환"""
    result = get_products()
    items = result.get("Data", {}).get("Result", [])
    keyword_lower = keyword.lower()
    matched = [
        item for item in items
        if keyword_lower in str(item.get("PROD_DES", "")).lower()
        or keyword_lower in str(item.get("PROD_CD", "")).lower()
    ]
    return matched

# ── 창고 코드 매핑 ────────────────────────────────────
WAREHOUSE_MAP = {
    "00001": "함평1공장[생산]",
    "00002": "함평1공장[완제품]",
    "00003": "함평2공장[원재료]",
    "00004": "함평2공장[완제품]",
    "00005": "씨레인보우[인천]",
    "00006": "우련평택[평택]",
    "00007": "CJ대한통운[군산]",
    "00008": "백제글로벌[인천]",
    "00009": "리움로직스[인천]",
    "00010": "서림물류[김포]",
    "00011": "서림물류[화성]",
    "00012": "채움로지스[인천]",
    "00013": "대신택배[3PL]",
    "00014": "CJ대한통운[함평]",
}

# ── 창고 별칭 (짧게 불러도 인식) ─────────────────────
WAREHOUSE_ALIASES = {
    # 함평 공장
    "함평생산":    "00001", "1공장생산":  "00001", "생산":       "00001",
    "함평완제":    "00002", "1공장완제":  "00002", "함평1":      "00002",
    "2공장원재료": "00003", "원재료":     "00003", "함평2원":    "00003",
    "2공장완제":   "00004", "함평2완제":  "00004", "함평2":      "00004",
    # 물류창고 (지역명으로 검색)
    "씨레인보우":  "00005", "씨레":       "00005",
    "평택":        "00006", "우련":       "00006",
    "군산":        "00007", "cj군산":     "00007",
    "백제":        "00008", "백제글로벌": "00008",
    "리움":        "00009", "리움로직스": "00009",
    "김포":        "00010", "서림김포":   "00010",
    "화성":        "00011", "서림화성":   "00011",
    "채움":        "00012", "채움로지스": "00012",
    "대신":        "00013", "3pl":        "00013",
    "cj함평":      "00014",
}

def find_warehouse(keyword: str) -> list:
    """창고명 키워드로 창고 코드 검색 (별칭 포함)"""
    keyword_lower = keyword.lower().replace(" ", "")

    # 1. 별칭 정확 매칭 → 바로 1개 반환
    if keyword_lower in WAREHOUSE_ALIASES:
        code = WAREHOUSE_ALIASES[keyword_lower]
        return [{"WH_CD": code, "WH_NM": WAREHOUSE_MAP[code]}]

    # 2. 창고명 부분 매칭
    matched = [
        {"WH_CD": code, "WH_NM": name}
        for code, name in WAREHOUSE_MAP.items()
        if keyword_lower in name.lower().replace(" ", "")
    ]
    return matched

# ── 재고 조회 ─────────────────────────────────────────
def get_stock(prod_cd="", wh_cd="", base_date=None):
    """재고현황 조회"""
    return _api("/OAPI/V2/InventoryBalance/GetListInventoryBalanceStatus", {
        "PROD_CD": prod_cd,
        "WH_CD": wh_cd,
        "BASE_DATE": base_date or datetime.today().strftime("%Y%m%d"),
        "ZERO_FLAG": "Y"
    })

def get_stock_by_warehouse(wh_cd: str, prod_cd="", base_date=None):
    """창고별 재고현황 조회 (재고 있는 품목만)"""
    return _api("/OAPI/V2/InventoryBalance/GetListInventoryBalanceStatus", {
        "WH_CD": wh_cd,
        "PROD_CD": prod_cd,
        "BASE_DATE": base_date or datetime.today().strftime("%Y%m%d"),
        "ZERO_FLAG": "N"   # 재고 0인 품목 제외
    })

# ── 매출 입력 ─────────────────────────────────────────
def save_sale(prod_cd, qty, price, cust_des="", wh_cd=None, io_date=None, remarks=""):
    """판매(매출) 전표 입력
    - prod_cd: 품목코드 (필수)
    - qty: 수량 (필수)
    - price: 단가
    - cust_des: 거래처명
    - wh_cd: 출하창고코드 (필수 - WH_CD 전역변수 사용)
    """
    supply_amt = int(qty * price)
    return _api("/OAPI/V2/Sale/SaveSale", {
        "SaleList": {
            "BulkDatas": [{
                "UPLOAD_SER_NO": 1,
                "IO_DATE": io_date or datetime.today().strftime("%Y%m%d"),
                "CUST_DES": cust_des,
                "WH_CD": wh_cd or WH_CD,
                "PROD_CD": prod_cd,
                "QTY": qty,
                "PRICE": price,
                "SUPPLY_AMT": supply_amt,
                "REMARKS": remarks
            }]
        }
    })

# ── 매입 입력 ─────────────────────────────────────────
def save_purchase(prod_cd, qty, price, cust_des="", wh_cd=None, io_date=None, remarks=""):
    """구매(매입/입고) 전표 입력"""
    supply_amt = int(qty * price)
    return _api("/OAPI/V2/Purchases/SavePurchases", {
        "PurchasesList": {
            "BulkDatas": [{
                "UPLOAD_SER_NO": 1,
                "IO_DATE": io_date or datetime.today().strftime("%Y%m%d"),
                "CUST_DES": cust_des,
                "WH_CD": wh_cd or WH_CD,
                "PROD_CD": prod_cd,
                "QTY": qty,
                "PRICE": price,
                "SUPPLY_AMT": supply_amt,
                "REMARKS": remarks
            }]
        }
    })

def save_move(prod_cd, qty, from_wh_cd, to_wh_cd, io_date=None, remarks=""):
    """창고 간 재고이동 전표 입력 (출고 → 입고)"""
    date = io_date or datetime.today().strftime("%Y%m%d")
    # 출고 (from)
    out = save_sale(prod_cd=prod_cd, qty=qty, price=0,
                    wh_cd=from_wh_cd, io_date=date, remarks=remarks)
    if str(out.get("Status")) != "200":
        return out  # 출고 실패 시 바로 반환
    # 입고 (to)
    inp = save_purchase(prod_cd=prod_cd, qty=qty, price=0,
                        wh_cd=to_wh_cd, io_date=date, remarks=remarks)
    return inp

# ── 테스트 실행 ───────────────────────────────────────
if __name__ == "__main__":
    login(use_test=True)

    print("\n=== 품목 목록 조회 ===")
    r = get_products()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:600])

    print("\n=== 재고 현황 조회 ===")
    r = get_stock()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:600])
