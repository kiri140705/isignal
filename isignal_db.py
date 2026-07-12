import sqlite3
import os
import json
from datetime import datetime

# 아이시그널 DB 경로 설정
DB_PATH = os.path.join(os.path.dirname(__file__), "isignal_data.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. 학생(아이) 정보 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS students (
            student_id TEXT PRIMARY KEY,
            parent_id TEXT NOT NULL,
            name TEXT NOT NULL,
            age INTEGER,
            grade TEXT,
            gender TEXT,
            interests TEXT,           -- 초기 학부모가 입력한 관심사 (JSON 배열)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 2. 실시간 대화 로그 테이블 (감정, 의도 분석 포함)
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            role TEXT NOT NULL,       -- 'user' (아이) 또는 'assistant' (AI)
            message TEXT NOT NULL,    -- 대화 원문
            emotion_score TEXT,       -- AI가 분석한 감정 상태 (JSON, 예: {"happiness": 80, "stress": 20})
            is_sos BOOLEAN DEFAULT 0, -- SOS 상황 여부 (0: 정상, 1: 위험)
            is_filtered BOOLEAN DEFAULT 0, -- 19금/폭력 등 필터링 여부
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students (student_id)
        )
    ''')
    
    # 3. 누적 적성 및 심리 프로파일 테이블 (다중지능, RIASEC, 빅파이브 등)
    c.execute('''
        CREATE TABLE IF NOT EXISTS aptitude_profiles (
            profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            big_five TEXT,            -- 빅파이브 분석 결과 (JSON)
            multiple_intelligences TEXT, -- 다중지능 분석 결과 (JSON)
            riasec TEXT,              -- 홀랜드 직업적성 유형 (JSON)
            subject_interests TEXT,   -- 교과목 관심도 점수 (JSON)
            weaknesses TEXT,          -- 취약점 및 개선점 (JSON)
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students (student_id)
        )
    ''')
    
    # 4. SOS 및 학부모 긴급 알림 내역 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS sos_alerts (
            alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            parent_id TEXT NOT NULL,
            severity_level INTEGER NOT NULL, -- 1: 주의, 2: 경고, 3: 긴급(SOS)
            alert_type TEXT NOT NULL,        -- 'bullying', 'self_harm', 'runaway', 'grooming', etc.
            alert_message TEXT NOT NULL,     -- 카톡으로 발송된 메시지 내용
            is_sent BOOLEAN DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students (student_id)
        )
    ''')
    
    # 5. 정기(월간/주간) 리포트 발행 내역 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS regular_reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            report_month TEXT NOT NULL,      -- 예: '2026-07'
            report_content TEXT NOT NULL,    -- 마크다운 포맷의 전체 리포트 원문
            percentile_data TEXT,            -- 전국 백분위 분석 결과 (JSON)
            career_recommendations TEXT,     -- 직업/성공사례 매칭 결과 (JSON)
            is_sent BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students (student_id)
        )
    ''')
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("✅ 아이시그널(iSignal) 데이터베이스 초기화 완료")
