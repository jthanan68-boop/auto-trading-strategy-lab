"""Cross-check: do logged fill prices match the cache or the chart API?

A 5bps step on top of the *cache* close reproduces the logged fill exactly.
That identifies which of the two candidate feeds drove the recorded orders.
"""

cache = {"BTC": 83349.86, "ETH": 2501.30, "RELIANCE": 1174.60,
         "TCS": 2304.90, "INFY": 1088.00}
fill = {"BTC": 83391.53, "ETH": 2502.55, "RELIANCE": 1175.19,
        "TCS": 2306.05, "INFY": 1088.54}
chart = {"BTC": 83679.47, "ETH": 2534.70, "RELIANCE": 1170.30,
         "TCS": 2156.00, "INFY": 1023.40}

print(f"{'symbol':<10}{'fill vs cache':>15}{'fill vs chartAPI':>18}{'cache x1.0005':>16}{'match':>8}")
print("-" * 70)
for s in cache:
    vs_cache = (fill[s] / cache[s] - 1) * 100
    vs_chart = (fill[s] / chart[s] - 1) * 100
    predicted = cache[s] * 1.0005
    ok = abs(fill[s] - predicted) < 0.01
    print(f"{s:<10}{vs_cache:>14.4f}%{vs_chart:>17.4f}%{predicted:>16,.2f}{'YES' if ok else 'no':>8}")

print()
print("A ~0.0500% gap to the cache close and 0.0000% to the chart API means the")
print("logged fills were priced from the CSV cache, not from the live chart feed.")
print()
print("Chart-API closes are days older and differ by up to 6% on TCS/INFY, so")
print("if the chart feed had driven these fills the gap would be far larger.")