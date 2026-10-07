"""Sincroniza dados da API oficial do WHOOP para um banco SQLite local (whoop.db).

Uso:
    python whoop_sync.py auth    # primeira vez: abre o navegador para login
    python whoop_sync.py         # sincroniza (incremental)
    python whoop_sync.py full    # baixa todo o historico de novo
"""
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_DIR = Path(os.environ.get("WHOOP_STATE_DIR", BASE_DIR))
TOKEN_FILE = STATE_DIR / "tokens.json"
DB_FILE = STATE_DIR / "whoop.db"

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API = "https://api.prod.whoop.com/developer"
UA = {"User-Agent": "whoop-dashboard/1.0"}
SCOPES = "offline read:recovery read:cycles read:workout read:sleep read:profile read:body_measurement"

# tabela -> (endpoint, campo usado como chave primaria)
RESOURCES = {
    "cycles": ("/v2/cycle", "id"),
    "recoveries": ("/v2/recovery", "cycle_id"),
    "sleeps": ("/v2/activity/sleep", "id"),
    "workouts": ("/v2/activity/workout", "id"),
}


def load_config():
    # no GitHub Actions as credenciais vem de variaveis de ambiente (secrets)
    if os.environ.get("WHOOP_CLIENT_ID"):
        return {
            "client_id": os.environ["WHOOP_CLIENT_ID"],
            "client_secret": os.environ["WHOOP_CLIENT_SECRET"],
            "redirect_uri": os.environ.get("WHOOP_REDIRECT_URI", "http://localhost:8080/callback"),
        }
    if not CONFIG_FILE.exists():
        sys.exit("Crie o config.json a partir do config.example.json com seu client_id e client_secret.")
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", **UA})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def save_tokens(tok):
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    TOKEN_FILE.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    return tok


def authorize(cfg):
    redirect = cfg["redirect_uri"]
    parsed = urllib.parse.urlparse(redirect)
    state = secrets.token_hex(8)
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Pronto! Pode fechar esta aba e voltar ao terminal.</h2>".encode())

        def log_message(self, *args):
            pass

    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": state,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("Abrindo o navegador para login no WHOOP...\nSe nao abrir, acesse:\n" + url)
    webbrowser.open(url)

    server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    while "code" not in result and "error" not in result:
        server.handle_request()

    if "error" in result:
        sys.exit(f"Erro na autorizacao: {result}")
    if result.get("state") != state:
        sys.exit("State invalido, tente de novo.")

    tok = post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": result["code"],
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": redirect,
    })
    save_tokens(tok)
    print("Autorizado com sucesso! Tokens salvos em tokens.json")


def access_token(cfg):
    if not TOKEN_FILE.exists():
        sys.exit("Rode primeiro: python whoop_sync.py auth")
    tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    new = post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "scope": "offline",
    })
    new.setdefault("refresh_token", tok["refresh_token"])
    return save_tokens(new)["access_token"]


def api_get(token, path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", **UA})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt * 5)
                continue
            raise
    raise RuntimeError("Limite de requisicoes excedido")


def fetch_all(token, path, start=None):
    params = {"limit": 25}
    if start:
        params["start"] = start
    while True:
        page = api_get(token, path, params)
        yield from page.get("records", [])
        nxt = page.get("next_token")
        if not nxt:
            break
        params["nextToken"] = nxt


def init_db(con):
    for table, (_, key) in RESOURCES.items():
        con.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            {key} TEXT PRIMARY KEY,
            start TEXT,
            updated_at TEXT,
            data TEXT NOT NULL)""")
    con.execute("CREATE TABLE IF NOT EXISTS profile (k TEXT PRIMARY KEY, data TEXT NOT NULL)")
    con.executescript("""
    CREATE VIEW IF NOT EXISTS v_daily AS
    SELECT
        date(json_extract(c.data, '$.start')) AS dia,
        json_extract(r.data, '$.score.recovery_score') AS recovery,
        json_extract(r.data, '$.score.hrv_rmssd_milli') AS hrv,
        json_extract(r.data, '$.score.resting_heart_rate') AS rhr,
        json_extract(r.data, '$.score.spo2_percentage') AS spo2,
        json_extract(r.data, '$.score.skin_temp_celsius') AS skin_temp,
        json_extract(c.data, '$.score.strain') AS strain,
        json_extract(c.data, '$.score.kilojoule') / 4.184 AS kcal,
        json_extract(s.data, '$.score.sleep_performance_percentage') AS sleep_perf,
        json_extract(s.data, '$.score.sleep_efficiency_percentage') AS sleep_eff,
        json_extract(s.data, '$.score.sleep_consistency_percentage') AS sleep_consist,
        json_extract(s.data, '$.score.respiratory_rate') AS resp_rate,
        (json_extract(s.data, '$.score.stage_summary.total_in_bed_time_milli')
         - json_extract(s.data, '$.score.stage_summary.total_awake_time_milli')) / 3600000.0 AS sono_h,
        json_extract(s.data, '$.score.stage_summary.total_rem_sleep_time_milli') / 3600000.0 AS rem_h,
        json_extract(s.data, '$.score.stage_summary.total_slow_wave_sleep_time_milli') / 3600000.0 AS profundo_h,
        json_extract(s.data, '$.start') AS dormiu,
        json_extract(s.data, '$.end') AS acordou
    FROM cycles c
    LEFT JOIN recoveries r ON r.cycle_id = c.id
    LEFT JOIN sleeps s ON s.id = json_extract(r.data, '$.sleep_id');
    """)


def sync(full=False):
    cfg = load_config()
    token = access_token(cfg)
    con = sqlite3.connect(DB_FILE)
    init_db(con)

    for table, (path, key) in RESOURCES.items():
        start = None
        if not full:
            last = con.execute(f"SELECT max(start) FROM {table}").fetchone()[0]
            if last:
                # volta alguns dias para pegar registros que foram re-pontuados
                dt = datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(days=3)
                start = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        n = 0
        for rec in fetch_all(token, path, start):
            rec_start = rec.get("start") or rec.get("created_at")
            con.execute(
                f"INSERT OR REPLACE INTO {table} ({key}, start, updated_at, data) VALUES (?, ?, ?, ?)",
                (str(rec[key]), rec_start, rec.get("updated_at"), json.dumps(rec)),
            )
            n += 1
        con.commit()
        print(f"{table}: {n} registros")

    for k, path in (("basic", "/v2/user/profile/basic"), ("body", "/v2/user/measurement/body")):
        con.execute("INSERT OR REPLACE INTO profile VALUES (?, ?)", (k, json.dumps(api_get(token, path))))
    con.commit()
    con.close()
    print(f"Sincronizado em {DB_FILE}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "auth":
        authorize(load_config())
    else:
        sync(full=(cmd == "full"))
