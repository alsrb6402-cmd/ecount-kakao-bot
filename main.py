from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import anthropic
import json
import os
from ecount_api import login, get_stock, save_sale, save_purchase, get_products

app = FastAPI()

# Anthropic 클라이언트
ai = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "여기에API키입력"))

# 이카운트 세션 (서버 시작 시 로그인)
_session_ready = False

def ensure_login():
    global _session_ready
    if not _session_ready:
        login(use_test=True)
        _session_ready = True

def parse_intent(text: str) -> dict:
    """Claude AI로 자연어 파싱"""
    prompt = f"""사용자가 보낸 메시지를 분석해서 JSON으로 반환해줘.

메시지: "{text}"

반환 형식:
{{
  "intent": "매입|매출|재고조회|품목조회|기타",
  "prod_cd": "품목코드 또는 null",
  "prod_nm": "품목명 또는 null",
  "qty": 수량 또는 null,
  "price": 단가 또는 null,
  "cust_des": "거래처명 또는 null",
  "remarks": "메모 또는 null"
}}

예시:
- "오늘 한국식품에서 사과 100개 500원에 매입" → intent: 매입
- "GS마트에 배 50개 1000원 판매" → intent: 매출
- "사과 재고 얼마야?" → intent: 재고조회
- "품목 목록 보여줘" → intent: 품목조회

JSON만 반환하고 다른 설명은 하지 마."""

    msg = ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return json.loads(msg.content[0].text)

def make_response(text: str) -> dict:
    """카카오 응답 형식"""
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
        user_msg = body["userRequest"]["utterance"]

        ensure_login()
        parsed = parse_intent(user_msg)
        intent = parsed.get("intent")

        if intent == "재고조회":
            result = get_stock(prod_cd=parsed.get("prod_cd") or "")
            data = result.get("Data", {})
            items = data.get("Result", [])
            if items:
                lines = [f"📦 재고현황"]
                for item in items[:10]:
                    lines.append(f"• {item['PROD_CD']}: {float(item['BAL_QTY']):.0f}개")
                reply = "\n".join(lines)
            else:
                reply = "재고 데이터가 없습니다."

        elif intent == "매입":
            if not parsed.get("prod_cd") and not parsed.get("prod_nm"):
                reply = "품목코드 또는 품목명을 입력해주세요.\n예) 사과 100개 500원 매입"
            elif not parsed.get("qty"):
                reply = "수량을 입력해주세요."
            else:
                result = save_purchase(
                    prod_cd=parsed.get("prod_cd") or parsed.get("prod_nm", ""),
                    qty=parsed["qty"],
                    price=parsed.get("price") or 0,
                    cust_des=parsed.get("cust_des") or "",
                    remarks=parsed.get("remarks") or ""
                )
                if str(result.get("Status")) == "200":
                    reply = f"✅ 매입 등록 완료!\n품목: {parsed.get('prod_nm') or parsed.get('prod_cd')}\n수량: {parsed['qty']}개\n단가: {parsed.get('price', 0):,}원"
                else:
                    errs = result.get("Errors", [{}])
                    reply = f"❌ 오류: {errs[0].get('Message', '알 수 없는 오류') if errs else '오류 발생'}"

        elif intent == "매출":
            if not parsed.get("prod_cd") and not parsed.get("prod_nm"):
                reply = "품목코드 또는 품목명을 입력해주세요.\n예) GS마트에 사과 50개 1000원 판매"
            elif not parsed.get("qty"):
                reply = "수량을 입력해주세요."
            else:
                result = save_sale(
                    prod_cd=parsed.get("prod_cd") or parsed.get("prod_nm", ""),
                    qty=parsed["qty"],
                    price=parsed.get("price") or 0,
                    cust_des=parsed.get("cust_des") or "",
                    remarks=parsed.get("remarks") or ""
                )
                if str(result.get("Status")) == "200":
                    reply = f"✅ 매출 등록 완료!\n품목: {parsed.get('prod_nm') or parsed.get('prod_cd')}\n수량: {parsed['qty']}개\n단가: {parsed.get('price', 0):,}원"
                else:
                    errs = result.get("Errors", [{}])
                    reply = f"❌ 오류: {errs[0].get('Message', '알 수 없는 오류') if errs else '오류 발생'}"

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

        else:
            reply = ("안녕하세요! 대성인더스 ERP 봇입니다.\n\n"
                     "사용 방법:\n"
                     "• 매입: '거래처에서 품목 수량 단가 매입'\n"
                     "• 매출: '거래처에 품목 수량 단가 판매'\n"
                     "• 재고조회: '품목 재고 얼마야?'\n"
                     "• 품목조회: '품목 목록 보여줘'")

        return JSONResponse(make_response(reply))

    except Exception as e:
        return JSONResponse(make_response(f"오류가 발생했습니다: {str(e)}"))

@app.get("/")
def health():
    return {"status": "ok", "service": "대성인더스 ERP 봇"}
