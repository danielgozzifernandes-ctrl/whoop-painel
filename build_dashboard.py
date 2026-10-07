"""Gera o painel a partir do whoop.db e do template.

Uso:
    python build_dashboard.py            # site/ (criptografado se DASH_PASSWORD estiver definido)
    python build_dashboard.py --plain    # sem senha, para visualizar localmente

Variaveis de ambiente:
    DASH_PASSWORD   senha que abre a pagina publicada
"""
import base64
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_DIR = Path(os.environ.get("WHOOP_STATE_DIR", BASE_DIR))
DB_FILE = STATE_DIR / "whoop.db"
TEMPLATE = BASE_DIR / "dashboard" / "template.html"
SITE = BASE_DIR / "site"
KDF_ITER = 600_000
# sal fixo por site: assim a chave lembrada no celular continua valendo apos cada atualizacao
KDF_SALT = hashlib.sha256(b"whoop-painel/v1").digest()[:16]

H = 3600000.0


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def offset(tz):
    """'-03:00' -> timedelta"""
    if not tz:
        return timedelta(0)
    sign = -1 if tz[0] == "-" else 1
    h, m = tz[1:].split(":")
    return sign * timedelta(hours=int(h), minutes=int(m))


def local(ts, tz):
    dt = parse(ts)
    return (dt + offset(tz)).replace(tzinfo=None) if dt else None


def r1(x, n=1):
    return round(x, n) if isinstance(x, (int, float)) else None


def load(con, table):
    return [json.loads(r[0]) for r in con.execute(f"SELECT data FROM {table}")]


def build():
    con = sqlite3.connect(DB_FILE)
    cycles = load(con, "cycles")
    recs = {r["cycle_id"]: r for r in load(con, "recoveries")}
    sleeps = load(con, "sleeps")
    workouts = load(con, "workouts")
    prof = {k: json.loads(v) for k, v in con.execute("SELECT k, data FROM profile")}
    con.close()

    sleeps_by_id = {s["id"]: s for s in sleeps}
    naps_by_day = {}
    for s in sleeps:
        if s.get("nap") and s.get("score"):
            d = local(s["end"], s["timezone_offset"]).date().isoformat()
            st = s["score"]["stage_summary"]
            naps_by_day[d] = naps_by_day.get(d, 0) + (st["total_in_bed_time_milli"] - st["total_awake_time_milli"]) / H

    days = []
    for c in sorted(cycles, key=lambda c: c["start"]):
        tz = c.get("timezone_offset")
        rec = recs.get(c["id"])
        rs = (rec or {}).get("score") or {}
        sl = sleeps_by_id.get((rec or {}).get("sleep_id"))
        ss = (sl or {}).get("score") or {}
        cs = c.get("score") or {}

        if sl:
            wake = local(sl["end"], sl["timezone_offset"])
            bed = local(sl["start"], sl["timezone_offset"])
            day = wake.date()
            midnight = datetime.combine(day, datetime.min.time())
            bed_min = round((bed - midnight).total_seconds() / 60)
            wake_min = round((wake - midnight).total_seconds() / 60)
        else:
            day = (local(c["start"], tz) + timedelta(hours=8)).date()
            bed_min = wake_min = None

        st = ss.get("stage_summary") or {}
        need = ss.get("sleep_needed") or {}
        asleep = None
        if st:
            asleep = (st["total_in_bed_time_milli"] - st["total_awake_time_milli"]) / H
        need_h = sum(need.get(k, 0) for k in (
            "baseline_milli", "need_from_sleep_debt_milli",
            "need_from_recent_strain_milli", "need_from_recent_nap_milli")) / H if need else None

        days.append({
            "d": day.isoformat(),
            "live": c.get("end") is None,
            "rec": rs.get("recovery_score"),
            "cal": rs.get("user_calibrating"),
            "hrv": r1(rs.get("hrv_rmssd_milli")),
            "rhr": rs.get("resting_heart_rate"),
            "spo2": r1(rs.get("spo2_percentage")),
            "temp": r1(rs.get("skin_temp_celsius"), 2),
            "strain": r1(cs.get("strain")),
            "kcal": round(cs["kilojoule"] / 4.184) if cs.get("kilojoule") else None,
            "avgHr": cs.get("average_heart_rate"),
            "maxHr": cs.get("max_heart_rate"),
            "steps": c.get("step_count"),
            "bed": bed_min,
            "wake": wake_min,
            "inBed": r1(st.get("total_in_bed_time_milli", 0) / H, 2) if st else None,
            "sleep": r1(asleep, 2),
            "light": r1(st.get("total_light_sleep_time_milli", 0) / H, 2) if st else None,
            "deep": r1(st.get("total_slow_wave_sleep_time_milli", 0) / H, 2) if st else None,
            "rem": r1(st.get("total_rem_sleep_time_milli", 0) / H, 2) if st else None,
            "awake": r1(st.get("total_awake_time_milli", 0) / H, 2) if st else None,
            "dist": st.get("disturbance_count"),
            "cycles": st.get("sleep_cycle_count"),
            "need": r1(need_h, 2),
            "needBase": r1(need.get("baseline_milli", 0) / H, 2) if need else None,
            "needStrain": r1(need.get("need_from_recent_strain_milli", 0) / H, 2) if need else None,
            "needNap": r1(need.get("need_from_recent_nap_milli", 0) / H, 2) if need else None,
            "debt": r1(need.get("need_from_sleep_debt_milli", 0) / H, 2) if need else None,
            "perf": ss.get("sleep_performance_percentage"),
            "eff": r1(ss.get("sleep_efficiency_percentage")),
            "cons": ss.get("sleep_consistency_percentage"),
            "resp": r1(ss.get("respiratory_rate")),
            "nap": r1(naps_by_day.get(day.isoformat()), 2),
        })

    # se dois ciclos caem no mesmo dia, fica o mais recente
    by_day = {}
    for d in days:
        by_day[d["d"]] = d
    days = sorted(by_day.values(), key=lambda d: d["d"])

    wos = []
    for w in sorted(workouts, key=lambda w: w["start"]):
        s = w.get("score") or {}
        start = local(w["start"], w.get("timezone_offset"))
        end = local(w["end"], w.get("timezone_offset"))
        z = s.get("zone_durations") or {}
        zones = [round(z.get(k, 0) / 60000, 1) for k in (
            "zone_zero_milli", "zone_one_milli", "zone_two_milli",
            "zone_three_milli", "zone_four_milli", "zone_five_milli")]
        wos.append({
            "d": start.date().isoformat(),
            "t": start.strftime("%H:%M"),
            "min": round((end - start).total_seconds() / 60),
            "sport": w.get("sport_name"),
            "strain": r1(s.get("strain")),
            "avgHr": s.get("average_heart_rate"),
            "maxHr": s.get("max_heart_rate"),
            "kcal": round(s["kilojoule"] / 4.184) if s.get("kilojoule") else None,
            "km": r1((s.get("distance_meter") or 0) / 1000, 2) or None,
            "zones": zones,
        })

    basic = prof.get("basic", {})
    body = prof.get("body", {})
    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": {
            "name": basic.get("first_name"),
            "maxHr": body.get("max_heart_rate"),
            "weight": body.get("weight_kilogram"),
            "height": r1(body.get("height_meter"), 2),
        },
        "naps": [
            {"d": local(n["end"], n["timezone_offset"]).date().isoformat(),
             "t": local(n["start"], n["timezone_offset"]).strftime("%H:%M"),
             "h": r1((n["score"]["stage_summary"]["total_in_bed_time_milli"] - n["score"]["stage_summary"]["total_awake_time_milli"]) / H, 2)}
            for n in sorted(sleeps, key=lambda n: n["start"]) if n.get("nap") and n.get("score")
        ],
        "days": days,
        "workouts": wos,
    }

    return data


