"""Alarm Blok2Candle XAUUSD M5 ke HP lewat ntfy (jalan di GitHub Actions, laptop boleh mati).

Aturan sama dengan indikator XAUUSD_Blok2Candle (MT5) / Blok 2 Candle M5 (TradingView):
2 candle M5 warna sama, candle ke-2 lebih kecil (range <= 0.95 x candle ke-1), candle ke-1 >= 0.5 x ATR14,
bentuk (body / ekor atas / ekor bawah) candle ke-2 mirip candle ke-1 minimal 70%. Hanya candle yang sudah close.

Data: TwelveData XAU/USD (spot) bila secret TWELVEDATA_KEY diisi, kalau tidak Yahoo Finance GC=F (futures emas,
harga beberapa dolar di atas spot, bentuk candle hampir sama).
Tanpa NTFY_TOPIC skrip hanya mencetak hasil (mode uji). Hanya pustaka bawaan Python.
Pemakaian pribadi, alat bantu visual, bukan sinyal entry.
"""
import calendar
import json
import os
import time
import urllib.parse
import urllib.request

MIRIP = float(os.getenv("MIRIP", "70"))
MAX_RASIO = float(os.getenv("MAX_RASIO", "0.95"))
MIN_ATR = float(os.getenv("MIN_ATR", "0.5"))
LOOKBACK_MIN = int(os.getenv("LOOKBACK_MIN", "30"))     # candle close dalam N menit terakhir yang diperiksa
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
WIB = 7 * 3600


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def data_twelve(key):
    d = get_json("https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=5min&outputsize=120"
                 "&timezone=UTC&apikey=" + urllib.parse.quote(key))
    if "values" not in d:
        raise RuntimeError("TwelveData: " + str(d.get("message", d))[:200])
    rows = []
    for v in reversed(d["values"]):
        t = calendar.timegm(time.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S"))
        rows.append((t, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"])))
    return rows, "XAU/USD"


def data_yahoo(sym="GC=F"):
    err = None
    for host in ("query1", "query2"):
        try:
            d = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                         "?interval=5m&range=5d")
            res = d["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            rows = [(int(t), o, h, l, c) for t, o, h, l, c in
                    zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"]) if None not in (o, h, l, c)]
            return rows, sym
        except Exception as e:  # coba host berikutnya
            err = e
    raise err


def shape(o, h, l, c):
    r = h - l
    return abs(c - o) / r, (h - max(o, c)) / r, (min(o, c) - l) / r


def find_signals(rows):
    """Kembalikan list (waktu_candle2, arah, mirip, close) untuk candle yang sudah close."""
    now = time.time()
    rows = [x for x in rows if x[0] + 300 <= now]          # buang candle yang belum close
    tr = []
    for i, (_, o, h, l, c) in enumerate(rows):
        pc = rows[i - 1][4] if i else c
        tr.append(max(h, pc) - min(l, pc))
    out = []
    for i in range(15, len(rows)):
        a = i - 1
        t, o2, h2, l2, c2 = rows[i]
        _, o1, h1, l1, c1 = rows[a]
        d1 = (c1 > o1) - (c1 < o1)
        d2 = (c2 > o2) - (c2 < o2)
        r1, r2 = h1 - l1, h2 - l2
        if d1 == 0 or d1 != d2 or r1 <= 0 or r2 <= 0 or r2 > MAX_RASIO * r1:
            continue
        atr = sum(tr[a - 13:a + 1]) / 14                     # ATR14 rata-rata sederhana (seperti iATR MT5)
        if MIN_ATR > 0 and r1 < MIN_ATR * atr:
            continue
        b1, u1, w1 = shape(o1, h1, l1, c1)
        b2, u2, w2 = shape(o2, h2, l2, c2)
        sim = 100 * (1 - (abs(b1 - b2) + abs(u1 - u2) + abs(w1 - w2)) / 2)
        if sim >= MIRIP:
            out.append((t, d1, sim, c2))
    return out


def send_ntfy(topic, title, text, naik):
    req = urllib.request.Request(f"https://ntfy.sh/{urllib.parse.quote(topic)}", data=text.encode("utf-8"),
                                 headers={"Title": title, "Priority": "urgent",
                                          "Tags": "chart_with_upwards_trend" if naik else "chart_with_downwards_trend"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status == 200


def main():
    topic = os.getenv("NTFY_TOPIC", "")
    key = os.getenv("TWELVEDATA_KEY", "")
    if time.gmtime().tm_wday == 5 and not os.getenv("UJI"):                         # Sabtu UTC: pasar tutup
        print("Sabtu, pasar emas tutup")
        return
    detik = time.time() % 300                     # dipicu tepat saat candle close? tunggu data matang
    if detik < 12:
        time.sleep(12 - detik)
    harap = int(time.time() // 300) * 300 - 300   # waktu buka candle terakhir yang mestinya sudah close
    for coba in range(3):
        try:
            rows, src = data_twelve(key) if key else data_yahoo()
        except Exception as e:
            print("Gagal ambil data utama:", e)
            rows, src = data_yahoo()
        if not key or rows[-1][0] >= harap or coba == 2:
            break
        print("Data candle terakhir belum masuk, ulang 10 detik lagi")
        time.sleep(10)
    state = {"last": 0}
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    batas = time.time() - LOOKBACK_MIN * 60 - 300
    sig = [s for s in find_signals(rows) if s[0] > state.get("last", 0) and s[0] >= batas]
    print(f"Sumber {src}, {len(rows)} candle, candle terakhir "
          f"{time.strftime('%d-%m %H:%M', time.gmtime(rows[-1][0] + WIB))} WIB, sinyal baru {len(sig)}")
    for t, d, sim, c in sig:
        jam = time.strftime("%d-%m %H:%M", time.gmtime(t + 300 + WIB))
        msg = (f"XAUUSD M5: 2 candle {'NAIK' if d == 1 else 'TURUN'}, candle ke-2 lebih kecil "
               f"(mirip {sim:.0f}%). Close {jam} WIB, harga {c:.2f} [{src}]")
        print(msg)
        if topic:
            title = f"Blok2Candle {'NAIK' if d == 1 else 'TURUN'} XAUUSD M5"
            print("ntfy terkirim" if send_ntfy(topic, title, msg, d == 1) else "ntfy GAGAL")
        state["last"] = max(state.get("last", 0), t)
    if sig and topic:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)


if __name__ == "__main__":
    main()
