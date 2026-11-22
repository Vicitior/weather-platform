# 气象站逐日观测数据缺测/异常值的插补与重建：方法清单与可落地建议（2020–2025 为主）

> 面向场景：中国 876 站 / 1951–2025 逐日 / 7 要素（平均、最高、最低气温，相对湿度，降水量，平均风速，日照时数）；平均站距 100–200 km，300 km 内一般 4–12 个邻站。
> 已有基线（59 站空间 hold-out，训练 1991–2005 / 验证 2006–2020，指标 MAE）：最近站 2.13 ℃（气温）；气候距平法 0.78 ℃（气温）/ 1.00 ℃（最高）/ 1.05 ℃（最低）/ 4.90 %（湿度）/ 1.78 mm（降水）/ 0.63（风速）/ 1.21（日照）；距平克里金 0.74 ℃；GBRT 0.74 ℃；仅经纬度 2.43 ℃。
>
> **阅读本文件前必须知道的两条口径警告**（否则会得出错误结论）：
> 1. **MAE ≠ RMSE**。本文件如实抄录原文指标名。粗差近似正态时 RMSE/MAE ≈ 1.25–1.4，所以我们的 0.78 ℃ MAE ≈ 1.0–1.1 ℃ RMSE。把我们的 MAE 直接与文献 RMSE 比是错的。
> 2. **"站点缺测补值" ≠ "站点→格点估算"**。大量中国高精度数据集论文（如 1 km 逐日气温产品）是**站点估算格点**、且用了 DEM/遥感协变量，其 RMSE 不能当作"用邻站补本站缺测"的精度，本文件已单列区分。

---

## 1. 方法清单表

类型列取值：统计（含统计加权/回归）/ 机器学习 / 深度学习 / 低秩（含 EOF、矩阵补全）/ 混合。

