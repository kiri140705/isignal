import os
import json
import sqlite3
from datetime import datetime
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

# isignal_db.py 로드
from isignal_db import get_db_connection
from prompts import SAFE_FILTER_PROMPT, SOS_DETECTION_PROMPT, SYSTEM_INSTRUCTION

# MCP 서버 인스턴스 생성
mcp = FastMCP(
    "isignal",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
)

def get_current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ------------------------------------------------------------------------------
# Mockup LLM Functions (나중에 실제 OpenAI/Gemini API로 교체)
# ------------------------------------------------------------------------------
def mock_llm_filter(message: str) -> dict:
    if "19금" in message or "야동" in message:
        return {"is_safe": False, "violation_type": "19금", "reason": "성적인 단어 포함"}
    if "때려" in message or "죽여" in message:
        return {"is_safe": False, "violation_type": "폭력", "reason": "폭력적인 단어 포함"}
    return {"is_safe": True, "violation_type": None, "reason": "안전함"}

def mock_llm_sos_detection(message: str) -> dict:
    if "옥상" in message or "사진 보내" in message:
        return {
            "sos_level": 3,
            "detected_risk": "긴급 위기 상황",
            "confidence_score": 95,
            "reason": "명백한 극단적 선택 암시 또는 그루밍 범죄 노출",
            "recommended_action": "부모님께 즉시 카카오톡 알림"
        }
    elif "왕따" in message or "우울해" in message:
        return {
            "sos_level": 2,
            "detected_risk": "우울증/따돌림 징후",
            "confidence_score": 75,
            "reason": "지속적인 우울감 암시",
            "recommended_action": "주의 관찰 요망"
        }
    return {
        "sos_level": 0,
        "detected_risk": "정상",
        "confidence_score": 100,
        "reason": "일상적인 대화",
        "recommended_action": "없음"
    }

def mock_llm_generate_reply(message: str, is_safe: bool, sos_level: int) -> str:
    if not is_safe:
        return "그 이야기는 좀 부적절한 것 같아. 우리 다른 재밌는 이야기 해볼까?"
    if sos_level == 3:
        return "너무 힘들었겠다... 내가 항상 네 편이 되어줄게. 무슨 일이 있었는지 더 이야기해줄 수 있어?"
    if sos_level == 2:
        return "요즘 많이 힘들구나. 네가 원한다면 언제든지 이야기 들어줄게."
    if "망함" in message or "짜증" in message:
        return "오늘 무슨 안 좋은 일 있었어? 괜찮아, 누구나 그럴 때가 있는걸! 내가 다 들어줄게."
    return "우와, 정말 흥미로운 이야기다! 그래서 어떻게 됐어?"

# ------------------------------------------------------------------------------
# MCP Tools
# ------------------------------------------------------------------------------

@mcp.tool()
def RegisterStudent(
    student_id: str, 
    parent_id: str, 
    name: str, 
    age: int, 
    interests: str = "[]"
) -> str:
    """학부모가 자녀(학생)의 정보를 iSignal 시스템에 초기 등록합니다."""
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO students (student_id, parent_id, name, age, interests) VALUES (?, ?, ?, ?, ?)",
            (student_id, parent_id, name, age, interests)
        )
        conn.commit()
        return json.dumps({"status": "success", "message": f"{name} 학생 정보가 성공적으로 등록되었습니다."})
    except sqlite3.IntegrityError:
        return json.dumps({"status": "error", "message": "이미 등록된 학생 ID입니다."})
    finally:
        conn.close()

@mcp.tool()
def ChatWithAI(student_id: str, message: str) -> str:
    """아이의 메시지를 수신하고, 필터/SOS 검사를 거친 후 AI 응답을 반환합니다."""
    # 1. 필터 통과 검사 (Safe Filter)
    filter_result = mock_llm_filter(message)
    is_safe = filter_result["is_safe"]
    is_filtered = not is_safe

    # 2. 실시간 SOS 감지 (Fast Track)
    sos_result = mock_llm_sos_detection(message)
    sos_level = sos_result["sos_level"]
    is_sos = (sos_level == 3)

    # 3. AI 응답 생성
    ai_reply = mock_llm_generate_reply(message, is_safe, sos_level)

    # 4. DB에 기록
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO chat_logs (student_id, role, message, emotion_score, is_sos, is_filtered)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (student_id, 'user', message, json.dumps(sos_result), is_sos, is_filtered))
        
        c.execute('''
            INSERT INTO chat_logs (student_id, role, message)
            VALUES (?, ?, ?)
        ''', (student_id, 'assistant', ai_reply))
        
        # SOS 긴급 알림 스케줄러 (가짜 전송 로직)
        if is_sos:
            c.execute("SELECT parent_id FROM students WHERE student_id = ?", (student_id,))
            parent = c.fetchone()
            parent_id = parent['parent_id'] if parent else "UNKNOWN"
            
            alert_msg = f"[🚨아이시그널 긴급 SOS] 자녀의 위험 징후가 감지되었습니다. (이유: {sos_result['reason']})"
            c.execute('''
                INSERT INTO sos_alerts (student_id, parent_id, severity_level, alert_type, alert_message)
                VALUES (?, ?, ?, ?, ?)
            ''', (student_id, parent_id, sos_level, sos_result['detected_risk'], alert_msg))
            print(f">>> [긴급 알림 발송 모의] {parent_id} 님에게 전송: {alert_msg}")

        conn.commit()
    finally:
        conn.close()

    return json.dumps({
        "status": "success",
        "reply": ai_reply,
        "is_safe": is_safe,
        "sos_level": sos_level
    }, ensure_ascii=False)

@mcp.tool()
def GetMonthlyReport(student_id: str, report_month: str) -> str:
    """월간 종합 심리/적성 분석 리포트를 생성하여 반환합니다."""
    # 실제로는 트랙 B(배치) 엔진이 누적된 chat_logs를 바탕으로 심리 분석을 돌려야 합니다.
    # 여기서는 목업 데이터를 반환합니다.
    report = f'''# {report_month} 아이시그널 월간 종합 리포트

🌟 **이달의 발견 (재능/강점)**
아이가 대화 중 체계적인 문제 해결과 관련된 질문을 자주 던지며 **논리수학 지능**이 또래 상위 5% 수준으로 발현 중입니다.

🌱 **성격 및 교우관계 (보완점)**
스트레스 상황에서 속마음을 잘 표현하지 못하는 성향이 관찰되었습니다. 대화를 통해 감정을 솔직하게 말하는 연습이 필요해 보입니다.

🧭 **진로 및 적성 추천**
탐구형(I) 성향이 높게 나타나며, 데이터 사이언티스트나 우주공학자 직군에 흥미를 보일 가능성이 큽니다.
'''
    return json.dumps({"status": "success", "report": report}, ensure_ascii=False)

app = mcp.streamable_http_app()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def health_check(request):
    return JSONResponse({"status": "ok"})

app.routes.append(Route("/", endpoint=health_check, methods=["GET"]))

if __name__ == "__main__":
    print("🚀 iSignal MCP 서버를 시작합니다 (PlayMCP 연동용 HTTP 모드)...")
    uvicorn.run(app, host="0.0.0.0", port=8080)
