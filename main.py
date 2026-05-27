from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import anthropic
import json
import os
import logging
from ecount_api import login, get_stock, save_sale, save_purchase, get_products

# ── 로그 설정 ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Anthropic 클라이언트
ai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "여기에API키입력"))

# ── 사용자 이름 저장소 ────────────────────────────────
user_names: dict = {}
waiting_for_name: set = set()

# ── 이카운트 세션 ─────────────────────────────────────
_session_ready = False

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

def parse_intent(text: str) -> dict:
    """Claude AI로 자연어 파싱"""
    prompt = f"""사용자가 보낸 메시지를 분석해서 JSON으로 반환해줘.

메시지: "{text}"

반환 형식:
{{
  "intent": "입고|출고|재고조회|품목조회|기타",
  "prod_cd": "품목코드 또는 null",
  "prod_nm": "품목명 또는 null",
  "qty": 수량 또는 null,
  "cust_des": "거래처명 또는 null",
  "remarks": "메모 또는 null"
}}

예시:
- "오늘 한국식품에서 사과 100개 입고" → intent: 입고
- "사과 100개 들어왔어" → intent: 입고
- "GS마트로 배 50개 출고" → intent: 출고
- "사과 50개 나갔어" → intent: 출고
- "사과 재고 얼마야?" → intent: 재고조회
- "품목 목록 보여줘" → intent: 품목조회

단가/가격 정보는 무시해도 됨. JSON만 반환하고 다른 설명은 하지 마."""

    msg = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return json.loads(msg.content[0].text)

