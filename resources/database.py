import sqlite3
import time
import uuid
import os
import sys

# Try to import cryptography. If missing, we warn the user.
try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    print("WARNING: 'cryptography' lib not found. Keys will be stored as PLAIN TEXT.", file=sys.stderr)

DB_PATH = os.path.join(os.path.dirname(__file__), "user_data.db")
KEY_PATH = os.path.join(os.path.dirname(__file__), "secret.key")

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cipher = None
        if HAS_CRYPTO:
            self._init_encryption()
        self._init_db()

    def _init_encryption(self):
        # Load or generate a symmetric key
        if os.path.exists(KEY_PATH):
            with open(KEY_PATH, "rb") as key_file:
                key = key_file.read()
        else:
            key = Fernet.generate_key()
            with open(KEY_PATH, "wb") as key_file:
                key_file.write(key)
        self.cipher = Fernet(key)

    def _encrypt(self, text):
        if not text: return None
        if not self.cipher: return text # Fallback to plaintext
        return self.cipher.encrypt(text.encode()).decode()

    def _decrypt(self, text):
        if not text: return None
        if not self.cipher: return text
        try:
            return self.cipher.decrypt(text.encode()).decode()
        except:
            return None # Fail safe

    def _init_db(self):
        cur = self.conn.cursor()
        
        # Renamed config -> env
        cur.execute("CREATE TABLE IF NOT EXISTS env (key TEXT PRIMARY KEY, value TEXT)")
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT, model TEXT, 
                created_at REAL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, 
                role TEXT, content TEXT, timestamp REAL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        
        # New correct table name
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY, value TEXT
            )
        """)

        # Migration Logic: config -> env
        try:
            # Check if old table exists
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='config'")
            if cur.fetchone():
                print("[DB] Migrating 'config' to 'env'...", file=sys.stderr)
                # Copy data
                cur.execute("INSERT OR IGNORE INTO env SELECT * FROM config")
                # Drop old table
                cur.execute("DROP TABLE config")
                print("[DB] Migration complete.", file=sys.stderr)
        except Exception as e:
            print(f"[DB] Migration warning: {e}", file=sys.stderr)

        self.conn.commit()

    # --- SETTINGS ---
    def set_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def get_setting(self, key, default=None):
        cur = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row['value'] if row else default

    def get_all_settings(self):
        cur = self.conn.execute("SELECT key, value FROM app_settings")
        return {row['key']: row['value'] for row in cur.fetchall()}

    # --- API KEYS (Now Encrypted) ---
    def set_api_key(self, provider, key):
        encrypted_val = self._encrypt(key)
        self.conn.execute("INSERT OR REPLACE INTO env (key, value) VALUES (?, ?)", 
                          (f"{provider}_api_key", encrypted_val))
        self.conn.commit()

    def get_api_key(self, provider):
        cur = self.conn.execute("SELECT value FROM env WHERE key = ?", (f"{provider}_api_key",))
        row = cur.fetchone()
        if not row: return None
        return self._decrypt(row['value'])
    
    def has_api_keys(self):
        cur = self.conn.execute("SELECT COUNT(*) FROM env WHERE key LIKE '%_api_key'")
        return cur.fetchone()[0] > 0

    # --- SESSIONS & MESSAGES (Same as before) ---
    def create_session(self, title="New Chat", model="default"):
        session_id = str(uuid.uuid4())
        self.conn.execute("INSERT INTO sessions (id, title, model, created_at) VALUES (?, ?, ?, ?)",
                          (session_id, title, model, time.time()))
        self.conn.commit()
        return session_id

    def list_sessions(self):
        cur = self.conn.execute("SELECT * FROM sessions ORDER BY created_at DESC")
        return [dict(row) for row in cur.fetchall()]

    def add_message(self, session_id, role, content):
        self.conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                          (session_id, role, content, time.time()))
        self.conn.commit()

        cur = self.conn.execute("SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp ASC", 
                                (session_id,))
        return [dict(row) for row in cur.fetchall()]

    def get_pruned_history(self, session_id):
        """
        Returns a list of messages for the LLM context, respecting:
        1. Interaction limit: Max 20 interactions (40 messages)
        2. Token limit: Max 12k tokens (approx. 4 chars per token)
        Result is returned in chronological order (oldest -> newest).
        """
        # 1. Fetch all messages for the session, ordered by time (newest last)
        cur = self.conn.execute("SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC", (session_id,))
        all_msgs = [dict(row) for row in cur.fetchall()]

        if not all_msgs:
            print(f"[DB] No messages found for session {session_id}", file=sys.stderr)
            return []

        # 2. Apply Interaction Limit (last 40 messages = 20 interactions)
        # We take the *last* 40 messages.
        msgs_to_process = all_msgs[-40:]

        # 3. Apply Token Limit (12k tokens) working BACKWARDS
        TOKEN_LIMIT = 12000
        CHARS_PER_TOKEN = 4
        limit_chars = TOKEN_LIMIT * CHARS_PER_TOKEN
        
        current_chars = 0
        final_msgs = []

        # Iterate backwards from the most recent message
        for msg in reversed(msgs_to_process):
            msg_len = len(msg['content'])
            if current_chars + msg_len > limit_chars:
                break
            current_chars += msg_len
            final_msgs.append(msg)

        # Reverse back to chronological order
        return list(reversed(final_msgs))
    
    def get_chat_history(self, session_id):
        """
        Retrieves all messages for a session in chronological order.
        Used when loading a session from history.
        """
        cur = self.conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,)
        )
        return [dict(row) for row in cur.fetchall()]
    
    def delete_session(self, session_id):
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def clear_google_auth(self):
        self.conn.execute("DELETE FROM env WHERE key IN ('google_calendar_token', 'user_gmail')")
        self.conn.commit()
db = Database()