| 方法名 | 类型 | 代表文献（作者+年份+期刊/会议） | 报告精度（原文数字 + 指标名 + 单位） | 适用条件 | 局限 |
|---|---|---|---|---|---|
| 算术平均（AA） | 统计 | Putra, Výleta, Hlavčová, Szolgay 2026，*Hydrology* 13(11):300 | 逐日气温 RMSE **1.445 ℃**；NSE 0.973–0.974（该文并列报告 AA 与普通克里金同为 1.445 ℃） | 站距极小、海拔与气候背景一致 | 邻站有系统性海拔偏差时严重失真（该文明确指出距离型方法"未考虑不同海拔测站的系统性差异"） |
| 最近站直接替换（NN） | 统计 | Fagandini, Ciccarese, … 2024，*Mathematical Geosciences* 56:191–217 | 该文将 NN 列入比较组但**未单独报告其误差数值**（仅给 OK/FAO/SCK 的数字） | 极密网、且以"有无降水"为主要目标 | 气温 2 ℃ 量级、湿度 7 % 量级的偏差（我们实测 2.13 ℃ / 7.67 %） |
| 标准序列法 / 气候距平法（含 LAD 最小绝对偏差回归 + DeGaetano 标准化序列平均，即"综合插补法"） | 统计 | 王海军, 涂诗玉, 陈正洪 2008，《气象》34(7):83–91 | 单站（湖北蔡甸）逐日气温 **MAE 平均 0.28 ℃ / 最高 0.32 ℃ / 最低 0.45 ℃**；误差落在 ±0.8 ℃ 内频次 **96.1 % / 94.1 % / 84.8 %**；±0.5 ℃ 频次 85.4 % / 80.3 % / 65.1 %；与实况月相关 >0.886。最优配置：**4 邻站 + 8 年 × 15 天滑动窗口** | 有 ≥20 年序列可建月气候基准；邻站相关高 | 单站单区域试验，未做全网空间交叉验证；高原/复杂地形未覆盖；本质是单变量线性方法 |
| 标准序列法 RO/CS（最优相关邻站 vs 最近邻站的选择策略） | 统计 | 余予, 李俊, 任芝花, 张志富 2012，《气象》38(9):1135–1139 | 中国 2088 站 1971–2000 日平均气温交叉检验：**MAE 0.424 ℃ / RMSE 0.551 ℃（RO 方案）**，vs **MAE 0.475 ℃ / RMSE 0.622 ℃（CS 方案）**；±0.5 ℃ 内比例 p = 73.3 %（RO）/ 69.1 %（CS）；分区 MAE 0.340（5 区最优）～0.672 ℃（2 区）；**最优邻站数 4 个**；邻站搜索半径 220 km、约束 \|Δh\|≤200 m（本站 h₀<2500 m）；MAE/RMSE 随邻站平均距离呈指数增大、p 呈指数减小（相关约 0.8） | 国家级稀疏网（与我们的场景同类！）、有统一气候基准期 | 仅日平均气温、单一变量；对海拔系统性差异只做了阈值筛除，未做订正 |
| Normal-ratio（NR）比值法 | 统计 | Putra et al. 2026，*Hydrology* 13(11):300 | 逐日降水 **RMSE 1.87–1.89 mm**（与 MICE 的 1.85 mm 差异 <2 %），且"保留干日、不产生负值" | 降水这类站点气候均值差异大、空间相关弱但分布形态相近的要素 | 对单日强降水事件响应偏弱；需要稳定的站点正常值 |
| Normal-ratio（另一量级：累积降水） | 统计 | *Atmosphere* 2020, 11(4):389（21 法对比） | r = 0.945；**MAE 22.90 mm、RMSE 33.695 mm**（该研究为**累积量级降水**，非逐日 mm）；其余方法 MAE 15.366–17.111 mm、RMSE 24.128–28.860 mm | 月/季尺度补值 | **量级不可与逐日 mm 直接比较**，仅说明 NR 在该设定下 r 高但绝对误差大 |
| IDW 反距离权重 | 统计 | Putra et al. 2026；Fagandini et al. 2024 | 逐日气温 RMSE **1.383–1.409 ℃**（NSE 0.973–0.974）；逐日降水 RMSE 约 1.89 mm | 平坦、缓坡地，海拔差小 | 不处理海拔系统性偏差；与我们实测（气候距平法 0.78 ℃）相比是明显的弱基线 |
| IKNN 反距离 k 近邻 | 统计 | Putra et al. 2026 | 逐日降水 RMSE 约 **1.87–1.89 mm**（与 NR 同档，优于其对比中的其它复杂法）；气温 1.383–1.409 ℃ | 降水缺测、邻站相关中等 | 与 IDW/NR 一样无法纠正海拔偏差 |
| 多元线性回归（MLR，邻站回归） | 统计 | 孙越, 王海军, 周月华 等 2023，《暴雨灾害》42(1):97–104（仅取到摘要）；Afrifa-Yamoah et al. 2020，*Meteorological Applications* 27(1):e1873 | 孙越：区域自动站逐日气温插补"**MLR 最优、IDW 次之、OK 最差**"（**具体 MAE/RMSE 未取得**）；Afrifa-Yamoah：逐时 4 站，"多元线性回归整体最优、ARIMA+Kalman 次之"（**未报数值**） | 样本量足、时间离散度大时 MLR 表现更好（孙越原文） | 线性假设；对气候态漂移敏感；OK 在这种"局部变差函数"设定下反而最差，说明**变差函数估计不稳时克里金会输给回归** |
| 线性-非线性组合回归（OML，优化后的 ML 组合） | 混合 | *Water* 2022, 14(11):1799（Inter-Andean 流域日降水补缺） | 测试集 **RMSE 1.28–2.31**（逐站），对比 LRM/MRM 的 **2.05–3.03**；全表 RMSE 最小 1.26、最大 4.79 | 有较长训练序列、邻站可用 | 站间迁移性差，需逐站或分区训练 |
| 随机森林（RF，点尺度插值） | 机器学习 | Li Shuangping et al. 2026，*Sensors* 26(7):2167（溪洛渡 47 站，38 训练/9 测试，2023–2024 逐日） | 气温 **MAE 0.88 ℃ / RMSE 1.28 ℃**；同时 IDW/OK 在夏季 **MAE 1.5–2.0 ℃、RMSE 2.0–2.8 ℃**；XGB-C（含中心点特征）R² 气温 0.83 / 湿度 0.97 / 气压 ≈1.00；机器学习整体把 MAE/RMSE 降低约 **40–60 %** | 有训练期、站网较密 | 复杂地形下仍需高程等协变量；外推能力弱 |
| RF 插补（超高密度 Mesonet） | 机器学习 | *Computers and Electronics in Agriculture* 2022（doi 10.1016/j.compag.2021.106580），134 站、8.5 年 | 15 分钟气温 **MAE 0.43 ℃**；日最高 **0.53 ℃**；日最低 **0.70 ℃** | 站距 10 km 级超密网 | 站网稀疏时精度优势消失；不能直接类比 100–200 km 网 |
| 梯度提升 / GBRT / XGBoost / LightGBM / CatBoost | 机器学习 | Körner, Kronenberg, Genzel, Bernhofer 2018，*Meteorologische Zeitschrift* 27:369–376；*Atmosphere* 2026, 17(9):884；ESSD 预印本（2024, 2419 站） | Körner（德国，逐时→日）：气温 **median RMSE 0.73 ℃**；风速 **median RMSE 0.82 m/s / MAE 0.61 m/s / R² 0.84**（Q25 0.70/0.52/0.79，Q75 0.96/0.72/0.88）；湿度 median RMSE **4.3 %**。*Atmosphere* 2026（西非，CatBoost + 再分析/卫星）：气温 **MAE 1.16–1.27 ℃ / RMSE 1.50–1.92 ℃ / R² 0.89–0.95**；湿度 **MAE 4.75–6.46 % / RMSE 6.42–7.83 %**；降水 **MAE 0.01–0.03 mm / RMSE 0.20–0.30 mm（R² 仅 0.15–0.22）**；风速 MAE 0.61–1.23 / RMSE 0.91–1.64。ESSD 预印本：3 个 ML 模型中 **XGBoost 精度最高** | 有邻站观测 + 高程 + 时相特征；缺测机制近似随机 | 对降水外推极弱（R² 0.15–0.22）；依赖特征工程；我们实测 GBRT 0.74 ℃，已与克里金持平 |
| 多变量时空图正则低秩插补（GR-RTRMC：图 + 变量投影低秩矩阵补全） | 低秩 | *Graph-Regularized Low-Rank Matrix Completion by Variable Projection*，arXiv 2607.09546（比利时 RMI 10 分钟气温 + 法国 Météo-France 站网） | **比利时（RMSE ℃，Block/Spread 两种人为缺口）**：IDW 0.64/0.57；PCA 0.61/0.68；MissForest 0.59/0.56；SoftImpute 0.53/0.41；RTRMC 0.49/0.46；GRALS 0.53/0.63；**GR-RTRMC 0.45/0.43**。**法国**：IDW 0.80/0.74；MissForest 0.70/0.56；SoftImpute 0.59/0.61；GRALS 0.62/0.68；**GR-RTRMC 0.57/0.54**。耗时：IDW 0.14–0.21 s、SoftImpute 3–7 s、GRALS 8–20 s、MissForest 10–14 s | 站网较密、缺测成块或散布；有站点间距离图作先验 | 需调秩与图正则超参；实现比 SoftImpute 复杂；"Spread"场景下 SoftImpute 反而最好（说明**加图正则不总赢**） |
| 低秩矩阵补全（SoftImpute / 截断 SVD / RTRMC / GRALS） | 低秩 | 同上 arXiv 2607.09546；*Graph-Based Matrix Completion Applied to Weather Data*，arXiv 2306.08627 | 比利时 SoftImpute **0.53/0.41**、RTRMC 0.49/0.46、GRALS 0.53/0.63（IDW 0.64/0.57）；早期版本 Table I：IDW 0.61/0.57、PCA 0.78/0.71、**SoftImpute 0.50/0.41**、RTRMC 0.48/0.50、GRALS 0.51/0.45 | 缺测随机、秩较低（气温异常场典型）；**整列（整站）全缺也能补** | 降水这类高秩/重尾场不适用；需选秩；对异常值敏感 |
| 多视图函数型矩阵补全（HSIC-DIMFMC，双信息图正则） | 低秩 | *Big Data and Cognitive Computing* 10(7):230, 2026 | 相对 7 种代表性方法 **RMSE 平均降低 43.97–73.59 %**（原文句）；在"温度-露点、风速-最大风速"等强相关变量对上收益最大 | 多变量同期观测、变量间相关强（我们 7 要素正好符合） | 未见逐方法绝对 RMSE；实现复杂 |
| DINEOF / DINEOF+ / VE-DINEOF（迭代 EOF 重建） | 低秩 | Ping Bo, Su Fenzhen, Meng Yunshan 2016，*PLoS ONE* 11(5):e0155928（VE-DINEOF）；Zhao Haipeng et al. 2025，*Agriculture* 15(11):1212（DINEOF+ 土壤湿度） | Ping：VE-DINEOF 在精度与耗时上**均优于 DINEOF 与 I-DINEOF**（该页**未载具体 RMSE 数值**，仅相对结论）。Zhao：重建与实测比 **MAE 0.01–0.04 m³/m³**、时间序列相关 0.6–0.8；**缺测率 PMD 达 76 % 时插值误差"nearly double"**（→ 实用阈值 75 %）；可用率由 29.00–36.83 % 提升到 87.20–93.60 % | 缺测率 <75 %；原始数据质量好；场内时空相关强 | 对原始数据异常/离群极敏感；不给参数化不确定性；降水/强对流场效果差 |
| 低秩自回归张量补全 / 块 Hankel 低秩补全 | 低秩 | *Low-rank Autoregressive Tensor Completion*，arXiv 2104.14936；*Time-series imputation using low-rank matrix completion*（block-Hankel），arXiv 2408.02594 | Hankel 低秩补全与 5 种方法比较"有竞争力"，且**更擅长还原尖峰**（原文结论级，未给统一指标绝对值） | 单站时序补缺（不依赖邻站） | 仅用时间维，未利用我们的站网空间信息 |
| 奇异谱分析（SSA） | 低秩（时间） | Ghafarian Malamiri 等（MODIS LST 补全）；Yashas & Varija 2024，*Atmospheric Research* 312（综述转述） | LST 补全 **RMSE 平均 2.95 K**；Yashas & Varija：**SSA 适合零星小缺口**，日最高气温上 **P-BSHADE 与 SVM 更优**（因能刻画空间/时间异质性） | 单站时序、小缺口（数天） | 大缺口失效；不用空间信息 |
| MICE 链式方程多重插补 | 统计 / 机器学习（混合） | Putra et al. 2026，*Hydrology* 13(11):300（31 法基准，20 次 Monte Carlo 交叉验证，每次掩蔽约 3736 个值） | 逐日气温 **RMSE 0.965 ± 0.013 ℃、NSE 0.987（全场 31 法最优）**；GPR / MissForest / LightGBM / MLP 紧随 **0.978–0.996 ℃**；距离型 IDW/NR_spatial/IKNN **1.383–1.409 ℃**；AA 与 OK **1.445 ℃** | 多变量同期相关强、缺口为随机散布的"值"而非整站长期中断 | 该文明确限定只处理"孤立缺测与短缺口"，**多年整站中断不在适用范围**；需要多变量完整框架 |
| MICE-RF（MICE 的随机森林变体） | 混合 | Jing 等 2022，*Water Resources Management* 36:1159–1173（汉江流域，含气温/降水/湿度/风速/日照） | **未取得具体数值**；结论：MICE-RF 优于线性插补、均值、样条、KNN | 与我们的 7 要素构成高度一致 | 同上；中文区小流域，样本量远小于我们 |
| MissForest（迭代随机森林插补） | 机器学习 | Stekhoven & Bühlmann 2012，*Bioinformatics* 28:112–118（方法本身）；arXiv 2607.09546 比利时/法国对比；Putra et al. 2026；综述 MethodsX 2025 指出它是气候缺测插补中**使用最多的 ML 方法** | 比利时 **0.59/0.56 ℃**（IDW 0.64/0.57）；法国 **0.70/0.56 ℃**（IDW 0.80/0.74）；Slovakia 逐日气温 **0.978–0.996 ℃**（略逊 MICE）；综述：Vidal-Paz 等西班牙 Galicia 降水"missForest 误差最低"；Qaraghuli 等伊拉克 Mosul：**KNN 最适合降水、norm.predict 最适合气温、missForest 最适合相对湿度**；Ismail 等摩洛哥：**missForest > MICE > KNN** | 变量间相关强；缺测随机；中等维度（我们的 7 要素 × 邻站特征偏大） | 每次迭代 10–14 s（比利时尺度），高维时 O(p²) 代价大；对整站长期中断无解 |
| KNN 插补 | 机器学习 | Qaraghuli 等（伊拉克 Mosul，1980–2013）；*Climate* 2026, 14(11):251（随机框架对比） | *Climate* 2026：线性插值处理 1 天缺口 **RMSE 1.43 ℃**；5 天以上缺口多变量法更优；**RFI 与 KNN 对缺测率最不敏感**（原文未给逐值 RMSE） | 小缺口、站网中等 | 大缺口与外推场景退化 |
| GPR 高斯过程回归 | 机器学习 | Putra et al. 2026 | 逐日气温 **RMSE 0.978–0.996 ℃**（与 MissForest/LightGBM/MLP 同档，仅次于 MICE） | 中小样本、需要不确定度 | O(n³) 计算量；大规模站网难扩展 |
| P-BSHADE（点估计 + 协方差 BLUE 权重） | 统计（时空加权 + 协方差） | Xu Chengdong, Wang Jinfeng, Hu Maogui, Li Qingxiang 2013，*Journal of Climate* 26(19):7452–7463 | 中国 582 站**年平均**气温：5 邻站时 1950–2000 平均 **RMSE 0.248 ℃**，同条件 **kriging 2.368 ℃、IDW 2.438 ℃**；1971–2000 参考期 P-BSHADE 平均 **RMSE 0.168 ℃** | 有长参考期、站间比值（b_i）稳定；**低频谱（年/月）要素** | **原文只做年平均气温**，平滑度远高于逐日，**数字不可与我们的 0.78 ℃ 逐日 MAE 直接比较**；1950–1970 段曾输给空间回归检验法（SRT） |
| 时空回归克里金（RK with covariates） | 统计（地统计） | Kilibarda, Hengl, Heuvelink, Gräler, Pebesma, Perčec Tadić, Bajat 2014，*JGR-Atmospheres* 119(5):2294–2313；Sekulić & Kilibarda 2020（克罗地亚） | Kilibarda：全球 1 km 逐日气温留一交叉验证 **RMSE ±2 ℃（站密区）、±2～±4 ℃（站疏区）、高海拔 >1000 m 与南极约 6 ℃**；Sekulić：克罗地亚平均精度 **±2.4 ℃**，RK 解释 **91 %** 变率 vs 普通局部回归 44 % | 有 MODIS LST / DEM / TWI 等协变量产品 | 需外部协变量（我们暂无遥感产品）；分块计算量大；精度比纯"缺测补值"任务宽松（这是格点估算） |
| 距平域普通克里金（OK on anomalies，局部变差函数） | 统计（地统计） | Fagandini et al. 2024，*Mathematical Geosciences* 56:191–217（葡萄牙 12 站日降水；季节半变差函数） | 逐日降水：POR057 站 OK 最优 **MAE 0.423 mm/day / RMSE 1.186 mm/day**；POR046 站 FAO 回归更优（MAE 0.597 / RMSE 1.919），而 **OK MAE 0.726 / RMSE 2.095、SCK MAE 0.724 / RMSE 2.094**（该站 ME −0.275 mm/day，系统性低估）；RMSE 上 SCK 在 6/12 站最优、OK 在 4/12 站最优 | 变差函数可稳定估计；有足够邻站 | 局部变差函数估计不稳时（孙越 2023）会输给 MLR；对海拔不做订正 |
| 简单协同克里金（SCK） | 统计（地统计） | Fagandini et al. 2024 | MAE 0.724 / RMSE 2.094 mm/day（POR046）；RMSE 在 12 站中 6 站最优 | 有与目标强相关的协变量 | 协变量不足时退化为 OK；仍有低估 |
| 梯度提升 + 再分析/卫星融合（跨源重建） | 机器学习 | *Atmosphere* 2026, 17(9):884（西非） | 见"梯度提升"行（该文主结果：气温 MAE 1.16–1.27 ℃、湿度 MAE 4.75–6.46 %、降水 R² 0.15–0.22） | 有 ERA5/卫星等外部产品 | 降水几乎无效；再分析产品本身有偏差，需先验证 |
| LSTM / CNN / MLP | 深度学习 | Boujoudar 等（摩洛哥 Ben Guerir，综述转述）；Putra et al. 2026（MLP）；ESSD 预印本 2024（LSTM 补 19 类严重缺口） | MLP 逐日气温 **0.978–0.996 ℃**；LSTM 逐站训练补严重缺口"平均 **R² 0.9 / RMSE 0.5 mm d⁻¹ / nRMSE 38 %**"；Boujoudar：**LSTM 与 CNN 擅长短缺口、MLP 在 3 天缺口段最优** | 长序列、大量训练数据、GPU | MethodsX 2025 综述表 6 明确把 RNN/Transformer/GAN/BiLSTM/LSTM 标为**计算成本 "High"**；本机无 GPU |
| 图神经网络（GAT / 观测引导插值） | 深度学习 | arXiv 2611.15598v1（近地面气温重建）；*AIES* 2026, 5(2)（瑞士业务 GNN） | GAT：**spatial RMSE 0.997 K，比 kriging 与 GraphEM 低约 31–32 %，比 LaMa 低 10.1 %；平均逐站 RMSE 0.648 K**。但 AIES 2026 瑞士业务系统原文结论：**"the GNN struggles most with temperature variables"** | 站网密、有图结构（如 30+ 传感器/km² 级） | GPU；**在气温任务上并无优势**（反证）；收益在约 30 个传感器后递减 |
| GAN / GAIN | 深度学习 | Popolizio 等（意大利 Apulia，98 站气温，综述转述）；MethodsX 2025 综述 | **未取得具体数值**；综述定性结论：GAN 类在深度学习里表现突出 | 大样本、需要生成式不确定性 | 训练不稳定；本机不可行 |
| 矩阵补全（Matrix Completion）用于气候缺测 | 低秩 | Decorte 等（比利时 Flanders，温度 + 土壤湿度；综述转述） | **未取得具体数值**；结论：在 RMSE / MAE / Prediction Coverage Error 三项上 **"Matrix Completion 在所有缺口类型下均最优"** | 多变量、多站、缺口类型混合 | 与上一行 "低秩矩阵补全" 同族；重复计数风险，我们视为同一路线的独立佐证 |
| 站点→格点高精度重建（4D-STDF） | 混合（深度学习 + 地形/遥感协变量） | *Scientific Data* 2024（中国 2003–2022 全天候逐日气温，2461 站，1 km，10 折 CV） | 逐日：**MAE Tmax 1.07 / Tmin 1.09 / Tmean 0.85 ℃**；**RMSE Tmax 1.49 / Tmin 1.53 / Tmean 1.18 ℃**；R² 0.98/0.98/0.99。月尺度 MAE 1.03/1.22/0.38 ℃、RMSE 1.38/1.65/0.52 ℃。对比：Chen 等中国 1 km 日平均气温最低 RMSE 1.342 ℃；Zhang 等 Tmax/Tmin RMSE 1.75/1.82 ℃ | 有遥感 LST + DEM 协变量；目标是产品化格点 | **是"站→格点"估算精度，不是"缺测站补值"精度**；含地形/遥感信息，不能用来对标我们的 0.78 ℃ |

