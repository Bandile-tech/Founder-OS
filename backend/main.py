from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from openai_client import get_chat_response
import sqlite3

app = FastAPI()

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # local dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# DB setup
conn = sqlite3.connect("chat_memory.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS chats (
    session_id TEXT,
    role TEXT,
    content TEXT
)
""")
conn.commit()

class ChatRequest(BaseModel):
    message: str
    session_id: str

@app.post("/chat")
def chat_endpoint(request: ChatRequest):
    session = request.session_id

    # Load previous messages from DB
    c.execute("SELECT role, content FROM chats WHERE session_id=?", (session,))
    messages = [{"role": r, "content": m} for r, m in c.fetchall()]

    # Append new user message
    messages.append({"role": "user", "content": request.message})
    c.execute("INSERT INTO chats (session_id, role, content) VALUES (?, ?, ?)",
              (session, "user", request.message))
    conn.commit()

    # Get bot response
    bot_reply = get_chat_response(messages)

    # Save bot reply to DB
    c.execute("INSERT INTO chats (session_id, role, content) VALUES (?, ?, ?)",
              (session, "assistant", bot_reply))
    conn.commit()

    return {"reply": bot_reply}
