import sqlite3
import time
import uuid
import os
import sys
import json

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

    def _json_loads(self, raw_value, fallback):
        if raw_value in (None, ""):
            return fallback
        try:
            value = json.loads(raw_value)
        except Exception:
            return fallback

        if isinstance(fallback, list):
            return value if isinstance(value, list) else fallback
        if isinstance(fallback, dict):
            return value if isinstance(value, dict) else fallback
        return value

    def _json_dumps(self, value, fallback):
        candidate = fallback if value is None else value
        try:
            return json.dumps(candidate)
        except Exception:
            return json.dumps(fallback)

    def _normalize_string_list(self, values):
        normalized = []
        for item in values or []:
            text = str(item).strip()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    def _ensure_column(self, table_name, column_name, column_sql):
        cur = self.conn.execute(f"PRAGMA table_info({table_name})")
        columns = {row["name"] for row in cur.fetchall()}
        if column_name in columns:
            return
        self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

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

        # Meeting mode tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                goal TEXT,
                user_name TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                started_at REAL,
                ended_at REAL,
                created_at REAL DEFAULT (strftime('%s','now'))
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meeting_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                raw_text TEXT,
                is_final INTEGER NOT NULL DEFAULT 0,
                confidence REAL,
                timestamp REAL NOT NULL,
                FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meeting_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                summary TEXT,
                cues_json TEXT,
                nudge TEXT,
                action_items_json TEXT,
                timestamp REAL NOT NULL,
                FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meeting_action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                action_item TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meeting_settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS integration_providers (
                provider TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS integration_accounts (
                account_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                platform_key TEXT NOT NULL DEFAULT 'default',
                email TEXT,
                display_name TEXT,
                account_label TEXT,
                status TEXT NOT NULL DEFAULT 'needs_auth',
                is_primary INTEGER NOT NULL DEFAULT 0,
                granted_scopes_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(provider) REFERENCES integration_providers(provider)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS integration_account_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                tool_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                platform_key TEXT NOT NULL DEFAULT 'default',
                scopes_json TEXT NOT NULL DEFAULT '[]',
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(account_id, tool_id),
                FOREIGN KEY(account_id) REFERENCES integration_accounts(account_id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS integration_account_credentials (
                account_id TEXT PRIMARY KEY,
                encrypted_access_token TEXT,
                encrypted_refresh_token TEXT,
                access_token_expires_at REAL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(account_id) REFERENCES integration_accounts(account_id) ON DELETE CASCADE
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_integration_accounts_provider
            ON integration_accounts(provider, is_primary DESC, updated_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_integration_account_tools_account
            ON integration_account_tools(account_id, tool_id)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cosmic_mail_inbound_seen (
                mailbox_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                seen_at REAL NOT NULL,
                PRIMARY KEY (mailbox_id, message_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_cosmic_mail_inbound_seen_mailbox
            ON cosmic_mail_inbound_seen(mailbox_id)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cosmic_mail_mailbox_poll (
                mailbox_id TEXT PRIMARY KEY,
                baseline_done INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
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

        self._ensure_column("meeting_transcripts", "raw_text", "TEXT")
        self.conn.commit()
        self._seed_default_integration_providers()

    def _seed_default_integration_providers(self):
        now = time.time()
        defaults = [
            {
                "provider": "google",
                "display_name": "Google",
                "metadata": {
                    "supports_multi_account": True,
                    "supports_tool_scopes": True,
                    "category": "productivity",
                    "base_scopes": [
                        "openid",
                        "https://www.googleapis.com/auth/userinfo.profile",
                        "https://www.googleapis.com/auth/userinfo.email",
                    ],
                },
            },
        ]
        for item in defaults:
            self.conn.execute(
                """
                INSERT INTO integration_providers (provider, display_name, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    display_name = excluded.display_name,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    item["provider"],
                    item["display_name"],
                    self._json_dumps(item.get("metadata"), {}),
                    now,
                    now,
                ),
            )
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

    # --- MEETING MODE ---
    def create_meeting(self, title, goal="", user_name="User"):
        meeting_id = str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO meetings (id, title, goal, user_name, status, started_at, created_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (meeting_id, title, goal, user_name, now, now),
        )
        self.conn.commit()
        return meeting_id

    def set_meeting_status(self, meeting_id, status):
        self.conn.execute(
            "UPDATE meetings SET status = ? WHERE id = ?",
            (status, meeting_id),
        )
        self.conn.commit()

    def end_meeting(self, meeting_id):
        self.conn.execute(
            "UPDATE meetings SET status = 'ended', ended_at = ? WHERE id = ?",
            (time.time(), meeting_id),
        )
        self.conn.commit()

    def add_meeting_transcript(self, meeting_id, speaker, text, is_final, confidence=0.0, timestamp=None, raw_text=None):
        ts = time.time() if timestamp is None else float(timestamp)
        cur = self.conn.execute(
            """
            INSERT INTO meeting_transcripts (meeting_id, speaker, text, raw_text, is_final, confidence, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (meeting_id, speaker, text, raw_text if raw_text is not None else text, int(bool(is_final)), float(confidence or 0.0), ts),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_meeting_transcript_text(self, transcript_id, text):
        self.conn.execute(
            "UPDATE meeting_transcripts SET text = ? WHERE id = ?",
            (text, int(transcript_id)),
        )
        self.conn.commit()

    def add_meeting_update(self, meeting_id, summary="", cues=None, nudge="", action_items=None, timestamp=None):
        ts = time.time() if timestamp is None else float(timestamp)
        cues = cues or []
        action_items = action_items or []
        self.conn.execute(
            """
            INSERT INTO meeting_updates (meeting_id, summary, cues_json, nudge, action_items_json, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (meeting_id, summary or "", json.dumps(cues), nudge or "", json.dumps(action_items), ts),
        )
        for item in action_items:
            item_text = str(item).strip()
            if item_text:
                self.add_meeting_action_item(meeting_id, item_text, commit=False)
        self.conn.commit()

    def add_meeting_action_item(self, meeting_id, action_item, commit=True):
        self.conn.execute(
            """
            INSERT INTO meeting_action_items (meeting_id, action_item, created_at)
            VALUES (?, ?, ?)
            """,
            (meeting_id, action_item, time.time()),
        )
        if commit:
            self.conn.commit()

    def get_meeting_context(self, meeting_id, transcript_limit=300, update_limit=50):
        transcripts_cur = self.conn.execute(
            """
            SELECT id, speaker, text, raw_text, is_final, confidence, timestamp
            FROM meeting_transcripts
            WHERE meeting_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (meeting_id, transcript_limit),
        )
        transcripts = [dict(row) for row in transcripts_cur.fetchall()]
        transcripts.reverse()

        updates_cur = self.conn.execute(
            """
            SELECT summary, cues_json, nudge, action_items_json, timestamp
            FROM meeting_updates
            WHERE meeting_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (meeting_id, update_limit),
        )
        updates = [dict(row) for row in updates_cur.fetchall()]
        updates.reverse()

        normalized_updates = []
        for update in updates:
            cues = []
            items = []
            try:
                cues = json.loads(update.get("cues_json") or "[]")
            except Exception:
                cues = []
            try:
                items = json.loads(update.get("action_items_json") or "[]")
            except Exception:
                items = []
            normalized_updates.append({
                "summary": update.get("summary") or "",
                "cues": cues,
                "nudge": update.get("nudge") or "",
                "action_items": items,
                "timestamp": update.get("timestamp"),
            })

        return {
            "transcripts": transcripts,
            "updates": normalized_updates,
        }

    def get_meeting_report(self, meeting_id):
        meeting_cur = self.conn.execute(
            "SELECT * FROM meetings WHERE id = ? LIMIT 1",
            (meeting_id,),
        )
        meeting = meeting_cur.fetchone()

        context = self.get_meeting_context(meeting_id, transcript_limit=2000, update_limit=500)
        action_cur = self.conn.execute(
            """
            SELECT action_item, created_at
            FROM meeting_action_items
            WHERE meeting_id = ?
            ORDER BY created_at ASC
            """,
            (meeting_id,),
        )
        action_rows = [dict(row) for row in action_cur.fetchall()]

        return {
            "meeting": dict(meeting) if meeting else None,
            "context": context,
            "action_items": action_rows,
        }

    def set_meeting_setting(self, key, value):
        self.conn.execute(
            """
            INSERT INTO meeting_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (str(key), str(value), time.time()),
        )
        self.conn.commit()

    def get_meeting_setting(self, key, default=None):
        cur = self.conn.execute("SELECT value FROM meeting_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

    def get_all_meeting_settings(self):
        cur = self.conn.execute("SELECT key, value FROM meeting_settings")
        return {row["key"]: row["value"] for row in cur.fetchall()}

    # --- INTEGRATIONS ---
    def get_integration_accounts(self, provider=None):
        provider_meta_map = {
            row["provider"]: self._json_loads(row["metadata_json"], {})
            for row in self.conn.execute("SELECT provider, metadata_json FROM integration_providers").fetchall()
        }
        query = """
            SELECT account_id, provider, platform_key, email, display_name, account_label,
                   status, is_primary, granted_scopes_json, metadata_json, created_at, updated_at
            FROM integration_accounts
        """
        params = []
        if provider:
            query += " WHERE provider = ?"
            params.append(str(provider).strip().lower())
        query += " ORDER BY is_primary DESC, updated_at DESC, created_at DESC"

        cur = self.conn.execute(query, params)
        accounts = []
        for row in cur.fetchall():
            provider_metadata = provider_meta_map.get(row["provider"], {})
            base_scopes = self._normalize_string_list(provider_metadata.get("base_scopes") or [])
            tool_cur = self.conn.execute(
                """
                SELECT tool_id, tool_name, platform_key, scopes_json, config_json, created_at, updated_at
                FROM integration_account_tools
                WHERE account_id = ?
                ORDER BY created_at ASC, tool_name ASC
                """,
                (row["account_id"],),
            )
            tools = []
            derived_scopes = []
            for tool_row in tool_cur.fetchall():
                scopes = self._normalize_string_list(self._json_loads(tool_row["scopes_json"], []))
                derived_scopes.extend(scopes)
                tools.append({
                    "tool_id": tool_row["tool_id"],
                    "tool_name": tool_row["tool_name"],
                    "platform_key": tool_row["platform_key"],
                    "scopes": scopes,
                    "config": self._json_loads(tool_row["config_json"], {}),
                    "created_at": tool_row["created_at"],
                    "updated_at": tool_row["updated_at"],
                })

            required_scopes = self._normalize_string_list(base_scopes + derived_scopes)
            granted_scopes = self._normalize_string_list(self._json_loads(row["granted_scopes_json"], []))
            credentials = self.get_integration_credentials(row["account_id"])
            metadata = self._json_loads(row["metadata_json"], {})
            metadata["has_refresh_token"] = bool(credentials.get("refresh_token"))
            metadata["access_token_expires_at"] = credentials.get("access_token_expires_at")
            metadata["scope_match"] = all(scope in granted_scopes for scope in required_scopes)

            accounts.append({
                "account_id": row["account_id"],
                "provider": row["provider"],
                "platform_key": row["platform_key"],
                "email": row["email"] or "",
                "display_name": row["display_name"] or "",
                "account_label": row["account_label"] or "",
                "status": row["status"] or "needs_auth",
                "is_primary": bool(row["is_primary"]),
                "granted_scopes": granted_scopes,
                "required_scopes": required_scopes,
                "selected_tools": [tool["tool_id"] for tool in tools],
                "metadata": metadata,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "tools": tools,
            })

        return accounts

    def get_integration_account(self, account_id):
        account_id = str(account_id).strip()
        if not account_id:
            return None
        accounts = self.get_integration_accounts()
        for account in accounts:
            if account["account_id"] == account_id:
                return account
        return None

    def get_integrations_snapshot(self):
        providers_cur = self.conn.execute(
            """
            SELECT provider, display_name, metadata_json, created_at, updated_at
            FROM integration_providers
            ORDER BY display_name ASC
            """
        )
        providers = []
        for row in providers_cur.fetchall():
            accounts = self.get_integration_accounts(row["provider"])
            providers.append({
                "provider": row["provider"],
                "display_name": row["display_name"],
                "metadata": self._json_loads(row["metadata_json"], {}),
                "accounts": accounts,
                "account_count": len(accounts),
                "connected_count": sum(
                    1
                    for account in accounts
                    if account.get("status") == "connected"
                    and account.get("metadata", {}).get("has_refresh_token")
                ),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return {"providers": providers}

    def save_integration_account(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Integration payload must be a dictionary")

        provider = str(payload.get("provider") or "").strip().lower()
        if not provider:
            raise ValueError("Integration provider is required")

        now = time.time()
        account_id = str(payload.get("account_id") or "").strip()
        if not account_id or account_id.startswith("draft-"):
            account_id = f"acc_{uuid.uuid4().hex[:12]}"

        existing_row = self.conn.execute(
            """
            SELECT status, granted_scopes_json, metadata_json
            FROM integration_accounts
            WHERE account_id = ?
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        existing_metadata = self._json_loads(existing_row["metadata_json"], {}) if existing_row else {}
        existing_granted_scopes = (
            self._normalize_string_list(self._json_loads(existing_row["granted_scopes_json"], []))
            if existing_row else []
        )
        existing_status = str(existing_row["status"]) if existing_row and existing_row["status"] else "needs_auth"

        display_name = str(payload.get("display_name") or "").strip()
        email = str(payload.get("email") or "").strip()
        account_label = str(payload.get("account_label") or "").strip()
        if not account_label:
            account_label = display_name or email or f"{provider.title()} account"

        platform_key = str(payload.get("platform_key") or "default").strip() or "default"
        if "status" in payload:
            status = str(payload.get("status") or existing_status).strip() or existing_status
        else:
            status = existing_status
        metadata_patch = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = {**existing_metadata, **metadata_patch}

        tools = []
        tool_ids = []
        seen_tool_ids = set()
        for raw_tool in payload.get("tools") or []:
            if not isinstance(raw_tool, dict):
                continue
            tool_id = str(raw_tool.get("tool_id") or raw_tool.get("id") or "").strip()
            if not tool_id or tool_id in seen_tool_ids:
                continue
            seen_tool_ids.add(tool_id)

            tool_name = str(raw_tool.get("tool_name") or raw_tool.get("label") or tool_id).strip() or tool_id
            tool_platform_key = str(raw_tool.get("platform_key") or platform_key).strip() or platform_key
            scopes = self._normalize_string_list(raw_tool.get("scopes") or [])
            config = raw_tool.get("config") if isinstance(raw_tool.get("config"), dict) else {}

            tools.append({
                "tool_id": tool_id,
                "tool_name": tool_name,
                "platform_key": tool_platform_key,
                "scopes": scopes,
                "config": config,
            })
            tool_ids.append(tool_id)

        if "granted_scopes" in payload:
            granted_scopes = self._normalize_string_list(payload.get("granted_scopes") or [])
        else:
            granted_scopes = existing_granted_scopes

        existing_primary = self.conn.execute(
            """
            SELECT account_id FROM integration_accounts
            WHERE provider = ? AND is_primary = 1
            LIMIT 1
            """,
            (provider,),
        ).fetchone()
        is_primary = bool(payload.get("is_primary"))
        if existing_primary is None:
            is_primary = True

        self.conn.execute(
            """
            INSERT INTO integration_providers (provider, display_name, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                str(payload.get("provider_display_name") or provider.title()),
                self._json_dumps({}, {}),
                now,
                now,
            ),
        )

        if is_primary:
            self.conn.execute(
                "UPDATE integration_accounts SET is_primary = 0 WHERE provider = ?",
                (provider,),
            )

        self.conn.execute(
            """
            INSERT INTO integration_accounts (
                account_id, provider, platform_key, email, display_name, account_label,
                status, is_primary, granted_scopes_json, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                provider = excluded.provider,
                platform_key = excluded.platform_key,
                email = excluded.email,
                display_name = excluded.display_name,
                account_label = excluded.account_label,
                status = excluded.status,
                is_primary = excluded.is_primary,
                granted_scopes_json = excluded.granted_scopes_json,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                account_id,
                provider,
                platform_key,
                email,
                display_name,
                account_label,
                status,
                1 if is_primary else 0,
                self._json_dumps(granted_scopes, []),
                self._json_dumps(metadata, {}),
                now,
                now,
            ),
        )

        if tool_ids:
            placeholders = ",".join("?" for _ in tool_ids)
            self.conn.execute(
                f"DELETE FROM integration_account_tools WHERE account_id = ? AND tool_id NOT IN ({placeholders})",
                [account_id, *tool_ids],
            )
        else:
            self.conn.execute(
                "DELETE FROM integration_account_tools WHERE account_id = ?",
                (account_id,),
            )

        for tool in tools:
            self.conn.execute(
                """
                INSERT INTO integration_account_tools (
                    account_id, tool_id, tool_name, platform_key, scopes_json, config_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, tool_id) DO UPDATE SET
                    tool_name = excluded.tool_name,
                    platform_key = excluded.platform_key,
                    scopes_json = excluded.scopes_json,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    tool["tool_id"],
                    tool["tool_name"],
                    tool["platform_key"],
                    self._json_dumps(tool["scopes"], []),
                    self._json_dumps(tool["config"], {}),
                    now,
                    now,
                ),
            )

        self.conn.commit()
        return self.get_integration_account(account_id)

    def get_integration_credentials(self, account_id):
        account_id = str(account_id).strip()
        if not account_id:
            return {}
        cur = self.conn.execute(
            """
            SELECT encrypted_access_token, encrypted_refresh_token, access_token_expires_at, updated_at
            FROM integration_account_credentials
            WHERE account_id = ?
            LIMIT 1
            """,
            (account_id,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "access_token": self._decrypt(row["encrypted_access_token"]),
            "refresh_token": self._decrypt(row["encrypted_refresh_token"]),
            "access_token_expires_at": row["access_token_expires_at"],
            "updated_at": row["updated_at"],
        }

    def set_integration_credentials(self, account_id, access_token=None, refresh_token=None, access_token_expires_at=None):
        account_id = str(account_id).strip()
        if not account_id:
            raise ValueError("Account id is required")
        now = time.time()
        existing = self.get_integration_credentials(account_id)
        access_token_value = access_token if access_token is not None else existing.get("access_token")
        refresh_token_value = refresh_token if refresh_token is not None else existing.get("refresh_token")
        expires_value = (
            float(access_token_expires_at)
            if access_token_expires_at is not None else existing.get("access_token_expires_at")
        )
        self.conn.execute(
            """
            INSERT INTO integration_account_credentials (
                account_id, encrypted_access_token, encrypted_refresh_token, access_token_expires_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                encrypted_access_token = excluded.encrypted_access_token,
                encrypted_refresh_token = excluded.encrypted_refresh_token,
                access_token_expires_at = excluded.access_token_expires_at,
                updated_at = excluded.updated_at
            """,
            (
                account_id,
                self._encrypt(access_token_value),
                self._encrypt(refresh_token_value),
                expires_value,
                now,
            ),
        )
        self.conn.commit()

    def clear_integration_credentials(self, account_id):
        account_id = str(account_id).strip()
        if not account_id:
            return
        self.conn.execute("DELETE FROM integration_account_credentials WHERE account_id = ?", (account_id,))
        self.conn.commit()

    def update_integration_account_auth(self, account_id, *, status=None, granted_scopes=None, email=None,
                                        display_name=None, account_label=None, metadata_patch=None):
        account_id = str(account_id).strip()
        if not account_id:
            raise ValueError("Account id is required")

        row = self.conn.execute(
            """
            SELECT email, display_name, account_label, status, granted_scopes_json, metadata_json
            FROM integration_accounts
            WHERE account_id = ?
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Integration account not found: {account_id}")

        merged_metadata = self._json_loads(row["metadata_json"], {})
        if isinstance(metadata_patch, dict):
            merged_metadata.update(metadata_patch)

        self.conn.execute(
            """
            UPDATE integration_accounts
            SET email = ?, display_name = ?, account_label = ?, status = ?, granted_scopes_json = ?,
                metadata_json = ?, updated_at = ?
            WHERE account_id = ?
            """,
            (
                str(email if email is not None else row["email"] or "").strip(),
                str(display_name if display_name is not None else row["display_name"] or "").strip(),
                str(account_label if account_label is not None else row["account_label"] or "").strip(),
                str(status if status is not None else row["status"] or "needs_auth").strip() or "needs_auth",
                self._json_dumps(
                    self._normalize_string_list(granted_scopes if granted_scopes is not None else self._json_loads(row["granted_scopes_json"], [])),
                    [],
                ),
                self._json_dumps(merged_metadata, {}),
                time.time(),
                account_id,
            ),
        )
        self.conn.commit()
        return self.get_integration_account(account_id)

    def delete_integration_account(self, account_id):
        account_id = str(account_id).strip()
        if not account_id:
            return

        cur = self.conn.execute(
            "SELECT provider, is_primary FROM integration_accounts WHERE account_id = ?",
            (account_id,),
        )
        row = cur.fetchone()

        self.conn.execute("DELETE FROM integration_account_tools WHERE account_id = ?", (account_id,))
        self.conn.execute("DELETE FROM integration_account_credentials WHERE account_id = ?", (account_id,))
        self.conn.execute("DELETE FROM integration_accounts WHERE account_id = ?", (account_id,))

        if row and row["is_primary"]:
            fallback = self.conn.execute(
                """
                SELECT account_id
                FROM integration_accounts
                WHERE provider = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (row["provider"],),
            ).fetchone()
            if fallback:
                self.conn.execute(
                    "UPDATE integration_accounts SET is_primary = 1 WHERE account_id = ?",
                    (fallback["account_id"],),
                )

        self.conn.commit()

    def cosmic_mail_is_baseline_done(self, mailbox_id: str) -> bool:
        mailbox_id = str(mailbox_id or "").strip()
        if not mailbox_id:
            return False
        row = self.conn.execute(
            "SELECT baseline_done FROM cosmic_mail_mailbox_poll WHERE mailbox_id = ?",
            (mailbox_id,),
        ).fetchone()
        return bool(row and int(row["baseline_done"] or 0) == 1)

    def cosmic_mail_set_baseline_done(self, mailbox_id: str) -> None:
        mailbox_id = str(mailbox_id or "").strip()
        if not mailbox_id:
            return
        self.conn.execute(
            """
            INSERT INTO cosmic_mail_mailbox_poll (mailbox_id, baseline_done, updated_at)
            VALUES (?, 1, strftime('%s','now'))
            ON CONFLICT(mailbox_id) DO UPDATE SET
                baseline_done = 1,
                updated_at = excluded.updated_at
            """,
            (mailbox_id,),
        )
        self.conn.commit()

    def cosmic_mail_seed_inbound_seen(self, mailbox_id: str, message_ids) -> None:
        mailbox_id = str(mailbox_id or "").strip()
        if not mailbox_id:
            return
        for raw in message_ids or []:
            mid = str(raw or "").strip()
            if not mid:
                continue
            self.conn.execute(
                """
                INSERT OR IGNORE INTO cosmic_mail_inbound_seen (mailbox_id, message_id, seen_at)
                VALUES (?, ?, strftime('%s','now'))
                """,
                (mailbox_id, mid),
            )
        self.conn.commit()

    def cosmic_mail_try_mark_inbound_seen(self, mailbox_id: str, message_id: str) -> bool:
        mailbox_id = str(mailbox_id or "").strip()
        message_id = str(message_id or "").strip()
        if not mailbox_id or not message_id:
            return False
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO cosmic_mail_inbound_seen (mailbox_id, message_id, seen_at)
            VALUES (?, ?, strftime('%s','now'))
            """,
            (mailbox_id, message_id),
        )
        self.conn.commit()
        return cur.rowcount > 0


db = Database()