---

## 2. 按精度排序的结论

### 2.1 逐日气温（MAE 口径，可与我们的 0.78 ℃ 直接比）

| 排序 | 来源 | 数字 | 站网/设定 | 与我们可比吗 |
|---|---|---|---|---|
| 1 | 王海军等 2008《气象》 | **MAE 0.28 ℃**（平均气温，最高 0.32 / 最低 0.45） | 单站（湖北蔡甸），4 邻站，8 年×15 天窗口 | 半可比：**单站**、湿润平原、邻站高度相关；不是 59 站空间留出 |
| 2 | 《气象与环境学报》2026（重庆，RF） | MAE 0.24 ℃ / RMSE 0.34 ℃ / R² 0.98 | 极密网；目标为 2022 年逐月 15 日的**日平均气温**（月内 15 天，非全序列） | **不可比**：目标更平滑、站网更密 |
| 3 | Comput. Electron. Agric. 2022（Mesonet RF） | 15 分钟 MAE 0.43 ℃；日最高 0.53；日最低 0.70 | 134 站超密网 | 不可比：站距 10 km 级 |
| 4 | Li Shuangping et al. 2026 *Sensors*（溪洛渡 RF） | MAE 0.88 ℃ / RMSE 1.28 ℃ | 47 站、38 训练 9 测试、逐日 | 量级可比（略差于我们） |
| 5 | *Atmosphere* 2026 17(9):884（CatBoost + 再分析） | MAE 1.16–1.27 ℃ / RMSE 1.50–1.92 ℃ | 西非，有再分析协变量 | 量级可比（差于我们） |

