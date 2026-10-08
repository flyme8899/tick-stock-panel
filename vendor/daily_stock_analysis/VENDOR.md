# Vendored daily_stock_analysis

This directory is a source snapshot of [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) (MIT).

- Upstream commit: `ce364e457aab288863a5707e7b3df79786ad07f2`
- Upstream date: 2026-10-05
- License: `LICENSE` in this directory (Copyright (c) 2026 ZhuLinsen)

Tick Stock Panel does not ship DSA's web or desktop UI (`apps/dsa-web`, `apps/dsa-desktop`). Those trees, the test suite, evaluation fixtures, and `docs/assets` media were omitted to keep this product on TSP's interface. The Python service (`main.py`, `api/`, `src/`, `data_provider/`, `bot/`, `strategies/`) is what the TSP decision workspace calls.

Do not edit upstream files here for product behavior. TSP integration lives in `backend/app/custom/dsa/` and `frontend/src/custom/dsa/`.
