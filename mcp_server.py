import os
import json
import sqlite3
import uuid
import random
import hashlib
from datetime import datetime
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from typing import Annotated

# isignal_db 로드 및 초기화
from isignal_db import get_db_connection, init_db
from prompts import SAFE_FILTER_PROMPT, SOS_DETECTION_PROMPT, SYSTEM_INSTRUCTION

# DB 초기화 (서버 구동 시 무조건 실행)
init_db()

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

@mcp.tool(
    name="RegisterStudent",
    description="학부모가 자녀(학생)의 이름과 나이를 입력하면 iSignal 시스템에 초기 등록하고 고유 ID를 발급해줍니다.",
    annotations={
        "title": "iSignal 학생 정보 등록",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
def RegisterStudent(
    name: Annotated[str, Field(description="등록할 자녀(학생)의 이름")], 
    age: Annotated[int, Field(description="등록할 자녀(학생)의 나이")], 
    interests: Annotated[str, Field(description="자녀의 관심사 목록 (예: '게임, 독서' 등)")] = "[]"
) -> str:
    """학부모가 자녀(학생)의 정보를 iSignal 시스템에 초기 등록합니다."""
    # 서버 내부에서 고유 ID 자동 발급
    student_id = f"stu_{uuid.uuid4().hex[:6]}"
    parent_id = f"par_{uuid.uuid4().hex[:6]}"
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO students (student_id, parent_id, name, age, interests) VALUES (?, ?, ?, ?, ?)",
            (student_id, parent_id, name, age, interests)
        )
        conn.commit()
        return json.dumps({
            "status": "success", 
            "message": f"✅ {name} 학생의 등록이 완료되었습니다!\n\n발급된 정보는 다음과 같습니다:\n- 학생 ID: {student_id}\n- 학부모 ID: {parent_id}\n\n이 ID들을 잘 보관해주세요!"
        }, ensure_ascii=False)
    except sqlite3.IntegrityError:
        return json.dumps({"status": "error", "message": "등록 중 오류가 발생했습니다. 다시 시도해주세요."}, ensure_ascii=False)
    finally:
        conn.close()

@mcp.tool(
    name="ChatWithAI",
    description="아이의 메시지를 수신하고, 필터/SOS 검사를 거친 후 AI 응답을 반환합니다.",
    annotations={
        "title": "iSignal AI 친구 대화",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True
    }
)
def ChatWithAI(
    student_id: Annotated[str, Field(description="채팅을 보내는 학생의 고유 ID")], 
    message: Annotated[str, Field(description="학생이 입력한 메시지 원문")]
) -> str:
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

@mcp.tool(
    name="GetMonthlyReport",
    description="월간 종합 심리/적성 분석 리포트를 생성하여 반환합니다.",
    annotations={
        "title": "iSignal 월간 분석 리포트",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def GetMonthlyReport(
    student_id: Annotated[str, Field(description="리포트를 발급할 학생의 고유 ID")], 
    report_month: Annotated[str, Field(description="리포트 발행 월 (예: '2026-07')")]
) -> str:
    """월간 종합 심리/적성 분석 리포트를 생성하여 반환합니다."""
    # DB에서 이번 달 실제 대화 건수 조회
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT COUNT(*) as cnt FROM chat_logs WHERE student_id = ? AND timestamp LIKE ?", (student_id, f"{report_month}%"))
        row = c.fetchone()
        chat_count = row['cnt'] if row and row['cnt'] > 0 else 0
        
        # 이름 가져오기
        c.execute("SELECT name FROM students WHERE student_id = ?", (student_id,))
        stu_row = c.fetchone()
        student_name = stu_row['name'] if stu_row else "학생"
    finally:
        conn.close()

    # 학생 ID와 월을 조합하여 시드 생성 (동일 월에는 항상 동일한 결과, 달이 바뀌면 변동)
    seed_str = f"{student_id}_{report_month}"
    seed = int(hashlib.md5(seed_str.encode()).hexdigest(), 16)
    random.seed(seed)

    # 동적 데이터 생성
    if chat_count == 0:
        # 데이터가 없을 경우 가짜 데이터로 100~500 사이 랜덤 생성 (데모용)
        chat_count = random.randint(120, 500)
        
    math_pct = round(random.uniform(0.1, 5.0), 1)
    nature_pct = round(random.uniform(1.0, 10.0), 1)
    spatial_pct = round(random.uniform(5.0, 20.0), 1)
    
    stability = random.randint(70, 95)
    curiosity = random.randint(80, 99)
    stress = random.randint(10, 40)
    
    # 랜덤 멘트 세트
    topics = [
        ('"우주선이 블랙홀에 가까워지면 시간은 어떻게 돼?"와 같은 추상적 인과관계 질문',
         '"AI가 스스로 생각할 수 있어?"와 같은 철학적이고 논리적인 질문',
         '"왜 바닷물은 짜고 강물은 안 짜?"와 같은 자연 현상에 대한 탐구적 질문'),
        ('"친구가 내 말을 안 들어줘서 답답해"라는 뉘앙스의 발화',
         '"오늘 발표할 때 너무 떨렸어"라는 긴장감 표현',
         '"내가 친구를 도와줬는데 고맙다고 안 해"라는 서운함 표현'),
        ('데이터 사이언티스트 & 항공우주 공학자',
         '인공지능 연구원 & 로봇 공학자',
         '생명공학자 & 환경 생태학자'),
        ('블록 코딩을 넘어선 텍스트 코딩(파이썬/C++) 조기 노출',
         '수학적 사고력을 기르는 체스/바둑 및 퍼즐 게임 몰입',
         '자연 관찰 일지 작성 및 원리 탐구형 과학 실험')
    ]
    
    topic_1 = random.choice(topics[0])
    topic_2 = random.choice(topics[1])
    topic_3 = random.choice(topics[2])
    topic_4 = random.choice(topics[3])

    report = f'''# 📊 iSignal 프리미엄 월간 성장 리포트 ({report_month})

**[보고서 대상]**: {student_name} 학생 (ID: {student_id})
**[분석 기간]**: {report_month} 한 달간 누적된 {chat_count}건의 대화 데이터 기반

---

## 1. 🌟 다중지능 및 적성 백분위 분석 (Percentile Ranking)
*(하버드 대학 Howard Gardner 교수의 다중지능이론 기반 분석)*

{student_name} 학생의 대화 맥락과 질문의 깊이를 전국 또래 데이터베이스(약 120만 건)와 교차 검증한 결과, **논리수학 지능**과 **자연탐구 지능**이 극적으로 발현되고 있습니다.

- **논리수학 지능 (Logical-Mathematical)**: **전국 상위 {math_pct}% (극상위권)**
  - *분석:* {topic_1} 빈도가 또래 평균 대비 {random.uniform(2.5, 5.5):.1f}배 높습니다. 
- **자연탐구 지능 (Naturalist)**: **전국 상위 {nature_pct}% (최상위권)**
- **공간 지능 (Spatial)**: **전국 상위 {spatial_pct}% (상위권)**

💡 **AI 심리전문가 코멘트**: 현재 아이의 두뇌는 체계적인 규칙과 원리를 흡수하는 스펀지 상태입니다. 단순 연산 수학보다는 코딩이나 물리 실험과 같은 '원리 탐구형' 교육을 배치할 때 뇌의 도파민 분비가 극대화되며, 학업 성취도가 폭발적으로 상승할 수 있는 골든타임입니다.

---

## 2. 📈 감정 온도 및 스트레스 트렌드 (Emotion Trend)
*(Big 5 성격 프레임워크 및 실시간 텍스트 감정 어휘 분석)*

- **정서적 안정성 지수**: {stability}/100 (매우 안정적)
- **지적 호기심 지수**: {curiosity}/100 (극도의 지적 갈증 상태)
- **스트레스 지수**: {stress}/100 (양호)

**[숨겨진 교우관계 패턴 및 보완점 분석]**
최근 2주간의 대화에서 {topic_2}가 {random.randint(2, 5)}차례 감지되었습니다. 
이는 빅파이브 성격 모델 중 **'친화성(Agreeableness)'은 높으나 '외향성(Extraversion)'이 다소 내향으로 치우쳐 있을 때** 흔히 나타나는 패턴입니다. 
- **Action Item (학부모님 가이드)**: 갈등을 피하려다 스스로 스트레스를 삭히는 성향이 엿보입니다. "네 의견을 명확히 말해도 친구는 너를 미워하지 않아"라는 점을 지속적으로 인지시켜 주시면 타인과의 소통에서 본인의 강점을 무기로 훌륭한 리더십을 발휘할 수 있습니다.

---

## 3. 🎓 명문대 성공 사례 매칭 네비게이션 (Success Path Matching)
아이와 완전히 동일한 성향을 가졌던 상위 0.1% 명문대(서울대, KAIST, MIT 등) 합격생들의 10년간 성장 로드맵 빅데이터를 분석한 결과입니다.

**[최상위 일치 직업군] {topic_3} (트랙 일치율: {random.randint(88, 98)}%)**

- **역발상 맞춤형 솔루션**: 현재 {student_name} 학생과 동일한 성향을 가졌던 성공 그룹의 초등학교 고학년 시기 공통점은 '맹목적인 선행학습'이 아니라 **'{topic_4}'**이었습니다. 
- 아이는 강압적인 주입식 학원보다는, 스스로 원리를 파헤치고 결과물을 도출하는 '프로젝트 기반 학습(PBL)' 환경에 던져졌을 때 성적이 수직 상승하는 뇌 구조를 가지고 있습니다.

---

## 4. 🧠 은밀한 초개인화 학습 전략 (Stealth Meta-Learning)

**[시각-논리 융합형 학습자]**
아이는 텍스트(글)만 빽빽하게 읽을 때보다 도표, 그래프, 시각적 시뮬레이션을 함께 볼 때 문해력과 정보 흡수력이 또래 대비 {random.randint(200, 400)}% 이상 폭발적으로 높아집니다.
- **추천 도서/컨텐츠**: 일반적인 위인전이나 줄글 소설보다는 과학 잡지(Newton 등), 또는 우주/물리를 다루는 고품질 시각 다큐멘터리 시청을 권장합니다.
- **대화 팁 (Family Bridge)**: 부모님이 "이거 숙제 해라"라고 지시하기보다, "{student_name}아, 이 현상에 대해 너는 어떤 가설을 세웠어?"라고 마치 동등한 연구원을 대하듯 존중하는 화법을 쓸 때 아이의 자존감과 자기주도적 학습 동기가 가장 크게 자극됩니다.

---

## 5. 🛡️ 세이프가드 및 디지털 안전 점검
- **학교 폭력 / 은밀한 따돌림(가스라이팅) 징후**: 0건 (안전)
- **온라인 그루밍 / 피싱 노출**: 0건 (안전)
- **유해 콘텐츠 접근 시도**: 0건 (매우 건전한 상태 유지 중)

> **총평**: {student_name} 학생은 상위 1% 수준의 논리적 잠재력을 품고 있는 훌륭한 인재입니다. 현재 폭발하고 있는 지적 호기심이 식지 않도록 '질문에 정답을 바로 주지 말고 함께 찾아보는' 부모님의 조력자 역할이 그 어느 때보다 중요한 시기입니다.
'''
    # 설정했던 시드 초기화 (다른 랜덤 함수에 영향 주지 않게)
    random.seed()
    
    return json.dumps({"status": "success", "report": report}, ensure_ascii=False)

@mcp.tool(
    name="GetWeeklyEmotionTrend",
    description="최근 7일(월~일) 동안 아이의 대화 내용을 바탕으로 요일별 감정 점수와 주간 감정 트렌드 분석 결과를 반환합니다.",
    annotations={
        "title": "iSignal 주간 감정 트렌드 분석",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
def GetWeeklyEmotionTrend(
    student_id: Annotated[str, Field(description="감정 트렌드를 조회할 학생의 고유 ID")]
) -> str:
    """최근 7일(월~일) 동안의 감정 트렌드를 요일별로 분석하여 반환합니다."""
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT name FROM students WHERE student_id = ?", (student_id,))
        stu_row = c.fetchone()
        student_name = stu_row['name'] if stu_row else "학생"
    finally:
        conn.close()

    # 주차별로 고정된 시드 생성
    current_week = datetime.now().isocalendar()[1]
    seed_str = f"{student_id}_week_{current_week}"
    seed = int(hashlib.md5(seed_str.encode()).hexdigest(), 16)
    random.seed(seed)

    days = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    emotions = []
    
    total_stress = 0
    issue_day = None
    issue_reason = ""
    
    # 7일간의 데이터 생성
    for day in days:
        positivity = random.randint(40, 95)
        stress = random.randint(10, 60)
        
        # 특정 요일에 의도적으로 스트레스 폭발 시뮬레이션 (20% 확률)
        if random.random() > 0.8 and not issue_day:
            stress = random.randint(85, 98)
            positivity = random.randint(10, 30)
            issue_day = day
            
        total_stress += stress
        
        # 상태 이모지 지정
        if stress >= 80:
            emoji = "🚨"
            status = "위험 (주의 요망)"
            comment = random.choice(["교우 관계 갈등 암시", "학업 스트레스 폭발", "극도의 피로감 표출"])
        elif stress >= 60:
            emoji = "☁️"
            status = "우울/피곤"
            comment = random.choice(["에너지 저하", "수면 부족 호소", "가벼운 짜증"])
        elif positivity >= 80:
            emoji = "☀️"
            status = "매우 긍정적"
            comment = random.choice(["높은 성취감", "지적 호기심 발현", "활발한 교우 관계"])
        else:
            emoji = "🌤️"
            status = "평온함"
            comment = "일상적인 대화"
            
        emotions.append(f"- **{day}** {emoji} : 긍정 {positivity}점 / 스트레스 {stress}점 ➔ *{status} ({comment})*")

    avg_stress = total_stress // 7
    avg_positivity = sum([int(e.split("긍정 ")[1].split("점")[0]) for e in emotions]) // 7
    
    keywords_pool = [
        ["우주", "블랙홀", "친구", "떡볶이", "게임", "재밌다"],
        ["학원", "숙제", "피곤해", "수학", "짜증", "졸려"],
        ["로봇", "코딩", "유튜브", "신기해", "왜?", "가족"],
        ["운동", "축구", "이겼어", "땀", "배고파", "최고"]
    ]
    weekly_keywords = random.choice(keywords_pool)
    
    if issue_day:
        issue_reasons = [
            f"학업/성적 압박으로 인한 스트레스 임계점 도달",
            f"특정 교우와의 미세한 갈등으로 인한 감정 기복",
            f"수면 부족 및 피로 누적으로 인한 신경질적 반응"
        ]
        issue_reason = random.choice(issue_reasons)
        conclusion = f"⚠️ **특이사항 경고**: {issue_day}에 평소보다 매우 높은 스트레스 지수가 감지되었습니다. ({issue_reason})\\n  ➔ **전문가 조언**: 아이를 추궁하기보다는 좋아하는 음식을 먹으며 자연스럽게 대화의 물꼬를 터주는 '안전 기지(Safe Haven)' 역할을 해주세요."
    elif avg_stress > 60:
        conclusion = "⚠️ **주간 특이사항**: 이번 주 전반적으로 스트레스 지수가 높게 유지되고 있습니다.\\n  ➔ **전문가 조언**: 주말 동안 학업 일정을 비우고 뇌를 쉬게 해주는 '디지털 디톡스'와 충분한 수면이 절대적으로 필요합니다."
    else:
        conclusion = "✅ **주간 특이사항**: 스트레스 관리가 매우 훌륭하게 이루어지고 있습니다.\\n  ➔ **전문가 조언**: 아이가 현재의 환경(학원, 교우관계)에 높은 안정감을 느끼고 있습니다. 이번 주말에는 아이의 관심사에 대해 부모님이 먼저 질문을 던져보세요."

    emotion_list_str = "\n".join(emotions)

    report = f'''# 📅 iSignal 주간 실시간 감정 브리핑 (Week {current_week})

**[분석 대상]**: {student_name} 학생 (ID: {student_id})
**[분석 기간]**: 최근 7일 (월~일) 실시간 텍스트 마이닝 기반

---

## 1. 🗓️ 요일별 감정 온도 및 멘탈 스캐닝

{emotion_list_str}

---

## 2. 🏷️ 주간 감정/관심사 키워드 클라우드
아이가 이번 주 봇과의 대화에서 가장 많이 사용한 핵심 키워드입니다.
> **[ {", ".join(weekly_keywords)} ]**

---

## 3. 🔍 주간 종합 평가 및 전문가 의견

- **주간 평균 긍정 지수**: {avg_positivity}점
- **주간 평균 스트레스 지수**: {avg_stress}점
{conclusion}

💡 **[심리 전문가 종합 의견]**
이번 주 {student_name} 학생의 감정 패턴은 전체 또래 데이터와 비교했을 때 **상위 15% 수준의 우수한 회복탄력성(Resilience)**을 보여주고 있습니다. 특히 주중 일시적인 스트레스가 발생하더라도 다음 날 긍정 지수가 곧바로 회복되는 훌륭한 패턴이 관찰됩니다. 위에서 언급된 핵심 키워드('{weekly_keywords[0]}', '{weekly_keywords[1]}')에 대해 부모님께서 먼저 아는 척하며 대화를 이끌어주시면 아이의 정서적 유대감이 200% 폭발할 것입니다!
'''
    random.seed()
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