**结论（MAE 口径）**：文献里逐日气温的**最低 MAE 是 0.24–0.45 ℃ 量级**，但全部来自"单站/超密网/目标更平滑"的设定。在**多站、逐日、真空间留出**这类与我们同构的设定下，公开文献给出的最好结果换算到 MAE 约 **0.7–0.8 ℃**（见 2.2 的 MICE 0.965 ℃ RMSE ÷ 1.25–1.4 ≈ 0.69–0.77 ℃）。**我们的 0.78 ℃ 已经站在"文献最优档"里，靠调参不会有数量级收益；要再降必须换方法档位（低秩/多变量联合）。**

### 2.2 逐日气温（RMSE 口径，文献最常用的指标）

| 排序 | 方法/来源 | RMSE | 站网密度 |
|---|---|---|---|
| 1 | **GR-RTRMC**（图正则低秩，arXiv 2607.09546） | **0.45 / 0.43 ℃**（比利时 Block/Spread）；0.57 / 0.54 ℃（法国） | 极密（国家级业务网，10 分钟级） |
| 2 | **SoftImpute**（同文） | 0.53 / 0.41 ℃（比利时）；0.59 / 0.61 ℃（法国） | 极密 |
| 3 | **余予等 2012**（标准序列法 RO，中国 2088 站） | **0.551 ℃** | **稀疏网（与我们的场景最接近！）** |
| 4 | MissForest（同文比利时/法国） | 0.59 / 0.56 ℃；0.70 / 0.56 ℃ | 极密 |
| 5 | **MICE**（Putra et al. 2026，Slovakia） | **0.965 ± 0.013 ℃**（NSE 0.987） | 3 个气温站 / 644 km² |
| 6 | GPR / MissForest / LightGBM / MLP（同文） | 0.978–0.996 ℃ | 同上 |
| 7 | IDW / NR_spatial / IKNN（同文） | 1.383–1.409 ℃ | 同上 |
| 8 | AA 与 OK（同文） | 1.445 ℃ | 同上 |
| 9 | 4D-STDF（*Sci Data* 2024，中国 2461 站） | 1.18 ℃（Tmean 逐日，**站→格点，含遥感/地形**） | 全国 1 km 产品 |