def encrypt(data, password):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    key = PBKDF2HMAC(hashes.SHA256(), 32, KDF_SALT, KDF_ITER).derive(password.encode())
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"enc": {"salt": b64(KDF_SALT), "iv": b64(iv), "ct": b64(ct), "iter": KDF_ITER}}


def make_icon(path, size):
    from PIL import Image, ImageDraw

    s = size * 4
    img = Image.new("RGB", (s, s), (16, 42, 58))
    d = ImageDraw.Draw(img)
    pad, w = s * 0.2, s * 0.085
    box = (pad, pad, s - pad, s - pad)
    d.arc(box, 0, 360, fill=(40, 70, 88), width=int(w))
    d.arc(box, -90, 180, fill=(92, 214, 140), width=int(w))
    img.resize((size, size), Image.LANCZOS).save(path)


def write_site(data, password):
    SITE.mkdir(exist_ok=True)
    payload = encrypt(data, password) if password else data
    js = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__WHOOP_DATA__*/null", js)
    (SITE / "index.html").write_text(html, encoding="utf-8")
    for size in (180, 512):
        make_icon(SITE / f"icon-{size}.png", size)
    (SITE / "manifest.webmanifest").write_text(json.dumps({
        "name": "Painel de Recuperação", "short_name": "Recuperação", "start_url": "./", "display": "standalone",
        "background_color": "#0f1c24", "theme_color": "#0f1c24",
        "icons": [{"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                  {"src": "icon-180.png", "sizes": "180x180", "type": "image/png"}],
    }, ensure_ascii=False), encoding="utf-8")
    (SITE / ".nojekyll").write_text("")
    mode = "criptografado" if password else "SEM senha"
    print(f"Painel gerado em {SITE} ({mode}): {len(data['days'])} dias, {len(data['workouts'])} treinos")


if __name__ == "__main__":
    plain = "--plain" in sys.argv
    pw = None if plain else os.environ.get("DASH_PASSWORD")
    if not plain and not pw:
        sys.exit("Defina DASH_PASSWORD ou use --plain para gerar sem senha.")
    write_site(build(), pw)
