# Third-party notices

## daily_stock_analysis

Source snapshot under `vendor/daily_stock_analysis/`.

- Project: https://github.com/ZhuLinsen/daily_stock_analysis
- Upstream commit: `ce364e457aab288863a5707e7b3df79786ad07f2`
- License: MIT
- Copyright (c) 2026 ZhuLinsen

The MIT license text is preserved at `vendor/daily_stock_analysis/LICENSE`.

Tick Stock Panel keeps its own React interface. DSA's web and desktop applications are not included in this snapshot. See `vendor/daily_stock_analysis/VENDOR.md`.

## Alpha158

Expressions in `backend/app/factors/alpha158.py` are adapted from vnpy's Alpha158, which was ported from Microsoft Qlib. TSP does not vendor either source tree and does not depend on the vnpy or qlib packages.

- vnpy: https://github.com/vnpy/vnpy/blob/master/vnpy/alpha/dataset/datasets/alpha_158.py
- Qlib: https://github.com/microsoft/qlib
- License: MIT
- Copyright (c) vnpy contributors
- Copyright (c) Microsoft Corporation

```
MIT License

Copyright (c) vnpy contributors
Copyright (c) Microsoft Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 国君191 / gtja191

`backend/app/factors/gtja191.py` 里的表达式改写成 TSP 因子 DSL，来源是国泰君安证券
金融工程团队的公开研究报告：

- 报告：《基于短周期价量特征的多因子选股体系》，国泰君安证券金融工程
- 公开发布日期：**2017-06-15**
- 内容：191 个短周期价量 Alpha 因子（业内通称 GTJA191 / 国君 191）

网络上部分转写版本把这个年份写成「2014」。那是转写误差，**不作为权威年份**；
本仓库一律以 2017-06-15 为准。

**TSP 不 vendor 任何第三方源码树，也不依赖任何第三方运行时库。** 具体地：

- 不依赖 Vibe-Trading —— 代码路径零 `import vibe`、零 pip 依赖、零 vendor 整仓。
  Vibe-Trading 对 GTJA191 的转写与标签只作为**隔离旁证**，不构成权威。
- 不依赖 DolphinDB —— 其 `gtja191Alpha` 附录仅作为**字段依赖 / ID 清单**旁证。
- 公式冲突时，以**公开发表的报告原文 + TSP 自身的 DSL 语义**为准。

本仓库只把公式写成 DSL 表达式（`backend/app/factors/gtja191.py`），不做源码复制。
报告原文的算子语义与 TSP 算子的对应关系、以及无法忠实表达因而标记为 `blocked:ops`
的部分，逐条记录在 `docs/gtja191.md` 与模块 docstring 中。

**关于使用与合规**：报告是有版权的商业研究文献。本仓库只摘录为验证 TSP 因子 DSL
表达能力所必需的**公式定义**（数学表达式），不转载正文、图表或结论段落。使用者若要
引用报告的分析方法与结论，应自行取得原始报告。研究报告不构成投资建议；本仓库对
gtja191 的任何计算都仅用于研究与工程验证，**不构成投资建议，执行须人工确认**。