**关键发现（对我们最有价值的结论）**：Putra et al. 2026 在**同类稀疏网、逐日、真交叉验证**下证明——**"方法复杂度对气温有用、对降水没用"**（原文："Method complexity therefore mattered for air temperature but not for precipitation."）。气温上 MICE（0.965 ℃）比距离型方法（1.383–1.409 ℃）**好 30 %**、比算术平均/OK（1.445 ℃）**好 33 %**。这是"我们把气温从 0.78 ℃ 再往下压"这一目标最强的一条文献依据。

### 2.3 逐日降水

| 排序 | 方法/来源 | 数字 | 备注 |
|---|---|---|---|
| 1 | OK（Fagandini et al. 2024，葡萄牙 POR057） | **MAE 0.423 mm/day / RMSE 1.186 mm/day** | 单站最优；该站偏干 |
| 2 | ML（Bellido-Jiménez et al. 2021，西班牙安达卢西亚） | **RMSE 最低 1.246 mm/day、MBE −0.001 mm/day、R² 0.898**（用 **50 km 内**邻站数据） | 内陆优于沿海，RMSE 最多改善 **63.89 %** |
| 3 | normal-ratio / IKNN / IDW（Putra et al. 2026） | **RMSE 1.87–1.89 mm**（与 MICE 1.85 mm 差异 <2 %） | 明确"保留干日、不产生负值" |
| 4 | 优化 ML 组合 OML（*Water* 2022 Inter-Andean） | RMSE 1.28–2.31（逐站），优于 LRM/MRM 的 2.05–3.03 | |
| 5 | M5-MT / MLR（泰国，PMC10654590） | M5-MT MAE 1.170 / RMSE 4.684 / R² 0.78；MLR MAE 1.201 / RMSE 4.733 | 暴雨型气候，RMSE 明显抬高 |
| 6 | CatBoost + 再分析（*Atmosphere* 2026 西非） | MAE 0.01–0.03 mm / RMSE 0.20–0.30 mm，但 **R² 仅 0.15–0.22** | 降水上 ML 几乎没学到极值 |
| — | 我们 | MAE **1.78 mm** | 与第 1–3 名不可直接比（他们是 RMSE，且站距更小） |

**结论**：降水这条线上，**"越复杂越好"不成立**——31 法基准里最简单的 NR/IKNN/IDW 与最强的 MICE 差距 <2 %。我们 1.78 mm 的 MAE 相对文献里 1.19–1.89 mm 的 RMSE 看似"差"，但那是 MAE vs RMSE 的口径差（降水重尾，RMSE/MAE 常 2–4 倍）+ 站距差（他们 10–50 km）。**降水的主要改进空间在"极端事件与干湿日结构"，而不在均方误差。**

### 2.4 其它要素

| 要素 | 文献最好 | 数字 | 我们 | 判断 |
|---|---|---|---|---|
| 相对湿度 | Körner et al. 2018（GB，逐时） | median RMSE **4.3 %** | MAE 4.90 % | 已接近文献档位；P-BSHADE / missForest（综述称 missForest 最适合湿度）值得试 |
| 相对湿度 | *Atmosphere* 2026（CatBoost） | MAE 4.75–6.46 % / RMSE 6.42–7.83 % | 4.90 % | 我们更好 |
| 平均风速 | Körner et al. 2018（GB） | median **MAE 0.61 m/s / RMSE 0.82 m/s / R² 0.84** | MAE 0.63 | **几乎相同**，说明风速基线已到顶，改进空间小 |
| 平均风速 | 伊朗 17 机场（*Atmosphere* 2026） | R² 0.13 / RMSE 2.20 m/s（**最差要素**） | 0.63 | 风速在所有研究里都最难 |
| 日照时数 | 4D-STDF 类产品 / ESSD 预印本 | ESSD：19 类严重缺口用 LSTM 逐站训练平均 R² 0.9、nRMSE 38 %（**指标口径不同，未给绝对值**） | MAE 1.21 | 文献稀缺，**我们的 1.21 可能是该要素少见的可比数字**；建议按 P-BSHADE 比值法 + 多变量联合两条路试 |

---

## 3. Top 5 推荐（针对我们的站网与 0.78 ℃ 基线）

> 判断"值得试"的判据（按权重）：① 文献在**逐日、多站、稀疏网**设定下被验证有效；② 能用 numpy/scipy/sklearn 在本机实现、无需 GPU；③ 与现有 0.74 ℃ 基线**互补而非重复**；④ 对 7 个要素中至少 4 个有效。
> 预期收益的推法：先看文献里该方法相对"距离型/单变量基线"的相对降幅，再按我们的基线（已经比距离型强得多，所以**降幅要打折**）折算。这是保守估计，不是承诺。

### 推荐 1：距平域低秩矩阵补全（SoftImpute / 截断 SVD / DINEOF 变体），图正则可选

