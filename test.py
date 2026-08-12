import engine as engine

part = 1
SPLIT = 1
test_date = '20250510'
test_code = None
# ['20241010_LOB.db']
backtest = engine.BackTest(part, SPLIT, test_date, test_code)
backtest.Data_load()

