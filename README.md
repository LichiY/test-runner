# Rerun Test Tool

这是一个面向 flaky test / NIO test 研究数据的重跑工具。它会从 `patch-data/` 读取样本，自动完成以下流程：

1. 根据 `repo_url` 和 `original_sha` 克隆并切换到目标仓库版本。
2. 定位目标测试文件与测试方法，并将 `generated_patch` 应用到对应测试中。
3. 根据项目的 Java 版本、模块信息和本地构建环境，自动决定使用本地还是 Docker 进行编译。
4. 多次重跑目标测试，输出稳定性结果到 `results/*.csv`。

当前版本重点增强了两类稳定性能力：

- 环境稳定性：优先按模块检测 Java 版本；`--docker auto` 会结合实际构建工具所使用的 JDK 做判断，减少“本地 JDK 看起来能用、实际编译失败”的误判。
- 补丁稳定性：补丁应用不再只做简单文本替换，而是结合文件候选打分、方法定位和原始 `flaky_code` 相似度校验，降低误贴到错误方法的概率。

## 目录说明

- `rerun_tool/`：工具核心实现。
- `patch-data/`：输入数据集，至少包含 `repo_url`、`original_sha`、`module`、`full_test_name`、`generated_patch` 等字段。
- `reference-paper/`：论文材料。
- `workspace/`：运行时克隆下来的目标仓库。
- `results/`：输出结果目录。
- `tests/`：本仓库自己的 Python 单元测试。

## 运行前准备

推荐环境如下：

- Python `3.10+`
- Git
- Docker
- 本地 Maven 或 Gradle：可选，但建议安装，因为 `--docker auto` 在部分项目上会读取构建工具版本信息来判断兼容性

本项目当前没有额外的 Python 三方依赖，默认使用 Python 标准库即可运行。

## 快速开始

如果你只想先确认工具能跑，建议按下面顺序操作。

### 1. 检查基础环境

```bash
python3 --version  # 检查 Python 版本
git --version  # 检查 Git 是否可用
docker info  # 检查 Docker 守护进程是否已启动
mvn -version  # 可选：检查 Maven 及其实际使用的 JDK
```

如果 `docker info` 失败，说明 Docker 还没有启动；此时你仍然可以用 `--docker never` 走本地模式，但跨项目稳定性会明显下降。

### 2. 运行本仓库单元测试

```bash
python3 -m unittest discover -s tests -v  # 运行本工具自带的单元测试
```

如果这一步失败，先不要跑数据集，优先修复本工具自身环境问题。

### 3. 先跑一个小样本

```bash
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --rows 1 --rerun 1 --docker auto -o results/quick_start.csv  # 只跑第 1 条样本做冒烟测试
```

这条命令适合首次验证，优点是快、定位问题简单；缺点是覆盖面有限，不能代表批量运行表现。

### 4. 再跑一批样本

```bash
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 10 --rerun 5 --docker auto -o results/batch_run.csv  # 跑前 10 条样本并对每条测试重跑 5 次
```

这条命令更接近真实评估，优点是可以观察构建与重跑稳定性；缺点是耗时更长，也更依赖 Docker 缓存和网络状况。

## 使用方法

基础命令形式如下：

```bash
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 5 --rerun 10 --docker auto  # 使用 CSV 数据集运行前 5 条样本并对每条重跑 10 次
```

常见参数如下。

### 输入与输出

- `--csv`：输入数据集路径。
- `--output` 或 `-o`：结果文件路径；不传时自动写入 `results/rerun_results_<timestamp>.csv`。
- `--workspace` 或 `-w`：目标仓库克隆目录，默认是 `workspace/`。

### 样本筛选

- `--rows 0,1,2`：按行号精确选择样本。
  权衡：最适合复现单个问题，定位最精确；但不适合批量评估。
- `--limit 10`：只处理前 `10` 条样本。
  权衡：适合冒烟测试和小规模批量试跑；但如果数据集顺序本身有偏差，代表性有限。
- `--project commons-lang`：只处理项目名中包含给定字符串的样本。
  权衡：适合分项目调试环境；但如果同项目模块多，仍然可能需要结合 `--rows` 细化定位。

### 重跑模式