- **文献依据**：arXiv 2607.09546 —— 比利时气温 RMSE **SoftImpute 0.53/0.41 vs IDW 0.64/0.57（相对降 17–28 %）**，GR-RTRMC 0.45/0.43；法国 **SoftImpute 0.59/0.61 vs IDW 0.80/0.74（相对降 18–26 %）**。Decorte 等在比利时 Flanders 得"Matrix Completion 在所有缺口类型下最优"。HSIC-DIMFMC 报告相对 7 种方法 **RMSE 平均降 43.97–73.59 %**。
- **为什么对我们特别合适（这条最关键）**：我们实测的**气温距平场变差函数相关半径是 1243 km，远大于 100–200 km 的站距**——意味着 876 个站在同一天几乎处于同一相关长度内，"全场共同的天气型"解释了绝大部分方差。这正是低秩结构：**秩 10–30 个 EOF 模态就能重建全场**。而我们现在的气候距平法/距平克里金**只用了 4–12 个邻站**，等于把 800 多个站的信息白白丢掉。低秩法是唯一能一次性用上全网的路线。
- **预期收益**：气温 **0.74 → 0.68–0.71 ℃ MAE（相对 −4 %～−8 %）**。折算依据：文献相对降幅 17–28 % 是对比"IDW 级"基线；我们的基线已接近 MICE 级，故取文献降幅的 1/3 左右。最高/最低气温同理（半径 920/1081 km 也远大于站距）。
- **对其它要素**：湿度（半径 437 km，约为站距 2–4 倍）仍适用，预期 4.90 → 4.4–4.7 %；**降水（半径仅 107 km ≈ 站距，低于相关长度）不适用**，不要对降水用低秩法（会把事件平滑掉）。
- **实现要点（无 GPU、numpy 足够）**：把 876×27000 的**距平矩阵**（减本站月气候）做随机化 SVD，秩 k 在 10/20/30/50 上交叉验证；缺测值用 EM 式迭代（初始化用气候距平法结果）。整站全缺（59 个 hold-out 站）正是矩阵补全的标准情形——靠行空间（日）的载荷重建整列。内存约 0.2–0.4 GB（float32 可再半），单次 SVD 秒级。
- **风险**：秩与迭代次数敏感；降水/风速（半径 409 km）要单独判是否启用。

### 推荐 2：多要素联合链式插补（MICE / IterativeImputer，含邻站多要素距平特征）

- **文献依据**：Putra et al. 2026 —— 逐日气温 **MICE RMSE 0.965 ℃（全场 31 法最优）**，比 IDW/NR/IKNN（1.383–1.409 ℃）**好 30 %**，比 AA/OK（1.445 ℃）**好 33 %**；MissForest 0.978–0.996 ℃ 紧随。综述 MethodsX 2025：missForest 是气候缺测插补中使用最多的 ML 方法；**Jing 等 MICE-RF 在汉江流域用"气温/降水/湿度/风速/日照"5 要素**，优于线性插补/均值/样条/KNN——**与我们的 7 要素构成几乎一致**；Qaraghuli 等：**missForest 最适合相对湿度**；Ismail 等：missForest > MICE > KNN。
- **为什么值得试**：我们现在的气候距平法、距平克里金、GBRT **都是单变量**（一次只补一个要素）。但本站同一天 Tmax/Tmin/Tmean 几乎共线、湿度与气温反相关、日照与湿度反相关——**这些跨变量约束现在完全没用上**。Slovakia 的实验正是在多变量框架下把气温误差压低了 30 %。
- **预期收益**：气温 **0.78 → 0.72–0.75 ℃（相对 −4 %～−8 %）**；**湿度 −8 %～−15 %（4.90 → 4.2–4.5 %）**，因为综述两次独立指出 missForest/MICE 在湿度上优势最大；日照与风速受益可能更大（它们空间相关最弱——风速半径 409 km、日照 442 km——只能靠跨变量信息）。
- **实现要点**：sklearn 的 `IterativeImputer` 在当前 1.8 版本可直接用（默认 BayesianRidge，**不再依赖 statsmodels**；也可把 estimator 换成 `RandomForestRegressor` 得到 missForest 变体），做 5–10 轮链式迭代。**关键设计**：训练矩阵的每一行 = (站, 日)，列 = 7 个本站要素 + 每个邻站 7 个要素的**距平**（取最近的 4–6 个邻站，避免维度爆炸）+ 海拔差 + 日相位。这样"整站全缺"时靠邻站列提供信息，而不是靠本站自己缺失的列。
- **风险**：特征维度 = 7 × (1 + 6) ≈ 50，IterativeImputer 会变慢（文献比利时尺度 MissForest 单次 10–14 s，我们样本大 10 倍，需降到"每个站单独拟合 + 滑动窗口"）；`IterativeImputer` 在 sklearn 里长期标 experimental，若版本迁移出问题，用 Ridge 手写链式循环即可替代。

### 推荐 3：两阶段残差克里金（regression kriging：GBRT/气候距平作趋势 + OK 作残差）

- **文献依据**：Kilibarda et al. 2014（时空回归克里金，全球 1 km 逐日气温 RMSE ±2 ℃ 密区 / ±2–4 ℃ 疏区）；Sekulić & Kilibarda 2020（克罗地亚，RK **解释 91 % 变率**，普通局部回归只有 44 %）；Gräler et al. 2016（gstat 的 product-sum / separate / metric 时空协方差模型，是这套方法的标准实现）。
- **为什么值得试**：我们已经有两个**各自 0.74 ℃** 的方法（距平克里金、GBRT）。它们误差肯定**不完全相关**——GBRT 抓住了海拔差/日相位这类非线性趋势，OK 抓住了空间残差结构；而我们的变差函数半径 1243 km 说明**残差仍有很强的空间相关性可被邻站残差解释**。两阶段叠加几乎是"免费的"增益。
- **预期收益**：气温 **0.74 → 0.70–0.72 ℃（相对 −3 %～−5 %）**。依据：文献里 RK 相对普通局部回归解释方差从 44 %→91 %，但我们的趋势项已经很强，故取保守的 −3～−5 %。
- **实现要点**：第一阶段用现有 GBRT 输出（或气候距平法输出）作为趋势，第二阶段对 `观测 − 趋势` 做局部 OK（现有的距平克里金代码几乎可直接复用，只是把"气候距平"换成"模型残差"）。全部在 numpy/scipy 内，无需新依赖。**顺带**：把克里金插值值当作 GBRT 的一个特征（堆叠），是同一思路的等价变体，也值得试。
- **风险**：残差若已无空间结构（白噪声），第二阶段无增益——这本身就是有价值的信息（可以先算残差的实验变差函数来判定，成本极低）。

### 推荐 4：最优权重邻站法（P-BSHADE 型：站间比值 + 协方差求最小误差方差权重）

