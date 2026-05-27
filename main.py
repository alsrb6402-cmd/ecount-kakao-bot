from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import anthropic
import json
import os
import logging
import sys
from datetime import datetime, timedelta
from ecount_api import (login, get_stock, get_stock_by_warehouse, find_warehouse,
                        save_sale, save_purchase, save_move, get_products, search_products_by_name)

# ── 한글 인코딩 ───────────────────────────────────────
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# ── 로그 설정 ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = FastAPI()
ai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "여기에API키입력"))

# ── 상태 저장소 ───────────────────────────────────────
user_names: dict          = {}   # user_id → 이름
waiting_for_name: set     = set()  # 이름 입력 대기
waiting_name_change: set  = set()  # 이름 변경 대기
pending_select: dict      = {}   # 품목/창고 선택 대기
pending_confirm: dict     = {}   # 등록 확인 대기 (네/아니오)
activity_log: dict        = {}   # user_id → [{date,time,action,prod,qty,wh}]

_session_ready = False

# ─────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────
def ensure_login():
    global _session_ready
    if not _session_ready:
        login(use_test=False)
        _session_ready = True

def get_user_id(body: dict) -> str:
    try:
        return body["userRequest"]["user"]["id"]
    except Exception:
        return "unknown"

def parse_date(date_str: str) -> str:
    """자연어 날짜 → YYYYMMDD"""
    today = datetime.today()
    if not date_str or date_str in ("오늘", "today", "null", "None"):
        return today.strftime("%Y%m%d")
    if date_str == "어제":
        return (today - timedelta(days=1)).strftime("%Y%m%d")
    if date_str == "그제":
        return (today - timedelta(days=2)).strftime("%Y%m%d")
    for fmt in ["%m/%d", "%m-%d", "%Y-%m-%d", "%Y%m%d"]:
        try:
            d = datetime.strptime(date_str, fmt)
            if fmt in ("%m/%d", "%m-%d"):
                d = d.replace(year=today.year)
            return d.strftime("%Y%m%d")
        except Exception:
            pass
    return today.strftime("%Y%m%d")

def display_date(yyyymmdd: str) -> str:
    try:
        return datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return yyyymmdd

def make_response(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}

def confirm_msg(action: dict) -> str:
    intent = action["intent"]
    if intent == "재고이동":
        lines = [
            f"📋 재고이동 확인",
            f"품목: {action['prod_nm']}",
            f"수량: {action['qty']}개",
            f"이동: {action['wh_from_nm']} → {action['wh_to_nm']}",
            f"날짜: {display_date(action['io_date'])}",
        ]
    else:
        lines = [
            f"📋 {'입고' if intent=='입고' else '출고'} 등록 확인",
            f"품목: {action['prod_nm']}",
            f"수량: {action['qty']}개",
            f"창고: {action['wh_nm']}",
            f"날짜: {display_date(action['io_date'])}",
        ]
        if action.get("cust_des"):
            lines.append(f"거래처: {action['cust_des']}")
    if action.get("remarks"):
        lines.append(f"메모: {action['remarks']}")
    lines.append("\n✅ 등록할까요? (네 / 아니오)")
    return "\n".join(lines)

def add_log(user_id: str, action: str, prod_nm: str, qty, wh_nm: str, io_date: str):
    if user_id not in activity_log:
        activity_log[user_id] = []
    activity_log[user_id].append({
        "date": io_date,
        "time": datetime.now().strftime("%H:%M"),
        "action": action,
        "prod_nm": prod_nm,
        "qty": qty,
        "wh_nm": wh_nm
    })
    activity_log[user_id] = activity_log[user_id][-30:]

