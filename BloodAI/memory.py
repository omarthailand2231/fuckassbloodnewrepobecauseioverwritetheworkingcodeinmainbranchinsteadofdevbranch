import os
import json
import logging
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from config import CONFIG

log = logging.getLogger("blood.memory")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

def _md_path(guild_id: str, which: int) -> str:
    name = "memory.md" if which == 1 else "memory_2.md" if which == 2 else "actions.md"
    return os.path.join(CONFIG["memory_dir"], guild_id, name)

def _user_md_path(guild_id: str, user_id: str) -> str:
    return os.path.join(CONFIG["memory_dir"], guild_id, "users", f"{user_id}.md")

def _xml_path(guild_id: str) -> str:
    return os.path.join(CONFIG["memory_dir"], guild_id, "users.xml")


class MemoryManager:
    def __init__(self):
        self.dir = CONFIG["memory_dir"]
        self.retention_days = CONFIG["memory_days"]
        self._convo_history: dict[str, list[dict]] = {}
        # Structured ledger cache: {channel_id: {"last_content": str, "count": int, "user_id": str}}
        self._ledger_cache: dict[str, dict] = {}
        self.file_cache: dict[str, str] = {}
        os.makedirs(self.dir, exist_ok=True)

    def _guild_dir(self, guild_id: str) -> str:
        p = os.path.join(self.dir, str(guild_id))
        os.makedirs(p, exist_ok=True)
        return p

    def _users_dir(self, guild_id: str) -> str:
        p = os.path.join(self.dir, str(guild_id), "users")
        os.makedirs(p, exist_ok=True)
        return p

    def _channels_dir(self, guild_id: str) -> str:
        p = os.path.join(self.dir, str(guild_id), "channels")
        os.makedirs(p, exist_ok=True)
        return p

    def _channel_md_path(self, guild_id: str, channel_id: str) -> str:
        return os.path.join(self._channels_dir(guild_id), f"{channel_id}.md")

    # ── Short-term conversation history (RAM) ─────────────────────────────────

    def push_message(self, channel_id: str, role: str, content):
        hist = self._convo_history.setdefault(channel_id, [])
        hist.append({"role": role, "content": content})
        if len(hist) > CONFIG["convo_history_cap"]:
            self._convo_history[channel_id] = hist[-CONFIG["convo_history_cap"]:]

    def push_raw(self, channel_id: str, raw: dict):
        hist = self._convo_history.setdefault(channel_id, [])
        hist.append(raw)
        if len(hist) > CONFIG["convo_history_cap"]:
            self._convo_history[channel_id] = hist[-CONFIG["convo_history_cap"]:]

    def pop_last(self, channel_id: str) -> dict | None:
        """Remove and return the last entry in history for this channel.
        Used to evict a poisoned assistant turn before retrying — prevents
        leaked reasoning from contaminating the next iteration's context.
        Returns None if history is empty."""
        hist = self._convo_history.get(channel_id)
        if hist:
            return hist.pop()
        return None

    def get_history(self, channel_id: str) -> list[dict]:
        return list(self._convo_history.get(channel_id, []))

    def clear_history(self, channel_id: str):
        self._convo_history.pop(channel_id, None)

    # ── Per-channel rolling chat logs ──────────────────────────────────────────

    def log_message(self, guild_id: str, username: str, user_id: str, channel: str,
                    content: str, channel_id: str | None = None):
        """Unified structured logger with deduplication."""
        ch_id = channel_id or "unknown"
        cache = self._ledger_cache.get(ch_id)
        
        # ── Deduplication logic ──────────────────────────────────────────
        if cache and cache["last_content"] == content and cache["user_id"] == user_id:
            cache["count"] += 1
            return # Don't log yet, wait for count to finish
            
        # ── Flush previous duplicate if it existed ───────────────────────
        if cache and cache["count"] > 1:
            self._write_to_ledger(guild_id, cache["username"], cache["user_id"], 
                                  cache["channel"], f"[Repeated {cache['count']}x] {cache['last_content']}", 
                                  cache["ch_id"])

        # ── Log the current unique message ───────────────────────────────
        self._write_to_ledger(guild_id, username, user_id, channel, content, ch_id)
        
        # ── Update cache ─────────────────────────────────────────────────
        self._ledger_cache[ch_id] = {
            "last_content": content,
            "count": 1,
            "user_id": user_id,
            "username": username,
            "channel": channel,
            "ch_id": ch_id
        }

    def _write_to_ledger(self, guild_id: str, username: str, user_id: str, 
                         channel: str, content: str, ch_id: str):
        self._guild_dir(guild_id)
        # Global log (memory.md)
        global_path = _md_path(guild_id, 1)
        line = f"[{_now_str()}] [#{channel} ({ch_id})] {username} ({user_id}): {content}\n"
        with open(global_path, "a", encoding="utf-8") as f:
            f.write(line)
        # Increased global ledger to ~5 MB (80k lines)
        self._trim_md(global_path, CONFIG["trim_global_ledger"])

        # Per-channel log (channels/{cid}.md)
        if ch_id and ch_id != "unknown":
            ch_path = self._channel_md_path(guild_id, ch_id)
            ch_line = f"[{_now_str()}] {username}: {content}\n"
            with open(ch_path, "a", encoding="utf-8") as f:
                f.write(ch_line)
            self._trim_md(ch_path, CONFIG["trim_channel_ledger"])

    def _trim_md(self, path: str, keep: int):
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > keep:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-keep:])
        except FileNotFoundError:
            pass

    def read_channel_log(self, guild_id: str, channel_id: str,
                         last_n: int = 200) -> str:
        path = self._channel_md_path(guild_id, channel_id)
        if not os.path.exists(path):
            return f"(no log for channel {channel_id})"
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-last_n:]).strip() or f"(no log for channel {channel_id})"

    def read_memory_md(self, guild_id: str, last_n: int = 200) -> str:
        """Legacy: read old global memory.md if it exists."""
        path = _md_path(guild_id, 1)
        if not os.path.exists(path):
            return "(no chat log yet)"
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-last_n:]).strip() or "(no chat log yet)"

    # ── Per-user log files ────────────────────────────────────────────────────

    def log_user_message(self, guild_id: str, user_id: str, username: str,
                         channel: str, content: str):
        self._users_dir(guild_id)
        path = _user_md_path(guild_id, user_id)
        line = f"[{_now_str()}] #{channel}: {content[:300]}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        self._trim_md(path, CONFIG["trim_user_log"])

    def read_channel_md(self, guild_id: str, ch_id: str, last_n: int = 100) -> str:
        path = self._channel_md_path(guild_id, ch_id)
        if not os.path.exists(path):
            return f"(no log for channel {ch_id})"
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # Return oldest to newest (chronological) history
            return "".join(lines[-last_n:])
        except Exception: return f"(error reading log for channel {ch_id})"

    def read_user_md(self, guild_id: str, user_id: str, last_n: int = 50) -> str:
        path = _user_md_path(guild_id, user_id)
        if not os.path.exists(path):
            return f"(no log for user {user_id})"
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-last_n:]).strip() or f"(no log for user {user_id})"

    def search_user_md(self, guild_id: str, user_id: str, keyword: str, limit: int = 100) -> list[str]:
        path = _user_md_path(guild_id, user_id)
        if not os.path.exists(path):
            return []
        results = []
        search_words = keyword.lower().split()
        if not search_words:
            return []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line_l = line.lower()
                if all(w in line_l for w in search_words):
                    results.append(line.strip())
        return results[:limit]

    def get_interaction_partners(self, guild_id: str, user_id: str) -> list[str]:
        root = self._load_xml(guild_id)
        el = root.find("users").find(f"user[@id='{user_id}']")
        if el is None:
            return []
        ix = el.find("interactions")
        if ix is None:
            return []
        partners = [(w.get("user_id"), int(w.get("count", "0"))) for w in ix]
        partners.sort(key=lambda x: x[1], reverse=True)
        return [uid for uid, _ in partners]

    # ── memory_2.md — summaries ───────────────────────────────────────────────

    def read_summary_md(self, guild_id: str) -> str:
        path = _md_path(guild_id, 2)
        if not os.path.exists(path):
            return "(no summaries yet)"
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip() or "(no summaries yet)"

    def append_summary(self, guild_id: str, text: str):
        self._guild_dir(guild_id)
        path = _md_path(guild_id, 2)
        entry = f"\n[{_now_str()}] {text.strip()}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
        self.vector_add(guild_id, entry.strip(), {"source": "summary"})

    # ── Per-server personality config ──────────────────────────────────────────

    def _server_config_path(self, guild_id: str) -> str:
        return os.path.join(self._guild_dir(guild_id), "server_config.json")

    def get_server_config(self, guild_id: str) -> dict:
        path = self._server_config_path(guild_id)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"custom_prompt": "", "features": {}}

    def set_server_config(self, guild_id: str, config: dict):
        path = self._server_config_path(guild_id)
        with open(path, "w") as f:
            json.dump(config, f, indent=2)

    # ── Coins ────────────────────────────────────────────────────────────────────

    def _coins_path(self, guild_id: str) -> str:
        return os.path.join(self._guild_dir(guild_id), "coins.json")

    def _load_coins(self, guild_id: str) -> dict:
        path = self._coins_path(guild_id)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_coins(self, guild_id: str, data: dict):
        path = self._coins_path(guild_id)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def add_coins(self, guild_id: str, user_id: str, amount: int) -> int:
        """Add coins to a user. Returns new balance."""
        data = self._load_coins(guild_id)
        data[user_id] = data.get(user_id, 0) + amount
        self._save_coins(guild_id, data)
        return data[user_id]

    def get_coins(self, guild_id: str, user_id: str) -> int:
        data = self._load_coins(guild_id)
        return data.get(user_id, 0)

    def get_coin_leaderboard(self, guild_id: str, top_n: int = 10) -> list[tuple[str, int]]:
        """Return top N users by coins as [(user_id, balance), ...]."""
        data = self._load_coins(guild_id)
        sorted_users = sorted(data.items(), key=lambda x: x[1], reverse=True)
        return sorted_users[:top_n]

    # ── Market Portfolio ──────────────────────────────────────────────────────

    def _market_path(self, guild_id: str) -> str:
        return os.path.join(self._guild_dir(guild_id), "market.json")

    def _load_market(self, guild_id: str) -> dict:
        path = self._market_path(guild_id)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_market(self, guild_id: str, data: dict):
        path = self._market_path(guild_id)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def market_buy(self, guild_id: str, user_id: str, ticker: str,
                   coins: int, price: float) -> dict:
        """Buy into a ticker. Returns the position."""
        shares = coins / price
        data = self._load_market(guild_id)
        user_data = data.setdefault(user_id, {})
        if ticker in user_data:
            # Average into existing position
            pos = user_data[ticker]
            total_coins = pos["coins_invested"] + coins
            total_shares = pos["shares"] + shares
            pos["avg_price"] = total_coins / total_shares if total_shares else price
            pos["coins_invested"] = total_coins
            pos["shares"] = total_shares
        else:
            user_data[ticker] = {
                "coins_invested": coins,
                "shares": shares,
                "avg_price": price,
            }
        data[user_id] = user_data
        self._save_market(guild_id, data)
        return user_data[ticker]

    def market_sell(self, guild_id: str, user_id: str, ticker: str,
                    current_price: float, sell_coins: int | None = None) -> dict | None:
        """Sell a position. Returns {coins_returned, profit, sold_shares} or None."""
        data = self._load_market(guild_id)
        user_data = data.get(user_id, {})
        pos = user_data.get(ticker)
        if not pos or pos["shares"] <= 0:
            return None
        total_value = pos["shares"] * current_price
        total_coins_value = round(total_value)

        if sell_coins is None or sell_coins >= total_coins_value:
            # Sell all
            coins_returned = total_coins_value
            profit = coins_returned - pos["coins_invested"]
            sold_shares = pos["shares"]
            del user_data[ticker]
        else:
            # Partial sell
            fraction = sell_coins / total_coins_value
            sold_shares = pos["shares"] * fraction
            coins_returned = sell_coins
            profit = coins_returned - round(pos["coins_invested"] * fraction)
            pos["shares"] -= sold_shares
            pos["coins_invested"] -= round(pos["coins_invested"] * fraction)

        data[user_id] = user_data
        self._save_market(guild_id, data)
        return {
            "coins_returned": coins_returned,
            "profit": profit,
            "sold_shares": round(sold_shares, 6),
        }

    def get_portfolio(self, guild_id: str, user_id: str) -> dict:
        """Return {ticker: {coins_invested, shares, avg_price}, ...}."""
        data = self._load_market(guild_id)
        return data.get(user_id, {})

    # ── memory_3.md — actions ─────────────────────────────────────────────────

    def append_action_log(self, guild_id: str, text: str):
        self._guild_dir(guild_id)
        path = _md_path(guild_id, 3)
        entry = f"[{_now_str()}] {text.strip()}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

    # ── search ────────────────────────────────────────────────────────────────

    def search_memory(self, guild_id: str, keyword: str, limit: int = 100,
                      user_id: str | None = None,
                      channel_id: str | None = None) -> str:
        """Search memory.  If channel_id is given, only that channel's log is
        searched.  Otherwise searches ALL channel logs + legacy memory.md +
        summaries (global)."""
        search_words = keyword.lower().split()
        results = []

        if not search_words:
            return "Nothing found — empty search."

        if channel_id:
            # ── Targeted channel search ──────────────────────────────────
            path = self._channel_md_path(guild_id, channel_id)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line_l = line.lower()
                        if all(w in line_l for w in search_words):
                            results.append(f"[ch:{channel_id}] {line.strip()[:220]}")
        else:
            # ── Global: all channel files ─────────────────────────────────
            channels_dir = os.path.join(self.dir, str(guild_id), "channels")
            if os.path.isdir(channels_dir):
                for fname in os.listdir(channels_dir):
                    if not fname.endswith(".md"):
                        continue
                    ch_id = fname[:-3]
                    fpath = os.path.join(channels_dir, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        for line in f:
                            line_l = line.lower()
                            if all(w in line_l for w in search_words):
                                results.append(f"[ch:{ch_id}] {line.strip()[:220]}")

            # ── Legacy memory.md (historical) ────────────────────────────
            legacy_path = _md_path(guild_id, 1)
            if os.path.exists(legacy_path):
                with open(legacy_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line_l = line.lower()
                        if all(w in line_l for w in search_words):
                            results.append(f"[log] {line.strip()[:220]}")

        # ── Summaries (always searched) ──────────────────────────────────
        summary_path = _md_path(guild_id, 2)
        if os.path.exists(summary_path):
            with open(summary_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_l = line.lower()
                    if all(w in line_l for w in search_words):
                        results.append(f"[summary] {line.strip()[:220]}")

        # ── Actions (always searched) ────────────────────────────────────
        actions_path = _md_path(guild_id, 3)
        if os.path.exists(actions_path):
            with open(actions_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_l = line.lower()
                    if all(w in line_l for w in search_words):
                        results.append(f"[action_log] {line.strip()[:220]}")

        # ── Per-user logs ────────────────────────────────────────────────
        if user_id:
            hits = self.search_user_md(guild_id, user_id, keyword, limit=limit)
            for h in hits:
                results.append(f"[user:{user_id}] {h[:220]}")

            partners = self.get_interaction_partners(guild_id, user_id)[:3]
            for partner_id in partners:
                phits = self.search_user_md(guild_id, partner_id, keyword, limit=5)
                for h in phits:
                    results.append(f"[linked:{partner_id}] {h[:220]}")

        if not results:
            return f"Nothing found for '{keyword}'."

        seen = set()
        deduped = []
        for r in reversed(results):
            if r not in seen:
                seen.add(r)
                deduped.append(r)
                if len(deduped) >= limit:
                    break
        return "\n".join(deduped)

    # ── XML user store ────────────────────────────────────────────────────────

    def _load_xml(self, guild_id: str) -> ET.Element:
        path = _xml_path(guild_id)
        self._guild_dir(guild_id)
        if os.path.exists(path):
            return ET.parse(path).getroot()
        root = ET.Element("guild", id=str(guild_id))
        ET.SubElement(root, "users")
        return root

    def _save_xml(self, guild_id: str, root: ET.Element):
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(_xml_path(guild_id), encoding="unicode", xml_declaration=False)

    def _upsert_user(self, users_el: ET.Element, user_id: str, username: str) -> ET.Element:
        el = users_el.find(f"user[@id='{user_id}']")
        if el is None:
            el = ET.SubElement(users_el, "user", id=user_id, username=username,
                               first_seen=_now_iso(), last_seen=_now_iso(), total_tokens="0")
            ET.SubElement(el, "interactions")
        else:
            el.set("last_seen", _now_iso())
            if username:
                el.set("username", username)
        return el

    def store_message(self, guild_id: str, user_id: str, username: str,
                      content: str, channel: str,
                      channel_id: str | None = None):
        self.log_message(guild_id, username, user_id, channel, content, channel_id=channel_id)
        self.log_user_message(guild_id, user_id, username, channel, content)
        root = self._load_xml(guild_id)
        self._upsert_user(root.find("users"), user_id, username)
        self._save_xml(guild_id, root)
        # Index into vector memory for semantic search
        if len(content) > 15:
            line = f"[{_now_str()}] #{channel} {username}: {content[:300]}"
            self.vector_add(guild_id, line, {
                "source": "channel", "channel_id": channel_id or "unknown",
                "user_id": user_id, "username": username,
            })

    def record_interaction(self, guild_id: str,
                           user_a_id: str, user_a_name: str,
                           user_b_id: str, user_b_name: str):
        root = self._load_xml(guild_id)
        users_el = root.find("users")
        for uid, uname, other_id in [
            (user_a_id, user_a_name, user_b_id),
            (user_b_id, user_b_name, user_a_id),
        ]:
            user_el = self._upsert_user(users_el, uid, uname)
            ix = user_el.find("interactions")
            w = ix.find(f"with[@user_id='{other_id}']")
            if w is None:
                ET.SubElement(ix, "with", user_id=other_id, count="1", last=_now_iso())
            else:
                w.set("count", str(int(w.get("count", "0")) + 1))
                w.set("last", _now_iso())
        self._save_xml(guild_id, root)

    def add_token_usage(self, guild_id: str, user_id: str, tokens: int):
        if tokens <= 0: return
        root = self._load_xml(guild_id)
        user_el = self._upsert_user(root.find("users"), user_id, "")
        tt = int(user_el.get("total_tokens", "0"))
        user_el.set("total_tokens", str(tt + tokens))
        self._save_xml(guild_id, root)

    def get_token_leaderboard(self, guild_id: str, limit: int = 5) -> list[tuple[str, int]]:
        root = self._load_xml(guild_id)
        users = []
        for u in root.find("users").findall("user"):
            tokens = int(u.get("total_tokens", "0"))
            if tokens > 0:
                users.append((u.get("username", "Unknown"), tokens))
        users.sort(key=lambda x: x[1], reverse=True)
        return users[:limit]

    def get_timeout_leaderboard(self, guild_id: str, limit: int = 5) -> list[tuple[str, int]]:
        path = _md_path(guild_id, 3) # actions.md
        if not os.path.exists(path):
            return []
        
        counts = {}
        import re
        # Pattern: [YYYY-MM-DD HH:MM] I timed out 'Username' (ID) ...
        # OR: [YYYY-MM-DD HH:MM] I banned 'Username' (ID) ...
        pattern = re.compile(r"I (timed out|banned|kicked) '([^']+)'")
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    match = pattern.search(line)
                    if match:
                        user = match.group(2)
                        counts[user] = counts.get(user, 0) + 1
        except Exception:
            pass
        
        sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return sorted_counts[:limit]

    def get_user_history(self, guild_id: str, user_id: str) -> str:
        root = self._load_xml(guild_id)
        el = root.find("users").find(f"user[@id='{user_id}']")
        if el is None:
            return f"No record for user {user_id}."
        username = el.get("username", "?")
        first = el.get("first_seen", "?")[:10]
        last = el.get("last_seen", "?")[:10]
        ix = el.find("interactions")
        ix_lines = [f"  with {w.get('user_id')} — {w.get('count')}x, last {w.get('last','')[:10]}"
                    for w in (ix or [])]
        recent = self.read_user_md(guild_id, user_id, last_n=15)
        result = f"=== {username} ({user_id}) ===\nFirst seen: {first} | Last: {last}\n"
        if ix_lines:
            result += "Interactions:\n" + "\n".join(ix_lines) + "\n"
        result += f"Recent messages:\n{recent}"
        return result

    def get_recent_context(self, guild_id: str, limit: int = 15) -> str:
        return self.read_memory_md(guild_id, last_n=limit)

    def cleanup_old_entries(self):
        for guild_id in os.listdir(self.dir):
            gdir = os.path.join(self.dir, guild_id)
            if not os.path.isdir(gdir):
                continue
            # Legacy memory.md
            path = _md_path(guild_id, 1)
            if os.path.exists(path):
                self._trim_md(path, CONFIG["cleanup_global"])
            # Summaries (memory_2.md) — trim to prevent unbounded growth
            path_summary = _md_path(guild_id, 2)
            if os.path.exists(path_summary):
                self._trim_md(path_summary, CONFIG.get("cleanup_summaries", 500))
            # Action logs
            path_actions = _md_path(guild_id, 3)
            if os.path.exists(path_actions):
                self._trim_md(path_actions, CONFIG["cleanup_actions"])
            # Per-channel logs
            chdir = os.path.join(gdir, "channels")
            if os.path.isdir(chdir):
                for fname in os.listdir(chdir):
                    if fname.endswith(".md"):
                        self._trim_md(os.path.join(chdir, fname), CONFIG["cleanup_channels"])
            # Per-user logs
            udir = os.path.join(gdir, "users")
            if os.path.isdir(udir):
                for fname in os.listdir(udir):
                    if fname.endswith(".md"):
                        self._trim_md(os.path.join(udir, fname), CONFIG["cleanup_users"])
        log.info("Memory cleanup done.")

    # ══════════════════════════════════════════════════════════════════════════
    # EMOTIONAL STATE — per-user + global bot mood
    # ══════════════════════════════════════════════════════════════════════════

    def _emotional_state_path(self, guild_id: str) -> str:
        return os.path.join(self._guild_dir(guild_id), "emotional_state.json")

    def _load_emotional_state(self, guild_id: str) -> dict:
        path = self._emotional_state_path(guild_id)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"users": {}, "bot_mood": "neutral", "mood_history": []}

    def _save_emotional_state(self, guild_id: str, data: dict):
        path = self._emotional_state_path(guild_id)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def get_user_emotional_data(self, guild_id: str, user_id: str) -> dict:
        """Return emotional data for a specific user."""
        state = self._load_emotional_state(guild_id)
        return state["users"].get(user_id, {
            "annoyance": 0, "respect": 0, "grudge": 0, "fear_level": 0,
            "category": "neutral", "notes": ""
        })

    def update_user_emotional_data(self, guild_id: str, user_id: str, deltas: dict):
        """Apply delta changes to a user's emotional data.
        deltas: {"annoyance": +2, "respect": -1, ...} or absolute sets like {"category": "feared"}
        """
        state = self._load_emotional_state(guild_id)
        user_data = state["users"].get(user_id, {
            "annoyance": 0, "respect": 0, "grudge": 0, "fear_level": 0,
            "category": "neutral", "notes": ""
        })
        for key, val in deltas.items():
            if key in ("annoyance", "respect", "grudge", "fear_level") and isinstance(val, (int, float)):
                user_data[key] = max(-100, min(100, user_data.get(key, 0) + val))
            else:
                user_data[key] = val
        state["users"][user_id] = user_data
        self._save_emotional_state(guild_id, state)

    def get_bot_mood(self, guild_id: str) -> str:
        state = self._load_emotional_state(guild_id)
        return state.get("bot_mood", "neutral")

    def set_bot_mood(self, guild_id: str, mood: str):
        state = self._load_emotional_state(guild_id)
        state["bot_mood"] = mood
        history = state.get("mood_history", [])
        history.append({"mood": mood, "at": _now_iso()})
        state["mood_history"] = history[-50:]  # keep last 50 mood changes
        self._save_emotional_state(guild_id, state)

    def get_emotional_summary(self, guild_id: str, cap: int = 600) -> str:
        """Compact emotional state summary for system prompt injection."""
        state = self._load_emotional_state(guild_id)
        mood = state.get("bot_mood", "neutral")
        lines = [f"MOOD: {mood}"]
        users = state.get("users", {})
        # Group by category
        categories = {}
        for uid, data in users.items():
            cat = data.get("category", "neutral")
            categories.setdefault(cat, []).append((uid, data))
        for cat in ["feared", "respected", "disrespected"]:
            if cat in categories:
                ids = [uid for uid, _ in categories[cat][:5]]
                lines.append(f"{cat.upper()}: {', '.join(ids)}")
        # Top grudges
        grudged = [(uid, d["grudge"]) for uid, d in users.items() if d.get("grudge", 0) > 10]
        grudged.sort(key=lambda x: x[1], reverse=True)
        if grudged:
            lines.append("GRUDGES: " + ", ".join(f"{uid}({g})" for uid, g in grudged[:5]))
        result = "\n".join(lines)
        return result[:cap]

    # ══════════════════════════════════════════════════════════════════════════
    # SCHEDULED TASKS
    # ══════════════════════════════════════════════════════════════════════════

    def _scheduled_tasks_path(self, guild_id: str) -> str:
        return os.path.join(self._guild_dir(guild_id), "scheduled_tasks.json")

    def load_scheduled_tasks(self, guild_id: str) -> list[dict]:
        path = self._scheduled_tasks_path(guild_id)
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def save_scheduled_tasks(self, guild_id: str, tasks: list[dict]):
        path = self._scheduled_tasks_path(guild_id)
        with open(path, "w") as f:
            json.dump(tasks, f, indent=2)

    def add_scheduled_task(self, guild_id: str, task: dict) -> str:
        """Add a scheduled task. Returns 'ok' or error string.
        task: {"action": str, "channel_id": str, "due_at": float (unix ts), "context": str}
        """
        tasks = self.load_scheduled_tasks(guild_id)
        max_pending = CONFIG.get("scheduled_tasks_max_pending", 10)
        if len(tasks) >= max_pending:
            return f"Task limit reached ({max_pending} pending). Complete or cancel existing tasks first."
        task["created_at"] = _now_iso()
        task["id"] = f"task_{int(datetime.now(timezone.utc).timestamp())}_{len(tasks)}"
        tasks.append(task)
        self.save_scheduled_tasks(guild_id, tasks)
        return "ok"

    def pop_due_tasks(self, guild_id: str) -> list[dict]:
        """Remove and return all tasks whose due_at has passed."""
        tasks = self.load_scheduled_tasks(guild_id)
        now = datetime.now(timezone.utc).timestamp()
        due = [t for t in tasks if t.get("due_at", float("inf")) <= now]
        remaining = [t for t in tasks if t.get("due_at", float("inf")) > now]
        if due:
            self.save_scheduled_tasks(guild_id, remaining)
        return due

    # ══════════════════════════════════════════════════════════════════════════
    # VECTOR MEMORY — semantic search via sentence-transformers + ChromaDB
    # ══════════════════════════════════════════════════════════════════════════

    _vector_ready = False
    _chroma_client = None
    _embed_model = None

    def _init_vector(self):
        """Lazy-init: load the embedding model + ChromaDB on first use."""
        if self._vector_ready:
            return True
        try:
            import chromadb
            from chromadb.config import Settings
            from sentence_transformers import SentenceTransformer

            vector_dir = os.path.join(self.dir, "_vectordb")
            os.makedirs(vector_dir, exist_ok=True)

            self.__class__._chroma_client = chromadb.PersistentClient(
                path=vector_dir,
                settings=Settings(anonymized_telemetry=False),
            )
            model_name = CONFIG.get("vector_model", "all-MiniLM-L6-v2")
            self.__class__._embed_model = SentenceTransformer(model_name)
            self.__class__._vector_ready = True
            log.info("Vector memory initialized (model=%s, db=%s)", model_name, vector_dir)
            return True
        except Exception as e:
            log.warning("Vector memory init failed (will use keyword fallback): %s", e)
            return False

    def _get_collection(self, guild_id: str):
        """Get or create a ChromaDB collection for a guild."""
        if not self._vector_ready:
            return None
        name = f"guild_{guild_id}"
        return self._chroma_client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def vector_add(self, guild_id: str, text: str, metadata: dict | None = None):
        """Embed and store a single text chunk."""
        if not self._init_vector():
            return
        text = text.strip()
        if not text or len(text) < 10:
            return
        col = self._get_collection(guild_id)
        if col is None:
            return
        # Deterministic ID to avoid duplicates
        import hashlib
        doc_id = hashlib.sha256(text.encode()).hexdigest()[:24]
        meta = metadata or {}
        meta["timestamp"] = _now_iso()
        # Truncate text for embedding (model max ~256 tokens ≈ 1000 chars)
        embed_text = text[:1000]
        try:
            embedding = self._embed_model.encode(embed_text).tolist()
            col.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[text[:2000]],
                metadatas=[meta],
            )
        except Exception as e:
            log.warning("Vector add failed: %s", e)

    def vector_search(self, guild_id: str, query: str, n_results: int = 15) -> list[dict]:
        """Semantic search. Returns [{text, score, metadata}, ...]."""
        if not self._init_vector():
            return []
        col = self._get_collection(guild_id)
        if col is None:
            return []
        try:
            log.info("\033[36m[VECTOR] Searching: \"%s\" (guild=%s, top=%d)\033[0m", query, guild_id, n_results)
            embedding = self._embed_model.encode(query[:1000]).tolist()
            results = col.query(
                query_embeddings=[embedding],
                n_results=n_results,
                include=["documents", "distances", "metadatas"],
            )
            hits = []
            for i, doc in enumerate(results["documents"][0]):
                distance = results["distances"][0][i]
                score = 1.0 - distance  # cosine distance → similarity
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                hits.append({"text": doc, "score": score, "metadata": meta})
            # Log top hits
            for i, h in enumerate(hits[:5]):
                src = h['metadata'].get('source', '?')
                log.info("\033[33m  #%d [%s %.0f%%] %s\033[0m", i+1, src, h['score']*100, h['text'][:120])
            if len(hits) > 5:
                log.info("\033[33m  ... +%d more hits\033[0m", len(hits)-5)
            if not hits:
                log.info("\033[31m  No vector hits\033[0m")
            return hits
        except Exception as e:
            log.warning("Vector search failed: %s", e)
            return []

    def vector_index_existing(self, guild_id: str) -> int:
        """Re-index all existing .md memory files for a guild. Returns count indexed."""
        if not self._init_vector():
            return 0
        count = 0

        # Channel logs
        channels_dir = os.path.join(self.dir, str(guild_id), "channels")
        if os.path.isdir(channels_dir):
            for fname in os.listdir(channels_dir):
                if not fname.endswith(".md"):
                    continue
                ch_id = fname[:-3]
                fpath = os.path.join(channels_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if len(line) > 15:
                                self.vector_add(guild_id, line, {"source": "channel", "channel_id": ch_id})
                                count += 1
                except Exception:
                    pass

        # User logs
        users_dir = os.path.join(self.dir, str(guild_id), "users")
        if os.path.isdir(users_dir):
            for fname in os.listdir(users_dir):
                if not fname.endswith(".md"):
                    continue
                uid = fname[:-3]
                fpath = os.path.join(users_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if len(line) > 15:
                                self.vector_add(guild_id, line, {"source": "user", "user_id": uid})
                                count += 1
                except Exception:
                    pass

        # Summaries
        summary_path = _md_path(guild_id, 2)
        if os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if len(line) > 15:
                            self.vector_add(guild_id, line, {"source": "summary"})
                            count += 1
            except Exception:
                pass

        # Actions
        actions_path = _md_path(guild_id, 3)
        if os.path.exists(actions_path):
            try:
                with open(actions_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if len(line) > 15:
                            self.vector_add(guild_id, line, {"source": "action"})
                            count += 1
            except Exception:
                pass

        log.info("Vector indexed %d entries for guild %s", count, guild_id)
        return count

    def hybrid_search(self, guild_id: str, query: str, limit: int = 30,
                      channel_id: str | None = None,
                      user_id: str | None = None) -> str:
        """Hybrid search: vector semantic + keyword, merged and deduped."""
        log.info("\033[35m[HYBRID] query=\"%s\" limit=%d ch=%s\033[0m", query, limit, channel_id)
        # 1. Vector results
        vector_hits = self.vector_search(guild_id, query, n_results=limit)
        results = []
        for hit in vector_hits:
            if hit["score"] >= 0.25:  # minimum relevance threshold
                src = hit["metadata"].get("source", "?")
                score_pct = f"{hit['score']:.0%}"
                results.append(f"[{src} {score_pct}] {hit['text'][:220]}")

        # 2. Keyword results (existing logic)
        keyword_results = self.search_memory(guild_id, query, limit=limit,
                                             user_id=user_id, channel_id=channel_id)
        kw_count = len(keyword_results.split("\n")) if keyword_results and keyword_results != f"Nothing found for '{query}'." else 0

        # 3. Merge: vector first (sorted by relevance), then keyword additions
        seen_texts = set()
        merged = []
        for r in results:
            # Extract the text portion for dedup
            text_part = r.split("] ", 1)[-1].strip().lower()[:100]
            if text_part not in seen_texts:
                seen_texts.add(text_part)
                merged.append(r)

        if keyword_results and keyword_results != f"Nothing found for '{query}'.":
            for line in keyword_results.split("\n"):
                text_part = line.split("] ", 1)[-1].strip().lower()[:100] if "] " in line else line.strip().lower()[:100]
                if text_part and text_part not in seen_texts:
                    seen_texts.add(text_part)
                    merged.append(line)

        log.info("\033[35m[HYBRID] Results: %d vector (≥25%%), %d keyword, %d merged (deduped)\033[0m",
                 len(results), kw_count, len(merged))

        if not merged:
            return f"Nothing found for '{query}'."

        return "\n".join(merged[:limit])

    # ══════════════════════════════════════════════════════════════════════════
    # GOALS — persistent per-guild goal system
    # ══════════════════════════════════════════════════════════════════════════

    def _goals_path(self, guild_id: str) -> str:
        return os.path.join(self.dir, guild_id, "goals.json")

    def _load_goals(self, guild_id: str) -> list[dict]:
        path = self._goals_path(guild_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_goals(self, guild_id: str, goals: list[dict]):
        path = self._goals_path(guild_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(goals, f, indent=2, ensure_ascii=False)

    def set_goal(self, guild_id: str, text: str, priority: str = "normal") -> dict:
        goals = self._load_goals(guild_id)
        active = [g for g in goals if g.get("status") == "active"]
        max_goals = CONFIG.get("goals_max_per_guild", 20)
        if len(active) >= max_goals:
            return {"error": f"Max {max_goals} active goals. Complete some first."}
        goal = {
            "id": len(goals) + 1,
            "text": text,
            "status": "active",
            "priority": priority,
            "created_at": _now_iso(),
            "completed_at": None,
            "notes": [],
        }
        goals.append(goal)
        self._save_goals(guild_id, goals)
        return goal

    def complete_goal(self, guild_id: str, goal_id: int, outcome: str = "completed") -> str:
        goals = self._load_goals(guild_id)
        for g in goals:
            if g["id"] == goal_id and g["status"] == "active":
                g["status"] = outcome  # "completed" or "abandoned"
                g["completed_at"] = _now_iso()
                self._save_goals(guild_id, goals)
                return f"Goal #{goal_id} marked as {outcome}."
        return f"Goal #{goal_id} not found or already completed."

    def list_goals(self, guild_id: str, status: str = "active") -> list[dict]:
        goals = self._load_goals(guild_id)
        if status == "all":
            return goals
        return [g for g in goals if g.get("status") == status]

    def get_goals_summary(self, guild_id: str, cap: int = 400) -> str:
        active = self.list_goals(guild_id, "active")
        if not active:
            return ""
        lines = []
        for g in active:
            prio = f" [{g['priority']}]" if g.get("priority", "normal") != "normal" else ""
            lines.append(f"#{g['id']}{prio}: {g['text']}")
        summary = "\n".join(lines)
        return summary[:cap]

    # ══════════════════════════════════════════════════════════════════════════
    # REFLECTIONS — periodic self-review journal
    # ══════════════════════════════════════════════════════════════════════════

    def _reflections_path(self, guild_id: str) -> str:
        return os.path.join(self.dir, guild_id, "reflections.md")

    def save_reflection(self, guild_id: str, content: str):
        path = self._reflections_path(guild_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        entry = f"\n---\n[{_now_str()}]\n{content}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

    def get_latest_reflection(self, guild_id: str, cap: int = 300) -> str:
        path = self._reflections_path(guild_id)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            blocks = text.strip().split("---")
            if blocks:
                latest = blocks[-1].strip()
                return latest[:cap]
        except Exception:
            pass
        return ""

    def get_message_count_since_reflection(self, guild_id: str) -> int:
        """Count total messages across all channels since last reflection."""
        path = self._reflections_path(guild_id)
        if not os.path.exists(path):
            return 9999  # no reflections yet, trigger one
        try:
            mtime = os.path.getmtime(path)
            count = 0
            guild_dir = os.path.join(self.dir, guild_id)
            if os.path.isdir(guild_dir):
                for fname in os.listdir(guild_dir):
                    if fname.startswith("ch_") and fname.endswith(".md"):
                        ch_path = os.path.join(guild_dir, fname)
                        try:
                            with open(ch_path, "r", encoding="utf-8") as f:
                                for line in f:
                                    # Each line with timestamp after mtime counts
                                    count += 1
                        except Exception:
                            pass
            return count
        except Exception:
            return 0