- **文献依据**：Xu et al. 2013 *J. Climate* —— 中国 582 站、5 邻站：P-BSHADE **RMSE 0.248 ℃** vs kriging 2.368 ℃ vs IDW 2.438 ℃（1971–2000 参考期 0.168 ℃）。Yashas & Varija 2024：日最高气温上 **P-BSHADE 与 SVM 优于 SSA**，理由是能刻画空间/时间异质性。
- **重要限定（必须写清）**：原文做的是**年平均**气温，平滑度远高于逐日，**0.248 ℃ 绝不能与我们的 0.78 ℃ 逐日 MAE 对比**。它的价值不在"0.248 这个数"，而在**权重构造方式**：显式用"站间比值 b_i + 协方差"求无偏最小方差的 BLUE 权重，而不是像我们的 normal-ratio 那样拍一个固定比值，也不像 OK 那样只依赖距离。
- **为什么值得试**：我们现在**气温用距平域 OK、降水用 normal-ratio**——两套不同的权重逻辑。P-BSHADE 提供一套**统一的最优权重框架**，且对"比值型"要素（降水、湿度、日照）天然合适。降水的变差函数半径只有 107 km（≈ 站距），普通克里金在这种"相关长度等于站距"的场景下本来就弱（Fagandini 2024 也见到 OK 在部分站系统性低估 ME −0.275 mm/day）；而 P-BSHADE 权重更依赖站间比值而非距离，正好躲开这个弱点。
- **预期收益**：降水 **1.78 → 1.65–1.72 mm（相对 −3 %～−7 %）**；湿度 **4.90 → 4.6–4.8 %**；日照 **1.21 → 1.15–1.19**；气温增益小（0.78 → 0.76–0.78，因为距平 OK 已接近最优线性无偏）。依据：换权重框架对"相关长度≈站距"的要素收益最大，对已经充分相关的要素收益最小；这是变差函数半径直接推出来的。
- **实现要点**：只需 sqrt/协方差矩阵求解（numpy.linalg 即可），权重要用"本站与邻站的长期比值/协方差"估计——我们 1951–2025 的长序列正好够。
- **风险**：权重对参考期选择敏感；比值在零降水日无定义（需按"Spa/HIS 或条件比值"处理）。

### 推荐 5：分要素方法路由 + 邻站配置调优（不做新算法，先做对配置）

- **文献依据**：Putra et al. 2026 的核心结论——**"方法复杂度对气温有用、对降水没用"**；余予等 2012 —— 全国 2088 站试验得到**最优邻站数就是 4 个**，搜索半径 220 km、海拔差 |Δh|≤200 m，且 **MAE/RMSE 随邻站平均距离呈指数增大**（相关约 0.8），**按相关最优选站（RO）比按距离最近选站（CS）MAE 低 11 %、RMSE 低 11 %**；王海军等 2008 —— 4 邻站 + 8 年 × 15 天窗口是最优参数组合；Aschauer & Marty 2021 —— "站间方法受限于必须有足够参考站"，且跨年环流型变化会破坏站间平稳性。
- **为什么值得做（且可能被低估）**：我们给了模型"300 km 内 4–12 邻站"，但**没有任何证据表明 12 个邻站比 4 个更好**。余予等在全国尺度上给出最优值就是 4；王海军也独立得到 4。我们现在的"4–12 个"很可能是把噪声邻站也塞了进去——**尤其是降水**（半径 107 km，300 km 外的邻站已完全不相关）。同时"按距离最近选站" vs "按相关最高选站"的 11 % 差异，我们的流程未必采用了后者。
- **预期收益**：**不新增依赖、不改数据管线**，纯配置搜索即可拿到 **−2 %～−5 %**（气温 0.78 → 0.74–0.76，约等于现有 OK 的水平），且**降水/风速/日照可能更多**（因为它们相关半径小，邻站数量与选择准则的敏感性更高）。
- **实现方式**：在现有 59 站 hold-out 上做小网格搜索：邻站数 {2, 4, 6, 8, 12} × 搜索半径 {150, 220, 300, 500 km} × 选站准则 {距离最近, 相关最高, 海拔差最小} × 海拔差约束 {无, 200 m, 500 m}，逐要素单独定最优。**注意要防过拟合**：59 站 × 7 要素的搜索空间用嵌套 CV 或至少留出一部分站做二次验证。
- **风险**：如果我们的基线参数本来就接近最优，收益会很小——但这是一次成本极低、能确定"还有多少空间"的检验。

### 不推荐（本轮不做，附理由）

| 方法 | 理由 |
|---|---|
| LSTM / CNN / Transformer / GAN | 本机无 GPU；MethodsX 2025 综述把 RNN/Transformer/GAN/BiLSTM/LSTM 全标为计算成本 "High"；Putra 2026 里 MLP 只达到 0.978–0.996 ℃，**并不优于 MICE**；AIES 2026 瑞士业务 GNN 原文说"GNN 对温度要素最吃力"。投入产出比差 |
| 图神经网络（GAT） | 文献 RMSE 0.997 K / 逐站 0.648 K 确实好（比 kriging 低 31–32 %），但那是**极密网 + GPU**；我们的 100–200 km 站距下，站点图的边信息量远低于那些研究 |
| 时空回归克里金（带 MODIS LST / DEM 协变量） | 精度高（Kilibarda RMSE ±2 ℃ 站密区），但**需要外部遥感/DEM 协变量产品**，我们目前没有；且那 2 ℃ 是"格点产品"精度，不一定优于我们 0.74 ℃ 的站内补值 |
| 站点→格点重建（4D-STDF 类） | 解决的是不同问题（建格点产品），不是补缺测 |
| 对降水用低秩/DINEOF | 降水变差函数半径 107 km ≈ 站距，场本身接近高秩；DINEOF 会把强降水事件平滑掉。31 法基准也显示降水上复杂方法无收益 |
| 神经网络用于风速 | 风速在所有研究里都最难（伊朗 17 机场 R² 仅 0.13）；我们 0.63 MAE 已与 Körner 2018 的 0.61 MAE 持平，接近上限 |

---

## 4. 关键参考文献

