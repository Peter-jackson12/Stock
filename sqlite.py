import sqlite3
conn = sqlite3.connect(r"C:\Projects\Stock\sampledata\raw_ticks\20260914_raw.db")
c = conn.cursor()
for t in ["raw_trades", "raw_quotes"]:
    print("===", t, "===")
    print(" 최초 행:", c.execute(f"SELECT * FROM {t} ORDER BY rowid ASC LIMIT 1").fetchone())
    print(" 최근 행:", c.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 1").fetchone())
    print(" 대략 행 수(max rowid):", c.execute(f"SELECT MAX(rowid) FROM {t}").fetchone())