# ─────────────────────────────────────────────────────
# AI 파싱
# ─────────────────────────────────────────────────────
def parse_intent(text: str) -> dict:
    prompt = f"""사용자 메시지를 분석해서 JSON으로만 반환해줘.

메시지: "{text}"

반환 형식:
{{
  "intent": "입고|출고|재고이동|재고조회|창고별재고|내역조회|이름변경|기타",
  "prod_nm": "품목명 또는 null",
  "qty": 숫자 또는 null,
  "wh_nm": "창고명 또는 null",
  "wh_from": "출발창고명 또는 null",
  "wh_to": "도착창고명 또는 null",
  "io_date": "오늘|어제|그제|날짜(5/25형식) 또는 null",
  "cust_des": "거래처명 또는 null",
  "remarks": "메모 또는 null"
}}

예시:
- "백제로 염화칼슘 100개 출고" → intent:출고, prod_nm:염화칼슘, qty:100, wh_nm:백제
- "어제 군산 염화칼슘 50개 입고" → intent:입고, io_date:어제, wh_nm:군산, qty:50
- "5/25 화성에서 눈길제로 30톤백 나갔어" → intent:출고, io_date:5/25, wh_nm:화성
- "함평에서 백제로 염화칼슘 100개 이동" → intent:재고이동, wh_from:함평, wh_to:백제, prod_nm:염화칼슘, qty:100
- "군산에서 평택으로 눈길제로 50개 옮겨" → intent:재고이동, wh_from:군산, wh_to:평택
- "염화칼슘 재고 얼마야?" → intent:재고조회, prod_nm:염화칼슘
- "백제 창고 재고 보여줘" → intent:창고별재고, wh_nm:백제
- "오늘 내가 등록한 거 보여줘" → intent:내역조회
- "이름 바꿔" / "이름 변경" → intent:이름변경

JSON만 반환."""

    msg = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return json.loads(msg.content[0].text)

# ─────────────────────────────────────────────────────
# 시작 이벤트
# ─────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    try:
        ensure_login()
        logger.info("[서버시작] 이카운트 로그인 완료")
    except Exception as e:
        logger.error(f"[서버시작] 로그인 실패: {e}")