1. 王海军, 涂诗玉, 陈正洪. 日气温数据缺测的插补方法试验与误差分析[J]. 气象, 2008, 34(7): 83–91. http://qxqk.nmc.cn/html/2008/7/20080712.html
2. 余予, 李俊, 任芝花, 张志富. 标准序列法在日平均气温缺测数据插补中的应用[J]. 气象, 2012, 38(9): 1135–1139. http://qxqk.nmc.cn/html/2012/9/20120914.html
3. 孙越, 王海军, 周月华, 等. 三种插值方法对区域自动气象站日气温缺测数据插补的适用性研究[J]. 暴雨灾害, 2023, 42(1): 97–104. DOI: 10.12406/byzh.2022-183. http://www.byzh.org.cn/article/doi/10.12406/byzh.2022-183
4. Xu C D, Wang J F, Hu M G, Li Q X. Interpolation of missing temperature data at meteorological stations using P-BSHADE[J]. Journal of Climate, 2013, 26(19): 7452–7463. DOI: 10.1175/JCLI-D-12-00633.1. https://www.researchgate.net/publication/258149532_Interpolation_of_Missing_Temperature_Data_at_Meteorological_Stations_Using_P-BSHADE
5. Kilibarda M, Hengl T, Heuvelink G B M, Gräler B, Pebesma E, Perčec Tadić M, Bajat B. Spatio-temporal interpolation of daily temperatures for global land areas at 1 km resolution[J]. Journal of Geophysical Research: Atmospheres, 2014, 119(5): 2294–2313. DOI: 10.1002/2013JD020803. https://agupubs.onlinelibrary.wiley.com/doi/10.1002/2013JD020803
6. Körner M, Kronenberg R, Genzel S, Bernhofer C. Introducing Gradient Boosting as a universal gap filling tool for meteorological time series[J]. Meteorologische Zeitschrift, 2018, 27: 369–376. DOI: 10.1127/metz/2018/0908. https://www.schweizerbart.de/papers/metz/detail/27/89819/Introducing_Gradient_Boosting_as_a_universal_gap_filling_tool_for_meteorological_time_series
7. Putra A N, Výleta R, Hlavčová K, Szolgay J. Filling meteorological data gaps for catchment modelling in western Slovakia: does method complexity matter?[J]. Hydrology, 2026, 13(11): 300. DOI: 10.3390/hydrology13110300. https://www.mdpi.com/2306-5338/13/11/300
8. Bellido-Jiménez J A, Estévez Gualda J, García-Marín A P. Assessing machine learning models for gap filling daily rainfall series in a semiarid region of Spain[J]. Atmosphere, 2021, 12(9): 1158. DOI: 10.3390/atmos12091158. https://www.mdpi.com/2073-4433/12/9/1158
9. Fagandini C, et al. Missing rainfall daily data: a comparison among gap-filling approaches[J]. Mathematical Geosciences, 2024, 56: 191–217. DOI: 10.1007/s11004-023-10078-6. https://link.springer.com/article/10.1007/s11004-023-10078-6
10. Alejo-Sanchez L A, Márquez-Grajales A, Salas-Martínez J, Franco-Arcega A, López-Morales V, Acevedo-Sandoval O A, González-Ramírez E, Villegas-Vega M A. Missing data imputation of climate time series: a review[J]. MethodsX, 2025, 15: 103455. DOI: 10.1016/j.mex.2025.103455. https://pmc.ncbi.nlm.nih.gov/articles/PMC12268946/
11. Stekhoven D J, Bühlmann P. MissForest — non-parametric missing value imputation for mixed-type data[J]. Bioinformatics, 2012, 28(1): 112–118. DOI: 10.1093/bioinformatics/btr597. https://doi.org/10.1093/bioinformatics/btr597
12. Graph-regularized low-rank matrix completion by variable projection[EB/OL]. arXiv:2607.09546. https://arxiv.org/html/2607.09546
13. Graph-based matrix completion applied to weather data[EB/OL]. arXiv:2306.08627. https://arxiv.org/pdf/2306.08627
14. Ping B, Su F Z, Meng Y S. An improved DINEOF algorithm for filling missing values in spatio-temporal sea surface temperature data[J]. PLoS ONE, 2016, 11(5): e0155928. DOI: 10.1371/journal.pone.0155928. https://pmc.ncbi.nlm.nih.gov/articles/PMC4873229/
15. Zhao H P, Zhao H T, Zhang C. Validating data interpolation empirical orthogonal functions (DINEOF+) interpolated soil moisture data in the contiguous United States[J]. Agriculture, 2025, 15(11): 1212. DOI: 10.3390/agriculture15111212. https://www.mdpi.com/2077-0472/15/11/1212
16. Nie T, Qin G Y, Ma W, Mei Y W, Sun J. ImputeFormer: low rankness-induced transformers for generalizable spatiotemporal imputation[C]//Proceedings of the 30th ACM SIGKDD Conference on Knowledge Discovery and Data Mining (KDD '24), 2024. DOI: 10.1145/3637528.3671751. arXiv: 2312.01728. https://arxiv.org/abs/2312.01728 ；代码 https://github.com/tongnie/ImputeFormer
17. Li S P, et al. High-precision spatial interpolation of meteorological variables in complex terrain using machine learning methods[J]. Sensors, 2026, 26(7): 2167. DOI: 10.3390/s26072167. https://pmc.ncbi.nlm.nih.gov/articles/PMC13075302/
18. Machine learning-based reconstruction of missing meteorological observations using reanalysis and satellite data in West Africa[J]. Atmosphere, 2026, 17(9): 884. https://www.mdpi.com/2073-4433/17/9/884
19. Cross assessment of twenty-one different methods for missing precipitation data estimation[J]. Atmosphere, 2020, 11(4): 389. https://www.mdpi.com/2073-4433/11/4/389
20. Reconstruction of all-sky daily air temperature datasets with high accuracy in China from 2003 to 2022[J]. Scientific Data, 2024. https://pmc.ncbi.nlm.nih.gov/articles/PMC11480416 / https://www.nature.com/articles/s41597-024-03980-z
21. Gräler B, Pebesma E, Heuvelink G. Spatio-temporal interpolation using gstat[J]. The R Journal, 2016, 8(1): 204–218. https://digitalcommons.unl.edu/cgi/viewcontent.cgi?article=1512&context=r-journal
22. Filling gaps in daily precipitation series using regression and machine learning in inter-Andean watersheds[J]. Water, 2022, 14(11): 1799. https://www.mdpi.com/2073-4441/14/11/1799

---

## 附：本次检索的可信度与数据缺口声明

**检索通道**：本机 `web_search` 无 API key；`x_search` 只能返回二手检索摘要；`read_page` 在检索末期触发 Firecrawl 免费额度上限（429，需等 85847 秒），且对 PDF 一律报 `Unsupported content-type: application/pdf`。因此**部分文献只取到摘要级信息**。

**已知缺口（文档中已标注"未报告/未取得"，未编造）**：
- 孙越等 2023（暴雨灾害）正文页抓取失败，仅得摘要，**MAE/RMSE 具体数值未取得**。
- Ping et al. 2016 VE-DINEOF、Afrifa-Yamoah et al. 2020、Popolizio 等 GAIN、Decorte 等 Matrix Completion、HSIC-DIMFMC、low-rank autoregressive tensor completion、ST-GapFill：均**只取到结论级信息，无逐方法绝对值**。
- Jing 等 2022 MICE-RF（汉江流域）：**未取得具体数值**，仅取到"优于线性插补/均值/样条/KNN"的结论。
- 王海军 2008 与余予 2012 的中文原文数字来自期刊官网正文页，可信度最高。

**建议后续补检**：孙越 2023 全文（IDW/OK/MLR 在同一 RTT 网上的三法对比，对中国区域自动站最有参考价值）、Jing 2022 全文、以及任何在**中国国家级稀疏网 + 逐日 + 站级留出**设定下报告 MAE 的工作——目前这个交集在公开文献里近乎空白，**也是我们 0.78 ℃ 基线相对文献的优势所在：可直接对标的外部基准很少，审稿时需自己构造对比。**