- `--rerun`：单个测试重复执行次数，默认 `10`。
  权衡：次数越多，越容易观察 flakiness；但耗时线性增加。
- `--mode isolated`：每次测试独立 JVM，默认值。
  权衡：隔离性最好，适合大多数 flaky test；但单次运行开销更大。
- `--mode same_jvm`：复用 JVM，主要用于 NIO 风格测试。
  权衡：更容易复现同环境状态污染；但对非 NIO 场景可能引入额外噪声。

### Docker 模式

- `--docker auto`：默认模式。工具会结合项目声明的 Java 版本和本地构建工具实际使用的 JDK 自动决定是否启用 Docker。
  权衡：通用性最好，推荐默认使用；但第一次拉镜像会比较慢。
- `--docker always`：始终使用 Docker。
  权衡：跨机器复现最稳定，适合做正式实验；但性能通常比本地直接执行略慢，首次准备时间更长。
- `--docker never`：始终使用本地环境。
  权衡：速度可能更快，也便于手工调试；但最依赖本机 JDK/Maven/Gradle 版本匹配，跨项目失败率最高。

### 超时与重试

- `--build-timeout`：构建超时，默认 `600` 秒。
- `--test-timeout`：单次测试超时，默认 `300` 秒。
- `--build-retries`：可恢复构建错误的重试次数，默认 `2`。

如果你在网络较差或首次拉依赖时运行，建议适当提高 `--build-timeout`。

### 日志

- `--verbose`：输出更详细的调试日志。
- `--log-file`：将日志额外写入文件。

推荐在批量跑实验时总是保存日志，例如：

```bash
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 20 --rerun 3 --docker auto --verbose --log-file results/batch_debug.log -o results/batch_debug.csv  # 批量运行并将详细日志保存到文件
```

## 输出结果说明

结果会写入一个 CSV 文件，常见字段包括：

- `status`：主流程状态，例如 `completed`、`build_failed`、`patch_failed`、`clone_failed`。
- `rerun_results`：每次重跑的结果数组。
- `pass_count` / `fail_count` / `error_count`：通过、失败、错误次数统计。
- `verdict`：综合结论，例如 `STABLE_PASS`、`STABLE_FAIL`、`FLAKY`、`BUILD_ERROR`、`SETUP_ERROR`、`RUN_ERROR`。

最常见的理解方式如下：

- `STABLE_PASS`：补丁应用后，多次重跑都通过。
- `FLAKY`：同一样本在多次重跑中既有 `pass` 又有 `fail`。
- `BUILD_ERROR`：项目或补丁无法编译。
- `SETUP_ERROR`：仓库克隆、测试文件定位或补丁应用阶段失败。

## 推荐使用姿势

如果你是在做研究实验，我建议按下面的顺序推进：

1. 先用 `--rows` 复现单个样本，确保目标项目能正确克隆、打补丁和编译。
2. 再对单个项目使用 `--project` 做小批量运行，观察该项目在本机上的 Docker/JDK 行为是否稳定。
3. 最后再扩大到整个数据集。

这种顺序的优点是问题定位最清晰；缺点是前期步骤更细，不如直接全量跑省事。但对于跨项目 flaky test 工具，先验证稳定性通常更划算。

## 配置到其他电脑

下面是把这个工具迁移到另一台电脑的推荐方法。

### 方案 A：推荐方案，Docker 优先

适用场景：你希望最大化跨机器一致性，尤其是不同项目需要不同 JDK 时。

优点：

- 复现性最好。
- 对本地 JDK 依赖较小。
- 更适合批量实验和长时间运行。

缺点：

- 第一次拉镜像和依赖会比较慢。
- 需要保证 Docker 本身可用，且磁盘空间足够。

迁移步骤如下：

1. 安装 Python、Git 和 Docker。
2. 把本仓库完整拷贝到新电脑，或者重新克隆。
3. 确保 `patch-data/` 也同步过去。
4. 运行单元测试。
5. 先跑一个小样本。

示例命令如下：

```bash
git clone <你的仓库地址> rerun-test  # 克隆本工具仓库到新电脑
cd rerun-test  # 进入项目根目录
python3 -m unittest discover -s tests -v  # 先验证工具本身没有坏
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --rows 1 --rerun 1 --docker auto -o results/migrate_check.csv  # 用单条样本验证新机器环境
```