# ─────────────────────────────────────────────────────
# 웹훅
# ─────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    try:
        body     = await request.json()
        user_id  = get_user_id(body)
        user_msg = body["userRequest"]["utterance"].strip()

        # ── 취소 처리 (언제든지) ──────────────────────
        if user_msg in ("취소", "ㄴ", "아니오", "아니"):
            pending_select.pop(user_id, None)
            pending_confirm.pop(user_id, None)
            waiting_for_name.discard(user_id)
            waiting_name_change.discard(user_id)
            return JSONResponse(make_response("취소됐습니다."))

        # ── 이름 입력 대기 ────────────────────────────
        if user_id in waiting_for_name or user_id in waiting_name_change:
            is_change = user_id in waiting_name_change
            name = user_msg
            user_names[user_id] = name
            waiting_for_name.discard(user_id)
            waiting_name_change.discard(user_id)
            logger.info(f"[이름{'변경' if is_change else '등록'}] {name}")
            if is_change:
                return JSONResponse(make_response(f"이름이 '{name}'(으)로 변경됐습니다! ✅"))
            return JSONResponse(make_response(
                f"반갑습니다, {name}님! 😊\n\n"
                "사용 방법:\n"
                "• 입고: '창고 품목 수량 입고'\n"
                "• 출고: '창고 품목 수량 출고'\n"
                "• 재고조회: '품목 재고 얼마야?'\n"
                "• 창고별재고: '백제 창고 재고 보여줘'\n"
                "• 내역조회: '오늘 내가 등록한 거'\n"
                "• 이름변경: '이름 바꿔'"
            ))

        # ── 신규 사용자 ───────────────────────────────
        if user_id not in user_names:
            waiting_for_name.add(user_id)
            return JSONResponse(make_response(
                "안녕하세요! 대성인더스 ERP 봇입니다. 👋\n\n"
                "성함을 입력해주세요.\n(예: 홍길동)"
            ))

        user_name = user_names[user_id]

        # ── 등록 확인 대기 (네/아니오) ────────────────
        if user_id in pending_confirm:
            action = pending_confirm.pop(user_id)
            if user_msg in ("네", "예", "ㅇ", "ㅇㅇ", "응", "yes", "y", "ㅇㅋ", "오케이"):
                remarks = f"[{user_name}] {action.get('remarks', '')}".strip()
                intent  = action["intent"]

                if intent == "재고이동":
                    result = save_move(
                        prod_cd=action["prod_cd"], qty=action["qty"],
                        from_wh_cd=action["wh_from_cd"], to_wh_cd=action["wh_to_cd"],
                        io_date=action["io_date"], remarks=remarks
                    )
                    if str(result.get("Status")) == "200":
                        add_log(user_id, "이동출고", action["prod_nm"], action["qty"], action["wh_from_nm"], action["io_date"])
                        add_log(user_id, "이동입고", action["prod_nm"], action["qty"], action["wh_to_nm"],   action["io_date"])
                        reply = (f"✅ 재고이동 완료!\n"
                                 f"품목: {action['prod_nm']}\n"
                                 f"수량: {action['qty']}개\n"
                                 f"이동: {action['wh_from_nm']} → {action['wh_to_nm']}\n"
                                 f"날짜: {display_date(action['io_date'])}")
                        logger.info(f"[재고이동완료] 사용자={user_name} | 품목={action['prod_nm']} | {action['wh_from_nm']}→{action['wh_to_nm']} | 수량={action['qty']}")
                    else:
                        errs = result.get("Errors", [{}])
                        reply = f"❌ 오류: {errs[0].get('Message','알 수 없는 오류') if errs else '오류 발생'}"

                elif intent == "입고":
                    result = save_purchase(
                        prod_cd=action["prod_cd"], qty=action["qty"], price=0,
                        cust_des=action.get("cust_des", ""), wh_cd=action["wh_cd"],
                        io_date=action["io_date"], remarks=remarks
                    )
                    if str(result.get("Status")) == "200":
                        add_log(user_id, "입고", action["prod_nm"], action["qty"], action["wh_nm"], action["io_date"])
                        reply = (f"✅ 입고 등록 완료!\n"
                                 f"품목: {action['prod_nm']}\n"
                                 f"수량: {action['qty']}개\n"
                                 f"창고: {action['wh_nm']}\n"
                                 f"날짜: {display_date(action['io_date'])}")
                        logger.info(f"[입고완료] 사용자={user_name} | 품목={action['prod_nm']} | 수량={action['qty']} | 창고={action['wh_nm']}")
                    else:
                        errs = result.get("Errors", [{}])
                        reply = f"❌ 오류: {errs[0].get('Message','알 수 없는 오류') if errs else '오류 발생'}"

                else:  # 출고
                    result = save_sale(
                        prod_cd=action["prod_cd"], qty=action["qty"], price=0,
                        cust_des=action.get("cust_des", ""), wh_cd=action["wh_cd"],
                        io_date=action["io_date"], remarks=remarks
                    )
                    if str(result.get("Status")) == "200":
                        add_log(user_id, "출고", action["prod_nm"], action["qty"], action["wh_nm"], action["io_date"])
                        reply = (f"✅ 출고 등록 완료!\n"
                                 f"품목: {action['prod_nm']}\n"
                                 f"수량: {action['qty']}개\n"
                                 f"창고: {action['wh_nm']}\n"
                                 f"날짜: {display_date(action['io_date'])}")
                        logger.info(f"[출고완료] 사용자={user_name} | 품목={action['prod_nm']} | 수량={action['qty']} | 창고={action['wh_nm']}")
                    else:
                        errs = result.get("Errors", [{}])
                        reply = f"❌ 오류: {errs[0].get('Message','알 수 없는 오류') if errs else '오류 발생'}"
            else:
                pending_confirm[user_id] = action
                reply = "'네' 또는 '아니오'로 답해주세요."
            return JSONResponse(make_response(reply))

        # ── 품목/창고 선택 대기 ───────────────────────
        if user_id in pending_select:
            pending = pending_select.pop(user_id)
            candidates = pending["candidates"]

            if not user_msg.isdigit():
                pending_select[user_id] = pending
                return JSONResponse(make_response("번호를 입력해주세요. (취소하려면 '취소')"))

            idx = int(user_msg) - 1
            if not (0 <= idx < len(candidates)):
                pending_select[user_id] = pending
                return JSONResponse(make_response(f"1~{len(candidates)} 사이 번호를 입력해주세요."))

            selected   = candidates[idx]
            step       = pending.get("step", "product")
            intent     = pending["intent"]

            # 품목 선택 완료
            if step == "product":
                pending["prod_cd"] = selected.get("PROD_CD", "")
                pending["prod_nm"] = selected.get("PROD_DES", pending["prod_cd"])

                if intent == "재고조회":
                    result = get_stock(prod_cd=pending["prod_cd"])
                    items  = result.get("Data", {}).get("Result", [])
                    reply  = (f"📦 재고현황\n품목: {pending['prod_nm']}\n수량: {float(items[0]['BAL_QTY']):.0f}개"
                              if items else f"'{pending['prod_nm']}' 재고가 없습니다.")
                    return JSONResponse(make_response(reply))

                if intent == "재고이동":
                    # 출발창고 확인
                    pending = _resolve_move_warehouse(user_id, pending, "wh_from")
                    if user_id in pending_select:
                        return JSONResponse(make_response(f"출발 창고를 선택해주세요:\n" + _warehouse_select_msg(pending_select[user_id]["candidates"])))
                    # 도착창고 확인
                    pending = _resolve_move_warehouse(user_id, pending, "wh_to")
                    if user_id in pending_select:
                        return JSONResponse(make_response(f"도착 창고를 선택해주세요:\n" + _warehouse_select_msg(pending_select[user_id]["candidates"])))
                    pending_confirm[user_id] = pending
                    return JSONResponse(make_response(confirm_msg(pending)))

                # 입고/출고 → 창고 확인
                pending = _resolve_warehouse(user_id, pending)
                if user_id in pending_select:
                    return JSONResponse(make_response(_warehouse_select_msg(pending_select[user_id]["candidates"])))
                pending_confirm[user_id] = pending
                return JSONResponse(make_response(confirm_msg(pending)))

            # 창고 선택 완료 (입고/출고)
            elif step == "warehouse":
                pending["wh_cd"] = selected.get("WH_CD", "00002")
                pending["wh_nm"] = selected.get("WH_NM", "함평1공장[완제품]")

                if intent == "창고별재고":
                    result = get_stock_by_warehouse(wh_cd=pending["wh_cd"])
                    items  = result.get("Data", {}).get("Result", [])
                    pf     = pending.get("prod_nm_filter", "")
                    if pf:
                        items = [i for i in items if pf.lower() in str(i.get("PROD_DES","")).lower()]
                    if items:
                        lines = [f"📦 {pending['wh_nm']} 재고현황"]
                        for item in items[:10]:
                            lines.append(f"• {item.get('PROD_DES', item['PROD_CD'])}: {float(item['BAL_QTY']):.0f}개")
                        reply = "\n".join(lines)
                    else:
                        reply = f"'{pending['wh_nm']}' 재고가 없습니다."
                    return JSONResponse(make_response(reply))

                pending_confirm[user_id] = pending
                return JSONResponse(make_response(confirm_msg(pending)))

            # 재고이동 출발창고 선택
            elif step == "wh_from":
                pending["wh_from_cd"] = selected.get("WH_CD", "")
                pending["wh_from_nm"] = selected.get("WH_NM", "")
                # 도착창고 확인
                pending = _resolve_move_warehouse(user_id, pending, "wh_to")
                if user_id in pending_select:
                    return JSONResponse(make_response(f"도착 창고를 선택해주세요:\n" + _warehouse_select_msg(pending_select[user_id]["candidates"])))
                pending_confirm[user_id] = pending
                return JSONResponse(make_response(confirm_msg(pending)))

            # 재고이동 도착창고 선택
            elif step == "wh_to":
                pending["wh_to_cd"] = selected.get("WH_CD", "")
                pending["wh_to_nm"] = selected.get("WH_NM", "")
                pending_confirm[user_id] = pending
                return JSONResponse(make_response(confirm_msg(pending)))

        # ── 일반 메시지 처리 ──────────────────────────
        ensure_login()
        parsed  = parse_intent(user_msg)
        intent  = parsed.get("intent")
        io_date = parse_date(parsed.get("io_date") or "오늘")
        logger.info(f"[요청] 사용자={user_name} | intent={intent} | 메시지={user_msg}")

        # 입고 / 출고
        if intent in ("입고", "출고"):
            prod_nm     = parsed.get("prod_nm")
            wh_nm_input = parsed.get("wh_nm") or ""
            qty         = parsed.get("qty")

            if not prod_nm:
                return JSONResponse(make_response("품목명을 입력해주세요.\n예) 백제로 염화칼슘 100개 출고"))
            if not qty:
                return JSONResponse(make_response("수량을 입력해주세요."))

            candidates = search_products_by_name(prod_nm)

            if len(candidates) == 0:
                reply = f"'{prod_nm}' 품목을 찾을 수 없어요.\n품목명을 다시 확인해주세요."
                return JSONResponse(make_response(reply))

            if len(candidates) > 1:
                # 품목 선택 필요
                pending_select[user_id] = {
                    "intent": intent, "qty": qty, "io_date": io_date,
                    "cust_des": parsed.get("cust_des") or "",
                    "remarks": parsed.get("remarks") or "",
                    "wh_nm_input": wh_nm_input,
                    "step": "product", "candidates": candidates[:5]
                }
                lines = ["어떤 품목인가요?"]
                for i, item in enumerate(candidates[:5], 1):
                    lines.append(f"{i}. {item.get('PROD_DES', item.get('PROD_CD'))}")
                return JSONResponse(make_response("\n".join(lines)))

            # 품목 1개 확정
            action = {
                "intent": intent,
                "prod_cd": candidates[0]["PROD_CD"],
                "prod_nm": candidates[0].get("PROD_DES", candidates[0]["PROD_CD"]),
                "qty": qty, "io_date": io_date,
                "cust_des": parsed.get("cust_des") or "",
                "remarks": parsed.get("remarks") or "",
                "wh_nm_input": wh_nm_input
            }
            action = _resolve_warehouse(user_id, action)
            if user_id in pending_select:
                return JSONResponse(make_response(_warehouse_select_msg(pending_select[user_id]["candidates"])))
            pending_confirm[user_id] = action
            reply = confirm_msg(action)

        # 재고조회
        elif intent == "재고조회":
            prod_nm = parsed.get("prod_nm") or ""
            if not prod_nm:
                result = get_stock()
                items  = result.get("Data", {}).get("Result", [])
                if items:
                    lines = ["📦 전체 재고현황"]
                    for item in items[:10]:
                        lines.append(f"• {item.get('PROD_DES', item['PROD_CD'])}: {float(item['BAL_QTY']):.0f}개")
                    reply = "\n".join(lines)
                else:
                    reply = "재고 데이터가 없습니다."
            else:
                candidates = search_products_by_name(prod_nm)
                if len(candidates) == 0:
                    reply = f"'{prod_nm}' 품목을 찾을 수 없어요."
                elif len(candidates) == 1:
                    result = get_stock(prod_cd=candidates[0]["PROD_CD"])
                    items  = result.get("Data", {}).get("Result", [])
                    reply  = (f"📦 재고현황\n품목: {candidates[0].get('PROD_DES')}\n수량: {float(items[0]['BAL_QTY']):.0f}개"
                              if items else f"'{candidates[0].get('PROD_DES')}' 재고가 없습니다.")
                else:
                    pending_select[user_id] = {
                        "intent": "재고조회", "qty": 0,
                        "step": "product", "candidates": candidates[:5]
                    }
                    lines = ["어떤 품목 재고를 볼까요?"]
                    for i, item in enumerate(candidates[:5], 1):
                        lines.append(f"{i}. {item.get('PROD_DES', item.get('PROD_CD'))}")
                    reply = "\n".join(lines)

        # 창고별재고
        elif intent == "창고별재고":
            wh_nm    = parsed.get("wh_nm") or ""
            prod_nm  = parsed.get("prod_nm") or ""
            if not wh_nm:
                reply = "창고명을 입력해주세요.\n예) 백제 창고 재고 보여줘"
            else:
                warehouses = find_warehouse(wh_nm)
                if len(warehouses) == 0:
                    reply = f"'{wh_nm}' 창고를 찾을 수 없어요.\n\n예) 군산, 평택, 백제, 김포, 화성, 함평완제 등"
                elif len(warehouses) == 1:
                    wh     = warehouses[0]
                    result = get_stock_by_warehouse(wh_cd=wh["WH_CD"])
                    items  = result.get("Data", {}).get("Result", [])
                    if prod_nm:
                        items = [i for i in items if prod_nm.lower() in str(i.get("PROD_DES","")).lower()]
                    if items:
                        lines = [f"📦 {wh['WH_NM']} 재고현황"]
                        for item in items[:10]:
                            lines.append(f"• {item.get('PROD_DES', item['PROD_CD'])}: {float(item['BAL_QTY']):.0f}개")
                        reply = "\n".join(lines)
                    else:
                        reply = f"'{wh['WH_NM']}' 재고가 없습니다."
                    logger.info(f"[창고별재고] 사용자={user_name} | 창고={wh['WH_NM']}")
                else:
                    pending_select[user_id] = {
                        "intent": "창고별재고", "qty": 0,
                        "prod_nm_filter": prod_nm,
                        "step": "warehouse", "candidates": warehouses
                    }
                    reply = _warehouse_select_msg(warehouses)

        # 재고이동
        elif intent == "재고이동":
            prod_nm      = parsed.get("prod_nm")
            wh_from_input = parsed.get("wh_from") or ""
            wh_to_input   = parsed.get("wh_to") or ""
            qty           = parsed.get("qty")

            if not prod_nm:
                reply = "품목명을 입력해주세요.\n예) 함평에서 백제로 염화칼슘 100개 이동"
            elif not qty:
                reply = "수량을 입력해주세요."
            elif not wh_from_input:
                reply = "출발 창고를 입력해주세요.\n예) 함평완제에서 백제로 염화칼슘 100개 이동"
            elif not wh_to_input:
                reply = "도착 창고를 입력해주세요.\n예) 함평완제에서 백제로 염화칼슘 100개 이동"
            else:
                candidates = search_products_by_name(prod_nm)
                if len(candidates) == 0:
                    reply = f"'{prod_nm}' 품목을 찾을 수 없어요."
                elif len(candidates) > 1:
                    pending_select[user_id] = {
                        "intent": "재고이동", "qty": qty, "io_date": io_date,
                        "remarks": parsed.get("remarks") or "",
                        "wh_from_input": wh_from_input,
                        "wh_to_input": wh_to_input,
                        "step": "product", "candidates": candidates[:5]
                    }
                    lines = ["어떤 품목인가요?"]
                    for i, item in enumerate(candidates[:5], 1):
                        lines.append(f"{i}. {item.get('PROD_DES', item.get('PROD_CD'))}")
                    reply = "\n".join(lines)
                else:
                    action = {
                        "intent": "재고이동",
                        "prod_cd": candidates[0]["PROD_CD"],
                        "prod_nm": candidates[0].get("PROD_DES", candidates[0]["PROD_CD"]),
                        "qty": qty, "io_date": io_date,
                        "remarks": parsed.get("remarks") or "",
                        "wh_from_input": wh_from_input,
                        "wh_to_input": wh_to_input
                    }
                    # 출발창고 확인
                    action = _resolve_move_warehouse(user_id, action, "wh_from")
                    if user_id in pending_select:
                        return JSONResponse(make_response("출발 창고를 선택해주세요:\n" + _warehouse_select_msg(pending_select[user_id]["candidates"])))
                    # 도착창고 확인
                    action = _resolve_move_warehouse(user_id, action, "wh_to")
                    if user_id in pending_select:
                        return JSONResponse(make_response("도착 창고를 선택해주세요:\n" + _warehouse_select_msg(pending_select[user_id]["candidates"])))
                    pending_confirm[user_id] = action
                    reply = confirm_msg(action)

        # 내역조회
        elif intent == "내역조회":
            logs      = activity_log.get(user_id, [])
            today_str = datetime.today().strftime("%Y%m%d")
            today_logs = [l for l in logs if l["date"] == today_str]
            if today_logs:
                lines = [f"📋 오늘 {user_name}님 등록 내역"]
                for l in today_logs:
                    lines.append(f"• {l['time']} [{l['action']}] {l['prod_nm']} {l['qty']}개 ({l['wh_nm']})")
                reply = "\n".join(lines)
            else:
                reply = "오늘 등록한 내역이 없습니다."

        # 이름변경
        elif intent == "이름변경":
            waiting_name_change.add(user_id)
            reply = f"현재 이름: {user_name}\n\n변경할 이름을 입력해주세요."

        else:
            reply = ("사용 방법:\n"
                     "• 입고: '백제로 염화칼슘 100개 입고'\n"
                     "• 출고: '군산으로 눈길제로 50개 출고'\n"
                     "• 날짜지정: '어제 / 5/25' 앞에 붙이기\n"
                     "• 재고조회: '염화칼슘 재고 얼마야?'\n"
                     "• 창고별재고: '백제 창고 재고 보여줘'\n"
                     "• 내역조회: '오늘 내가 등록한 거'\n"
                     "• 이름변경: '이름 바꿔'")

        return JSONResponse(make_response(reply))

    except Exception as e:
        logger.error(f"[오류] {str(e)}")
        return JSONResponse(make_response(f"오류가 발생했습니다: {str(e)}"))