def make_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        user_id = get_user_id(body)
        user_msg = body["userRequest"]["utterance"]

        # ── 이름 입력 대기 중 ────────────────────────────
        if user_id in waiting_for_name:
            name = user_msg.strip()
            user_names[user_id] = name
            waiting_for_name.discard(user_id)
            logger.info(f"[이름등록] ID={user_id[:8]} | 이름={name}")
            return JSONResponse(make_response(
                f"반갑습니다, {name}님! 😊\n\n"
                "이제 ERP 봇을 사용하실 수 있어요.\n\n"
                "사용 방법:\n"
                "• 입고: '거래처에서 품목 수량 입고'\n"
                "• 출고: '거래처로 품목 수량 출고'\n"
                "• 재고조회: '품목 재고 얼마야?'\n"
                "• 품목조회: '품목 목록 보여줘'"
            ))

        # ── 신규 사용자 → 이름 질문 ──────────────────────
        if user_id not in user_names:
            waiting_for_name.add(user_id)
            logger.info(f"[신규사용자] ID={user_id[:8]} | 이름 질문 중")
            return JSONResponse(make_response(
                "안녕하세요! 대성인더스 ERP 봇입니다. 👋\n\n"
                "처음 이용하시네요!\n"
                "성함을 입력해주세요.\n"
                "(예: 홍길동)"
            ))

        # ── 기존 사용자 ──────────────────────────────────
        user_name = user_names[user_id]
        logger.info(f"[요청] 사용자={user_name} | 메시지={user_msg}")

        ensure_login()
        parsed = parse_intent(user_msg)
        intent = parsed.get("intent")

        if intent == "입고":
            if not parsed.get("prod_cd") and not parsed.get("prod_nm"):
                reply = "품목명을 입력해주세요.\n예) 사과 100개 입고"
            elif not parsed.get("qty"):
                reply = "수량을 입력해주세요.\n예) 사과 100개 입고"
            else:
                base_remarks = parsed.get("remarks") or ""
                remarks_with_user = f"[{user_name}] {base_remarks}".strip()
                result = save_purchase(
                    prod_cd=parsed.get("prod_cd") or parsed.get("prod_nm", ""),
                    qty=parsed["qty"],
                    price=0,
                    cust_des=parsed.get("cust_des") or "",
                    remarks=remarks_with_user
                )
                if str(result.get("Status")) == "200":
                    reply = (f"✅ 입고 등록 완료!\n"
                             f"품목: {parsed.get('prod_nm') or parsed.get('prod_cd')}\n"
                             f"수량: {parsed['qty']}개\n"
                             f"담당: {user_name}")
                    logger.info(f"[입고완료] 사용자={user_name} | 품목={parsed.get('prod_nm') or parsed.get('prod_cd')} | 수량={parsed['qty']}")
                else:
                    errs = result.get("Errors", [{}])
                    err_msg = errs[0].get('Message', '알 수 없는 오류') if errs else '오류 발생'
                    reply = f"❌ 오류: {err_msg}"
                    logger.info(f"[입고실패] 사용자={user_name} | 오류={err_msg}")

        elif intent == "출고":
            if not parsed.get("prod_cd") and not parsed.get("prod_nm"):
                reply = "품목명을 입력해주세요.\n예) 사과 50개 출고"
            elif not parsed.get("qty"):
                reply = "수량을 입력해주세요.\n예) 사과 50개 출고"
            else:
                base_remarks = parsed.get("remarks") or ""
                remarks_with_user = f"[{user_name}] {base_remarks}".strip()
                result = save_sale(
                    prod_cd=parsed.get("prod_cd") or parsed.get("prod_nm", ""),
                    qty=parsed["qty"],
                    price=0,
                    cust_des=parsed.get("cust_des") or "",
                    remarks=remarks_with_user
                )
                if str(result.get("Status")) == "200":
                    reply = (f"✅ 출고 등록 완료!\n"
                             f"품목: {parsed.get('prod_nm') or parsed.get('prod_cd')}\n"
                             f"수량: {parsed['qty']}개\n"
                             f"담당: {user_name}")
                    logger.info(f"[출고완료] 사용자={user_name} | 품목={parsed.get('prod_nm') or parsed.get('prod_cd')} | 수량={parsed['qty']}")
                else:
                    errs = result.get("Errors", [{}])
                    err_msg = errs[0].get('Message', '알 수 없는 오류') if errs else '오류 발생'
                    reply = f"❌ 오류: {err_msg}"
                    logger.info(f"[출고실패] 사용자={user_name} | 오류={err_msg}")

        elif intent == "재고조회":
            result = get_stock(prod_cd=parsed.get("prod_cd") or "")
            data = result.get("Data", {})
            items = data.get("Result", [])
            if items:
                lines = ["📦 재고현황"]
                for item in items[:10]:
                    lines.append(f"• {item['PROD_CD']}: {float(item['BAL_QTY']):.0f}개")
                reply = "\n".join(lines)
            else:
                reply = "재고 데이터가 없습니다."
            logger.info(f"[재고조회] 사용자={user_name} | 완료")

        elif intent == "품목조회":
            result = get_products()
            data = result.get("Data", {})
            items = data.get("Result", [])
            if items:
                lines = ["📋 품목 목록"]
                for item in items[:10]:
                    lines.append(f"• [{item.get('PROD_CD')}] {item.get('PROD_DES', '')}")
                reply = "\n".join(lines)
            else:
                reply = "품목 데이터가 없습니다."
            logger.info(f"[품목조회] 사용자={user_name} | 완료")

        else:
            reply = ("사용 방법:\n"
                     "• 입고: '거래처에서 품목 수량 입고'\n"
                     "• 출고: '거래처로 품목 수량 출고'\n"
                     "• 재고조회: '품목 재고 얼마야?'\n"
                     "• 품목조회: '품목 목록 보여줘'")
            logger.info(f"[기타] 사용자={user_name} | 메시지={user_msg}")

        return JSONResponse(make_response(reply))

    except Exception as e:
        logger.error(f"[오류] {str(e)}")
        return JSONResponse(make_response(f"오류가 발생했습니다: {str(e)}"))

@app.get("/")
def health():
    return {"status": "ok", "service": "대성인더스 ERP 봇"}