### 方案 B：本地构建优先

适用场景：你没有 Docker，或者希望直接在本机调试 Maven/Gradle/JDK 问题。

优点：

- 启动更快。
- 容易直接观察本机构建链问题。
- 对图形化 Docker 环境没有依赖。

缺点：

- 对 JDK、Maven、Gradle 版本更敏感。
- 对旧 Java 项目不够稳定。
- 在不同电脑之间更容易出现“这台机器能跑，那台不能跑”的情况。

如果你选择这一方案，建议至少保证：

- 本机安装了 `mvn`。
- 本机安装了 `java`。
- 对需要旧版 Java 的项目，尽量准备多个 JDK 并手工切换。

运行方式如下：

```bash
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 5 --rerun 3 --docker never -o results/local_only.csv  # 强制使用本地环境运行
```

### Windows 电脑建议

如果你是在 Windows 上部署，优先建议使用 `WSL2 + Docker Desktop`。

优点：

- 与当前仓库的命令行习惯更接近。
- Git、Docker、Python 的行为更接近 Linux/macOS。

缺点：

- 初次配置会比 macOS / Linux 更复杂。
- 需要同时关注 Windows 与 WSL2 的磁盘和 Docker 资源设置。

不推荐直接在纯 Windows `cmd` 环境下长期跑批量实验，因为 Git、路径和 Docker 行为更容易出现兼容性差异。

## 迁移时建议一起带走的内容

建议迁移以下目录：

- `patch-data/`：必需，工具的输入数据集。
- `reference-paper/`：可选，便于对照论文。
- `tests/`：建议保留，用来验证新机器安装是否正确。
- `results/`：可选，如果你希望保留历史实验结果。

下面这些目录不一定需要拷贝：

- `workspace/`
  权衡：拷过去可以减少第一次重新克隆的时间；但它体积大，而且里面是运行时缓存，新机器上重新生成通常更干净。

## 常见问题

### 1. 为什么 `--docker auto` 还是很慢

常见原因有两个：

- 第一次拉 Docker 镜像。
- 第一次下载 Maven / Gradle 依赖。

这是正常现象。第二次运行通常会明显更快，因为工具会复用 Docker volume 缓存。

### 2. 为什么我本地有 Java，工具还是选择 Docker

因为工具不是只看 `java -version`，而是尽量判断“实际构建时会用哪个 JDK”。如果项目声明的是 `1.5`、`1.6`、`1.7` 这类老版本，现代本地 JDK 往往并不真正兼容，所以工具会倾向于切到 Docker。

### 3. 为什么补丁应用成功了，但编译还是失败

这通常说明问题已经不在“环境层”，而在“补丁层”，例如：

- `generated_patch` 新增了符号但没有补 import。
- 目标测试方法虽然定位正确，但补丁本身并不完整。
- 上下文依赖没有一起修改。

也就是说，`patch_applied` 不等于 `build_passed`，这两步需要分开看。

### 4. 什么时候应该用 `--docker always`

如果你在做正式实验、跑整批数据，或者需要迁移到另一台电脑复现，优先推荐 `--docker always` 或 `--docker auto`。只有当你明确知道本地构建链和目标项目完全兼容时，才建议长期使用 `--docker never`。

## 一套推荐命令

如果你想在新机器上从零开始，下面是一套最稳妥的顺序：

```bash
python3 -m unittest discover -s tests -v  # 先跑单元测试确认工具本身正常
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --rows 1 --rerun 1 --docker auto -o results/smoke.csv  # 再跑单样本做冒烟验证
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --project fastjson --limit 5 --rerun 3 --docker auto -o results/project_fastjson.csv  # 然后按项目做小批量验证
python3 -m rerun_tool --csv patch-data/cleaned_mutation_data.csv --limit 50 --rerun 5 --docker auto -o results/full_batch.csv  # 最后再扩大到更大的批量
```

这套流程的优点是稳、便于定位问题；缺点是前几步比直接全量运行更花时间。但如果目标是让工具在另一台电脑上可靠落地，这种顺序最省总时间。