# ─────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────
def _resolve_warehouse(user_id: str, action: dict) -> dict:
    """창고 결정. 선택 필요 시 pending_select에 저장 후 None 반환"""
    wh_nm_input = action.get("wh_nm_input", "")
    if wh_nm_input:
        warehouses = find_warehouse(wh_nm_input)
        if len(warehouses) == 1:
            action["wh_cd"] = warehouses[0]["WH_CD"]
            action["wh_nm"] = warehouses[0]["WH_NM"]
        elif len(warehouses) > 1:
            action["step"]       = "warehouse"
            action["candidates"] = warehouses
            pending_select[user_id] = action
        else:
            # 못 찾으면 기본 창고
            action["wh_cd"] = "00002"
            action["wh_nm"] = "함평1공장[완제품]"
    else:
        action["wh_cd"] = "00002"
        action["wh_nm"] = "함평1공장[완제품]"
    return action

def _warehouse_select_msg(warehouses: list) -> str:
    lines = ["어떤 창고인가요?"]
    for i, wh in enumerate(warehouses, 1):
        lines.append(f"{i}. {wh['WH_NM']}")
    return "\n".join(lines)

def _resolve_move_warehouse(user_id: str, action: dict, which: str) -> dict:
    """재고이동 출발/도착 창고 결정. 선택 필요 시 pending_select에 저장"""
    input_key = f"{which}_input"
    cd_key    = f"{which}_cd"
    nm_key    = f"{which}_nm"
    step_name = which  # "wh_from" or "wh_to"

    wh_input = action.get(input_key, "")
    if not wh_input:
        return action

    warehouses = find_warehouse(wh_input)
    if len(warehouses) == 1:
        action[cd_key] = warehouses[0]["WH_CD"]
        action[nm_key] = warehouses[0]["WH_NM"]
    elif len(warehouses) > 1:
        action["step"]       = step_name
        action["candidates"] = warehouses
        pending_select[user_id] = action
    else:
        action[cd_key] = "00002"
        action[nm_key] = "함평1공장[완제품]"
    return action

# ─────────────────────────────────────────────────────
# 헬스체크
# ─────────────────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "service": "대성인더스 ERP 봇"}

@app.get("/my-ip")
def my_ip():
    import httpx
    r = httpx.get("https://api.ipify.org?format=json", timeout=5)
    return r.json()
